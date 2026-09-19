"""Phase 7A — Selector V2 benchmark harness (offline; no provider calls).

Every test in this module runs inside the harness network guard: any socket
connection other than the throwaway test Postgres, and any provider SDK
completion call, raises ``LiveCallBlocked`` and fails the test.
"""
import hashlib
import json
import os
import socket
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

from benchmarks.selector_v2 import cli
from benchmarks.selector_v2.contract import (
    build_model_input,
    order_candidates,
    selector_contract_fingerprint,
)
from benchmarks.selector_v2.evaluator import SELF_TEST_LABEL, evaluate, metrics
from benchmarks.selector_v2.gold_loader import load_reviewed_gold
from benchmarks.selector_v2.providers import (
    FakeSelectorProvider,
    LiveSelectorBackend,
    selector_config,
)
from benchmarks.selector_v2.runner import (
    RESULTS_FILE,
    ForeignResultError,
    RunIdentity,
    load_results,
    run_benchmark,
)
from benchmarks.selector_v2.safety import LiveCallBlocked, check_live_gate, no_live_calls
from benchmarks.selector_v2.schema import (
    BenchmarkCase,
    EvaluationStatus,
    FrozenCandidate,
    Outcome,
    PoolStatus,
)
from benchmarks.selector_v2.snapshot import (
    generate_snapshots,
    load_near_pairs,
    load_snapshot,
    snapshot_fingerprint,
    write_snapshot,
)
from benchmarks.selector_v2.stats import mcnemar_exact, paired_compare
from benchmarks.selector_v2.tokens import estimate
from services.candidate_eligibility import build_candidate_set
from services.selector import build_selector_prompt


# ── network / provider guard for the whole module ──────────────────────────

@pytest.fixture(autouse=True)
def _no_network():
    db_hosts = [urlparse(os.environ[name]).hostname
                for name in ("DATABASE_URL", "ADMIN_DATABASE_URL", "CHAT_DATABASE_URL")
                if os.environ.get(name)]
    with no_live_calls(allow_hosts=db_hosts):
        yield


@pytest.fixture()
def no_provider_clients(monkeypatch):
    """Instantiating a real provider SDK client fails the test."""
    import services.llm_provider as llm_provider

    def refuse(*_a, **_k):
        raise AssertionError("real provider client constructed in an offline test")

    monkeypatch.setattr(llm_provider, "OpenAI", refuse)
    monkeypatch.setattr(llm_provider.genai, "Client", refuse)


# ── fixtures: cases + production eligibility over scripted retrieval ───────

def _hit(qna_id, question=None, answer=None, score=0.9, source="qdrant"):
    return {"qna_id": qna_id, "question": question or f"Soru {qna_id}",
            "answer": answer if answer is not None else f"Cevap {qna_id}",
            "score": score, "source": source}


def _case(case_id, refs, *, decision="SELECT", text=None, **kw):
    return BenchmarkCase(case_id=str(case_id), intent_text=text or f"niyet {case_id}",
                         expected_decision=decision, acceptable_candidate_refs=refs, **kw)


def _builder(hits_by_text, *, active=None, max_candidates=32):
    def build(text, calendar_entries):
        hits = hits_by_text[text]
        ids = {h["qna_id"] for h in hits}
        live = ids if active is None else active
        return build_candidate_set(
            calendar_entries=calendar_entries, qna_hits=hits, routing_policy=None,
            active_qna_lookup=lambda requested: {i for i in requested if i in live},
            max_candidates=max_candidates,
        )
    return build


