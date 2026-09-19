"""Phase 7B semantic HOLDOUT (variant_a_v1): frozen baseline, plan guard,
call budget and post-run integrity. Offline: module-wide network guard."""
import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

from benchmarks.selector_v2 import prompt_experiment as px
from benchmarks.selector_v2 import semantic_gold as sg
from benchmarks.selector_v2 import semantic_holdout as sh
from benchmarks.selector_v2.plan import PlanApprovalError
from benchmarks.selector_v2.prompt_contract import (
    load_committed_manifest, load_prompt, prompt_manifest, serializer_contract_fingerprint,
)
from benchmarks.selector_v2.providers import selector_config
from benchmarks.selector_v2.safety import check_live_gate, no_live_calls

REPO = Path(__file__).resolve().parents[2]
OUT = REPO / "outputs" / "selector-v2-benchmark"
HOLD = OUT / "prompt-experiment" / "semantic-holdout-a"
SNAPSHOT_FP = "3e558768561814dabe58cd0a8fc7ada71e6c1ad98e6e5bfa460320c21dea75f6"
SPLIT_FP = "0fcb244120bdb1144de7f10f853cd2f6fe0c6d13b3f9c08411c041a48efb5777"
GOLD_FP = "36ab1d6f06b990b247f8fee882acd0aba3d4f235ec0ecafc65504c3ffc9ea713"

pytestmark = pytest.mark.skipif(not (HOLD / sh.BASELINE_FILE).exists(),
                                reason="private benchmark outputs not present")


@pytest.fixture(autouse=True)
def _no_network():
    db_hosts = [urlparse(os.environ[name]).hostname
                for name in ("DATABASE_URL", "ADMIN_DATABASE_URL", "CHAT_DATABASE_URL")
                if os.environ.get(name)]
    with no_live_calls(allow_hosts=db_hosts):
        yield


@pytest.fixture(scope="module")
def world():
    from benchmarks.selector_v2.challenge import load_challenge
    from benchmarks.selector_v2.runner import RESULTS_FILE, load_results
    from benchmarks.selector_v2.snapshot import load_snapshot

    with no_live_calls():
        manifest, snaps = load_snapshot(OUT / "snapshots" / SNAPSHOT_FP[:16])
        cm, cases = load_challenge(OUT / "challenge-v1", manifest)
        split = px.load_split(challenge_ids={c["case_id"] for c in cases})
        gold_manifest, gold_cases = sh.load_semantic_gold(OUT / "semantic-gold-v1", GOLD_FP)
        production = load_results(OUT / "live-stage-a" / "runs" / "9dfc72c140dc7e93" / RESULTS_FILE).by_case
    return SimpleNamespace(manifest=manifest, snaps=snaps, cm=cm, cases=cases, split=split,
                           gold_manifest=gold_manifest, sem=sg.semantic_snapshots(snaps, gold_cases),
                           production=production,
                           membership={c["case_id"]: c["membership"] for c in cases},
                           baseline=sh.verify_baseline(HOLD / sh.BASELINE_FILE),
                           plan=json.loads((HOLD / sh.PLAN_FILE).read_text()))


def _config(provider="openrouter"):
    return selector_config(provider=provider, model="openai/gpt-4o-mini", temperature=0.0, max_tokens=32)


def _validate(world, plan=None, config=None, prompt=None, approved=None):
    plan = plan or world.plan
    sh.validate_holdout_plan(plan, approved or plan["plan_fingerprint"], config=config or _config(),
                             prompt=prompt or load_prompt("variant_a_v1"), split=world.split,
                             baseline=world.baseline, snapshot_fp=SNAPSHOT_FP,
                             serializer_fp=serializer_contract_fingerprint(), gold_fp=GOLD_FP)


# ── pre-run ────────────────────────────────────────────────────────────────

def test_frozen_identities(world):
    prompt = load_prompt("variant_a_v1")
    assert prompt.fingerprint == sh.SELECTED_PROMPT_FINGERPRINT
    assert prompt_manifest() == load_committed_manifest()
    assert world.manifest["snapshot_fingerprint"] == SNAPSHOT_FP
    assert world.split["split_fingerprint"] == SPLIT_FP
    assert world.gold_manifest["semantic_gold_fingerprint"] == GOLD_FP   # recomputed on load
    assert len(world.split["holdout"]) == 42 and world.baseline["holdout_case_ids"] == world.split["holdout"]


def test_baseline_frozen_and_reproducible(world, tmp_path):
    fresh = sh.build_prelive_baseline(snapshots=world.snaps, sem=world.sem, split=world.split,
                                      production=world.production, membership=world.membership,
                                      identity=world.baseline["identity"])
    assert fresh["baseline_fingerprint"] == world.baseline["baseline_fingerprint"]
    assert world.baseline["variant_outputs_read"] is False
    assert world.baseline["promotion_gate"] == sh.PROMOTION_GATE
    tampered = copy.deepcopy(world.baseline)
    tampered["production"]["exact"] += 1
    (tmp_path / "b.json").write_text(json.dumps(tampered))
    with pytest.raises(SystemExit, match="changed after freeze"):
        sh.verify_baseline(tmp_path / "b.json")


