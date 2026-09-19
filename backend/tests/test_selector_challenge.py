"""Phase 7B-Prep — challenge set, selector-value metrics, reasoning transport,
live-plan approval and benchmark isolation. Offline: no provider call."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

from benchmarks.selector_v2 import cli
from benchmarks.selector_v2.challenge import (
    CHALLENGE,
    EASY_CONTROL_SIZE,
    FULL,
    build_challenge,
    first_candidate_correct,
    first_candidate_correctness,
    gold_rank,
    load_challenge,
    selector_value,
    two_layer_report,
    write_challenge,
)
from benchmarks.selector_v2.contract import selector_contract_fingerprint
from benchmarks.selector_v2.plan import (
    PRICE_REQUIRED,
    PlanApprovalError,
    build_live_plan,
    discover_selector_models,
    load_approved_plan,
    write_plan,
)
from benchmarks.selector_v2.providers import (
    FakeSelectorProvider,
    LiveSelectorBackend,
    selector_config,
)
from benchmarks.selector_v2.runner import RESULTS_FILE, RunIdentity, load_results, run_benchmark
from benchmarks.selector_v2.safety import no_live_calls
from benchmarks.selector_v2.snapshot import generate_snapshots, load_near_pairs, write_snapshot
from benchmarks.selector_v2.tokens import estimate
from services.llm_config import (
    ReasoningTransportError,
    reasoning_request_fields,
)
from tests.test_selector_benchmark import _builder, _case, _hit


@pytest.fixture(autouse=True)
def _no_network():
    db_hosts = [urlparse(os.environ[name]).hostname
                for name in ("DATABASE_URL", "ADMIN_DATABASE_URL", "CHAT_DATABASE_URL")
                if os.environ.get(name)]
    with no_live_calls(allow_hosts=db_hosts):
        yield


# ── synthetic frozen snapshot ───────────────────────────────────────────────

def _snapshots():
    """3 first-wrong, 1 near pair (129/342), 1 multi, 1 kb, 40 easy cases."""
    hits, cases = {}, []

    def add(cid, refs, hit_ids, **kw):
        text = f"niyet {cid}"
        cases.append(_case(cid, refs, text=text, **kw))
        hits[text] = [_hit(i, score=1 - n / 100) for n, i in enumerate(hit_ids)]

    add(1, ["qna:11"], [10, 11])                    # first wrong
    add(2, ["qna:21"], [20, 21])                    # first wrong
    add(3, ["qna:342"], [129, 342])                 # first wrong + near + general/specific
    add(4, ["qna:129"], [129, 342])                 # near + g/s, first right
    add(5, ["qna:50", "qna:51"], [51, 50])          # multi-acceptable, first right
    add(6, ["qna:60"], [60, 61], tags=["kb_overlap_flagged"])
    for cid in range(100, 140):
        add(cid, [f"qna:{cid}"], [cid, cid + 1000])  # easy
    return generate_snapshots(cases, build_pool=_builder(hits), pairs=load_near_pairs())


@pytest.fixture()
def frozen(tmp_path):
    snaps = _snapshots()
    manifest = write_snapshot(tmp_path / "snap", snaps,
                              {"selector_contract_fingerprint": selector_contract_fingerprint()})
    built = build_challenge(snaps)
    challenge_manifest = write_challenge(tmp_path / "challenge", manifest, built, {})
    return SimpleNamespace(snaps=snaps, manifest=manifest, built=built,
                           challenge=challenge_manifest, dir=tmp_path)


# ── first-candidate baseline + challenge ────────────────────────────────────

def test_first_candidate_baseline_and_gold_rank(frozen):
    by_id = {s.case.case_id: s for s in frozen.snaps}
    assert not first_candidate_correct(by_id["1"]) and gold_rank(by_id["1"]) == 2
    assert first_candidate_correct(by_id["5"])  # any acceptable ref counts
    none_case = generate_snapshots([_case(9, [], decision="NONE")],
                                   build_pool=_builder({"niyet 9": [_hit(1)]}))[0]
    assert not first_candidate_correct(none_case)  # baseline never says NONE


def test_challenge_union_is_deterministic_deduplicated_and_complete(frozen):
    counts = frozen.built["counts"]
    groups = frozen.built["groups"]
    assert groups["A_first_candidate_wrong"] == ["1", "2", "3"]
    assert set(groups["B_near_qna"]) == {"3", "4"} == set(groups["C_general_specific"])
    assert groups["D_kb_overlap_flagged"] == ["6"] and groups["E_multi_acceptable"] == ["5"]
    assert counts["union_before_control"] == 6  # case 3 is in A, B and C once
    ids = [c["case_id"] for c in frozen.built["cases"]]
    assert len(ids) == len(set(ids)) == 6 + EASY_CONTROL_SIZE
    assert build_challenge(frozen.snaps) == frozen.built
    assert build_challenge(list(reversed(frozen.snaps)))["cases"] == frozen.built["cases"]


def test_easy_control_is_deterministic_hash_sample_outside_hard_slices(frozen):
    control = frozen.built["control"]
    assert len(control) == EASY_CONTROL_SIZE
    assert set(control) <= {str(i) for i in range(100, 140)}
    assert control == build_challenge(list(reversed(frozen.snaps)))["control"]
    assert control != sorted(control, key=int)  # hash order, not id order


def test_challenge_artifact_fingerprinted_and_immutable(frozen):
    manifest, cases = load_challenge(frozen.dir / "challenge", frozen.manifest)
    assert manifest["challenge_fingerprint"] == frozen.challenge["challenge_fingerprint"]
    assert manifest["source_snapshot_fingerprint"] == frozen.manifest["snapshot_fingerprint"]
    assert len(cases) == manifest["case_count"]
    assert write_challenge(frozen.dir / "challenge", frozen.manifest, frozen.built, {}) == manifest
    other = dict(frozen.built, control=frozen.built["control"][:-1],
                 cases=frozen.built["cases"][:-1])
    with pytest.raises(SystemExit):
        write_challenge(frozen.dir / "challenge", frozen.manifest, other, {})
    path = frozen.dir / "challenge" / "cases.jsonl"
    path.write_text(path.read_text(encoding="utf-8").replace('"1"', '"999"', 1), encoding="utf-8")
    with pytest.raises(ValueError):
        load_challenge(frozen.dir / "challenge")


# ── selector-value metrics ──────────────────────────────────────────────────

def test_rescue_corruption_preserve_unresolved():
    snaps = _snapshots()
    universe = [s for s in snaps if s.case.case_id in {"1", "2", "3", "4", "5", "6"}]
    model = {"1": True, "2": False, "3": True, "4": False, "5": True, "6": True}
    value = selector_value(universe, model)
    assert (value["rescue_count"], value["unresolved_count"]) == (2, 1)
    assert (value["corruption_count"], value["preserve_count"]) == (1, 2)
    assert value["net_corrections"] == 1
    assert value["rescue_rate"] == round(2 / 3, 6) and value["corruption_rate"] == round(1 / 3, 6)


def test_two_layer_report_separates_full_and_challenge_and_position(frozen):
    ids = [c["case_id"] for c in frozen.built["cases"]]
    baseline = two_layer_report(frozen.snaps, ids, first_candidate_correctness(frozen.snaps),
                                label="baseline")
    assert baseline[FULL]["layer"] == FULL and baseline[CHALLENGE]["layer"] == CHALLENGE
    assert baseline[FULL]["exact"]["cases"] == 46 and baseline[CHALLENGE]["exact"]["cases"] == 36
    fv = baseline[CHALLENGE]["selector_value"]
    assert fv["rescue_count"] == fv["corruption_count"] == 0  # baseline vs itself
    pos = baseline[CHALLENGE]["position"]
    assert pos["gold_at_position_1"]["accuracy_when_gold_rank1"] == 1.0
    assert pos["gold_not_at_position_1"]["accuracy_when_gold_not_rank1"] == 0.0
    gs = baseline[CHALLENGE]["general_specific"]
    assert gs["general_accuracy"]["accuracy"] == 1.0 and gs["specific_accuracy"]["accuracy"] == 0.0
    assert set(baseline[CHALLENGE]["near_qna_pairs"]) == {"129-342"}
    assert "selection bias" in baseline[CHALLENGE]["kb_overlap_flagged"]["caveat"]
    assert "cannot be measured" in baseline[CHALLENGE]["none_limitation"]
    assert baseline["winner"] is None


@pytest.mark.parametrize(("policy", "rescue", "corruption", "correct"), [
    ("first_candidate", 0, 0, 33), ("oracle", 3, 0, 36), ("always_none", 0, 33, 0),
])
def test_fake_policies_on_challenge(frozen, tmp_path, policy, rescue, corruption, correct):
    ids = {c["case_id"] for c in frozen.built["cases"]}
    config = selector_config(provider="fake", model=f"fake-{policy}")
    identity = RunIdentity(selector_contract_fingerprint(), config,
                           frozen.manifest["snapshot_fingerprint"], f"DRY_RUN_FAKE:{policy}")
    backend = FakeSelectorProvider(policy, config)
    summary = run_benchmark(frozen.snaps, backend, identity, tmp_path / "out", case_ids=ids)
    assert summary.executed == backend.calls == len(ids)
    results = load_results(Path(summary.run_dir) / RESULTS_FILE, identity).by_case
    report = two_layer_report(frozen.snaps, sorted(ids),
                              {k: r.correct for k, r in results.items()}, label="t",
                              results=results)
    value = report[CHALLENGE]["selector_value"]
    assert (value["rescue_count"], value["corruption_count"]) == (rescue, corruption)
    assert report[CHALLENGE]["exact"]["correct"] == correct
    if policy == "always_none":
        assert report[CHALLENGE]["exact_metrics"]["false_none"] == len(ids)
    paired = report["paired_vs_first_candidate"][CHALLENGE]
    assert (paired["only_a_correct"], paired["only_b_correct"]) == (rescue, corruption)


# ── reasoning transport (production adapters, mocked SDK) ───────────────────

class _CapturingCompletions:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            id="r", model="m", usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"decision":"NONE"}'),
                                     finish_reason="stop")])


def _openai_compatible(provider_name, effort, monkeypatch):
    from services import llm_provider

    monkeypatch.setattr(llm_provider, "OpenAI", lambda **_k: SimpleNamespace())
    cls = llm_provider.OpenRouterProvider if provider_name == "openrouter" \
        else llm_provider.OpenAIProvider
    client = cls(model="some/model")
    completions = _CapturingCompletions()
    client.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    from benchmarks.selector_v2.providers import config_set_for

    bound = llm_provider._bind_configs(client, config_set_for(
        selector_config(provider=provider_name, model="some/model", reasoning_effort=effort)))
    return bound, completions


def _candidates():
    from services.candidate_eligibility import CandidateKind, SelectorCandidate

    return [SelectorCandidate("qna:1", CandidateKind.QNA, "q", "a", qna_id=1)]


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_reasoning_transport_openrouter_and_openai(monkeypatch, effort):
    bound, completions = _openai_compatible("openrouter", effort, monkeypatch)
    bound.ask_with_result("soru", _candidates())
    assert completions.calls[0]["extra_body"] == {"reasoning": {"effort": effort}}
    assert "reasoning_effort" not in completions.calls[0]
    bound, completions = _openai_compatible("openai", effort, monkeypatch)
    bound.ask_with_result("soru", _candidates())
    assert completions.calls[0]["reasoning_effort"] == effort
    assert "extra_body" not in completions.calls[0]


def test_no_reasoning_payload_is_unchanged(monkeypatch):
    for provider in ("openrouter", "openai"):
        bound, completions = _openai_compatible(provider, None, monkeypatch)
        bound.ask_with_result("soru", _candidates())
        assert set(completions.calls[0]) == {"model", "messages", "max_tokens", "temperature"}


def test_unsupported_reasoning_fails_before_any_request(monkeypatch):
    from services import llm_provider

    calls = []
    gemini = llm_provider.GeminiProvider.__new__(llm_provider.GeminiProvider)
    gemini.provider_name = "gemini"
    gemini._configured_clients = {}
    gemini.client = SimpleNamespace(models=SimpleNamespace(
        generate_content=lambda **k: calls.append(k)))
    config = selector_config(provider="gemini", model="gemini-x", reasoning_effort="high")
    with pytest.raises(ReasoningTransportError):
        gemini._invoke("s", "u", config)
    assert calls == []
    bound, completions = _openai_compatible("openrouter", "none", monkeypatch)
    with pytest.raises(ReasoningTransportError):
        bound.ask_with_result("soru", _candidates())
    assert completions.calls == []
    with pytest.raises(SystemExit):
        LiveSelectorBackend(config)
    assert reasoning_request_fields("openrouter", None) == {}


def test_reasoning_level_changes_config_fingerprint():
    fps = {selector_config(provider="openrouter", model="m", reasoning_effort=e).fingerprint
           for e in (None, "low", "medium", "high")}
    assert len(fps) == 4


def test_registry_rejects_untransmittable_reasoning_assignment():
    from services.ai_registry import (
        AIConfigError, CapabilityParams, ModelDefinition, QualificationStatus, validate_assignment,
    )
    from services.llm_config import LLMCapability

    def model(provider):
        return ModelDefinition(None, "x", provider, "m", True, ("selector",), True, True,
                               ("low", "none"), QualificationStatus.QUALIFIED)

    validate_assignment(LLMCapability.SELECTOR, model("openrouter"),
                        CapabilityParams(max_tokens=32, reasoning_effort="low"))
    for provider, effort in (("gemini", "low"), ("openrouter", "none")):
        with pytest.raises(AIConfigError) as exc:
            validate_assignment(LLMCapability.SELECTOR, model(provider),
                                CapabilityParams(max_tokens=32, reasoning_effort=effort))
        assert exc.value.code == "reasoning_transport_unsupported"


# ── registry discovery + live plan ──────────────────────────────────────────

def _registry_models():
    base = dict(enabled=True, allowed_capabilities=["intent_analyzer", "selector"],
                supports_structured_output=True, supports_reasoning_effort=False,
                allowed_reasoning_efforts=[], qualification_status="LEGACY_APPROVED")
    return [
        {**base, "id": 1, "provider": "gemini", "model_identifier": "gemini-2.5-flash-lite"},
        {**base, "id": 3, "provider": "openrouter", "model_identifier": "openai/gpt-4o-mini"},
        {**base, "id": 4, "provider": "openrouter", "model_identifier": "vendor/reasoner",
         "supports_reasoning_effort": True, "allowed_reasoning_efforts": ["low", "high"],
         "qualification_status": "QUALIFIED"},
        {**base, "id": 5, "provider": "openrouter", "model_identifier": "vendor/untested",
         "qualification_status": "UNTESTED"},
    ]


def _plan(frozen, prices=None):
    discovered = discover_selector_models(_registry_models(), {"openrouter": True})
    ids = {c["case_id"] for c in frozen.built["cases"]}
    production = {**selector_config(provider="openrouter", model="openai/gpt-4o-mini").to_dict(),
                  "config_fingerprint": selector_config(
                      provider="openrouter", model="openai/gpt-4o-mini").fingerprint}
    return build_live_plan(
        snapshot_manifest=frozen.manifest, challenge_manifest=frozen.challenge,
        contract_fingerprint=selector_contract_fingerprint(), production_selector=production,
        discovered=discovered,
        challenge_estimate=estimate([s for s in frozen.snaps if s.case.case_id in ids],
                                    max_tokens=32, primary_only=True),
        full_estimate=estimate(frozen.snaps, max_tokens=32, primary_only=True),
        prior_models=["openrouter/openai/gpt-5.6-luna"], prices=prices)


def test_discovery_and_plan_matrix(frozen):
    discovered = {m["model_identifier"]: m for m in
                  discover_selector_models(_registry_models(), {"openrouter": True})}
    assert discovered["gemini-2.5-flash-lite"]["blockers"] == ["provider_key_missing"]
    assert discovered["vendor/untested"]["blockers"] == ["qualification_UNTESTED"]
    assert discovered["vendor/reasoner"]["reasoning_transport_efforts"] == ["high", "low"]
    plan = _plan(frozen)
    ids = [c["config_id"] for c in plan["proposed_configs"]]
    assert ids == ["openrouter/openai/gpt-4o-mini@none", "openrouter/vendor/reasoner@none",
                   "openrouter/vendor/reasoner@high", "openrouter/vendor/reasoner@low"]
    assert plan["proposed_configs"][0]["role"] == "production_baseline"
    assert plan["live"] is False and plan["approved"] is False and plan["winner"] is None
    assert plan["stage_a"]["total_calls"] == 36 * 4
    assert plan["stage_b_policy"]["runs_automatically"] is False
    assert all(c["stage_a"]["cost"]["status"] == PRICE_REQUIRED for c in plan["proposed_configs"])
    unregistered = [b for b in plan["blocked_or_unregistered"] if b["status"] == "NOT_REGISTERED"]
    assert unregistered[0]["model"] == "openai/gpt-5.6-luna"
    priced = _plan(frozen, prices={"*": (1.0, 2.0)})
    assert priced["proposed_configs"][0]["stage_a"]["cost"]["estimated_cost"] > 0
    assert priced["plan_fingerprint"] != plan["plan_fingerprint"]


def test_live_plan_approval_rejects_mismatches(frozen, tmp_path):
    plan = _plan(frozen)
    path = tmp_path / "live-plan.json"
    write_plan(path, plan)
    config = selector_config(provider="openrouter", model="openai/gpt-4o-mini")
    kwargs = dict(snapshot_fingerprint=frozen.manifest["snapshot_fingerprint"],
                  challenge_fingerprint=frozen.challenge["challenge_fingerprint"],
                  contract_fingerprint=selector_contract_fingerprint(),
                  config_fingerprint=config.fingerprint)
    assert load_approved_plan(path, plan["plan_fingerprint"], **kwargs)["stage"] == "A"
    for bad in (dict(approved="0" * 64), dict(snapshot_fingerprint="x"),
                dict(challenge_fingerprint="x"),
                dict(config_fingerprint=selector_config(
                    provider="openrouter", model="openai/gpt-4o-mini", temperature=0.5).fingerprint)):
        approved = bad.pop("approved", plan["plan_fingerprint"])
        with pytest.raises(PlanApprovalError):
            load_approved_plan(path, approved, **{**kwargs, **bad})
    tampered = json.loads(path.read_text(encoding="utf-8"))
    tampered["stage_a"]["total_calls"] = 1
    path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(PlanApprovalError):
        load_approved_plan(path, plan["plan_fingerprint"], **kwargs)
    with pytest.raises(PlanApprovalError):
        load_approved_plan(None, None, **kwargs)


def test_cli_live_without_plan_is_refused_before_any_client(frozen, monkeypatch, tmp_path):
    from services import llm_provider

    monkeypatch.setattr(llm_provider, "OpenAI", lambda **_k: pytest.fail("client built"))
    with pytest.raises(SystemExit):
        cli.main(["run", "--snapshot", str(frozen.dir / "snap"), "--out", str(tmp_path / "o"),
                  "--challenge", str(frozen.dir / "challenge"), "--live",
                  "--confirm-live-provider-calls", "--provider", "openrouter",
                  "--model", "openai/gpt-4o-mini"])


# ── isolation: registry config, circuit breaker, DecisionTrace ─────────────

def test_benchmark_run_does_not_touch_breaker_trace_or_config(frozen, tmp_path, monkeypatch, db):
    from core.database import AICapabilityConfig, AIConfigVersion
    from services import decision_trace, llm_provider
    from services.circuit_breaker import LLM_CIRCUIT_BREAKER

    def boom(*_a, **_k):
        raise AssertionError("production telemetry touched by benchmark")

    monkeypatch.setattr(decision_trace.DecisionTrace, "__init__", boom)
    monkeypatch.setattr(decision_trace, "emit_decision_trace", boom)
    monkeypatch.setattr(LLM_CIRCUIT_BREAKER, "acquire", boom)
    monkeypatch.setattr(LLM_CIRCUIT_BREAKER, "record", boom)
    before = (db.query(AIConfigVersion).count(), db.query(AICapabilityConfig).count())

    class Failing:
        def create(self, **_kwargs):
            raise TimeoutError("provider down")

    def factory(model):
        monkeypatch.setattr(llm_provider, "OpenAI", lambda **_k: SimpleNamespace())
        client = llm_provider.OpenRouterProvider(model=model)
        client.client = SimpleNamespace(chat=SimpleNamespace(completions=Failing()))
        return client

    config = selector_config(provider="openrouter", model="openai/gpt-4o-mini",
                             reasoning_effort="high")
    backend = LiveSelectorBackend(config, client_factory=factory)
    identity = RunIdentity(selector_contract_fingerprint(), config,
                           frozen.manifest["snapshot_fingerprint"], "LIVE")
    ids = {c["case_id"] for c in frozen.built["cases"]}
    summary = run_benchmark(frozen.snaps, backend, identity, tmp_path / "out", case_ids=ids)
    results = load_results(Path(summary.run_dir) / RESULTS_FILE, identity).by_case
    assert {r.outcome.value for r in results.values()} == {"TIMEOUT"}
    assert LLM_CIRCUIT_BREAKER._states == {}
    assert (db.query(AIConfigVersion).count(), db.query(AICapabilityConfig).count()) == before
