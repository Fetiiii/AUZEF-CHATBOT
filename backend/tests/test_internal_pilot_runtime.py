"""Runtime preflight tests: the three internal-pilot blockers, closed.

These are distinct from ``test_internal_pilot_freeze.py``, which asserts the
frozen *decision* is recorded correctly. Here we assert the *runtime* behaves
that way when configured as the pilot configures it.

The freeze manifest and its fingerprint are treated as immutable history:
a test below pins the fingerprint so closing a gap can never quietly rewrite
what was frozen.

No live LLM call is made anywhere in this module.
"""
from __future__ import annotations

import json

import pytest

from services.decision_trace import (
    DecisionTrace,
    SourceAvailability,
    calendar_availability_from_snapshot,
    utc_now_iso,
)
from services.internal_pilot_freeze import (
    SELECTOR_PROMPT_FINGERPRINT,
    build_freeze_manifest,
    repo_root,
)
from services.internal_pilot_runtime import (
    PILOT_PROMPT_VERSION,
    load_pilot_env,
    run_runtime_preflight,
    runtime_modules_importing_benchmarks,
    selector_request_reasoning_fields,
)
from services.llm_config import (
    LLMCapability,
    ReasoningEffort,
    resolve_capability_config,
)
from services.selector_prompt_catalog import (
    DEFAULT_PROMPT_VERSION,
    PRODUCTION_V2,
    PROMPT_VERSION_ENV,
    UnknownSelectorPromptVersion,
    VARIANT_A_V1,
    load_selector_prompt,
    resolve_selector_prompt,
)

PRODUCTION_PROMPT_FP = (
    "2d59cfb65f0aaa08ffcc481d81fed5a89f29803251cdc8a5438da673cb867f3c"
)
FREEZE_FINGERPRINT = (
    "c7081ff54d959d183e7964498720dce7866461e72c3fa407c7346ea3cf32c5c6"
)


def _check(report, name):
    for check in report.checks:
        if check.name == name:
            return check
    raise AssertionError(f"missing check {name!r}")


# ── The freeze itself is immutable history ──────────────────────────────────


def test_freeze_fingerprint_is_unchanged_by_closing_blockers():
    """Closing a runtime gap must never rewrite what was frozen.

    The freeze records the state at freeze time, including the observability
    gaps. Gap closure is reported on the separate runtime-preflight axis.
    """
    manifest = build_freeze_manifest(
        git_commit="22a63e87af81960a5f376cb26165149d6d69b70c",
        created_at="2026-09-20T08:21:12Z",
    )
    assert manifest["freeze_fingerprint"] == FREEZE_FINGERPRINT


def test_committed_freeze_artifact_still_records_the_original_gaps():
    committed = json.loads(
        (repo_root() / "deploy" / "internal-pilot" / "answer-pipeline-freeze.json")
        .read_text(encoding="utf-8")
    )
    assert committed["freeze_fingerprint"] == FREEZE_FINGERPRINT
    assert committed["observability_contract"]["gap_status"] == "MUST_CLOSE_BEFORE_PILOT"


# ── BLOCKER-1: Variant A runtime prompt ─────────────────────────────────────


def test_default_selector_prompt_is_still_the_historical_production_prompt():
    """Public/default behaviour must not change by upgrading."""
    assert DEFAULT_PROMPT_VERSION == PRODUCTION_V2
    spec = resolve_selector_prompt({})
    assert spec.version == PRODUCTION_V2
    assert spec.fingerprint == PRODUCTION_PROMPT_FP


def test_production_prompt_constant_is_untouched():
    from services.selector import SELECTOR_SYSTEM_PROMPT
    from services.selector_prompt_catalog import normalize_prompt

    assert normalize_prompt(SELECTOR_SYSTEM_PROMPT) == SELECTOR_SYSTEM_PROMPT


def test_internal_pilot_resolves_variant_a():
    spec = resolve_selector_prompt({PROMPT_VERSION_ENV: VARIANT_A_V1})
    assert spec.version == VARIANT_A_V1
    assert spec.fingerprint == SELECTOR_PROMPT_FINGERPRINT


def test_variant_a_fingerprint_is_exact():
    assert load_selector_prompt(VARIANT_A_V1).fingerprint == (
        "1aed568885db02f474534224695a45eb6f95835bc31f94e877af9659efe94a1e"
    )


