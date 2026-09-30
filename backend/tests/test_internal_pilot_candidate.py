"""Pilot candidate (freeze amendment 5): prompt catalog, contract, preflight, Luna activation.

No live LLM call. The activation script is exercised end to end against the
real admin API (FastAPI TestClient) on the throwaway test database.
"""
from __future__ import annotations

import dataclasses
import importlib.util
import json

import pytest

from services import ai_registry
from services.ai_registry import CapabilityAssignment, CapabilityParams, ModelDefinition, QualificationStatus
from services.internal_pilot_candidate import (
    AMENDMENT_4_FINGERPRINT,
    AMENDMENT_5_PATH,
    ANALYZER_CONFIG_FINGERPRINT,
    CHAT_INPUT_MAX_CHARS,
    LUNA_QUALIFICATION_REFERENCE,
    LUNA_REGISTRY_ENTRY,
    LiveCandidateState,
    PARSER_CONTRACT_FINGERPRINT,
    ROLLBACK_ANALYZER_CONFIG_FINGERPRINT,
    SELECTOR_PROMPT_FINGERPRINT,
    SELECTOR_PROMPT_VERSION,
    amendment_5_fingerprint,
    backend_chat_max_chars,
    build_freeze_amendment_5,
    collect_live_state,
    expected_analyzer_config,
    parser_contract_fingerprint,
    run_candidate_preflight,
    widget_chat_max_chars,
)
from services.internal_pilot_freeze import (
    SELECTOR_CONFIG_FINGERPRINT,
    intent_analyzer_prompt_fingerprint,
    repo_root,
)
from services.llm_config import EffectiveLLMConfig, LLMCapability, ReasoningEffort
from services.llm_runtime import AI_CONFIG_CACHE
from services.selector_prompt_catalog import (
    DEFAULT_PROMPT_VERSION,
    PRODUCTION_V2,
    PROMPT_VERSION_ENV,
    VARIANT_A_V1,
    VARIANT_A_V2,
    load_selector_prompt,
    resolve_selector_prompt,
)

PRODUCTION_FP = "2d59cfb65f0aaa08ffcc481d81fed5a89f29803251cdc8a5438da673cb867f3c"
V1_FP = "1aed568885db02f474534224695a45eb6f95835bc31f94e877af9659efe94a1e"
SUPER = "super@iu.tr"


def _check(report, name):
    for check in report.checks:
        if check.name == name:
            return check
    raise AssertionError(f"missing check {name!r}")