def _standard():
    cases = [
        _case(1, ["qna:10"]),                          # correct target first
        _case(2, ["qna:21", "qna:22"], tags=["multi_acceptable"]),
        _case(3, [], decision="NONE"),
        _case(4, ["qna:40"]),                          # retrieval miss
        _case(5, ["qna:50"]),                          # eligibility miss (inactive)
        _case(6, ["qna:61"]),                          # expected second
        BenchmarkCase(case_id="7", evaluation_status=EvaluationStatus.EXCLUDED,
                      status_reason="EXCLUDED_FROM_EVAL"),
        BenchmarkCase(case_id="8", evaluation_status=EvaluationStatus.HOLD,
                      status_reason="PENDING_CONTENT"),
        BenchmarkCase(case_id="9", evaluation_status=EvaluationStatus.NOT_SELECTOR_EVALUABLE,
                      status_reason="context_required_unresolved_intent"),
    ]
    hits = {
        "niyet 1": [_hit(10, score=0.95), _hit(11, score=0.5)],
        "niyet 2": [_hit(20, score=0.9), _hit(22, score=0.8)],
        "niyet 3": [_hit(30), _hit(31)],
        "niyet 4": [_hit(41), _hit(42)],
        "niyet 5": [_hit(50, score=0.99), _hit(51)],
        "niyet 6": [_hit(60, score=0.9), _hit(61, score=0.7)],
    }
    active = {10, 11, 20, 22, 30, 31, 41, 42, 51, 60, 61}
    return cases, _builder(hits, active=active)


@pytest.fixture()
def snapshot_dir(tmp_path):
    cases, build = _standard()
    snaps = generate_snapshots(cases, build_pool=build, pairs=[])
    manifest = write_snapshot(tmp_path / "snap", snaps,
                              {"selector_contract_fingerprint": selector_contract_fingerprint()})
    return tmp_path / "snap", manifest, snaps


def _identity(manifest, policy="oracle", **config_kw):
    config = selector_config(provider="fake", model=f"fake-{policy}", **config_kw)
    return RunIdentity(selector_contract_fingerprint(), config,
                       manifest["snapshot_fingerprint"], f"DRY_RUN_FAKE:{policy}")


def _run(snaps, manifest, out, policy="oracle", **kw):
    identity = _identity(manifest, policy)
    backend = FakeSelectorProvider(policy, identity.config)
    summary = run_benchmark(snaps, backend, identity, out, **kw)
    return identity, backend, summary


# ── schema ──────────────────────────────────────────────────────────────────

def test_schema_valid_select_none_and_multi_acceptable():
    assert _case(1, ["qna:1"]).expected_decision == "SELECT"
    assert _case(2, [], decision="NONE").acceptable_candidate_refs == []
    multi = _case(3, ["qna:317", "qna:320"])
    assert multi.multi_acceptable and multi.primary  # several refs != multi-intent
    assert _case(4, ["calendar:12"]).acceptable_candidate_refs == ["calendar:12"]


@pytest.mark.parametrize("bad", [
    dict(refs=["qna:abc"]), dict(refs=["QNA-336"]), dict(refs=["qna:0"]),
    dict(refs=["qna:1", "qna:1"]), dict(refs=[]), dict(refs=["qna:1"], decision="NONE"),
])
def test_schema_rejects_invalid_refs_and_decision_mismatch(bad):
    with pytest.raises(ValueError):
        _case(1, bad["refs"], decision=bad.get("decision", "SELECT"))


def test_schema_exclusion_needs_reason_and_is_not_evaluable():
    with pytest.raises(ValueError):
        BenchmarkCase(case_id="1", evaluation_status=EvaluationStatus.EXCLUDED)
    case = BenchmarkCase(case_id="1", evaluation_status=EvaluationStatus.HOLD,
                         status_reason="SOURCE_MISSING_HOLD")
    assert case.intent_text is None


# ── snapshot ────────────────────────────────────────────────────────────────

def test_snapshot_is_deterministic_and_fingerprinted(tmp_path):
    cases, build = _standard()
    first = generate_snapshots(cases, build_pool=build)
    second = generate_snapshots(cases, build_pool=build)
    assert snapshot_fingerprint(first) == snapshot_fingerprint(second)
    m1 = write_snapshot(tmp_path / "a", first, {})
    m2 = write_snapshot(tmp_path / "b", second, {})
    assert m1["snapshot_file_sha256"] == m2["snapshot_file_sha256"]
    changed = generate_snapshots([_case(1, ["qna:10"], text="niyet 6")], build_pool=build)
    assert snapshot_fingerprint(changed) != snapshot_fingerprint(first[:1])


