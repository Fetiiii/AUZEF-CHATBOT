"""INTERNAL_PILOT freeze manifest, fingerprint and preflight tests.

These tests assert the freeze *declaration* is correct and that preflight
detects drift. They deliberately do NOT assert that the live runtime matches
the freeze: it does not yet (the runtime still serves the ``production``
selector prompt and seeds ``LLM_ENABLED=false``), and that gap is the
documented pilot blocker rather than something these tests should paper over.

No live LLM call is made anywhere in this module.
"""
from __future__ import annotations

import json
import re

import pytest

from benchmarks.selector_v2.prompt_contract import load_committed_manifest
from services.internal_pilot_freeze import (
    BACKLOG_PHASES,
    BACKLOG_STATUS,
    CANDIDATE_ORDER,
    KNOWN_ISSUE_IDS,
    MILESTONE,
    PHASE_7G,
    SELECTOR_CONFIG_FINGERPRINT,
    SELECTOR_MAX_TOKENS,
    SELECTOR_MODEL,
    SELECTOR_PROMPT_FINGERPRINT,
    SELECTOR_PROMPT_VERSION,
    SELECTOR_PROVIDER,
    SELECTOR_REASONING,
    SELECTOR_TEMPERATURE,
    build_freeze_manifest,
    freeze_fingerprint,
    repo_root,
    run_preflight,
    runtime_selector_prompt_fingerprint,
    seeded_llm_enabled_default,
)
from services.llm_config import (
    EffectiveLLMConfig,
    LLMCapability,
    ReasoningEffort,
)

PILOT_ENV = {"LLM_PROVIDER": "openrouter"}
MANIFEST_PATH = repo_root() / "deploy" / "internal-pilot" / "answer-pipeline-freeze.json"
SCENARIOS_PATH = repo_root() / "tests" / "e2e" / "internal_pilot" / "scenarios.json"


def _manifest(created_at="2026-09-20T00:00:00Z", commit="deadbeef"):
    return build_freeze_manifest(git_commit=commit, created_at=created_at)


# ── Fingerprint and determinism ─────────────────────────────────────────────


def test_freeze_manifest_fingerprint_is_deterministic():
    first = _manifest()
    second = _manifest()
    assert first == second
    assert first["freeze_fingerprint"] == second["freeze_fingerprint"]


def test_freeze_fingerprint_ignores_created_at():
    """Regenerating on another day must not change the fingerprint."""
    early = _manifest(created_at="2026-01-01T00:00:00Z")
    late = _manifest(created_at="2027-12-31T23:59:59Z")
    assert early["created_at"] != late["created_at"]
    assert early["freeze_fingerprint"] == late["freeze_fingerprint"]


def test_freeze_fingerprint_changes_when_a_frozen_value_changes():
    """The fingerprint must actually cover the frozen configuration."""
    manifest = _manifest()
    mutated = dict(manifest)
    mutated["selector_model"] = "openai/gpt-4o"
    assert freeze_fingerprint(mutated) != manifest["freeze_fingerprint"]


def test_freeze_fingerprint_survives_json_round_trip():
    manifest = _manifest()
    reloaded = json.loads(json.dumps(manifest, ensure_ascii=False))
    assert freeze_fingerprint(reloaded) == manifest["freeze_fingerprint"]


def test_committed_manifest_matches_its_own_fingerprint():
    committed = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert freeze_fingerprint(committed) == committed["freeze_fingerprint"]


def test_committed_manifest_matches_regenerated_manifest():
    committed = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    regenerated = _manifest(
        created_at=committed["created_at"], commit=committed["git_commit"]
    )
    assert committed == regenerated


# ── Prompt fingerprint correctness ──────────────────────────────────────────


def test_frozen_prompt_fingerprint_is_variant_a():
    """The freeze must track the real variant_a_v1 prompt, not a stale hash."""
    prompts = load_committed_manifest()["prompts"]
    assert SELECTOR_PROMPT_VERSION in prompts
    assert prompts[SELECTOR_PROMPT_VERSION]["prompt_fingerprint"] == (
        SELECTOR_PROMPT_FINGERPRINT
    )


def test_frozen_prompt_is_not_the_production_prompt():
    """Variant A is a genuinely different prompt from what runtime serves."""
    prompts = load_committed_manifest()["prompts"]
    assert prompts["production"]["prompt_fingerprint"] != SELECTOR_PROMPT_FINGERPRINT
    assert runtime_selector_prompt_fingerprint() == (
        prompts["production"]["prompt_fingerprint"]
    )


