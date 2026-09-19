"""Phase 7B prompt-experiment prep: benchmark-only prompt override, frozen
split, DEV plan, gates and isolation. Offline: no provider call."""
import json
import os
import re
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

from benchmarks.selector_v2 import prompt_experiment as px
from benchmarks.selector_v2.challenge import build_challenge
from benchmarks.selector_v2.contract import selector_contract_fingerprint
from benchmarks.selector_v2.plan import PlanApprovalError
from benchmarks.selector_v2.postmortem import stratified_split
from benchmarks.selector_v2.prompt_contract import (
    PROMPT_IDS,
    load_committed_manifest,
    load_prompt,
    normalize_prompt,
    prompt_manifest,
    serializer_contract_fingerprint,
)
from benchmarks.selector_v2.providers import FakeSelectorProvider, LiveSelectorBackend, selector_config
from benchmarks.selector_v2.runner import RESULTS_FILE, RunIdentity, load_results, run_benchmark
from benchmarks.selector_v2.safety import check_live_gate, no_live_calls
from benchmarks.selector_v2.snapshot import load_near_pairs, snapshot_fingerprint
from tests.test_selector_challenge import _snapshots

REPO = Path(__file__).resolve().parents[2]
OUTPUTS = REPO / "outputs" / "selector-v2-benchmark"
PRODUCTION_CONTRACT = "3f49b198d621329022be807beada6d75891449b9722ffd9d7e097c6cf403ceed"
PRODUCTION_PROMPT_FP = "2d59cfb65f0aaa08ffcc481d81fed5a89f29803251cdc8a5438da673cb867f3c"
SPLIT_FP = "0fcb244120bdb1144de7f10f853cd2f6fe0c6d13b3f9c08411c041a48efb5777"


@pytest.fixture(autouse=True)
def _no_network():
    db_hosts = [urlparse(os.environ[name]).hostname
                for name in ("DATABASE_URL", "ADMIN_DATABASE_URL", "CHAT_DATABASE_URL")
                if os.environ.get(name)]
    with no_live_calls(allow_hosts=db_hosts):
        yield


# ── prompts ─────────────────────────────────────────────────────────────────

def test_production_prompt_and_contract_unchanged():
    assert selector_contract_fingerprint() == PRODUCTION_CONTRACT
    production = load_prompt("production")
    assert production.fingerprint == PRODUCTION_PROMPT_FP and not production.benchmark_only
    assert not (Path(px.__file__).with_name("prompts") / "production.md").exists()


def test_variant_fingerprints_stable_and_match_committed_manifest():
    assert prompt_manifest() == load_committed_manifest()
    for pid in PROMPT_IDS:
        assert load_prompt(pid).fingerprint == load_prompt(pid).fingerprint
    a, b = load_prompt("variant_a_v1"), load_prompt("variant_b_v1")
    assert len({a.fingerprint, b.fingerprint, PRODUCTION_PROMPT_FP}) == 3
    assert a.benchmark_only and b.benchmark_only
    # whitespace policy: CRLF / trailing spaces / outer blank lines do not matter
    noisy = "\n\n" + a.text.replace("\n", "  \r\n") + "\n\n"
    assert normalize_prompt(noisy) == a.text


def test_variants_contain_no_benchmark_specific_content():
    near_questions = [q for p in load_near_pairs() for q in p.questions]
    for pid in ("variant_a_v1", "variant_b_v1"):
        text = load_prompt(pid).text
        assert not re.search(r"(qna|calendar):\d", text)
        assert not re.search(r"\d{2,}", text)
        for question in near_questions:
            assert question.casefold() not in text.casefold()
        for phrase in ("yatay geçiş", "merkezi", "çözüm merkezi", "öğrenci affı"):
            assert not re.search(rf"\b{phrase}\b", text.casefold())


# ── override changes only the system prompt ─────────────────────────────────

def test_override_changes_only_system_prompt(tmp_path):
    snaps = [s for s in _snapshots() if s.selector_evaluable][:3]
    config = selector_config(provider="fake", model="fake-oracle")
    requests = {}
    for pid in PROMPT_IDS:
        backend = FakeSelectorProvider("oracle", config, prompt=load_prompt(pid))
        for s in snaps:
            assert backend.select(s, s.selector_candidates()).selected_candidate_ref
        requests[pid] = backend.requests
    users = {pid: [u for _s, u in reqs] for pid, reqs in requests.items()}
    assert users["production"] == users["variant_a_v1"] == users["variant_b_v1"]
    for pid in PROMPT_IDS:
        assert {s for s, _u in requests[pid]} == {load_prompt(pid).text}
    # production path is the unmodified ask_with_result request
    plain = FakeSelectorProvider("oracle", config)
    plain._local.current = (snaps[0], snaps[0].selector_candidates())
    plain.ask_with_result(snaps[0].case.intent_text, snaps[0].selector_candidates())
    assert plain.requests[0] == requests["production"][0]
    assert len({serializer_contract_fingerprint() for _ in PROMPT_IDS}) == 1


