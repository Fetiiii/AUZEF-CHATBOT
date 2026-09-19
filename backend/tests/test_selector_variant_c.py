"""Phase 7B Variant C: 471/472 lock → Semantic Gold V1.1 → Variant C DEV.
Offline: module-wide network guard; the live run is only inspected."""
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse

import pytest

from benchmarks.selector_v2 import prompt_experiment as px
from benchmarks.selector_v2 import semantic_gold as sg
from benchmarks.selector_v2 import semantic_holdout as sh
from benchmarks.selector_v2 import variant_c as vc
from benchmarks.selector_v2.plan import PlanApprovalError
from benchmarks.selector_v2.prompt_contract import (
    load_committed_manifest, load_prompt, prompt_manifest, serializer_contract_fingerprint,
)
from benchmarks.selector_v2.providers import FakeSelectorProvider, selector_config
from benchmarks.selector_v2.safety import check_live_gate, no_live_calls

REPO = Path(__file__).resolve().parents[2]
OUT = REPO / "outputs" / "selector-v2-benchmark"
VC = OUT / "prompt-experiment" / "variant-c-dev"
LOCK = OUT / "qualifier-postmortem" / "adjudicated"
GOLD11 = OUT / "semantic-gold-v1.1"
GOLD11_FP = "1d22cac8e20d32e1b32b0433a886dff3bd2c5b658ab0952a93234ce3ed971138"
C_FP = "fc1811443061737b090b388f0b52bb03f68af99a965f2956c4833663847eb9a9"
A_FP = "1aed568885db02f474534224695a45eb6f95835bc31f94e877af9659efe94a1e"
B_FP = "44893b6bb069da5b2849413b3d903fa3ff8dbfd1e1e7ea8242d1302135e093c9"
A_DEV_RUN = OUT / "prompt-experiment" / "runs" / "f47cf095e441a6e8"