def test_frozen_selector_config_fingerprint_is_the_validated_one():
    """'reasoning = none' is UNSET, matching every validated Variant A run."""
    config = EffectiveLLMConfig(
        capability=LLMCapability.SELECTOR,
        provider=SELECTOR_PROVIDER,
        model=SELECTOR_MODEL,
        reasoning_effort=SELECTOR_REASONING,
        temperature=SELECTOR_TEMPERATURE,
        max_tokens=SELECTOR_MAX_TOKENS,
    )
    assert SELECTOR_REASONING is None
    assert config.fingerprint == SELECTOR_CONFIG_FINGERPRINT


def test_reasoning_none_enum_is_a_different_unvalidated_config():
    """Guards against silently freezing the untested explicit-'none' request."""
    explicit = EffectiveLLMConfig(
        capability=LLMCapability.SELECTOR,
        provider=SELECTOR_PROVIDER,
        model=SELECTOR_MODEL,
        reasoning_effort=ReasoningEffort.NONE,
        temperature=SELECTOR_TEMPERATURE,
        max_tokens=SELECTOR_MAX_TOKENS,
    )
    assert explicit.fingerprint != SELECTOR_CONFIG_FINGERPRINT


# ── Preflight mismatch detection ────────────────────────────────────────────


def _check(report, name):
    for check in report.checks:
        if check.name == name:
            return check
    raise AssertionError(f"missing check {name!r}")


def test_preflight_detects_prompt_fingerprint_mismatch():
    report = run_preflight(
        environ=PILOT_ENV,
        llm_enabled_default=True,
        selector_prompt_fingerprint="0" * 64,
    )
    assert not _check(report, "selector_prompt_fingerprint_matches").passed
    assert report.status == "FAIL"


def test_preflight_detects_provider_mismatch():
    report = run_preflight(
        environ={"LLM_PROVIDER": "openai"},
        llm_enabled_default=True,
        selector_prompt_fingerprint=SELECTOR_PROMPT_FINGERPRINT,
    )
    assert not _check(report, "selector_provider_matches").passed
    assert not _check(report, "provider_policy_openrouter_only").passed
    assert report.status == "FAIL"


def test_preflight_detects_model_mismatch():
    report = run_preflight(
        environ={**PILOT_ENV, "LLM_SELECTOR_MODEL": "openai/gpt-4o"},
        llm_enabled_default=True,
        selector_prompt_fingerprint=SELECTOR_PROMPT_FINGERPRINT,
    )
    assert not _check(report, "selector_model_matches").passed
    assert not _check(report, "selector_config_fingerprint_matches").passed


def test_preflight_detects_max_tokens_mismatch():
    report = run_preflight(
        environ={**PILOT_ENV, "LLM_SELECTOR_MAX_TOKENS": "64"},
        llm_enabled_default=True,
        selector_prompt_fingerprint=SELECTOR_PROMPT_FINGERPRINT,
    )
    assert not _check(report, "selector_max_tokens_matches").passed


def test_preflight_detects_temperature_mismatch():
    report = run_preflight(
        environ={**PILOT_ENV, "LLM_SELECTOR_TEMPERATURE": "0.7"},
        llm_enabled_default=True,
        selector_prompt_fingerprint=SELECTOR_PROMPT_FINGERPRINT,
    )
    assert not _check(report, "selector_temperature_matches").passed


def test_preflight_detects_reasoning_mismatch():
    """An explicit reasoning level is a different request and must be caught."""
    report = run_preflight(
        environ={**PILOT_ENV, "LLM_SELECTOR_REASONING_EFFORT": "none"},
        llm_enabled_default=True,
        selector_prompt_fingerprint=SELECTOR_PROMPT_FINGERPRINT,
    )
    assert not _check(report, "selector_reasoning_matches").passed


def test_preflight_rejects_llm_disabled_by_default_seed():
    report = run_preflight(
        environ=PILOT_ENV,
        llm_enabled_default=False,
        selector_prompt_fingerprint=SELECTOR_PROMPT_FINGERPRINT,
    )
    assert not _check(report, "llm_enabled_seeded_default").passed
    assert report.status == "FAIL"


def test_preflight_passes_when_everything_matches():
    """Proves the preflight is satisfiable, not unconditionally red."""
    report = run_preflight(
        environ=PILOT_ENV,
        llm_enabled_default=True,
        selector_prompt_fingerprint=SELECTOR_PROMPT_FINGERPRINT,
    )
    assert report.status == "PASS", report.to_dict()["failed_checks"]


def test_preflight_reports_unresolvable_config_without_hiding_other_checks():
    report = run_preflight(
        environ={},
        llm_enabled_default=True,
        selector_prompt_fingerprint=SELECTOR_PROMPT_FINGERPRINT,
    )
    assert not _check(report, "selector_config_resolvable").passed
    # The config-independent checks must still have been evaluated.
    assert _check(report, "candidate_order_expected").passed
    assert _check(report, "degraded_mode_available").passed