def test_invalid_output_contract_identical_for_variants():
    snap = [s for s in _snapshots() if s.selector_evaluable][0]
    config = selector_config(provider="fake", model="fake-malformed")
    outcomes = set()
    for pid in PROMPT_IDS:
        result = FakeSelectorProvider("malformed", config, prompt=load_prompt(pid)).select(
            snap, snap.selector_candidates())
        outcomes.add((result.status.value, result.invalid_reason))
    assert outcomes == {("invalid_output", "malformed_json")}


# ── frozen split ────────────────────────────────────────────────────────────

def test_committed_split_is_frozen():
    split = px.load_split()
    assert split["split_fingerprint"] == SPLIT_FP
    assert len(split["dev"]) == 95 and len(split["holdout"]) == 42
    assert not set(split["dev"]) & set(split["holdout"])
    assert px.split_fingerprint(split["dev"], split["holdout"]) == SPLIT_FP


@pytest.mark.skipif(not (OUTPUTS / "challenge-v1").exists(), reason="private artifacts absent")
def test_split_regenerates_from_frozen_challenge():
    from benchmarks.selector_v2.challenge import load_challenge
    from benchmarks.selector_v2.snapshot import load_snapshot

    manifest, _snaps = load_snapshot(OUTPUTS / "snapshots" / "3e558768561814da")
    _cm, cases = load_challenge(OUTPUTS / "challenge-v1", manifest)
    regenerated = stratified_split(cases)
    committed = px.load_split(challenge_ids={c["case_id"] for c in cases})
    assert (regenerated["dev"], regenerated["holdout"]) == (committed["dev"], committed["holdout"])


@pytest.mark.skipif(not (OUTPUTS / "live-stage-a").exists(), reason="private artifacts absent")
def test_production_dev_baseline_reproduced_from_stage_a():
    from benchmarks.selector_v2.challenge import load_challenge
    from benchmarks.selector_v2.snapshot import load_snapshot

    manifest, snaps = load_snapshot(OUTPUTS / "snapshots" / "3e558768561814da")
    _cm, cases = load_challenge(OUTPUTS / "challenge-v1", manifest)
    split = px.load_split()
    results = load_results(OUTPUTS / "live-stage-a" / "runs" / "9dfc72c140dc7e93" / RESULTS_FILE).by_case
    failures = [json.loads(line) for line in
                (OUTPUTS / "postmortem-v1" / "failure-cases.jsonl").read_text().splitlines()]
    side = {**dict.fromkeys(split["dev"], "DEV"), **dict.fromkeys(split["holdout"], "HOLDOUT")}
    report = px.dev_report(snaps, split["dev"], results,
                           {c["case_id"]: c["membership"] for c in cases},
                           px.taxonomy_slices(failures), label="prod", split_side=side)
    value = report["selector_value"]
    assert report["exact"]["correct"] == 34 and report["false_none"] == 29
    assert (value["rescue_count"], value["first_candidate_wrong"]) == (6, 13)
    assert (value["corruption_count"], value["first_candidate_right"]) == (54, 82)
    assert value["net_corrections"] == -48
    assert report["critical_cases"]["471"]["split"] == "HOLDOUT"


# ── synthetic experiment world ──────────────────────────────────────────────

def _world():
    snaps = _snapshots()
    groups = build_challenge(snaps, control_size=10)
    cases = groups["cases"]
    split = stratified_split(cases)
    return snaps, cases, split


def _identity(snaps, split, prompt, policy="oracle", model="fake-oracle"):
    config = selector_config(provider="fake", model=model)
    return RunIdentity(serializer_contract_fingerprint(), config, snapshot_fingerprint(snaps),
                       f"DRY_RUN_FAKE:{policy}", prompt_fingerprint=prompt.fingerprint,
                       split_fingerprint=split["split_fingerprint"])


def test_variant_namespaces_are_isolated_and_resumable(tmp_path):
    snaps, _cases, split = _world()
    dev = set(split["dev"])
    a, b = load_prompt("variant_a_v1"), load_prompt("variant_b_v1")
    ida, idb = _identity(snaps, split, a), _identity(snaps, split, b)
    legacy = RunIdentity(selector_contract_fingerprint(), ida.config, ida.snapshot_fingerprint,
                         ida.run_mode)
    assert len({ida.run_id, idb.run_id, legacy.run_id}) == 3
    assert "prompt_fingerprint" not in legacy.to_dict()  # old run ids unchanged
    back_a = FakeSelectorProvider("oracle", ida.config, prompt=a)
    first = run_benchmark(snaps, back_a, ida, tmp_path, case_ids=dev, max_cases=5)
    assert first.executed == 5
    back_b = FakeSelectorProvider("oracle", idb.config, prompt=b)
    assert run_benchmark(snaps, back_b, idb, tmp_path, case_ids=dev).executed == len(dev)
    back_a2 = FakeSelectorProvider("oracle", ida.config, prompt=a)
    resumed = run_benchmark(snaps, back_a2, ida, tmp_path, case_ids=dev)
    assert (resumed.skipped_completed, resumed.executed) == (5, len(dev) - 5)
    assert back_a2.calls == len(dev) - 5