def test_snapshot_preserves_production_candidate_order():
    cases, build = _standard()
    snap = generate_snapshots(cases[:1], build_pool=build)[0]
    production = build("niyet 1", []).candidates
    assert [c.candidate_ref for c in snap.selector_candidates()] == \
        [c.candidate_ref for c in production]
    assert [c.order for c in snap.candidates] == [1, 2]


def test_snapshot_classifies_retrieval_eligibility_and_budget_misses():
    cases, build = _standard()
    by_id = {s.case.case_id: s for s in generate_snapshots(cases, build_pool=build)}
    assert by_id["1"].pool_status is PoolStatus.IN_POOL
    assert by_id["2"].pool_status is PoolStatus.IN_POOL  # one acceptable ref suffices
    assert by_id["3"].pool_status is PoolStatus.NOT_APPLICABLE
    assert by_id["4"].pool_status is PoolStatus.RETRIEVAL_MISS
    assert by_id["5"].pool_status is PoolStatus.ELIGIBILITY_MISS
    assert {"candidate_ref": "qna:50", "reason": "inactive_or_missing"} in by_id["5"].excluded
    assert by_id["7"].pool_status is PoolStatus.NOT_SNAPSHOTTED
    budget = generate_snapshots(
        [cases[5]], build_pool=_builder({"niyet 6": [_hit(60), _hit(61, score=0.1)]},
                                        max_candidates=1))[0]
    assert budget.pool_status is PoolStatus.BUDGET_MISS and budget.truncated_refs == ["qna:61"]
    assert not budget.selector_evaluable


def test_snapshot_load_rejects_tampering(snapshot_dir):
    directory, _manifest, _snaps = snapshot_dir
    path = directory / "snapshot.jsonl"
    path.write_text(path.read_text(encoding="utf-8").replace("Cevap 10", "Cevap X"),
                    encoding="utf-8")
    with pytest.raises(ValueError):
        load_snapshot(directory)


def test_calendar_candidate_ref_supported_only_when_dataset_states_relevance():
    entry = SimpleNamespace(id=7, period="Güz", event="Final", start_date="01.01.2027",
                            end_date="02.01.2027")
    build = _builder({"niyet 1": [_hit(10)]})
    relevant = _case(1, ["calendar:7"], calendar_relevant=True)
    snap = generate_snapshots([relevant], build_pool=build,
                              calendar_lookup=lambda _text: [entry])[0]
    assert snap.pool_status is PoolStatus.IN_POOL
    assert snap.candidates[0].candidate_ref == "calendar:7"
    unstated = generate_snapshots([_case(1, ["qna:10"])], build_pool=build,
                                  calendar_lookup=lambda _t: pytest.fail("calendar queried"))[0]
    assert all(c.kind == "QNA" for c in unstated.candidates)


def test_near_pair_fixture_matches_verified_selector_test_pairs():
    from tests.test_selector_v2 import NEAR_QNA_PAIRS

    pairs = load_near_pairs()
    assert {(p.a, p.questions[0], p.b, p.questions[1]) for p in pairs} == {
        (left[0], left[1], right[0], right[1]) for left, right in NEAR_QNA_PAIRS
    }
    gs = {p.key: p for p in pairs if p.relation == "general_specific"}
    assert set(gs) == {"310-405", "129-342"}


def test_near_qna_and_general_specific_tags_need_competing_sibling():
    pairs = load_near_pairs()
    build = _builder({"niyet 1": [_hit(129), _hit(342)], "niyet 2": [_hit(129), _hit(5)]})
    both, alone = generate_snapshots(
        [_case(1, ["qna:342"]), _case(2, ["qna:129"])], build_pool=build, pairs=pairs)
    assert {"near_qna", "general_specific", "gs_expected:specific",
            "near_qna_pair:129-342"} <= set(both.derived_tags)
    assert "near_qna" not in alone.derived_tags  # sibling not in the pool


# ── contract reuse / leakage ────────────────────────────────────────────────