def _script():
    path = repo_root() / "deploy" / "internal-pilot" / "activate-luna.py"
    spec = importlib.util.spec_from_file_location("activate_luna", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── Selector prompt catalog ────────────────────────────────────────────────


def test_v1_and_production_fingerprints_are_unchanged():
    assert load_selector_prompt(VARIANT_A_V1).fingerprint == V1_FP
    assert load_selector_prompt(PRODUCTION_V2).fingerprint == PRODUCTION_FP
    assert DEFAULT_PROMPT_VERSION == PRODUCTION_V2
    assert resolve_selector_prompt({}).version == PRODUCTION_V2


def test_v2_loads_through_the_runtime_resolver_with_a_deterministic_fingerprint():
    first = resolve_selector_prompt({PROMPT_VERSION_ENV: VARIANT_A_V2})
    second = load_selector_prompt(VARIANT_A_V2)
    assert first.version == VARIANT_A_V2 == SELECTOR_PROMPT_VERSION
    assert first.fingerprint == second.fingerprint == SELECTOR_PROMPT_FINGERPRINT
    assert first.source == "services/prompts/selector/variant_a_v2.md"


def test_v2_keeps_every_v1_line_verbatim():
    v1 = load_selector_prompt(VARIANT_A_V1).text.splitlines()
    v2 = load_selector_prompt(VARIANT_A_V2).text
    assert all(line in v2 for line in v1)


def test_v2_adds_only_the_specified_narrow_rules():
    v2 = load_selector_prompt(VARIANT_A_V2).text
    assert "5. Kullanıcı bu kurumda bulunmayan bir hizmeti" in v2          # premise
    assert '"hakkı bulunmamaktadır"' in v2                                 # premise guard
    assert "öncülünü düzelten candidate farklı bir ihtiyaca" in v2          # NONE carve-out
    assert "Sınavsız İkinci Üniversite" in v2 and "Ek Madde 1" in v2       # over-specific
    assert "2 iş günü içinde" in v2                                        # relative time
    assert "Bu yalnız zaman sorularına uygulanır" in v2                    # no spill-over
    assert "6. Kullanıcı bir programın" in v2 and "TUTARINI" in v2         # safe fallback
    assert "Bir ücretin var olup olmadığı sorulduğunda" in v2              # fallback guard
    assert v2.rstrip().endswith("Markdown, açıklama, gerekçe, güven skoru veya ek alan yazma.")


def test_v2_prompt_contains_no_benchmark_query_text():
    """The selector gate must not be gamed by copying its queries."""
    gold = (repo_root() / "outputs" / "performance-readiness"
            / "20260926-135453-selector-baseline" / "gold-v1.1.jsonl")
    if not gold.exists():
        pytest.skip("selector baseline gold (git-ignored) not present")
    v2 = load_selector_prompt(VARIANT_A_V2).text
    for line in gold.read_text(encoding="utf-8").splitlines():
        if line.strip():
            assert json.loads(line)["query"] not in v2


# ── Contract constants ──────────────────────────────────────────────────────


def test_expected_analyzer_config_fingerprint():
    assert expected_analyzer_config().fingerprint == ANALYZER_CONFIG_FINGERPRINT


def test_unset_reasoning_is_a_different_config():
    unset = dataclasses.replace(expected_analyzer_config(), reasoning_effort=None)
    assert unset.fingerprint != ANALYZER_CONFIG_FINGERPRINT


def test_rollback_config_fingerprint_is_gpt_4o_mini_at_600():
    config = EffectiveLLMConfig(
        capability=LLMCapability.INTENT_ANALYZER, provider="openrouter",
        model="openai/gpt-4o-mini", max_tokens=600)
    assert config.fingerprint == ROLLBACK_ANALYZER_CONFIG_FINGERPRINT


def test_parser_contract_fingerprint_is_pinned():
    assert parser_contract_fingerprint() == PARSER_CONTRACT_FINGERPRINT


def test_input_limits_read_statically_match_the_policy():
    from core.limits import CHAT_MESSAGE_MAX_CHARS

    assert backend_chat_max_chars() == widget_chat_max_chars() == CHAT_INPUT_MAX_CHARS
    assert CHAT_MESSAGE_MAX_CHARS == CHAT_INPUT_MAX_CHARS


def test_activation_script_constants_equal_the_contract():
    from services import internal_pilot_candidate as contract

    script = _script()
    assert script.ANALYZER_MODEL == contract.ANALYZER_MODEL
    assert script.ANALYZER_PROVIDER == contract.ANALYZER_PROVIDER
    assert script.ANALYZER_MAX_TOKENS == contract.ANALYZER_MAX_TOKENS
    assert script.ANALYZER_TEMPERATURE == contract.ANALYZER_TEMPERATURE
    assert script.ANALYZER_REASONING == contract.ANALYZER_REASONING.value
    assert script.ANALYZER_CONFIG_FINGERPRINT == contract.ANALYZER_CONFIG_FINGERPRINT
    assert script.ROLLBACK_MODEL == contract.ROLLBACK_ANALYZER_MODEL
    assert script.ROLLBACK_CONFIG_FINGERPRINT == contract.ROLLBACK_ANALYZER_CONFIG_FINGERPRINT
    assert script.SELECTOR_CONFIG_FINGERPRINT == SELECTOR_CONFIG_FINGERPRINT
    assert script.LUNA_REGISTRY_ENTRY == LUNA_REGISTRY_ENTRY
    assert script.LUNA_QUALIFICATION_REFERENCE == LUNA_QUALIFICATION_REFERENCE


def test_pilot_env_selects_the_current_candidate_and_seeds_a_600_token_analyzer():
    # Amendment 5 pinned variant_a_v2 here; amendment 6 (current pilot candidate)
    # moved the pilot prompt to variant_a_v3_contract. Amendment 5 stays history.
    from services.internal_pilot_amendment6 import SELECTOR_PROMPT_VERSION as AM6_PROMPT
    from services.internal_pilot_runtime import PILOT_PROMPT_VERSION, load_pilot_env

    env = load_pilot_env()
    assert env["SELECTOR_PROMPT_VERSION"] == PILOT_PROMPT_VERSION == AM6_PROMPT
    assert env["LLM_INTENT_ANALYZER_MAX_TOKENS"] == "600"
    assert "LLM_INTENT_ANALYZER_REASONING_EFFORT" not in env


# ── Amendment 5 ─────────────────────────────────────────────────────────────


def _amendment5():
    return json.loads(repo_root().joinpath(*AMENDMENT_5_PATH).read_text(encoding="utf-8"))


def test_amendment_5_file_matches_its_fingerprint_and_the_builder():
    stored = _amendment5()
    assert amendment_5_fingerprint(stored) == stored["amendment_fingerprint"]
    rebuilt = build_freeze_amendment_5(git_commit="x" * 40, created_at="2030-01-01T00:00:00Z")
    assert rebuilt["amendment_fingerprint"] == stored["amendment_fingerprint"]


def test_amendment_5_fingerprint_ignores_metadata_and_gate_evidence():
    a = build_freeze_amendment_5(git_commit="a" * 40, created_at="2026-01-01T00:00:00Z")
    b = build_freeze_amendment_5(git_commit="b" * 40, created_at="2027-01-01T00:00:00Z",
                                 gate_evidence={"analyzer": "PASS"})
    assert a["amendment_fingerprint"] == b["amendment_fingerprint"]


def test_amendment_5_chains_to_immutable_amendment_4():
    a5 = _amendment5()
    a4 = json.loads((repo_root() / "deploy" / "internal-pilot"
                     / "answer-pipeline-freeze-amendment-4.json").read_text(encoding="utf-8"))
    assert a5["parent_amendment_fingerprint"] == AMENDMENT_4_FINGERPRINT == a4["amendment_fingerprint"]
    assert a5["parents_are_immutable"] is True


def test_amendment_5_pins_the_candidate_contract():
    a5 = _amendment5()
    analyzer, selector = a5["intent_analyzer"], a5["selector"]
    assert (analyzer["provider"], analyzer["model"]) == ("openrouter", "openai/gpt-6-luna")
    assert analyzer["reasoning_effort"] == "none" and analyzer["max_tokens"] == 600
    assert analyzer["config_fingerprint"] == ANALYZER_CONFIG_FINGERPRINT
    assert analyzer["prompt_fingerprint"] == intent_analyzer_prompt_fingerprint()
    assert selector["model"] == "openai/gpt-4o-mini" and selector["max_tokens"] == 32
    assert selector["prompt_version"] == "variant_a_v2"
    assert selector["prompt_fingerprint"] == SELECTOR_PROMPT_FINGERPRINT
    assert selector["config_fingerprint"] == SELECTOR_CONFIG_FINGERPRINT
    assert a5["parser"]["contract_fingerprint"] == PARSER_CONTRACT_FINGERPRINT
    assert a5["parser"]["parser_v3"] is False
    assert a5["input_policy"]["widget_max_chars"] == a5["input_policy"]["backend_max_chars"] == 500
    assert a5["rollback"]["intent_analyzer_config_fingerprint"] == ROLLBACK_ANALYZER_CONFIG_FINGERPRINT
    assert a5["rollback"]["selector_prompt_version"] == "variant_a_v1"
    c = a5["classification"]
    for unchanged in ("selector_model_change", "selector_config_change", "parser_contract_change",
                      "retrieval_change", "eligibility_or_threshold_change",
                      "candidate_budget_change", "provider_retry_or_deadline_change"):
        assert c[unchanged] is False


def test_amendments_1_to_4_are_unchanged():
    from services.internal_pilot_freeze import (
        amendment_2_fingerprint, amendment_3_fingerprint, amendment_4_fingerprint,
        amendment_fingerprint,
    )
    base = repo_root() / "deploy" / "internal-pilot"
    for n, fn in ((1, amendment_fingerprint), (2, amendment_2_fingerprint),
                  (3, amendment_3_fingerprint), (4, amendment_4_fingerprint)):
        stored = json.loads((base / f"answer-pipeline-freeze-amendment-{n}.json")
                            .read_text(encoding="utf-8"))
        assert fn(stored) == stored["amendment_fingerprint"]


# ── Candidate preflight: synthetic live state ───────────────────────────────


def _model(mid, identifier, caps=("intent_analyzer", "selector"), qualification="LEGACY_APPROVED",
           reasoning=False, efforts=()):
    return ModelDefinition(
        id=mid, display_name=identifier, provider="openrouter", model_identifier=identifier,
        enabled=True, allowed_capabilities=tuple(caps), supports_structured_output=True,
        supports_reasoning_effort=reasoning, allowed_reasoning_efforts=tuple(efforts),
        qualification_status=QualificationStatus(qualification))


LUNA = _model(4, "openai/gpt-6-luna", caps=("intent_analyzer",), qualification="QUALIFIED",
              reasoning=True, efforts=("none",))
MINI = _model(3, "openai/gpt-4o-mini")


def _state(**overrides):
    base = dict(
        analyzer=CapabilityAssignment(
            LLMCapability.INTENT_ANALYZER, LUNA,
            CapabilityParams(temperature=0.0, max_tokens=600, reasoning_effort="none")),
        selector=CapabilityAssignment(
            LLMCapability.SELECTOR, MINI, CapabilityParams(temperature=0.0, max_tokens=32)),
        config_version=5,
        llm_enabled=True,
        selector_prompt_version=VARIANT_A_V2,
        selector_prompt_fingerprint=SELECTOR_PROMPT_FINGERPRINT,
        backend_max_chars=500,
        widget_max_chars=500,
        parser_fingerprint=PARSER_CONTRACT_FINGERPRINT,
    )
    base.update(overrides)
    return LiveCandidateState(**base)


def _analyzer(model=LUNA, **params):
    values = dict(temperature=0.0, max_tokens=600, reasoning_effort="none")
    values.update(params)
    return CapabilityAssignment(LLMCapability.INTENT_ANALYZER, model, CapabilityParams(**values))


def test_candidate_preflight_passes_for_the_candidate():
    report = run_candidate_preflight(_state())
    assert report.status == "PASS", report.to_dict()["failed_checks"]


@pytest.mark.parametrize("overrides,failing", [
    ({"analyzer": _analyzer(model=MINI, reasoning_effort=None)},
     ["analyzer_model_matches", "analyzer_config_fingerprint_matches"]),
    ({"analyzer": _analyzer(max_tokens=300)},
     ["analyzer_max_tokens_matches", "analyzer_config_fingerprint_matches"]),
    ({"analyzer": _analyzer(reasoning_effort=None)},
     ["analyzer_reasoning_is_explicit_none", "analyzer_config_fingerprint_matches"]),
    ({"selector_prompt_version": VARIANT_A_V1, "selector_prompt_fingerprint": V1_FP},
     ["selector_prompt_version_matches", "selector_prompt_fingerprint_matches"]),
    ({"selector": CapabilityAssignment(LLMCapability.SELECTOR, MINI,
                                       CapabilityParams(temperature=0.0, max_tokens=40))},
     ["selector_max_tokens_matches", "selector_config_fingerprint_matches"]),
    ({"widget_max_chars": 1000},
     ["widget_chat_input_max_matches", "input_limits_are_equal"]),
    ({"backend_max_chars": None},
     ["backend_chat_input_max_matches", "input_limits_are_equal"]),
    ({"llm_enabled": False}, ["llm_enabled_live_is_true"]),
    ({"parser_fingerprint": "0" * 64}, ["strict_parser_contract_unchanged"]),
    ({"analyzer": _analyzer(model=dataclasses.replace(LUNA, qualification_status=QualificationStatus.UNTESTED))},
     ["analyzer_model_is_qualified_and_enabled"]),
    ({"analyzer": None, "selector": None, "config_error": "config_invalid"},
     ["managed_config_loaded", "analyzer_model_matches", "selector_model_matches"]),
])
def test_candidate_preflight_fails_on_drift(overrides, failing):
    report = run_candidate_preflight(_state(**overrides))
    assert report.status == "FAIL"
    for name in failing:
        assert not _check(report, name).passed, name


# ── Luna activation script against the real admin API ───────────────────────


def _bootstrap(db):
    ai_registry.bootstrap_ai_registry(db, environ={"LLM_PROVIDER": "openrouter"})
    db.commit()
    AI_CONFIG_CACHE.reset()


def _call(client):
    def call(method, path, body):
        response = client.request(method, path, json=body)
        return response.status_code, response.json()
    return call


def _config(client):
    return client.get("/api/ai-config/config").json()


def _fp(view, capability):
    return view["capabilities"][capability]["config_fingerprint"]


@pytest.fixture()
def super_client(db, make_user, login):
    _bootstrap(db)
    make_user(SUPER, role="super_admin")
    return login(SUPER)


def test_activation_creates_rollback_version_registers_and_assigns_luna(super_client):
    before = _config(super_client)
    assert before["version"] == 1
    assert before["capabilities"]["intent_analyzer"]["max_tokens"] == 300
    assert _fp(before, "selector") == SELECTOR_CONFIG_FINGERPRINT

    result = _script().activate(_call(super_client), log=lambda *_: None)

    assert result["changes"] == ["rollback_version_created", "luna_registered",
                                 "luna_qualified", "luna_assigned"]
    history = super_client.get("/api/ai-config/config/history").json()["versions"]
    rollback = next(v for v in history if v["version"] == result["rollback_version"])
    assert result["rollback_version"] == 2
    assert rollback["snapshot"]["capabilities"]["intent_analyzer"]["config_fingerprint"] == (
        ROLLBACK_ANALYZER_CONFIG_FINGERPRINT)

    after = _config(super_client)
    analyzer = after["capabilities"]["intent_analyzer"]
    assert _fp(after, "intent_analyzer") == ANALYZER_CONFIG_FINGERPRINT
    assert analyzer["reasoning_effort"] == "none" and analyzer["max_tokens"] == 600
    assert analyzer["model"]["model_identifier"] == "openai/gpt-6-luna"
    assert analyzer["model"]["qualification_status"] == "QUALIFIED"
    assert analyzer["model"]["qualification_reference"] == LUNA_QUALIFICATION_REFERENCE
    assert analyzer["model"]["allowed_capabilities"] == ["intent_analyzer"]
    assert analyzer["model"]["allowed_reasoning_efforts"] == ["none"]
    assert _fp(after, "selector") == SELECTOR_CONFIG_FINGERPRINT
    assert result["active_version"] == after["version"] == 3


def test_activation_is_idempotent(super_client):
    script = _script()
    first = script.activate(_call(super_client), log=lambda *_: None)
    models_before = super_client.get("/api/ai-config/models").json()["models"]
    second = script.activate(_call(super_client), log=lambda *_: None)
    assert second["changes"] == []
    assert second["rollback_version"] == first["rollback_version"]
    assert second["active_version"] == first["active_version"]
    assert super_client.get("/api/ai-config/models").json()["models"] == models_before


def test_luna_cannot_be_assigned_to_the_selector(super_client):
    _script().activate(_call(super_client), log=lambda *_: None)
    view = _config(super_client)
    luna_id = view["capabilities"]["intent_analyzer"]["model"]["id"]
    response = super_client.put("/api/ai-config/config/selector", json={
        "expected_version": view["version"], "model_registry_id": luna_id,
        "temperature": 0, "max_tokens": 32})
    assert response.status_code == 400 and response.json()["code"] == "capability_not_allowed"


def test_rollback_goes_to_gpt_4o_mini_at_600_and_reactivation_reuses_it(super_client):
    script = _script()
    first = script.activate(_call(super_client), log=lambda *_: None)
    current = _config(super_client)["version"]
    rolled = super_client.post(f"/api/ai-config/config/rollback/{first['rollback_version']}",
                               json={"expected_version": current})
    assert rolled.status_code == 200
    assert _fp(_config(super_client), "intent_analyzer") == ROLLBACK_ANALYZER_CONFIG_FINGERPRINT
    assert _fp(_config(super_client), "selector") == SELECTOR_CONFIG_FINGERPRINT

    again = script.activate(_call(super_client), log=lambda *_: None)
    # Already at gpt-4o-mini @600: the current version is itself a valid rollback base.
    assert "rollback_version_created" not in again["changes"]
    assert again["changes"] == ["luna_assigned"]
    assert _fp(_config(super_client), "intent_analyzer") == ANALYZER_CONFIG_FINGERPRINT


def test_existing_untested_luna_with_drift_is_corrected_not_duplicated(super_client):
    created = super_client.post("/api/ai-config/models", json={
        **LUNA_REGISTRY_ENTRY, "allowed_capabilities": ["intent_analyzer", "selector"]})
    assert created.status_code == 201
    result = _script().activate(_call(super_client), log=lambda *_: None)
    assert "luna_registered" not in result["changes"]
    assert {"luna_fields_corrected", "luna_qualified"} <= set(result["changes"])
    models = super_client.get("/api/ai-config/models").json()["models"]
    lunas = [m for m in models if m["model_identifier"] == "openai/gpt-6-luna"]
    assert len(lunas) == 1 and lunas[0]["allowed_capabilities"] == ["intent_analyzer"]


def test_activation_refuses_to_run_when_the_selector_drifted(super_client):
    view = _config(super_client)
    selector_model = view["capabilities"]["selector"]["model"]["id"]
    super_client.put("/api/ai-config/config/selector", json={
        "expected_version": view["version"], "model_registry_id": selector_model,
        "temperature": 0, "max_tokens": 40})
    script = _script()
    with pytest.raises(script.ActivationError, match="never edits the selector"):
        script.activate(_call(super_client), log=lambda *_: None)
    models = super_client.get("/api/ai-config/models").json()["models"]
    assert not any(m["model_identifier"] == "openai/gpt-6-luna" for m in models)


def test_concurrent_edit_stops_the_activation_with_a_conflict(super_client):
    script = _script()
    real = _call(super_client)

    def racing(method, path, body):
        if method == "PUT" and body and body.get("expected_version") is not None:
            body = {**body, "expected_version": body["expected_version"] - 1 or 999}
        return real(method, path, body)

    with pytest.raises(script.ActivationError, match="HTTP 409"):
        script.activate(racing, log=lambda *_: None)


def test_dry_run_changes_nothing(super_client):
    before = _config(super_client)
    result = _script().activate(_call(super_client), dry_run=True, log=lambda *_: None)
    assert result["dry_run"] is True and result["changes"] == []
    assert _config(super_client) == before


def test_candidate_preflight_against_the_activated_test_database(db, super_client):
    from core.database import SystemConfig

    _script().activate(_call(super_client), log=lambda *_: None)
    row = db.get(SystemConfig, "LLM_ENABLED")
    if row is None:
        db.add(SystemConfig(key="LLM_ENABLED", value="true"))
    else:
        row.value = "true"
    db.commit()
    db.expire_all()

    state = collect_live_state(db, environ={PROMPT_VERSION_ENV: VARIANT_A_V2})
    report = run_candidate_preflight(state)
    assert report.status == "PASS", report.to_dict()["failed_checks"]

    # Operator reverts the analyzer to the rollback version -> preflight notices.
    view = _config(super_client)
    history = super_client.get("/api/ai-config/config/history").json()["versions"]
    rollback = min(v["version"] for v in history if v["version"] > 1)
    super_client.post(f"/api/ai-config/config/rollback/{rollback}",
                      json={"expected_version": view["version"]})
    db.expire_all()
    reverted = run_candidate_preflight(collect_live_state(db, environ={PROMPT_VERSION_ENV: VARIANT_A_V2}))
    assert not _check(reverted, "analyzer_model_matches").passed
    v1 = run_candidate_preflight(collect_live_state(db, environ={PROMPT_VERSION_ENV: VARIANT_A_V1}))
    assert not _check(v1, "selector_prompt_version_matches").passed