pytestmark = pytest.mark.skipif(not (VC / vc.BASELINE_FILE).exists(),
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
    from benchmarks.selector_v2.snapshot import load_snapshot

    with no_live_calls():
        manifest, snaps = load_snapshot(OUT / "snapshots" / "3e558768561814da")
        v1_manifest, v1_cases = sh.load_semantic_gold(OUT / "semantic-gold-v1", vc.PARENT_GOLD_FP)
        g_manifest, g_cases = vc.load_gold_v11(GOLD11, GOLD11_FP)
    return {"manifest": manifest, "snaps": snaps, "by_id": {s.case.case_id: s for s in snaps},
            "v1_cases": v1_cases, "g_manifest": g_manifest, "g_cases": g_cases,
            "baseline": vc.verify_baseline(VC / vc.BASELINE_FILE),
            "plan": json.loads((VC / vc.PLAN_FILE).read_text()), "split": px.load_split()}


# ── review lock / Semantic Gold V1.1 ───────────────────────────────────────

def _filled_csv(world, labels=None):
    records = [json.loads(l) for l in (OUT / "qualifier-postmortem/review/review-cases.jsonl")
               .read_text().splitlines() if l.strip()]
    labels = labels or {"471": "I", "472": "D"}
    rows = ["case_id,intent_text,all_labels,candidate_view_complete,candidate_count,blind_decision,"
            "acceptable_labels,review_note,reviewer,reviewed_at"]
    for rec in records:
        rows.append(f"{rec['case_id']},{rec['intent_text']},x,true,{rec['candidate_count']},"
                    f"SELECT_ACCEPTABLE,{labels[rec['case_id']]},n,r,2026-09-19")
    return ("\n".join(rows) + "\n").encode()


def test_review_lock_deterministic_and_labels_map_deterministically(world):
    kwargs = dict(review_dir=OUT / "qualifier-postmortem/review", snapshots=world["by_id"])
    first = vc.lock_qualifier_review(decisions_bytes=_filled_csv(world), **kwargs)
    second = vc.lock_qualifier_review(decisions_bytes=_filled_csv(world), **kwargs)
    assert first == second
    assert {r["case_id"]: r["mapped_refs"] for r in first["records"]} == {"471": ["qna:129"], "472": ["qna:129"]}
    for cid, label in (("471", "I"), ("472", "D")):
        assert sg.blind_label_map(world["by_id"][cid])[label] == "qna:129"
    with pytest.raises(SystemExit, match="unknown labels"):
        vc.lock_qualifier_review(decisions_bytes=_filled_csv(world, {"471": "Z", "472": "D"}), **kwargs)
    manifest, records = vc.load_lock(LOCK)
    assert manifest["parent_review_packet_fingerprint"].startswith("bf73542d")
    assert [r["mapped_refs"] for r in records] == [["qna:129"], ["qna:129"]]


def test_semantic_gold_parent_unchanged_and_v11_deterministic(world):
    _manifest, records = vc.load_lock(LOCK)
    built = vc.build_gold_v11(world["v1_cases"], records, parent_fp=vc.PARENT_GOLD_FP,
                              lock_fp=world["g_manifest"]["qualifier_review_lock_fingerprint"])
    assert built["fingerprint"] == GOLD11_FP == world["g_manifest"]["semantic_gold_fingerprint"]
    assert [c.model_dump() for c in built["cases"]] == [c.model_dump() for c in world["g_cases"]]
    assert {c["case_id"]: c["derived_outcome"] for c in built["changes"]} == \
        {"471": "KEEP_CURRENT", "472": "KEEP_CURRENT"}
    assert [c.model_dump() for c in world["g_cases"]] == [c.model_dump() for c in world["v1_cases"]]
    assert world["g_manifest"]["changed_cases"] == []


# ── Variant C isolation ────────────────────────────────────────────────────

def test_prompts_frozen_and_c_is_generic():
    assert prompt_manifest() == load_committed_manifest()
    assert load_prompt("variant_a_v1").fingerprint == A_FP
    assert load_prompt("variant_b_v1").fingerprint == B_FP
    text = load_prompt("variant_c_v1").text
    assert load_prompt("variant_c_v1").fingerprint == C_FP
    assert not re.search(r"(qna|calendar):\d", text) and not re.search(r"\d{2,}", text)
    for phrase in ("yatay geçiş", "merkezi", "471", "472"):
        assert phrase not in text.casefold()


def test_c_differs_from_a_only_in_prompt_text(world):
    snaps = [s for s in world["snaps"] if s.case.case_id in world["split"]["dev"]][:5]
    config = selector_config(provider="fake", model="fake-oracle")
    requests = {}
    for pid in ("variant_a_v1", "variant_c_v1"):
        backend = FakeSelectorProvider("oracle", config, prompt=load_prompt(pid))
        for s in snaps:
            backend.select(s, s.selector_candidates())
        requests[pid] = backend.requests
    assert [u for _s, u in requests["variant_a_v1"]] == [u for _s, u in requests["variant_c_v1"]]
    assert {s for s, _u in requests["variant_c_v1"]} == {load_prompt("variant_c_v1").text}


def test_plan_binds_config_split_calls_and_provider(world):
    plan, baseline, split = world["plan"], world["baseline"], world["split"]
    a_identity = json.loads((A_DEV_RUN / "run-manifest.json").read_text())
    assert plan["config_fingerprint"] == a_identity["config_fingerprint"]          # same model config
    assert plan["candidate_order"].startswith("production") and a_identity["candidate_order"] == "production"
    assert plan["split_fingerprint"] == split["split_fingerprint"] == a_identity["split_fingerprint"]
    assert plan["dev_case_ids"] == split["dev"] and len(split["dev"]) == 95
    assert plan["diagnostic_case_ids"] == ["471", "472"]
    assert plan["max_logical_calls"] == plan["calls"]["total"] == 97
    assert not any(plan["other_calls"].values())
    assert plan["prompt_fingerprint"] == C_FP == baseline["identity"]["variant_c_prompt_fingerprint"]
    assert baseline["dev_gate"] == vc.DEV_GATE and baseline["heuristic"] == vc.heuristic_fingerprint()
    kwargs = dict(prompt=load_prompt("variant_c_v1"), split=split, baseline=baseline,
                  snapshot_fp=world["manifest"]["snapshot_fingerprint"],
                  serializer_fp=serializer_contract_fingerprint(), gold_fp=GOLD11_FP)
    ok = selector_config(provider="openrouter", model="openai/gpt-4o-mini", temperature=0.0, max_tokens=32)
    vc.validate_plan(plan, plan["plan_fingerprint"], config=ok, **kwargs)
    with pytest.raises(PlanApprovalError, match="provider"):
        vc.validate_plan(plan, plan["plan_fingerprint"],
                         config=selector_config(provider="openai", model="openai/gpt-4o-mini"), **kwargs)
    with pytest.raises(PlanApprovalError, match="variant_c_v1"):
        vc.validate_plan(plan, plan["plan_fingerprint"], config=ok,
                         **{**kwargs, "prompt": load_prompt("variant_a_v1")})
    bad = {**plan, "other_calls": {**plan["other_calls"], "old_holdout_full": 40}}
    bad["plan_fingerprint"] = px.plan_fingerprint(bad)
    with pytest.raises(PlanApprovalError, match="other calls"):
        vc.validate_plan(bad, bad["plan_fingerprint"], config=ok, **kwargs)
    with pytest.raises(SystemExit, match="not allowed"):
        check_live_gate(live=True, confirmed=True, provider="gemini", model="x")


def test_old_holdout_full_run_prohibited(world):
    split = world["split"]
    assert vc.scope_case_ids("dev", split) == split["dev"]
    assert vc.scope_case_ids("diagnostic", split) == ["471", "472"]
    for scope in ("holdout", "all", "dev+holdout"):
        with pytest.raises(SystemExit, match="forbidden"):
            vc.scope_case_ids(scope, split)
    backend = sh.BudgetedBackend(object(), vc.MAX_DIAGNOSTIC_CALLS, set(vc.KNOWN_REGRESSIONS))
    with pytest.raises(sh.CallBudgetExceeded):
        backend.select(world["by_id"]["465"], [])


def test_heuristic_and_baseline_frozen(world, tmp_path):
    baseline = world["baseline"]
    assert baseline["variant_c_outputs_read"] is False
    tampered = {**baseline, "dev_gate": {**baseline["dev_gate"], "false_none_max": 50}}
    (tmp_path / "b.json").write_text(json.dumps(tampered))
    with pytest.raises(SystemExit, match="changed after freeze"):
        vc.verify_baseline(tmp_path / "b.json")


# ── post-run (skipped until the live run exists) ───────────────────────────

def test_live_run_integrity_and_scoring(world):
    from benchmarks.selector_v2.runner import RESULTS_FILE, RUN_MANIFEST, load_results

    roots = {"dev": VC / "runs", "diagnostic": VC / "diagnostics" / "runs"}
    if not all(r.exists() for r in roots.values()):
        pytest.skip("live Variant C run not present")
    loaded = {}
    for scope, root in roots.items():
        (run_dir,) = [d for d in root.iterdir() if d.is_dir()]
        ident = json.loads((run_dir / RUN_MANIFEST).read_text())
        assert ident["run_mode"] == "LIVE" and ident["config"]["provider"] == "openrouter"
        assert ident["prompt_fingerprint"] == C_FP and ident["config_fingerprint"] == world["plan"]["config_fingerprint"]
        lines = [json.loads(l) for l in (run_dir / RESULTS_FILE).read_text().splitlines() if l.strip()]
        loaded[scope] = load_results(run_dir / RESULTS_FILE)
        assert len(lines) == len(loaded[scope].by_case) and loaded[scope].corrupt_lines == 0
        acct = json.loads((run_dir / "call-accounting.json").read_text())
        assert acct["refused_by_budget_guard"] == 0 and not acct["old_holdout_full_run"]
        for r in loaded[scope].by_case.values():
            assert r.outcome.value not in ("INVALID_OUTPUT", "MODEL_ERROR", "TIMEOUT")
    assert sorted(loaded["dev"].by_case) == sorted(world["split"]["dev"])
    assert sorted(loaded["diagnostic"].by_case) == ["471", "472"]
    metrics = json.loads((VC / "metrics.json").read_text())
    assert sum(a["logical_calls_total"] for a in metrics["call_accounting"].values()) <= 97
    manifest = json.loads((VC / "manifest.json").read_text())
    assert manifest["baseline_fingerprint"] == world["baseline"]["baseline_fingerprint"]
    assert manifest["semantic_gold_fingerprint"] == GOLD11_FP