def test_runtime_and_benchmark_variant_a_copies_are_identical():
    """Two copies exist by necessity; this test is what keeps them honest.

    The benchmark copy is a historical artifact recorded in the committed
    benchmark prompt manifest, which must not be rewritten. The runtime copy
    exists so services/ never imports benchmarks/.
    """
    runtime = (
        repo_root() / "backend" / "services" / "prompts" / "selector" / "variant_a_v1.md"
    ).read_text(encoding="utf-8")
    benchmark = (
        repo_root() / "backend" / "benchmarks" / "selector_v2" / "prompts"
        / "variant_a_v1.md"
    ).read_text(encoding="utf-8")
    assert runtime == benchmark

    from benchmarks.selector_v2.prompt_contract import load_prompt

    assert load_prompt(VARIANT_A_V1).fingerprint == (
        load_selector_prompt(VARIANT_A_V1).fingerprint
    )


def test_unknown_prompt_version_is_rejected():
    with pytest.raises(UnknownSelectorPromptVersion):
        resolve_selector_prompt({PROMPT_VERSION_ENV: "variant_z_v9"})


def test_build_selector_prompt_follows_the_configured_version(monkeypatch):
    from services.candidate_eligibility import CandidateKind, SelectorCandidate
    from services.selector import build_selector_prompt

    candidate = SelectorCandidate(
        candidate_ref="qna:1", kind=CandidateKind.QNA,
        canonical_text="q", answer_text="a", qna_id=1,
    )
    default_system, default_user = build_selector_prompt("intent", [candidate])
    monkeypatch.setenv(PROMPT_VERSION_ENV, VARIANT_A_V1)
    pilot_system, pilot_user = build_selector_prompt("intent", [candidate])

    assert default_system != pilot_system
    # Only the system prompt changes; the candidate serializer is untouched.
    assert default_user == pilot_user


def test_runtime_does_not_import_the_benchmarks_package():
    assert runtime_modules_importing_benchmarks() == []


# ── Reasoning omission (payload level) ──────────────────────────────────────


def test_pilot_selector_request_carries_no_reasoning_key():
    config = resolve_capability_config(
        LLMCapability.SELECTOR, environ=load_pilot_env()
    )
    assert config.reasoning_effort is None
    assert selector_request_reasoning_fields(config) == {}


def test_explicit_reasoning_none_would_emit_a_reasoning_key():
    """Guards the distinction the freeze depends on."""
    env = {**load_pilot_env(), "LLM_SELECTOR_REASONING_EFFORT": "none"}
    config = resolve_capability_config(LLMCapability.SELECTOR, environ=env)
    assert config.reasoning_effort is ReasoningEffort.NONE
    fields = selector_request_reasoning_fields(config)
    assert fields == {"extra_body": {"reasoning": {"effort": "none"}}}


def test_pilot_env_does_not_set_reasoning_effort():
    assert "LLM_SELECTOR_REASONING_EFFORT" not in load_pilot_env()


def test_pilot_selector_identity_matches_the_freeze():
    config = resolve_capability_config(
        LLMCapability.SELECTOR, environ=load_pilot_env()
    )
    assert config.provider == "openrouter"
    assert config.model == "openai/gpt-4o-mini"
    assert float(config.temperature) == 0.0
    assert int(config.max_tokens) == 32
    assert config.structured_output_enabled is False
    assert config.fingerprint == (
        "af9eb2d0767d37cd632799cbae39e7938585b243ceb4c7a1527b8028cd489a6e"
    )


# ── BLOCKER-2: initial LLM state ────────────────────────────────────────────


def test_public_default_seed_is_still_false():
    from scripts.init_system import llm_enabled_default

    assert llm_enabled_default({}) == "false"


def test_internal_pilot_seed_is_true():
    from scripts.init_system import llm_enabled_default

    assert llm_enabled_default(load_pilot_env()) == "true"
    assert llm_enabled_default({"LLM_ENABLED_DEFAULT": "true"}) == "true"


@pytest.mark.parametrize("raw", ["1", "TRUE", "yes", "On"])
def test_truthy_values_accepted(raw):
    from scripts.init_system import llm_enabled_default

    assert llm_enabled_default({"LLM_ENABLED_DEFAULT": raw}) == "true"


@pytest.mark.parametrize("raw", ["0", "FALSE", "no", "Off"])
def test_falsy_values_accepted(raw):
    from scripts.init_system import llm_enabled_default

    assert llm_enabled_default({"LLM_ENABLED_DEFAULT": raw}) == "false"


@pytest.mark.parametrize("raw", ["maybe", "2", "enabled", "-"])
def test_invalid_boolean_is_rejected(raw):
    from scripts.init_system import llm_enabled_default

    with pytest.raises(RuntimeError):
        llm_enabled_default({"LLM_ENABLED_DEFAULT": raw})