def test_model_input_is_exactly_the_production_selector_request(snapshot_dir):
    _directory, _manifest, snaps = snapshot_dir
    snap = snaps[0]
    production = build_candidate_set(
        calendar_entries=[], qna_hits=[_hit(10, score=0.95), _hit(11, score=0.5)],
        routing_policy=None, active_qna_lookup=lambda ids: set(ids), max_candidates=32,
    ).candidates
    assert build_model_input(snap.case.intent_text, snap.selector_candidates()) == \
        build_selector_prompt(snap.case.intent_text, production)


def test_retrieval_score_rank_provider_never_reach_the_model(snapshot_dir):
    _directory, _manifest, snaps = snapshot_dir
    snap = snaps[0]
    assert snap.candidates[0].score is not None and snap.candidates[0].source == "qdrant"
    _system, user = build_model_input(snap.case.intent_text, snap.selector_candidates())
    payload = json.loads(user)
    assert set(payload) == {"resolved_intent", "candidates"}
    for item in payload["candidates"]:
        assert set(item) == {"candidate_ref", "kind", "canonical_text", "answer_text"}
    for leaked in ("score", "qdrant", "retrieval_stage", "alias_match", "0.95", '"order"'):
        assert leaked not in user
    rebuilt = snap.candidates[0].to_selector_candidate()
    assert rebuilt.score is None and rebuilt.source is None and not rebuilt.alias_match


def test_contract_fingerprint_tracks_the_production_prompt(monkeypatch):
    import services.selector as selector

    before = selector_contract_fingerprint()
    assert selector_contract_fingerprint() == before
    monkeypatch.setattr(selector, "SELECTOR_SYSTEM_PROMPT", selector.SELECTOR_SYSTEM_PROMPT + "!")
    import benchmarks.selector_v2.contract as contract

    monkeypatch.setattr(contract, "SELECTOR_SYSTEM_PROMPT", selector.SELECTOR_SYSTEM_PROMPT)
    assert selector_contract_fingerprint() != before


def test_position_permutation_is_deterministic_extension_point(snapshot_dir):
    _directory, _manifest, snaps = snapshot_dir
    candidates = snaps[0].selector_candidates()
    assert order_candidates(candidates, case_id="1") == candidates
    permuted = order_candidates(candidates, case_id="1", order="permute:7")
    assert permuted == order_candidates(candidates, case_id="1", order="permute:7")
    assert sorted(c.candidate_ref for c in permuted) == sorted(c.candidate_ref for c in candidates)
    with pytest.raises(ValueError):
        order_candidates(candidates, case_id="1", order="random")


# ── evaluator ───────────────────────────────────────────────────────────────

def _results(snaps, manifest, tmp_path, script):
    """Run a scripted fake: case_id -> raw text or exception class."""
    identity = _identity(manifest, "oracle")
    backend = FakeSelectorProvider("oracle", identity.config)

    def policy(snapshot, _candidates):
        item = script[snapshot.case.case_id]
        if isinstance(item, type):
            raise item("scripted")
        return item

    backend._policy = policy
    run_benchmark(snaps, backend, identity, tmp_path)
    return load_results(tmp_path / "runs" / identity.run_id / RESULTS_FILE, identity).by_case