def test_plan_guard_accepts_only_the_approved_envelope(world):
    _validate(world)
    assert world.plan["logical_calls"] == 42 and world.plan["provider"] == "openrouter"
    assert world.plan["calls_by_config"] == {"variant_a_v1": 42, "production": 0,
                                             "variant_b_v1": 0, "stage_b": 0}
    with pytest.raises(PlanApprovalError, match="approved fingerprint"):
        _validate(world, approved="0" * 64)
    with pytest.raises(PlanApprovalError, match="provider"):
        _validate(world, config=_config("openai"))
    with pytest.raises(PlanApprovalError, match="variant_a_v1"):
        _validate(world, prompt=load_prompt("variant_b_v1"))
    for field, value, reason in (("holdout_case_ids", world.split["dev"][:42], "HOLDOUT split"),
                                 ("logical_calls", 43, "plan file modified"),
                                 ("prelive_baseline_fingerprint", "x", "plan file modified")):
        bad = {**world.plan, field: value}
        with pytest.raises(PlanApprovalError, match=reason):
            _validate(world, plan=bad)
        bad["plan_fingerprint"] = px.plan_fingerprint(bad)
        with pytest.raises(PlanApprovalError):
            _validate(world, plan=bad, approved=bad["plan_fingerprint"])
    extra = {**world.plan, "calls_by_config": {**world.plan["calls_by_config"], "production": 42}}
    extra["plan_fingerprint"] = px.plan_fingerprint(extra)
    with pytest.raises(PlanApprovalError, match="other configs"):
        _validate(world, plan=extra, approved=extra["plan_fingerprint"])
    with pytest.raises(SystemExit, match="not allowed"):
        check_live_gate(live=True, confirmed=True, provider="openai", model="gpt-4o-mini")


def test_call_budget_refuses_extra_and_foreign_calls(world):
    class Inner:
        calls = 0

        def select(self, snapshot, candidates):
            Inner.calls += 1
            return "ok"

    by_id = {s.case.case_id: s for s in world.snaps}
    backend = sh.BudgetedBackend(Inner(), 42, set(world.split["holdout"]))
    for cid in world.split["holdout"]:
        assert backend.select(by_id[cid], []) == "ok"
    with pytest.raises(sh.CallBudgetExceeded):
        backend.select(by_id[world.split["holdout"][0]], [])
    with pytest.raises(sh.CallBudgetExceeded):
        sh.BudgetedBackend(Inner(), 42, set(world.split["holdout"])).select(by_id[world.split["dev"][0]], [])
    assert Inner.calls == 42 and backend.calls == 42 and backend.refused == 1


def test_semantic_gate_report_and_holdout_guard(world):
    prompt = load_prompt("variant_a_v1")
    report = json.loads((HOLD / sh.SEMANTIC_GATE_FILE).read_text())
    assert report["gate"]["passed"] and report["prompt_fingerprint"] == prompt.fingerprint
    px.holdout_guard(prompt=prompt, selected_winner="variant_a_v1",
                     gate_report_path=HOLD / sh.SEMANTIC_GATE_FILE, split_fp=SPLIT_FP, live=True,
                     plan_path=HOLD / sh.PLAN_FILE, approved_fp=world.plan["plan_fingerprint"])
    old = OUT / "prompt-experiment" / "runs" / "f47cf095e441a6e8" / "dev-gate-report.json"
    with pytest.raises(SystemExit, match="did not pass"):
        px.holdout_guard(prompt=prompt, selected_winner="variant_a_v1", gate_report_path=old,
                         split_fp=SPLIT_FP, live=False, plan_path=None, approved_fp=None)


# ── post-run (skipped until the live HOLDOUT run exists) ───────────────────

def _live_run_dir():
    runs = HOLD / "runs"
    dirs = [d for d in runs.iterdir() if d.is_dir()] if runs.exists() else []
    return dirs[0] if len(dirs) == 1 else None


def test_live_run_integrity_and_deterministic_scoring(world):
    from benchmarks.selector_v2.runner import RESULTS_FILE, RUN_MANIFEST, load_results

    run_dir = _live_run_dir()
    if run_dir is None:
        pytest.skip("live HOLDOUT run not present")
    identity = json.loads((run_dir / RUN_MANIFEST).read_text())
    assert identity["run_mode"] == "LIVE"
    assert identity["prompt_fingerprint"] == sh.SELECTED_PROMPT_FINGERPRINT
    assert identity["config_fingerprint"] == world.plan["config_fingerprint"]
    assert identity["config"]["provider"] == "openrouter"
    loaded = load_results(run_dir / RESULTS_FILE)
    lines = [json.loads(l) for l in (run_dir / RESULTS_FILE).read_text().splitlines() if l.strip()]
    assert len(lines) == 42 and loaded.superseded == 0 and loaded.corrupt_lines == 0
    assert sorted(loaded.by_case) == sorted(world.split["holdout"])
    accounting = json.loads((run_dir / "call-accounting.json").read_text())
    assert accounting["logical_calls_total"] <= 42 and accounting["refused_by_budget_guard"] == 0
    assert accounting["production_calls"] == accounting["variant_b_calls"] == accounting["stage_b_calls"] == 0
    kwargs = dict(baseline=world.baseline, snapshots=world.snaps, sem=world.sem,
                  production=world.production, variant=loaded.by_case, membership=world.membership,
                  changes=sh.load_changes(OUT / "semantic-gold-v1"))
    first, second = sh.evaluate_holdout(**kwargs), sh.evaluate_holdout(**kwargs)
    assert first == second                                  # deterministic
    assert first["production"] == world.baseline["production"]   # baseline unchanged
    assert first["semantic_evaluable"] == world.baseline["semantic_evaluable_count"]
    metrics = json.loads((HOLD / "metrics.json").read_text())
    assert metrics["promotion_gate"] == first["promotion_gate"]