def test_seed_only_applies_when_the_setting_is_missing():
    """Seed-if-missing semantics are preserved verbatim.

    An admin's persisted choice — including an emergency off — must survive
    re-initialization. Asserted on the source of the seeding loop so no DB is
    required and the guarantee cannot be refactored away unnoticed.
    """
    source = (
        repo_root() / "backend" / "scripts" / "init_system.py"
    ).read_text(encoding="utf-8")
    assert "row = db.get(SystemConfig, key)" in source
    assert "if row is None:" in source


class _FakeRow:
    def __init__(self, value):
        self.value = value


class _FakeDB:
    """Minimal stand-in exercising only the seeding branch."""

    def __init__(self, existing=None):
        self.rows = dict(existing or {})
        self.added = []

    def get(self, _model, key):
        value = self.rows.get(key)
        return _FakeRow(value) if value is not None else None

    def add(self, obj):
        self.added.append(obj)


def _seed_into(db, environ):
    """Replicate the seeding loop against a fake DB (no database needed)."""
    from scripts.init_system import default_system_config

    for key, default_value in default_system_config(environ).items():
        row = db.get(None, key)
        if row is None:
            db.added.append((key, default_value))
            db.rows[key] = default_value
    return db.rows


def test_fresh_pilot_environment_seeds_true():
    rows = _seed_into(_FakeDB(), {"LLM_ENABLED_DEFAULT": "true"})
    assert rows["LLM_ENABLED"] == "true"


def test_existing_false_is_not_overwritten_by_pilot_default():
    db = _FakeDB({"LLM_ENABLED": "false"})
    rows = _seed_into(db, {"LLM_ENABLED_DEFAULT": "true"})
    assert rows["LLM_ENABLED"] == "false"
    assert db.added == [], "admin emergency-off must survive initialization"


def test_existing_true_is_preserved():
    db = _FakeDB({"LLM_ENABLED": "true"})
    rows = _seed_into(db, {"LLM_ENABLED_DEFAULT": "false"})
    assert rows["LLM_ENABLED"] == "true"
    assert db.added == []


# ── BLOCKER-3: observability ────────────────────────────────────────────────


def test_trace_carries_opaque_correlation_ids():
    trace = DecisionTrace(endpoint="widget_chat", conversation_id=42)
    request = trace.to_dict()["request"]
    assert request["conversation_id"] == 42
    # request_id is an opaque UUID, never derived from user content.
    assert len(request["request_id"]) == 36


def test_trace_timestamp_is_timezone_aware_utc_iso8601():
    from datetime import datetime, timezone

    stamp = DecisionTrace(endpoint="widget_chat").to_dict()["request"]["timestamp"]
    assert stamp.endswith("Z")
    parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timezone.utc.utcoffset(None)


def test_utc_now_iso_is_never_naive():
    from datetime import datetime

    parsed = datetime.fromisoformat(utc_now_iso().replace("Z", "+00:00"))
    assert parsed.tzinfo is not None


def test_retrieval_ms_is_recorded_and_non_negative():
    from services.candidate_eligibility import build_candidate_set

    build = build_candidate_set(
        calendar_entries=(), qna_hits=(), routing_policy=None,
        active_qna_lookup=None, max_candidates=8, retrieval_ms=12.5,
    )
    assert build.trace_snapshot["retrieval_ms"] == 12.5
    assert build.trace_snapshot["retrieval_ms"] >= 0


def test_retrieval_ms_scope_is_the_qna_leg_only():
    """Pins the documented definition so it cannot drift silently.

    ``_build_candidate_pool_result`` times Qdrant + Meili only. Calendar
    entries are resolved upstream by ``search_calendar`` and arrive already
    built, and selector latency is recorded separately per selector entry.
    """
    import inspect

    from services import answer_pipeline

    source = inspect.getsource(answer_pipeline._build_candidate_pool_result)
    started = source.index("started = time.perf_counter()")
    measured = source.index("retrieval_ms = round(")
    window = source[started:measured]
    assert "QDRANT_PROVIDER.search" in window
    assert "meili_search_safe" in window
    # The timed window must not contain calendar retrieval or the selector.
    assert "retrieve_calendar_candidates" not in window
    assert "ask_with_result" not in window


@pytest.mark.parametrize(
    "state", ["available", "unavailable", "skipped"]
)
def test_source_availability_accepts_each_state(state):
    trace = DecisionTrace(endpoint="widget_chat")
    trace.record_source_availability("meili", state)
    assert trace.to_dict()["source_availability"]["meili"] == state