def test_evaluator_outcomes_and_denominators(snapshot_dir, tmp_path):
    _directory, manifest, snaps = snapshot_dir
    sel = lambda ref: json.dumps({"decision": "SELECT", "candidate_ref": ref})  # noqa: E731
    results = _results(snaps, manifest, tmp_path, {
        "1": sel("qna:10"),                 # correct SELECT
        "2": sel("qna:22"),                 # multi-acceptable: either ref is correct
        "3": sel("qna:30"),                 # false SELECT (expected NONE)
        "6": json.dumps({"decision": "NONE"}),  # false NONE
    })
    outcomes = {k: r.outcome for k, r in results.items()}
    assert outcomes == {"1": Outcome.CORRECT_SELECT, "2": Outcome.CORRECT_SELECT,
                        "3": Outcome.FALSE_SELECT, "6": Outcome.FALSE_NONE}
    report = evaluate(snaps, results, snapshot_manifest=manifest)
    d = report["denominators"]
    assert (d["total_cases"], d["retrieval_misses"], d["eligibility_misses"]) == (9, 1, 1)
    assert (d["excluded"], d["hold"], d["not_selector_evaluable"]) == (1, 1, 1)
    assert d["selector_evaluable"] == d["primary_denominator"] == 4  # misses excluded
    p = report["primary"]
    assert p["exact_selector_accuracy"] == 0.5
    assert p["false_none"] == 1 and p["false_select"] == 1
    assert p["none_recall"] == 0.0 and p["none_precision"] == 0.0
    assert p["retrieval_inclusive_accuracy"] == round(2 / 6, 6)
    assert report["none_matrix"]["NONE"]["SELECT_other_ref"] == 1
    assert "multi_acceptable" in report["by_slice"]
    assert "general_specific" not in report["by_slice"]  # absent tag: no invented metric
    assert report["self_test"] and report["label"] == SELF_TEST_LABEL


def test_evaluator_wrong_select_and_correct_none(snapshot_dir, tmp_path):
    _directory, manifest, snaps = snapshot_dir
    results = _results(snaps, manifest, tmp_path, {
        "1": json.dumps({"decision": "SELECT", "candidate_ref": "qna:11"}),
        "2": json.dumps({"decision": "SELECT", "candidate_ref": "qna:20"}),
        "3": json.dumps({"decision": "NONE"}),
        "6": json.dumps({"decision": "SELECT", "candidate_ref": "qna:61"}),
    })
    m = metrics([s for s in snaps if s.selector_evaluable], results)
    assert m["wrong_select"] == 2 and m["outcomes"]["CORRECT_NONE"] == 1
    assert m["none_precision"] == 1.0 and m["none_recall"] == 1.0
    assert m["select_accuracy"] == round(1 / 3, 6)


# ── fake providers ──────────────────────────────────────────────────────────

@pytest.mark.parametrize(("policy", "expected"), [
    ("oracle", {"1": Outcome.CORRECT_SELECT, "2": Outcome.CORRECT_SELECT,
                "3": Outcome.CORRECT_NONE, "6": Outcome.CORRECT_SELECT}),
    ("always_none", {"1": Outcome.FALSE_NONE, "2": Outcome.FALSE_NONE,
                     "3": Outcome.CORRECT_NONE, "6": Outcome.FALSE_NONE}),
    ("malformed", dict.fromkeys("1236", Outcome.INVALID_OUTPUT)),
    ("unknown_ref", dict.fromkeys("1236", Outcome.INVALID_OUTPUT)),
    ("empty", dict.fromkeys("1236", Outcome.INVALID_OUTPUT)),
    ("timeout", dict.fromkeys("1236", Outcome.TIMEOUT)),
    ("model_error", dict.fromkeys("1236", Outcome.MODEL_ERROR)),
])
def test_fake_policies(snapshot_dir, tmp_path, policy, expected, no_provider_clients):
    _directory, manifest, snaps = snapshot_dir
    identity, backend, summary = _run(snaps, manifest, tmp_path, policy)
    results = load_results(Path(summary.run_dir) / RESULTS_FILE, identity).by_case
    assert {k: r.outcome for k, r in results.items()} == expected
    assert backend.calls == 4 == summary.executed
    report = evaluate(snaps, results, snapshot_manifest=manifest)
    assert report["self_test"] and report["token_latency"]["real_provider_usage"] is False
    if policy == "oracle":
        assert report["primary"]["exact_selector_accuracy"] == 1.0
    if policy in ("malformed", "unknown_ref", "empty"):
        assert report["primary"]["invalid_output_rate"] == 1.0
        assert report["primary"]["predicted_valid_none"] == 0  # never semantic NONE


# ── resume / isolation ──────────────────────────────────────────────────────

