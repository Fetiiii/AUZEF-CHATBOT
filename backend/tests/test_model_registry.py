"""Phase 6: managed LLM model registry, capability config, versions, admin API."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from core.database import (
    AICapabilityConfig,
    AIConfigAudit,
    AIConfigVersion,
    AIModelRegistry,
    SystemConfig,
    execute_admin_sql,
)
from services import ai_registry
from services.ai_registry import AIConfigError, QualificationStatus
from services.candidate_eligibility import CandidateKind, SelectorCandidate
from services.circuit_breaker import (
    LLM_CIRCUIT_BREAKER,
    CallOutcomeKind,
    CircuitState,
    breaker_key,
)
from services.llm_config import LLMCapability, resolve_llm_config_set
from services.llm_provider import BaseLLMProvider, ManagedLLMProvider
from services.llm_types import LLMInvocationResult, LLMOutcomeStatus, LLMResponseMetadata
from services.llm_runtime import (
    AI_CONFIG_CACHE,
    AIConfigCache,
    ConfigSource,
    ConfigStatus,
)

ENV = {"LLM_PROVIDER": "openrouter"}
SUPER = "super@iu.tr"


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


# ── helpers ──────────────────────────────────────────────────────────────────

def _bootstrap(db, environ=ENV):
    result = ai_registry.bootstrap_ai_registry(db, environ=environ)
    db.commit()
    AI_CONFIG_CACHE.reset()
    return result


def _model_id(db, provider, identifier):
    return db.query(AIModelRegistry).filter_by(
        provider=provider, model_identifier=identifier).one().id


def _register(db, identifier="openai/gpt-new", *, provider="openrouter",
              caps=("intent_analyzer", "selector"), structured=True,
              reasoning=False, efforts=(), qualify=True):
    model = ai_registry.register_model(db, {
        "display_name": identifier, "provider": provider, "model_identifier": identifier,
        "allowed_capabilities": list(caps), "supports_structured_output": structured,
        "supports_reasoning_effort": reasoning, "allowed_reasoning_efforts": list(efforts),
    }, actor=SUPER)
    if qualify:
        model = ai_registry.update_model(db, model.id, {
            "qualification_status": "QUALIFIED",
            "qualification_reference": "manual-review-ticket-123",
        }, actor=SUPER)
    db.commit()
    return model


def _params(**overrides):
    values = dict(temperature=0.0, max_tokens=32, reasoning_effort=None,
                  timeout_seconds=None, max_retries=None, structured_output_enabled=False)
    values.update(overrides)
    return values


def _set_capability(db, capability, model_id, **overrides):
    version = ai_registry.current_version_id(db)
    base = _params(max_tokens=300 if capability == "intent_analyzer" else 32)
    base.update(overrides)
    result = ai_registry.update_capability_config(
        db, capability, {"model_registry_id": model_id, **base},
        expected_version=version, actor=SUPER,
    )
    db.commit()
    AI_CONFIG_CACHE.invalidate()
    return result


def _super(make_user, login):
    make_user(SUPER, role="super_admin")
    return login(SUPER)


class ScriptedClient(BaseLLMProvider):
    provider_name = "openrouter"

    def __init__(self, outputs=()):
        self.model = "boot-time-model"
        self._configs = resolve_llm_config_set("openrouter", environ={})
        self.outputs = list(outputs)
        self.calls = []

    def _complete(self, system, user, max_tokens=5):
        raise AssertionError("managed path must use _invoke with managed config")

    def _invoke(self, system, user, config):
        self.calls.append(config)
        output = self.outputs.pop(0) if self.outputs else '{"decision":"NONE"}'
        return LLMInvocationResult(LLMOutcomeStatus.SUCCESS, output, 1.0,
                                   LLMResponseMetadata(requested_model=config.model))


# ── registry (§48) ───────────────────────────────────────────────────────────

def test_register_valid_model_is_untested_and_audited(db):
    model = _register(db, qualify=False)
    assert model.qualification_status is QualificationStatus.UNTESTED
    assert model.enabled is True
    event = db.query(AIConfigAudit).filter_by(event_type="MODEL_REGISTERED").one()
    assert event.actor == SUPER and json.loads(event.new_value)["model_identifier"] == "openai/gpt-new"


def test_duplicate_provider_model_rejected_by_service_and_db(db):
    _register(db, qualify=False)
    with pytest.raises(AIConfigError) as exc:
        _register(db, qualify=False)
    assert exc.value.code == "duplicate_model" and exc.value.status == 409
    db.rollback()
    db.add(AIModelRegistry(display_name="x", provider="openrouter",
                           model_identifier="openai/gpt-new"))
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()
    # Same identifier under another provider is a different registry identity.
    _register(db, provider="openai", qualify=False)


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"provider": "anthropic-direct"}, "unsupported_provider"),
        ({"allowed_capabilities": ["summarizer"]}, "invalid_capability"),
        ({"allowed_capabilities": []}, "invalid_capability"),
        ({"model_identifier": "has space"}, "invalid_model_identifier"),
        ({"allowed_reasoning_efforts": ["low"]}, "invalid_reasoning_effort"),
        ({"supports_reasoning_effort": True}, "invalid_reasoning_effort"),
        ({"supports_reasoning_effort": True, "allowed_reasoning_efforts": ["extreme"]},
         "invalid_reasoning_effort"),
    ],
)
def test_invalid_model_definitions_rejected(db, overrides, code):
    payload = {"display_name": "m", "provider": "openrouter", "model_identifier": "x/y",
               "allowed_capabilities": ["selector"], "supports_structured_output": True,
               "supports_reasoning_effort": False, "allowed_reasoning_efforts": []}
    payload.update(overrides)
    with pytest.raises(AIConfigError) as exc:
        ai_registry.register_model(db, payload, actor=SUPER)
    assert exc.value.code == code


def test_qualification_requires_real_reference_and_legacy_is_bootstrap_only(db):
    model = _register(db, qualify=False)
    with pytest.raises(AIConfigError) as exc:
        ai_registry.update_model(db, model.id, {"qualification_status": "QUALIFIED"}, actor=SUPER)
    assert exc.value.code == "qualification_reference_required"
    with pytest.raises(AIConfigError) as exc:
        ai_registry.update_model(
            db, model.id, {"qualification_status": "LEGACY_APPROVED"}, actor=SUPER)
    assert exc.value.code == "invalid_qualification"
    qualified = ai_registry.update_model(db, model.id, {
        "qualification_status": "QUALIFIED", "qualification_reference": "run-42"}, actor=SUPER)
    assert qualified.qualified_by == SUPER and qualified.qualification_reference == "run-42"
    assert qualified.qualified_at is not None


@pytest.mark.parametrize(
    ("model_kwargs", "patch", "code"),
    [
        ({}, {"enabled": False}, "model_disabled"),
        ({"qualify": False}, {}, "model_not_qualified"),
        ({}, {"qualification_status": "BLOCKED"}, "model_not_qualified"),
        ({"caps": ("selector",)}, {}, "capability_not_allowed"),
        ({"structured": False}, {}, "structured_output_unsupported"),
    ],
)
def test_ineligible_models_cannot_be_assigned(db, model_kwargs, patch, code):
    _bootstrap(db)
    model = _register(db, **model_kwargs)
    if patch:
        ai_registry.update_model(db, model.id, patch, actor=SUPER)
        db.commit()
    with pytest.raises(AIConfigError) as exc:
        _set_capability(db, "intent_analyzer", model.id)
    assert exc.value.code == code
    db.rollback()
    assert ai_registry.current_version_id(db) == 1


def test_reasoning_effort_is_capability_aware(db):
    _bootstrap(db)
    plain = _register(db, "plain/model")
    reasoner = _register(db, "reason/model", reasoning=True, efforts=("low", "medium"))
    with pytest.raises(AIConfigError) as exc:
        _set_capability(db, "selector", plain.id, reasoning_effort="low")
    assert exc.value.code == "reasoning_unsupported"
    db.rollback()
    with pytest.raises(AIConfigError) as exc:
        _set_capability(db, "selector", reasoner.id, reasoning_effort="high")
    assert exc.value.code == "invalid_reasoning_effort"
    db.rollback()
    assert _set_capability(db, "selector", reasoner.id, reasoning_effort="medium")["changed"]


# ── capability config validation (§49) ──────────────────────────────────────

@pytest.mark.parametrize(
    ("capability", "overrides", "code"),
    [
        ("selector", {"max_tokens": 1}, "invalid_max_tokens"),
        ("selector", {"max_tokens": 5000}, "invalid_max_tokens"),
        ("intent_analyzer", {"max_tokens": 50}, "invalid_max_tokens"),
        ("selector", {"temperature": -0.1}, "invalid_temperature"),
        ("selector", {"temperature": 1.5}, "invalid_temperature"),
        ("selector", {"temperature": float("nan")}, "invalid_temperature"),
        ("selector", {"timeout_seconds": 0.0}, "invalid_timeout"),
        ("selector", {"timeout_seconds": 600.0}, "invalid_timeout"),
        ("selector", {"max_retries": -1}, "invalid_retries"),
        ("selector", {"max_retries": 50}, "invalid_retries"),
        ("selector", {"structured_output_enabled": True}, "native_structured_output_unavailable"),
    ],
)
def test_capability_parameter_bounds_are_enforced(db, capability, overrides, code):
    _bootstrap(db)
    model_id = _model_id(db, "openrouter", "openai/gpt-4o-mini")
    with pytest.raises(AIConfigError) as exc:
        _set_capability(db, capability, model_id, **overrides)
    assert exc.value.code == code


def test_capabilities_can_use_different_or_same_models(db):
    _bootstrap(db)
    same = ai_registry.load_active_config(db)
    assert (same.assignments[LLMCapability.INTENT_ANALYZER].model.id
            == same.assignments[LLMCapability.SELECTOR].model.id)
    openai_id = _model_id(db, "openai", "gpt-4o-mini")
    _set_capability(db, "selector", openai_id, timeout_seconds=8.0, max_retries=2)
    active = ai_registry.load_active_config(db).config_set()
    assert (active.intent_analyzer.provider, active.intent_analyzer.model) == (
        "openrouter", "openai/gpt-4o-mini")
    assert (active.selector.provider, active.selector.model) == ("openai", "gpt-4o-mini")
    assert active.selector.timeout_seconds == 8.0 and active.selector.max_retries == 2


# ── bootstrap preserves Phase 5 behavior (§12/§50/§53) ──────────────────────

@pytest.mark.parametrize("provider", ["openai", "openrouter", "gemini"])
def test_bootstrap_reproduces_phase5_effective_config_exactly(db, provider):
    env = {"LLM_PROVIDER": provider}
    assert _bootstrap(db, env)["bootstrapped"] is True
    expected = resolve_llm_config_set(provider, environ=env)
    actual = ai_registry.load_active_config(db).config_set()
    assert actual.to_dict() == expected.to_dict()
    assert actual.fingerprint == expected.fingerprint
    assert actual.selector.max_tokens == 32 and actual.intent_analyzer.max_tokens == 300
    assert actual.selector.temperature == 0.0 and actual.selector.reasoning_effort is None
    statuses = {m.qualification_status for m in ai_registry.list_models(db)}
    assert statuses == {QualificationStatus.LEGACY_APPROVED}


def test_bootstrap_is_idempotent_and_keeps_env_model_overrides(db):
    env = {"LLM_PROVIDER": "openrouter", "LLM_SELECTOR_MODEL": "openai/gpt-4.1-mini",
           "LLM_SELECTOR_TIMEOUT_SECONDS": "9"}
    assert _bootstrap(db, env)["bootstrapped"] is True
    assert _bootstrap(db, env) == {"bootstrapped": False, "reason": "already_versioned"}
    assert db.query(AIConfigVersion).count() == 1
    assert db.query(AIModelRegistry).count() == 4  # 3 defaults + env override
    actual = ai_registry.load_active_config(db).config_set()
    assert actual.fingerprint == resolve_llm_config_set("openrouter", environ=env).fingerprint


def test_bootstrap_never_rewrites_out_of_bounds_env_config(db):
    env = {"LLM_PROVIDER": "openrouter", "LLM_SELECTOR_MAX_TOKENS": "5"}
    result = _bootstrap(db, env)
    assert result["bootstrapped"] is False
    assert result["reason"].startswith("env_config_out_of_bounds")
    assert ai_registry.current_version_id(db) is None  # legacy env path stays


def test_bootstrap_without_env_provider_seeds_registry_only(db):
    assert _bootstrap(db, {}) == {"bootstrapped": False, "reason": "no_env_provider"}
    assert db.query(AIModelRegistry).count() == 3


def test_init_system_db_bootstraps_idempotently(monkeypatch):
    import scripts.init_system as init_system
    from core.database import SessionLocal

    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    for _ in range(3):
        assert init_system.main(["db"]) == 0
    with SessionLocal() as db:
        assert db.query(AIConfigVersion).count() == 1
        assert db.query(AIModelRegistry).count() == 3


# ── runtime resolution, cache and propagation (§13–§15, §37–§39, §51/§52/§59) ─

def _wire_clients(monkeypatch, clients):
    import core.deps as deps

    monkeypatch.setattr(deps, "_provider_client", lambda name, _db: clients.get(name))


def test_runtime_uses_db_config_without_restart_and_routes_capabilities(db, monkeypatch):
    import core.deps as deps

    _bootstrap(db)
    router_client, openai_client = ScriptedClient(), ScriptedClient()
    _wire_clients(monkeypatch, {"openrouter": router_client, "openai": openai_client})
    first = deps.get_llm_provider(db)
    assert isinstance(first, ManagedLLMProvider)
    assert first.runtime.version_id == 1 and first.runtime.source is ConfigSource.DB
    old_fp = first.configs.selector.fingerprint

    _set_capability(db, "selector", _model_id(db, "openai", "gpt-4o-mini"), max_tokens=48)
    second = deps.get_llm_provider(db)
    assert second.runtime.version_id == 2
    assert second.configs.selector.fingerprint != old_fp
    candidates = [SelectorCandidate("qna:1", CandidateKind.QNA, "q", "a", qna_id=1)]
    second.ask_with_result("soru", candidates)
    assert openai_client.calls[-1].model == "gpt-4o-mini"
    assert openai_client.calls[-1].max_tokens == 48
    assert router_client.calls == []  # analyzer client not used for selector
    # The shared client's boot-time env config was never mutated.
    assert openai_client._configs.selector.max_tokens == 32


def test_trace_records_config_version_fingerprint_source_and_registry_ids(db, monkeypatch):
    from services import answer_pipeline
    from services.decision_trace import DecisionTrace

    _bootstrap(db)
    db.add(SystemConfig(key="LLM_ENABLED", value="true"))
    db.commit()
    analyzer_json = json.dumps({"intent_count": 1, "intents": [{
        "source_text": "soru", "normalized_text": "soru", "resolved_text": "soru",
        "context_used": False, "calendar_relevant": False}]})
    client = ScriptedClient([analyzer_json])
    _wire_clients(monkeypatch, {"openrouter": client})
    monkeypatch.setattr(answer_pipeline, "meili_search_safe", lambda _q, limit: [])
    monkeypatch.setattr(answer_pipeline.QDRANT_PROVIDER, "search", lambda _q, limit=3: [])
    trace = DecisionTrace(endpoint="test")
    answer_pipeline.answer_question("soru", db, trace=trace)
    request = trace.to_dict()["request"]
    assert trace.to_dict()["schema_version"] == 6
    assert request["ai_config_version"] == 1
    assert request["ai_config_source"] in ("DB", "CACHE")
    assert request["ai_config_status"] == "OK"
    model_id = _model_id(db, "openrouter", "openai/gpt-4o-mini")
    for cap in ("intent_analyzer", "selector"):
        entry = request["ai_capabilities"][cap]
        assert entry["registry_model_id"] == model_id
        assert entry["qualification_status"] == "LEGACY_APPROVED"
        assert len(entry["config_fingerprint"]) == 64
    assert request["ai_capabilities"]["selector"]["config_fingerprint"] == (
        ai_registry.load_active_config(db).config_set().selector.fingerprint
    )
    assert request["ai_config_fingerprint"] is not None


def test_two_node_propagation_is_bounded_by_ttl(db):
    _bootstrap(db)
    clock = FakeClock()
    node_a = AIConfigCache(ttl_seconds=5, max_stale_seconds=300, clock=clock)
    node_b = AIConfigCache(ttl_seconds=5, max_stale_seconds=300, clock=clock)
    assert node_a.get(db).version_id == node_b.get(db).version_id == 1
    _set_capability(db, "selector", _model_id(db, "openrouter", "openai/gpt-4o-mini"),
                    max_tokens=40)
    node_a.invalidate()                       # the writing node
    assert node_a.get(db).version_id == 2
    clock.now += 4.9
    stale = node_b.get(db)
    assert stale.version_id == 1 and stale.source is ConfigSource.CACHE
    clock.now += 0.1                           # ttl elapsed → version probe
    fresh = node_b.get(db)
    assert fresh.version_id == 2 and fresh.source is ConfigSource.DB


def test_db_failure_uses_bounded_stale_cache_then_safe_unavailable(db):
    _bootstrap(db)
    clock = FakeClock()
    failing = {"on": False}

    def prober(session):
        if failing["on"]:
            raise DBAPIError("SELECT", {}, Exception("db down"))
        return ai_registry.current_version_id(session)

    cache = AIConfigCache(ttl_seconds=5, max_stale_seconds=60, clock=clock, prober=prober)
    assert cache.get(db).status is ConfigStatus.OK
    failing["on"] = True
    clock.now += 10
    stale = cache.get(db)
    assert stale.status is ConfigStatus.OK and stale.source is ConfigSource.STALE_CACHE
    clock.now += 60
    gone = cache.get(db)
    assert gone.status is ConfigStatus.CONFIG_UNAVAILABLE and gone.configs is None
    cold = AIConfigCache(ttl_seconds=5, max_stale_seconds=60, clock=clock, prober=prober)
    assert cold.get(db).status is ConfigStatus.CONFIG_UNAVAILABLE


def test_malformed_persisted_config_is_config_degraded_not_semantic_none(db, monkeypatch):
    from services import answer_pipeline
    from services.decision_trace import DecisionTrace
    import core.deps as deps

    _bootstrap(db)
    db.add(SystemConfig(key="LLM_ENABLED", value="true"))
    # Bypass the API: an out-of-band edit makes the persisted state invalid.
    execute_admin_sql(
        db, text("UPDATE ai_capability_config SET max_tokens = 1 WHERE capability='selector'"))
    db.commit()
    AI_CONFIG_CACHE.reset()
    _wire_clients(monkeypatch, {"openrouter": ScriptedClient()})
    assert deps.get_llm_provider(db) is None
    assert deps.llm_config_problem(db) == "config_invalid"
    monkeypatch.setattr(answer_pipeline, "meili_search_safe", lambda _q, limit: [])
    monkeypatch.setattr(answer_pipeline.QDRANT_PROVIDER, "search", lambda _q, limit=3: [])
    trace = DecisionTrace(endpoint="test")
    assert answer_pipeline.answer_question("soru", db, trace=trace) == (None, "none")
    execution = trace.to_dict()["execution"]
    assert execution["execution_mode"] == "CONFIG_DEGRADED"
    assert execution["degraded_reason"] == "config_invalid"
    assert execution["intents"][0]["resolution"] == "degraded_none"
    assert trace.to_dict()["selectors"] == []


# ── versions, rollback, audit, concurrency, disable (§16–§22, §40, §53–§58) ─

def test_rollback_creates_new_version_from_old_snapshot(db):
    _bootstrap(db)
    router_id = _model_id(db, "openrouter", "openai/gpt-4o-mini")
    v1_fp = ai_registry.load_active_config(db).config_set().fingerprint
    _set_capability(db, "selector", router_id, max_tokens=40)                  # v2
    _set_capability(db, "selector", _model_id(db, "openai", "gpt-4o-mini"))  # v3
    result = ai_registry.rollback_to_version(db, 1, expected_version=3, actor=SUPER)
    db.commit()
    assert result == {"changed": True, "version": 4, "source_version": 1}
    rows = db.query(AIConfigVersion).order_by(AIConfigVersion.id).all()
    assert [r.change_type for r in rows] == ["BOOTSTRAP", "UPDATE", "UPDATE", "ROLLBACK"]
    assert rows[3].source_version_id == 1 and rows[3].previous_version_id == 3
    assert json.loads(rows[3].snapshot)["capabilities"] == json.loads(rows[0].snapshot)["capabilities"]
    assert ai_registry.load_active_config(db).config_set().fingerprint == v1_fp
    event = db.query(AIConfigAudit).filter_by(event_type="AI_CONFIG_ROLLED_BACK").one()
    assert (event.old_version_id, event.new_version_id, event.actor) == (3, 4, SUPER)


def test_rollback_to_now_invalid_model_is_rejected_and_active_config_kept(db):
    _bootstrap(db)
    old_id = _model_id(db, "openrouter", "openai/gpt-4o-mini")
    new = _register(db)
    _set_capability(db, "intent_analyzer", new.id, max_tokens=300)
    _set_capability(db, "selector", new.id)
    ai_registry.update_model(db, old_id, {"enabled": False}, actor=SUPER)
    db.commit()
    with pytest.raises(AIConfigError) as exc:
        ai_registry.rollback_to_version(db, 1, expected_version=3, actor=SUPER)
    assert exc.value.code == "rollback_invalid"
    db.rollback()
    active = ai_registry.load_active_config(db)
    assert active.version_id == 3
    assert active.assignments[LLMCapability.SELECTOR].model.id == new.id


def test_stale_expected_version_conflicts_instead_of_overwriting(db):
    _bootstrap(db)
    router_id = _model_id(db, "openrouter", "openai/gpt-4o-mini")
    ai_registry.update_capability_config(
        db, "selector", {"model_registry_id": router_id, **_params(max_tokens=40)},
        expected_version=1, actor="a@iu.tr")
    db.commit()
    with pytest.raises(AIConfigError) as exc:
        ai_registry.update_capability_config(
            db, "selector", {"model_registry_id": router_id, **_params(max_tokens=64)},
            expected_version=1, actor="b@iu.tr")
    assert exc.value.status == 409 and exc.value.code == "stale_version"
    db.rollback()
    assert ai_registry.load_active_config(db).config_set().selector.max_tokens == 40


def test_active_model_cannot_be_disabled_until_moved(db):
    _bootstrap(db)
    old_id = _model_id(db, "openrouter", "openai/gpt-4o-mini")
    for patch in ({"enabled": False}, {"qualification_status": "BLOCKED"},
                  {"allowed_capabilities": ["intent_analyzer"]},
                  {"supports_structured_output": False}):
        with pytest.raises(AIConfigError) as exc:
            ai_registry.update_model(db, old_id, patch, actor=SUPER)
        assert exc.value.code == "model_in_use"
        db.rollback()
    replacement = _model_id(db, "openai", "gpt-4o-mini")
    _set_capability(db, "intent_analyzer", replacement, max_tokens=300)
    _set_capability(db, "selector", replacement)
    disabled = ai_registry.update_model(db, old_id, {"enabled": False}, actor=SUPER)
    db.commit()
    assert disabled.enabled is False
    assert db.get(AIModelRegistry, old_id) is not None  # soft-disable, history intact
    assert db.query(AIConfigAudit).filter_by(event_type="MODEL_DISABLED").count() == 1


def test_audit_has_actor_old_new_versions_and_is_append_only(db):
    db.add(SystemConfig(key="OPENROUTER_API_KEY", value="sk-or-v1-super-secret-value-123"))
    db.commit()
    _bootstrap(db)
    _set_capability(db, "selector", _model_id(db, "openai", "gpt-4o-mini"))
    event = db.query(AIConfigAudit).filter_by(event_type="AI_CONFIG_CHANGED").one()
    old, new = json.loads(event.old_value), json.loads(event.new_value)
    assert (event.actor, event.capability, event.old_version_id, event.new_version_id) == (
        SUPER, "selector", 1, 2)
    assert old["provider"] == "openrouter" and new["provider"] == "openai"
    assert event.created_at is not None
    everything = " ".join(
        [r.snapshot for r in db.query(AIConfigVersion)]
        + [f"{r.old_value} {r.new_value}" for r in db.query(AIConfigAudit)]
    )
    assert "sk-or-v1" not in everything and "api_key" not in everything.lower()
    for statement in ("UPDATE ai_config_audit SET actor='x'", "DELETE FROM ai_config_version"):
        with pytest.raises(DBAPIError):
            execute_admin_sql(db, text(statement))
        db.rollback()


# ── circuit breaker integration (§35/§60) ───────────────────────────────────

def test_config_switch_is_isolated_and_rollback_resets_reactivated_breaker(db):
    _bootstrap(db)
    cache = AIConfigCache(ttl_seconds=5, max_stale_seconds=60, clock=FakeClock())
    v1 = cache.get(db).configs.selector
    old_key = breaker_key("selector", v1.provider, v1.model, v1.fingerprint)
    for _ in range(3):
        LLM_CIRCUIT_BREAKER.record(LLM_CIRCUIT_BREAKER.acquire(old_key), CallOutcomeKind.FAILURE)
    assert LLM_CIRCUIT_BREAKER.snapshot(old_key)[0] is CircuitState.OPEN

    _set_capability(db, "selector", _model_id(db, "openai", "gpt-4o-mini"))
    cache.invalidate()
    v2 = cache.get(db).configs.selector
    new_key = breaker_key("selector", v2.provider, v2.model, v2.fingerprint)
    assert new_key != old_key and LLM_CIRCUIT_BREAKER.acquire(new_key).allowed
    assert old_key not in LLM_CIRCUIT_BREAKER._states  # bounded cleanup

    for _ in range(3):  # pretend the old key got OPEN again somewhere
        LLM_CIRCUIT_BREAKER.record(LLM_CIRCUIT_BREAKER.acquire(old_key), CallOutcomeKind.FAILURE)
    ai_registry.rollback_to_version(db, 1, expected_version=2, actor=SUPER)
    db.commit()
    cache.invalidate()
    assert cache.get(db).configs.selector.fingerprint == v1.fingerprint
    assert LLM_CIRCUIT_BREAKER.snapshot(old_key) == (CircuitState.CLOSED, 0)


def test_missing_provider_key_is_typed_config_failure_and_visible(
    db, make_user, login, monkeypatch
):
    """Real client path (no _provider_client patch): assigning a provider with
    no API key is surfaced at save time and degrades as provider_key_missing."""
    from services import answer_pipeline
    from services.decision_trace import DecisionTrace
    import core.deps as deps

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(deps, "_static_clients", {})
    db.add_all([SystemConfig(key="LLM_ENABLED", value="true"),
                SystemConfig(key="OPENROUTER_API_KEY", value="sk-or-v1-test-key-not-real-0000")])
    db.commit()
    _bootstrap(db)
    sup = _super(make_user, login)
    openai_id = _model_id(db, "openai", "gpt-4o-mini")
    saved = sup.put("/api/ai-config/config/selector", json={
        "expected_version": 1, "model_registry_id": openai_id, "temperature": 0, "max_tokens": 32})
    assert saved.status_code == 200 and saved.json()["warnings"] == ["provider_key_missing"]
    view = sup.get("/api/ai-config/config").json()["capabilities"]
    assert view["selector"]["provider_key_configured"] is False
    assert view["intent_analyzer"]["provider_key_configured"] is True
    assert "sk-or-v1" not in json.dumps(view)

    AI_CONFIG_CACHE.reset()
    assert deps.get_llm_provider(db) is None
    assert deps.llm_config_problem(db) == "provider_key_missing"
    monkeypatch.setattr(answer_pipeline, "meili_search_safe", lambda _q, limit: [])
    monkeypatch.setattr(answer_pipeline.QDRANT_PROVIDER, "search", lambda _q, limit=3: [])
    trace = DecisionTrace(endpoint="test")
    assert answer_pipeline.answer_question("soru", db, trace=trace) == (None, "none")
    execution = trace.to_dict()["execution"]
    assert (execution["execution_mode"], execution["degraded_reason"]) == (
        "CONFIG_DEGRADED", "provider_key_missing")


# ── admin LLM OFF / ON (§36/§61) ────────────────────────────────────────────

def test_config_can_change_while_llm_off_and_is_used_when_on(db, monkeypatch):
    from services import answer_pipeline
    from services.decision_trace import DecisionTrace
    import core.deps as deps

    _bootstrap(db)
    db.add(SystemConfig(key="LLM_ENABLED", value="false"))
    db.commit()
    client = ScriptedClient()
    _wire_clients(monkeypatch, {"openrouter": client, "openai": client})
    _set_capability(db, "selector", _model_id(db, "openai", "gpt-4o-mini"))
    monkeypatch.setattr(answer_pipeline, "meili_search_safe", lambda _q, limit: [])
    monkeypatch.setattr(answer_pipeline.QDRANT_PROVIDER, "search", lambda _q, limit=3: [])
    trace = DecisionTrace(endpoint="test")
    answer_pipeline.answer_question("soru", db, trace=trace)
    assert trace.to_dict()["execution"]["execution_mode"] == "ADMIN_DEGRADED"
    assert client.calls == []
    db.get(SystemConfig, "LLM_ENABLED").value = "true"
    db.commit()
    provider = deps.get_llm_provider(db)
    assert provider.runtime.version_id == 2
    assert provider.configs.selector.provider == "openai"


# ── admin API + authorization (§24–§26, §56/§57) ────────────────────────────

def test_authorization_read_is_admin_write_is_super_admin(db, client, make_user, login):
    _bootstrap(db)
    assert client.get("/api/ai-config/config").status_code == 401
    make_user("editor@iu.tr", role="editor")
    make_user("admin@iu.tr", role="admin")
    editor, admin = login("editor@iu.tr"), login("admin@iu.tr")
    assert editor.get("/api/ai-config/config").status_code == 403
    assert admin.get("/api/ai-config/config").status_code == 200
    assert admin.get("/api/ai-config/models").status_code == 200
    assert admin.get("/api/ai-config/config/history").status_code == 200
    router_id = _model_id(db, "openrouter", "openai/gpt-4o-mini")
    writes = [
        ("post", "/api/ai-config/models", {"display_name": "x", "provider": "openai",
                                             "model_identifier": "x", "allowed_capabilities": ["selector"],
                                             "supports_structured_output": True}),
        ("patch", f"/api/ai-config/models/{router_id}", {"display_name": "y"}),
        ("put", "/api/ai-config/config/selector", {"expected_version": 1, "model_registry_id": router_id,
                                                    "temperature": 0, "max_tokens": 40}),
        ("post", "/api/ai-config/config/rollback/1", {"expected_version": 1}),
    ]
    for method, url, body in writes:
        assert getattr(admin, method)(url, json=body).status_code == 403, url
        assert getattr(editor, method)(url, json=body).status_code == 403, url
    assert ai_registry.current_version_id(db) == 1


def test_super_admin_api_flow_with_conflict_and_typed_errors(db, make_user, login):
    _bootstrap(db)
    sup = _super(make_user, login)
    view = sup.get("/api/ai-config/config").json()
    assert view["version"] == 1 and view["status"] == "OK"
    router_id = view["capabilities"]["selector"]["model"]["id"]
    assert router_id in view["capabilities"]["selector"]["eligible_model_ids"]

    created = sup.post("/api/ai-config/models", json={
        "display_name": "New", "provider": "openrouter", "model_identifier": "vendor/new",
        "allowed_capabilities": ["selector"], "supports_structured_output": True})
    assert created.status_code == 201 and created.json()["qualification_status"] == "UNTESTED"
    assert created.json()["id"] not in sup.get("/api/ai-config/config").json()[
        "capabilities"]["selector"]["eligible_model_ids"]
    unqualified = sup.put("/api/ai-config/config/selector", json={
        "expected_version": 1, "model_registry_id": created.json()["id"],
        "temperature": 0, "max_tokens": 32})
    assert unqualified.status_code == 400 and unqualified.json()["code"] == "model_not_qualified"
    arbitrary = sup.put("/api/ai-config/config/selector", json={
        "expected_version": 1, "model": "openrouter/whatever-i-want",
        "temperature": 0, "max_tokens": 32})
    assert arbitrary.status_code == 422
    bad_provider = sup.post("/api/ai-config/models", json={
        "display_name": "x", "provider": "someone", "model_identifier": "x",
        "allowed_capabilities": ["selector"], "supports_structured_output": True})
    assert bad_provider.status_code == 422

    ok = sup.put("/api/ai-config/config/selector", json={
        "expected_version": 1, "model_registry_id": router_id, "temperature": 0, "max_tokens": 40})
    assert ok.status_code == 200 and ok.json()["version"] == 2
    stale = sup.put("/api/ai-config/config/selector", json={
        "expected_version": 1, "model_registry_id": router_id, "temperature": 0, "max_tokens": 64})
    assert stale.status_code == 409 and stale.json()["code"] == "stale_version"
    history = sup.get("/api/ai-config/config/history").json()["versions"]
    assert [v["version"] for v in history] == [2, 1]
    rolled = sup.post("/api/ai-config/config/rollback/1", json={"expected_version": 2})
    assert rolled.status_code == 200 and rolled.json()["version"] == 3
    events = [e["event_type"] for e in sup.get("/api/ai-config/audit").json()["events"]]
    assert events[:3] == ["AI_CONFIG_ROLLED_BACK", "AI_CONFIG_CHANGED", "MODEL_REGISTERED"]
    assert sup.get("/api/ai-config/audit").json()["events"][0]["actor"] == SUPER


# ── migration (§42) ──────────────────────────────────────────────────────────

def test_model_registry_migration_is_reversible(db):
    from sqlalchemy import inspect

    from core.database import admin_engine
    from scripts.migrate_model_registry import apply_model_registry_migration

    db.close()
    tables = {"ai_model_registry", "ai_config_version", "ai_capability_config", "ai_config_audit"}
    apply_model_registry_migration("downgrade")
    assert not tables & set(inspect(admin_engine).get_table_names())
    apply_model_registry_migration("upgrade")
    apply_model_registry_migration("upgrade")  # idempotent
    assert tables <= set(inspect(admin_engine).get_table_names())
    with admin_engine.begin() as conn:
        triggers = {row[0] for row in conn.execute(text(
            "SELECT tgname FROM pg_trigger WHERE tgname LIKE 'ai_%append_only'"))}
    assert triggers == {"ai_config_version_append_only", "ai_config_audit_append_only"}