def test_preflight_confirms_candidate_order_and_degraded_mode():
    report = run_preflight(environ=PILOT_ENV, llm_enabled_default=True)
    assert _check(report, "candidate_order_expected").passed
    assert _check(report, "degraded_mode_available").passed
    assert CANDIDATE_ORDER == "production"


def test_preflight_does_not_mutate_configuration(monkeypatch):
    monkeypatch.setenv("LLM_SELECTOR_MODEL", "openai/gpt-4o")
    before = dict(__import__("os").environ)
    run_preflight()
    assert dict(__import__("os").environ) == before


def test_live_runtime_currently_fails_preflight():
    """The documented pilot blocker, asserted so it cannot regress silently.

    If this test starts failing because the runtime now matches the freeze,
    that is the *intended* resolution: land variant_a_v1 and flip the seed,
    then update this test to assert PASS.
    """
    report = run_preflight(environ=PILOT_ENV)
    failed = set(report.to_dict()["failed_checks"])
    assert "selector_prompt_fingerprint_matches" in failed
    assert "llm_enabled_seeded_default" in failed
    assert seeded_llm_enabled_default() is False


# ── Secrets ─────────────────────────────────────────────────────────────────

# Deliberately does not match `max_tokens` / `max_output_tokens`, which are
# model parameters rather than credentials.
_SECRET_KEY_PATTERN = re.compile(
    r"(api[_-]?key|secret|password|passwd|credential|authorization|bearer"
    r"|(?:access|auth|refresh|session|bearer|id)[_-]?token"
    r"|^token$|(?:private|public|signing|encryption)[_-]?key"
    r"|dsn$|connection[_-]?string)",
    re.IGNORECASE,
)


def test_secret_values_are_never_serialized(monkeypatch):
    """Assert on the actually emitted manifest, not a hand-built dict."""
    sentinel = "sk-or-v1-INTERNALPILOTSENTINELVALUE"
    for name in (
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "SECRET_KEY",
        "DATABASE_URL",
    ):
        monkeypatch.setenv(name, sentinel)
    emitted = json.dumps(_manifest(), ensure_ascii=False)
    assert sentinel not in emitted

    committed = MANIFEST_PATH.read_text(encoding="utf-8")
    assert sentinel not in committed

    def walk(node, path=""):
        if isinstance(node, dict):
            for key, value in node.items():
                assert not _SECRET_KEY_PATTERN.search(str(key)), (
                    f"secret-like key at {path}/{key}"
                )
                walk(value, f"{path}/{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")

    walk(json.loads(committed))


# ── Milestone, backlog and deferral status ──────────────────────────────────


def test_milestone_is_internal_pilot_not_public_production():
    manifest = _manifest()
    assert manifest["milestone"] == MILESTONE == "INTERNAL_PILOT"
    assert manifest["not_milestone"] == "PUBLIC_PRODUCTION"


def test_backlog_phases_present_and_marked():
    manifest = _manifest()
    for phase in ("7C", "7D", "7E", "7F"):
        assert phase in manifest["backlog_phases"]
        assert manifest["backlog_phases"][phase]["status"] == BACKLOG_STATUS
    assert BACKLOG_STATUS == "BACKLOG_POST_INTERNAL_PILOT"
    assert set(BACKLOG_PHASES) == {"7C", "7D", "7E", "7F"}


def test_phase_7g_is_deferred_until_pilot_data():
    manifest = _manifest()
    assert manifest["phase_7g"]["name"] == "PUBLIC_PRODUCTION_FINAL_FREEZE"
    assert manifest["phase_7g"]["status"] == "DEFERRED_UNTIL_INTERNAL_PILOT_DATA"
    assert manifest["phase_7g"]["runs_before_internal_pilot"] is False
    assert PHASE_7G["runs_before_internal_pilot"] is False


def test_known_issues_are_recorded_and_not_softened():
    manifest = _manifest()
    assert list(manifest["known_issue_ids"]) == list(KNOWN_ISSUE_IDS)
    assert len(KNOWN_ISSUE_IDS) == 3
    for issue_id in KNOWN_ISSUE_IDS:
        assert issue_id in manifest["known_issues"]


def test_historical_holdout_fail_is_not_converted_to_pass():
    provenance = _manifest()["validation_provenance"]
    assert provenance["variant_a_holdout"]["historical_gate_status"] == "FAIL"
    assert provenance["gold_provenance"]["independent_human_gold"] is False
    assert provenance["gold_provenance"]["adjudicator"] == "GPT-5.6 Sol"


def test_luna_stays_a_research_candidate_and_m1_stays_fail():
    luna = _manifest()["validation_provenance"]["luna"]
    assert luna["m1"] == "FAIL"
    assert luna["status"] == "research candidate"
    assert luna["auto_promoted_to_pilot_selector"] is False


def test_provider_policy_is_openrouter_only():
    policy = _manifest()["provider_policy"]
    assert policy["openrouter_only"] is True
    assert policy["direct_openai_calls_allowed"] is False
    assert policy["direct_gemini_calls_allowed"] is False