def test_resume_skips_completed_cases(snapshot_dir, tmp_path):
    _directory, manifest, snaps = snapshot_dir
    _run(snaps, manifest, tmp_path, max_cases=2)
    _identity_, backend, summary = _run(snaps, manifest, tmp_path)
    assert (summary.skipped_completed, summary.executed, backend.calls) == (2, 2, 2)
    _identity_, backend, summary = _run(snaps, manifest, tmp_path)
    assert (summary.executed, backend.calls) == (0, 0)


def test_different_config_gets_its_own_namespace(snapshot_dir, tmp_path):
    _directory, manifest, snaps = snapshot_dir
    first, _b, _s = _run(snaps, manifest, tmp_path)
    other = _identity(manifest, "oracle", temperature=0.5)
    backend = FakeSelectorProvider("oracle", other.config)
    summary = run_benchmark(snaps, backend, other, tmp_path)
    assert other.run_id != first.run_id and summary.executed == 4 and backend.calls == 4
    changed_snapshot = RunIdentity(first.selector_contract_fingerprint, first.config,
                                   "0" * 64, first.run_mode)
    assert changed_snapshot.run_id != first.run_id


def test_corrupted_partial_result_is_ignored_and_rerun(snapshot_dir, tmp_path):
    _directory, manifest, snaps = snapshot_dir
    identity, _b, summary = _run(snaps, manifest, tmp_path, max_cases=1)
    path = Path(summary.run_dir) / RESULTS_FILE
    with open(path, "a", encoding="utf-8") as handle:
        handle.write('{"result_key": "torn-write')  # no newline: interrupted append
    loaded = load_results(path, identity)
    assert loaded.corrupt_lines == 1 and len(loaded.by_case) == 1
    _i, backend, summary = _run(snaps, manifest, tmp_path)
    assert summary.corrupt_lines_ignored == 1 and backend.calls == 3
    final = load_results(path, identity)
    assert len(final.by_case) == 4 and final.corrupt_lines == 1


def test_foreign_result_is_rejected(snapshot_dir, tmp_path):
    _directory, manifest, snaps = snapshot_dir
    identity, _b, summary = _run(snaps, manifest, tmp_path, max_cases=1)
    path = Path(summary.run_dir) / RESULTS_FILE
    record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    record.update(case_id="2", result_key="f" * 24)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")
    with pytest.raises(ForeignResultError):
        load_results(path, identity)


def test_retry_errors_reruns_only_errors_without_double_counting(snapshot_dir, tmp_path):
    _directory, manifest, snaps = snapshot_dir
    identity = _identity(manifest, "timeout")
    run_benchmark(snaps, FakeSelectorProvider("timeout", identity.config), identity, tmp_path)
    backend = FakeSelectorProvider("timeout", identity.config)
    backend._policy = lambda s, _c: json.dumps({"decision": "NONE"})
    summary = run_benchmark(snaps, backend, identity, tmp_path, retry_errors=True)
    assert summary.executed == 4
    loaded = load_results(Path(summary.run_dir) / RESULTS_FILE, identity)
    assert loaded.superseded == 4 and len(loaded.by_case) == 4
    assert {r.attempt for r in loaded.by_case.values()} == {2}
    assert evaluate(snaps, loaded.by_case)["primary"]["completed"] == 4


# ── paired statistics ───────────────────────────────────────────────────────

def test_mcnemar_exact_known_values():
    assert mcnemar_exact(10, 2) == pytest.approx(2 * (1 + 12 + 66) / 4096)
    assert mcnemar_exact(2, 10) == mcnemar_exact(10, 2)
    assert mcnemar_exact(0, 0) == 1.0 and mcnemar_exact(5, 5) == 1.0
    assert mcnemar_exact(0, 6) == pytest.approx(2 / 64)
    scipy_stats = pytest.importorskip("scipy.stats")
    assert mcnemar_exact(17, 6) == pytest.approx(
        scipy_stats.binomtest(6, 23, 0.5, alternative="two-sided").pvalue)