def test_source_availability_states_are_exactly_three():
    assert {item.value for item in SourceAvailability} == {
        "available", "unavailable", "skipped"
    }


def test_zero_results_stays_available():
    """The core distinction: an empty answer is not an outage."""
    from services.candidate_eligibility import build_candidate_set

    build = build_candidate_set(
        calendar_entries=(), qna_hits=(), routing_policy=None,
        active_qna_lookup=None, max_candidates=8,
        qdrant_candidate_count=0, meili_candidate_count=0,
        qdrant_available=True, meili_available=True,
    )
    trace = DecisionTrace(endpoint="widget_chat")
    trace.record_retrieval({**build.trace_snapshot, "purpose": "selector"})
    availability = trace.to_dict()["source_availability"]
    assert availability["meili"] == "available"
    assert availability["qdrant"] == "available"
    assert build.trace_snapshot["meili_candidate_count"] == 0


def test_failure_marks_source_unavailable():
    from services.candidate_eligibility import build_candidate_set

    build = build_candidate_set(
        calendar_entries=(), qna_hits=(), routing_policy=None,
        active_qna_lookup=None, max_candidates=8,
        qdrant_available=False, meili_available=True,
    )
    trace = DecisionTrace(endpoint="widget_chat")
    trace.record_retrieval({**build.trace_snapshot, "purpose": "selector"})
    availability = trace.to_dict()["source_availability"]
    assert availability["qdrant"] == "unavailable"
    assert availability["meili"] == "available"


def test_unavailable_is_sticky_within_a_request():
    trace = DecisionTrace(endpoint="widget_chat")
    trace.record_source_availability("qdrant", SourceAvailability.UNAVAILABLE)
    trace.record_source_availability("qdrant", SourceAvailability.AVAILABLE)
    assert trace.to_dict()["source_availability"]["qdrant"] == "unavailable"


def test_skipped_is_distinguishable_from_unavailable():
    from services.calendar_retrieval import (
        failed_calendar_result,
        skipped_calendar_result,
    )

    skipped = calendar_availability_from_snapshot(
        skipped_calendar_result(relevant=False).trace_snapshot
    )
    failed = calendar_availability_from_snapshot(
        failed_calendar_result().trace_snapshot
    )
    assert skipped is SourceAvailability.SKIPPED
    assert failed is SourceAvailability.UNAVAILABLE
    assert skipped != failed


def test_calendar_route_sets_availability_on_the_trace():
    from services.calendar_retrieval import skipped_calendar_result

    trace = DecisionTrace(endpoint="widget_chat")
    trace.record_calendar_route(
        skipped_calendar_result(relevant=False).trace_snapshot, purpose="intent_1"
    )
    assert trace.to_dict()["source_availability"]["calendar"] == "skipped"


def test_unknown_source_or_state_is_rejected():
    trace = DecisionTrace(endpoint="widget_chat")
    with pytest.raises(ValueError):
        trace.record_source_availability("elasticsearch", "available")
    with pytest.raises(ValueError):
        trace.record_source_availability("meili", "degraded")


def test_meili_availability_distinguishes_empty_from_broken(monkeypatch):
    from core import deps

    monkeypatch.setattr(deps, "MEILI_STATUS", {"healthy": True, "last_check": 0})
    monkeypatch.setattr(
        deps.MEILI_PROVIDER, "search", lambda *a, **k: [], raising=False
    )
    hits, available = deps.meili_search_with_availability("q", 5)
    assert hits == [] and available is True

    def _boom(*_a, **_k):
        raise RuntimeError("meili down")

    monkeypatch.setattr(deps.MEILI_PROVIDER, "search", _boom, raising=False)
    monkeypatch.setattr(deps, "MEILI_STATUS", {"healthy": True, "last_check": 0})
    hits, available = deps.meili_search_with_availability("q", 5)
    assert hits == [] and available is False


def test_meili_search_safe_behaviour_is_unchanged(monkeypatch):
    from core import deps

    monkeypatch.setattr(deps, "MEILI_STATUS", {"healthy": True, "last_check": 0})
    monkeypatch.setattr(
        deps.MEILI_PROVIDER, "search", lambda *a, **k: [{"qna_id": 1}], raising=False
    )
    assert deps.meili_search_safe("q", 5) == [{"qna_id": 1}]


# ── Privacy regression ──────────────────────────────────────────────────────