def _reports(snaps, cases, split, correct_prod, correct_var):
    membership = {c["case_id"]: c["membership"] for c in cases}
    side = {**dict.fromkeys(split["dev"], "DEV"), **dict.fromkeys(split["holdout"], "HOLDOUT")}

    def fake_results(correct):
        return {cid: SimpleNamespace(correct=ok, outcome=SimpleNamespace(
            value="CORRECT_SELECT" if ok else "FALSE_NONE")) for cid, ok in correct.items()}

    prod = px.dev_report(snaps, split["dev"], fake_results(correct_prod), membership, {},
                         label="prod", split_side=side)
    var = px.dev_report(snaps, split["dev"], fake_results(correct_var), membership, {},
                        label="var", split_side=side)
    return prod, var


def test_selection_gate_requires_all_conditions_and_never_picks_winner():
    snaps, cases, split = _world()
    dev = split["dev"]
    prod, perfect = _reports(snaps, cases, split, dict.fromkeys(dev, False), dict.fromkeys(dev, True))
    gate = px.selection_gate(perfect, prod)
    assert gate["passed"] and gate["winner"] is None
    prod2, worse = _reports(snaps, cases, split, dict.fromkeys(dev, True), dict.fromkeys(dev, False))
    failed = px.selection_gate(worse, prod2)
    assert not failed["passed"]
    assert not failed["checks"]["net_corrections >= 0"]
    assert not failed["checks"]["false_none < production"]


def test_holdout_requires_selected_winner_gate_and_approval(tmp_path):
    split = px.load_split()
    a = load_prompt("variant_a_v1")
    kw = dict(split_fp=split["split_fingerprint"], live=False, plan_path=None, approved_fp=None)
    with pytest.raises(SystemExit):
        px.holdout_guard(prompt=load_prompt("production"), selected_winner="production",
                         gate_report_path=None, **kw)
    with pytest.raises(SystemExit):
        px.holdout_guard(prompt=a, selected_winner=None, gate_report_path=None, **kw)
    with pytest.raises(SystemExit):
        px.holdout_guard(prompt=a, selected_winner="variant_b_v1", gate_report_path=None, **kw)
    report = tmp_path / "gate.json"
    report.write_text(json.dumps({"prompt_fingerprint": a.fingerprint,
                                  "split_fingerprint": split["split_fingerprint"],
                                  "gate": {"passed": False}}))
    with pytest.raises(SystemExit):
        px.holdout_guard(prompt=a, selected_winner="variant_a_v1", gate_report_path=report, **kw)
    report.write_text(json.dumps({"prompt_fingerprint": a.fingerprint,
                                  "split_fingerprint": split["split_fingerprint"],
                                  "gate": {"passed": True}}))
    px.holdout_guard(prompt=a, selected_winner="variant_a_v1", gate_report_path=report, **kw)
    with pytest.raises(SystemExit):  # live HOLDOUT additionally needs its own approved plan
        px.holdout_guard(prompt=a, selected_winner="variant_a_v1", gate_report_path=report,
                         **{**kw, "live": True})


def _dev_plan(snaps, split):
    by_id = {s.case.case_id: s for s in snaps}
    config = selector_config(provider="openrouter", model="openai/gpt-4o-mini")
    return px.build_dev_plan(
        snapshot_manifest={"snapshot_fingerprint": snapshot_fingerprint(snaps)},
        challenge_manifest={"challenge_fingerprint": "c" * 64}, split=split,
        serializer_fp=serializer_contract_fingerprint(), config=config,
        prompts=[load_prompt("variant_a_v1"), load_prompt("variant_b_v1")],
        production_prompt=load_prompt("production"),
        dev_snapshots=[by_id[i] for i in split["dev"]], baseline_run_id="9dfc72c140dc7e93"), config