def test_paired_compare_counts(snapshot_dir, tmp_path):
    _directory, manifest, snaps = snapshot_dir
    _ia, _b, a = _run(snaps, manifest, tmp_path / "a", "oracle")
    _ib, _b, b = _run(snaps, manifest, tmp_path / "b", "always_none")
    ra = load_results(Path(a.run_dir) / RESULTS_FILE).by_case
    rb = load_results(Path(b.run_dir) / RESULTS_FILE).by_case
    by_case = {s.case.case_id: s for s in snaps if s.selector_evaluable}
    result = paired_compare(by_case, ra, rb)
    assert (result["both_correct"], result["only_a_correct"], result["only_b_correct"],
            result["both_wrong"]) == (1, 3, 0, 0)
    assert result["mcnemar_exact_p_two_sided"] == pytest.approx(0.25)
    other = {k: r.model_copy(update={"snapshot_fingerprint": "x"}) for k, r in rb.items()}
    with pytest.raises(ValueError):
        paired_compare(by_case, ra, other)


# ── tokens / cost ───────────────────────────────────────────────────────────

def test_token_estimate_never_invents_prices(snapshot_dir):
    _directory, _manifest, snaps = snapshot_dir
    plan = estimate(snaps, max_tokens=32, configs_planned=5)
    assert plan["cases"] == 4 and plan["planned_live_calls"] == 20
    assert plan["estimated_cost_per_config"] is None and "not computed" in plan["cost_note"]
    assert plan["input_tokens"]["total"] > 0 and plan["output_tokens_estimated"]["max"] <= 32
    priced = estimate(snaps, max_tokens=32, input_price_per_1m=1.0, output_price_per_1m=2.0)
    expected = (priced["input_tokens"]["total"] * 1.0
                + priced["output_tokens_estimated"]["total"] * 2.0) / 1_000_000
    assert priced["estimated_cost_per_config"] == pytest.approx(expected, abs=1e-6)


# ── live safety ─────────────────────────────────────────────────────────────

def test_live_gate_requires_both_flags_and_explicit_model():
    check_live_gate(live=False, confirmed=False, provider=None, model=None)
    for kwargs in (dict(confirmed=False, provider="openrouter", model="m"),
                   dict(confirmed=True, provider=None, model="m"),
                   dict(confirmed=True, provider="openrouter", model=None)):
        with pytest.raises(SystemExit):
            check_live_gate(live=True, **kwargs)


def test_guard_blocks_sockets_and_provider_sdks():
    with pytest.raises(LiveCallBlocked):
        socket.create_connection(("203.0.113.10", 443), timeout=1)
    with pytest.raises(LiveCallBlocked):
        socket.getaddrinfo("openrouter.ai", 443)
    from openai.resources.chat.completions import Completions

    with pytest.raises(LiveCallBlocked):
        Completions.create(None, model="x", messages=[])


def test_reasoning_effort_live_run_is_refused_until_adapters_transmit_it():
    config = selector_config(provider="openrouter", model="m", reasoning_effort="low")
    with pytest.raises(SystemExit):
        LiveSelectorBackend(config)


def test_default_cli_run_is_a_dry_run_with_zero_provider_calls(
        snapshot_dir, tmp_path, capsys, no_provider_clients):
    directory, _manifest, _snaps = snapshot_dir
    cli.main(["run", "--snapshot", str(directory), "--out", str(tmp_path / "out")])
    output = capsys.readouterr().out
    payload = json.loads(output[output.index("{"):])
    assert payload["label"] == SELF_TEST_LABEL
    assert payload["summary"]["backend_calls"] == 4
    run_dir = Path(payload["summary"]["run_dir"])
    assert (run_dir / "metrics.json").exists() and (run_dir / "REPORT.md").exists()
    assert "NOT a model accuracy" in (run_dir / "REPORT.md").read_text(encoding="utf-8")
    with pytest.raises(SystemExit):
        cli.main(["run", "--snapshot", str(directory), "--out", str(tmp_path / "x"),
                  "--live", "--provider", "openrouter", "--model", "openai/gpt-4o-mini"])


# ── reviewed Gold loader (synthetic contract fixture) ──────────────────────