_FORBIDDEN = (
    "Yatay geçiş nasıl yapılır",  # raw user message
    "12345678901",                # identity number
    "+905551112233",              # phone
    "123456",                     # verification code
    "sk-or-v1-SECRETKEYVALUE",    # OpenRouter API key
    "Bearer eyJhbGciOi",          # authorization header
)


def test_no_raw_user_content_or_secrets_in_the_trace(monkeypatch):
    for name in ("OPENROUTER_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.setenv(name, "sk-or-v1-SECRETKEYVALUE")

    trace = DecisionTrace(endpoint="widget_chat", conversation_id=7)
    trace.set_context(
        enabled=True,
        messages=({"role": "user", "content": "Yatay geçiş nasıl yapılır"},),
    )
    trace.record_source_availability("meili", SourceAvailability.AVAILABLE)
    payload = json.dumps(trace.to_dict(), ensure_ascii=False)
    for secret in _FORBIDDEN:
        assert secret not in payload, f"leaked {secret!r} into the decision trace"


def test_context_retains_only_aggregate_counts():
    trace = DecisionTrace(endpoint="widget_chat")
    trace.set_context(
        enabled=True,
        messages=({"role": "user", "content": "gizli mesaj"},),
    )
    assert "gizli mesaj" not in json.dumps(trace.to_dict(), ensure_ascii=False)


# ── Whole-report status ─────────────────────────────────────────────────────


def test_runtime_preflight_passes_with_the_pilot_configuration():
    report = run_runtime_preflight()
    assert report.status == "PASS", report.to_dict()["failed_checks"]


def test_runtime_preflight_fails_on_the_default_configuration():
    """Proves the preflight is discriminating, not unconditionally green."""
    report = run_runtime_preflight(environ={"LLM_PROVIDER": "openrouter"})
    assert report.status == "FAIL"
    assert not _check(report, "selector_prompt_version_is_variant_a").passed
    assert not _check(report, "pilot_fresh_llm_enabled_is_true").passed


def test_runtime_preflight_detects_explicit_reasoning():
    env = {**load_pilot_env(), "LLM_SELECTOR_REASONING_EFFORT": "none"}
    report = run_runtime_preflight(environ=env)
    assert not _check(report, "selector_reasoning_is_unset").passed
    assert not _check(report, "selector_request_omits_reasoning_key").passed


def test_runtime_preflight_detects_model_drift():
    env = {**load_pilot_env(), "LLM_SELECTOR_MODEL": "openai/gpt-4o"}
    report = run_runtime_preflight(environ=env)
    assert not _check(report, "selector_model_matches").passed


def test_pilot_env_artifact_declares_the_required_settings():
    env = load_pilot_env()
    assert env["SELECTOR_PROMPT_VERSION"] == PILOT_PROMPT_VERSION
    assert env["LLM_ENABLED_DEFAULT"] == "true"
    assert env["LLM_PROVIDER"] == "openrouter"


def test_pilot_env_artifact_contains_no_secret_values():
    raw = (
        repo_root() / "deploy" / "internal-pilot" / "pilot.env.example"
    ).read_text(encoding="utf-8")
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        # Credential names only — MAX_TOKENS is a model parameter, not a secret.
        if any(
            t in key.upper()
            for t in ("API_KEY", "PASSWORD", "SECRET", "ACCESS_TOKEN", "CREDENTIAL")
        ):
            assert value.strip() == "", f"{key} must not carry a value"


# ── Compose wiring (static, no container is started) ────────────────────────


def test_compose_passes_pilot_settings_to_the_backend_service():
    compose = (repo_root() / "docker-compose.yml").read_text(encoding="utf-8")
    assert "SELECTOR_PROMPT_VERSION: ${SELECTOR_PROMPT_VERSION:-production_v2}" in compose
    assert "LLM_ENABLED_DEFAULT: ${LLM_ENABLED_DEFAULT:-false}" in compose


def test_compose_defaults_preserve_public_behaviour():
    """Unset variables must reproduce the historical public defaults."""
    compose = (repo_root() / "docker-compose.yml").read_text(encoding="utf-8")
    assert ":-production_v2}" in compose
    assert "LLM_ENABLED_DEFAULT: ${LLM_ENABLED_DEFAULT:-false}" in compose


def test_production_env_example_documents_both_settings():
    example = (
        repo_root() / "deploy" / "production" / "config" / "backend.env.example"
    ).read_text(encoding="utf-8")
    assert "SELECTOR_PROMPT_VERSION=production_v2" in example
    assert "LLM_ENABLED_DEFAULT=false" in example