def test_degraded_mode_is_not_the_normal_operating_mode():
    manifest = _manifest()
    assert manifest["llm_default_enabled"] is True
    assert manifest["degraded_mode"]["is_normal_pilot_behaviour"] is False


def test_rate_limit_finding_recorded_as_benchmark_only():
    note = _manifest()["operational_notes"]["upstream_rate_limit"]
    assert note["observed"] == "429 rate_limit_exceeded"
    assert note["scope"] == "benchmark tooling only"
    assert note["runtime_request_semantics_changed"] is False


# ── E2E scenario catalog ────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def catalog():
    return json.loads(SCENARIOS_PATH.read_text(encoding="utf-8"))


def test_scenario_catalog_size_is_in_range(catalog):
    assert 30 <= len(catalog["scenarios"]) <= 50


def test_scenario_ids_are_unique(catalog):
    ids = [item["id"] for item in catalog["scenarios"]]
    assert len(ids) == len(set(ids))


def test_scenario_schema_is_complete(catalog):
    required = {
        "id",
        "category",
        "turns",
        "expected_intent_mode",
        "expected_selector_decision",
        "calendar_relevant",
        "degraded_expected",
        "semantic_target",
    }
    for scenario in catalog["scenarios"]:
        missing = required - set(scenario)
        assert not missing, f"{scenario['id']} missing {sorted(missing)}"
        assert scenario["turns"], f"{scenario['id']} has no turns"
        assert scenario["category"] in catalog["categories"]


def test_critical_categories_are_all_covered(catalog):
    """§22: every critical behaviour class must be represented."""
    covered = {item["category"] for item in catalog["scenarios"]}
    for required in (
        "generic_vs_specific_qualifier",
        "explicit_qualifier",
        "valid_none",
        "calendar_relevant",
        "multi_intent",
        "conversation_context",
        "provider_failure",
        "llm_disabled",
    ):
        assert required in covered, f"uncovered critical category: {required}"


def test_every_declared_category_has_a_scenario(catalog):
    covered = {item["category"] for item in catalog["scenarios"]}
    assert set(catalog["categories"]) == covered


def test_catalog_is_not_bound_to_benchmark_case_ids(catalog):
    """§22: E2E must not be pinned to the 471/472 benchmark texts."""
    blob = json.dumps(catalog, ensure_ascii=False)
    assert "471" not in blob
    assert "472" not in blob
    assert catalog["evaluation_policy"]["benchmark_case_ids_embedded"] is False


def test_catalog_uses_semantic_targets_not_exact_wording(catalog):
    assert catalog["evaluation_policy"]["exact_wording_comparison"] is False
    for scenario in catalog["scenarios"]:
        assert scenario["semantic_target"].strip()


def test_catalog_is_not_yet_executed(catalog):
    """This task authors the catalog; it makes no live calls."""
    assert catalog["execution_status"] == "NOT_EXECUTED"


def test_known_issue_references_in_catalog_are_valid(catalog):
    for scenario in catalog["scenarios"]:
        issue_id = scenario.get("known_issue_id")
        if issue_id is not None:
            assert issue_id in KNOWN_ISSUE_IDS


# ── Provenance fields excluded from the fingerprint ─────────────────────────


def test_freeze_fingerprint_ignores_git_commit():
    """git_commit is provenance, not frozen configuration.

    Regenerating the manifest from a later commit that changes nothing frozen
    must yield the same fingerprint, otherwise every commit would silently
    invalidate the published value.
    """
    at_freeze = _manifest(commit="22a63e87af81960a5f376cb26165149d6d69b70c")
    later = _manifest(commit="0" * 40)
    assert at_freeze["git_commit"] != later["git_commit"]
    assert at_freeze["freeze_fingerprint"] == later["freeze_fingerprint"]


# ── Observability contract is verified, not merely asserted ─────────────────


def test_observability_contract_separates_verified_fields_from_gaps():
    """§13 asked us to verify traceability, not to declare it."""
    contract = _manifest()["observability_contract"]
    assert contract["verified_source"] == "services.decision_trace"
    assert contract["verified"]["intent"]
    assert contract["gaps"], "gaps must be recorded, not hidden"
    assert contract["gap_status"] == "MUST_CLOSE_BEFORE_PILOT"


def test_observability_gaps_are_the_ones_found_in_decision_trace():
    gaps = _manifest()["observability_contract"]["gaps"]
    assert set(gaps) == {
        "session_correlation",
        "wall_clock_timestamp",
        "retrieval_latency",
        "source_availability",
    }


def test_observability_privacy_stance_is_preserved():
    privacy = _manifest()["observability_contract"]["privacy"]
    assert privacy["raw_user_content_required_in_telemetry"] is False