def _write_gold(root: Path, records, manifest_overrides=None):
    gold = root / "gold"
    gold.mkdir(parents=True)
    body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
    (gold / "gold-reviewed-all.jsonl").write_text(body, encoding="utf-8")
    counts = {}
    for r in records:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    manifest = {
        "counts": counts,
        "evaluation_targets": counts.get("READY", 0) + counts.get("CONTEXT_REQUIRED", 0),
        "scorable_targets": counts.get("READY", 0),
        "decisions": {"2": "MULTI_ACCEPTABLE_QNA"},
        "case_ids": {"kb_overlap_flagged": [1]},
        "outputs_sha256": {"gold-reviewed-all.jsonl": hashlib.sha256(body.encode()).hexdigest()},
        **(manifest_overrides or {}),
    }
    (gold / "gold-reviewed-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return gold


def _gold_record(case_id, status="READY", intents=None, message=None, multi=False):
    return {"case_id": case_id, "status": status, "user_message": message or f"mesaj {case_id}",
            "multi_intent": multi, "context_required": status == "CONTEXT_REQUIRED",
            "temporal": False, "routing_guarded": False,
            "expected_intents": intents if intents is not None else []}


def _intent(ids, text, index=1):
    return {"intent_index": index, "intent_text": text, "accepted_qna_ids": ids}


def test_reviewed_gold_loader_maps_statuses_and_intents(tmp_path):
    gold = _write_gold(tmp_path, [
        _gold_record(1, intents=[_intent([306], "mesaj 1")]),
        _gold_record(2, intents=[_intent([317, 320], "mesaj 2")]),
        _gold_record(3, status="CONTEXT_REQUIRED"),
        _gold_record(4, status="EXCLUDED_FROM_EVAL"),
        _gold_record(5, status="CONTENT_REVIEW_HOLD"),
        _gold_record(6, status="PENDING_CONTENT"),
        _gold_record(7, multi=True, message="iki soru",
                     intents=[_intent([398], "birinci", 1), _intent([181], "ikinci", 2)]),
    ])
    loaded = load_reviewed_gold(gold)
    assert not loaded.failed
    by_id = {c.case_id: c for c in loaded.cases}
    assert by_id["1"].primary and "kb_overlap_flagged" in by_id["1"].tags
    assert by_id["2"].multi_acceptable and by_id["2"].review_decision == "MULTI_ACCEPTABLE_QNA"
    assert by_id["3"].evaluation_status is EvaluationStatus.NOT_SELECTOR_EVALUABLE
    assert by_id["4"].evaluation_status is EvaluationStatus.EXCLUDED
    assert by_id["5"].evaluation_status is by_id["6"].evaluation_status is EvaluationStatus.HOLD
    assert not by_id["7#i1"].primary and "reformulated_intent_text" in by_id["7#i2"].tags
    assert by_id["7#i2"].acceptable_candidate_refs == ["qna:181"]
    statuses = {c.name: c.status for c in loaded.checks}
    assert statuses["reference_denominator_503_17_486"] == "WARN"  # not the real dataset
    assert statuses["no_semantic_none_expectations"] == "WARN"


def test_reviewed_gold_loader_fails_on_hash_or_count_mismatch(tmp_path):
    record = _gold_record(1, intents=[_intent([306], "mesaj 1")])
    tampered = _write_gold(tmp_path / "a", [record],
                           {"outputs_sha256": {"gold-reviewed-all.jsonl": "0" * 64}})
    assert "sha256:gold-reviewed-all.jsonl" in {c.name for c in load_reviewed_gold(tampered).failed}
    miscounted = _write_gold(tmp_path / "b", [record], {"scorable_targets": 486})
    assert "manifest_scorable" in {c.name for c in load_reviewed_gold(miscounted).failed}


def test_frozen_candidate_rejects_kind_ref_mismatch():
    with pytest.raises(ValueError):
        FrozenCandidate(candidate_ref="calendar:1", kind="QNA", canonical_text="q",
                        answer_text="a", order=1)