def test_dev_plan_counts_and_approval(tmp_path):
    snaps, _cases, split = _world()
    plan, config = _dev_plan(snaps, split)
    n = len(split["dev"])
    assert plan["calls"] == {"variant_a_v1": n, "variant_b_v1": n, "total": 2 * n}
    assert plan["production_baseline_calls"] == 0 and plan["holdout_calls"] == 0
    assert plan["approved"] is False and plan["live"] is False and plan["cost"] == "PRICE_REQUIRED"
    assert plan["provider"] == "openrouter" and plan["reasoning"] is None
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan))
    kw = dict(snapshot_fp=plan["snapshot_fingerprint"], split_fp=split["split_fingerprint"],
              serializer_fp=serializer_contract_fingerprint(), config=config)
    px.validate_dev_plan(path, plan["plan_fingerprint"], prompt=load_prompt("variant_a_v1"), **kw)
    for bad in (dict(prompt=load_prompt("production")),
                dict(prompt=load_prompt("variant_a_v1"), split_fp="x"),
                dict(prompt=load_prompt("variant_a_v1"),
                     config=selector_config(provider="openrouter", model="openai/gpt-4o-mini",
                                            reasoning_effort="low"))):
        with pytest.raises(PlanApprovalError):
            px.validate_dev_plan(path, plan["plan_fingerprint"], **{**kw, **bad})
    with pytest.raises(PlanApprovalError):
        px.validate_dev_plan(path, "0" * 64, prompt=load_prompt("variant_a_v1"), **kw)
    tampered = dict(plan, calls={"total": 1})
    path.write_text(json.dumps(tampered))
    with pytest.raises(PlanApprovalError):
        px.validate_dev_plan(path, plan["plan_fingerprint"], prompt=load_prompt("variant_a_v1"), **kw)


@pytest.mark.skipif(not (OUTPUTS / "prompt-experiment" / "live-plan-dev.json").exists(),
                    reason="private artifacts absent")
def test_real_dev_plan_is_exactly_190_dev_calls():
    plan = json.loads((OUTPUTS / "prompt-experiment" / "live-plan-dev.json").read_text())
    assert plan["calls"] == {"variant_a_v1": 95, "variant_b_v1": 95, "total": 190}
    assert plan["holdout_calls"] == 0 and plan["production_baseline_calls"] == 0
    assert plan["split_fingerprint"] == SPLIT_FP and plan["approved"] is False
    assert px.plan_fingerprint(plan) == plan["plan_fingerprint"]


# ── gates and isolation ─────────────────────────────────────────────────────

def test_live_is_openrouter_only_and_production_prompt_run_refused(tmp_path):
    with pytest.raises(SystemExit):
        check_live_gate(live=True, confirmed=True, provider="openai", model="gpt-4o-mini")
    from benchmarks.selector_v2 import cli

    with pytest.raises(SystemExit):
        cli.main(["prompt-run", "--snapshot", "x", "--challenge", "x", "--out", str(tmp_path),
                  "--prompt", "production", "--case-set", "dev"])


def test_variant_run_leaves_breaker_trace_and_config_untouched(tmp_path, monkeypatch, db):
    from core.database import AICapabilityConfig, AIConfigVersion
    from services import decision_trace, llm_provider
    from services.circuit_breaker import LLM_CIRCUIT_BREAKER

    def boom(*_a, **_k):
        raise AssertionError("production state touched by prompt experiment")

    monkeypatch.setattr(decision_trace.DecisionTrace, "__init__", boom)
    monkeypatch.setattr(decision_trace, "emit_decision_trace", boom)
    monkeypatch.setattr(LLM_CIRCUIT_BREAKER, "acquire", boom)
    monkeypatch.setattr(LLM_CIRCUIT_BREAKER, "record", boom)
    before = (db.query(AIConfigVersion).count(), db.query(AICapabilityConfig).count())
    snaps, _cases, split = _world()
    captured = []

    class Completions:
        def create(self, **kwargs):
            captured.append(kwargs)
            raise TimeoutError("mocked provider down")

    def factory(model):
        monkeypatch.setattr(llm_provider, "OpenAI", lambda **_k: SimpleNamespace())
        client = llm_provider.OpenRouterProvider(model=model)
        client.client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
        return client

    a = load_prompt("variant_a_v1")
    config = selector_config(provider="openrouter", model="openai/gpt-4o-mini")
    backend = LiveSelectorBackend(config, client_factory=factory, prompt=a)
    identity = RunIdentity(serializer_contract_fingerprint(), config, snapshot_fingerprint(snaps),
                           "LIVE", prompt_fingerprint=a.fingerprint,
                           split_fingerprint=split["split_fingerprint"])
    summary = run_benchmark(snaps, backend, identity, tmp_path, case_ids=set(split["dev"]))
    assert summary.executed == len(split["dev"])
    assert all(call["messages"][0]["content"] == a.text for call in captured)
    assert {k for call in captured for k in call} == {"model", "messages", "max_tokens", "temperature"}
    assert LLM_CIRCUIT_BREAKER._states == {}
    assert (db.query(AIConfigVersion).count(), db.query(AICapabilityConfig).count()) == before
