"""INTERNAL_PILOT freeze declaration, manifest and preflight validation.

This module is a *declaration*, not a behaviour change. It records the
Answer Pipeline V2 configuration the AUZEF internal pilot is meant to run
and provides a preflight that compares the live runtime against it.

Two deliberate design rules:

1. **Nothing here mutates runtime behaviour.** The frozen selector prompt is
   ``variant_a_v1``, which currently lives only in
   ``benchmarks/selector_v2/prompts/`` as a benchmark-only file. The runtime
   still serves ``services.selector.SELECTOR_SYSTEM_PROMPT`` (the
   ``production`` prompt). Preflight therefore FAILs today; that gap is the
   pilot blocker, and closing it is a separate, explicitly approved change.
   Preflight never rewrites configuration to make itself pass.

2. **The fingerprint is deterministic.** ``created_at`` and the fingerprint
   field itself are excluded from the hashed payload, so regenerating the
   manifest on another day yields the same ``freeze_fingerprint``. The hash
   shape follows the existing convention in ``services.llm_config``:
   ``json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False)``
   over the canonical subset, then sha256.

Secrets are never read or serialized: the manifest is built from capability
config identity (provider/model/params) and code-derived fingerprints only.
API keys live in env/DB and are not consulted.
"""
from __future__ import annotations

import ast
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional, Sequence

from services.llm_config import (
    LLMCapability,
    ReasoningEffort,
    resolve_capability_config,
)

SCHEMA_VERSION = 1
MILESTONE = "INTERNAL_PILOT"

# The internal pilot is explicitly NOT the public production milestone.
NOT_MILESTONE = "PUBLIC_PRODUCTION"

# ── Frozen selector baseline (Phase 7B decision) ────────────────────────────
# Declared as constants rather than imported from ``benchmarks`` so that the
# runtime package never depends on benchmark tooling. The values are verified
# against the benchmark prompt manifest by
# ``tests/test_internal_pilot_freeze.py`` — if variant_a_v1.md ever changes,
# that test fails rather than the freeze silently tracking the edit.
SELECTOR_PROMPT_VERSION = "variant_a_v1"
SELECTOR_PROMPT_FINGERPRINT = (
    "1aed568885db02f474534224695a45eb6f95835bc31f94e877af9659efe94a1e"
)
SELECTOR_PROVIDER = "openrouter"
SELECTOR_MODEL = "openai/gpt-4o-mini"
SELECTOR_TEMPERATURE = 0.0
SELECTOR_MAX_TOKENS = 32

# "reasoning = none" means NO reasoning field is transmitted, i.e.
# ``reasoning_effort`` is UNSET (None) — not ``ReasoningEffort.NONE``.
#
# This distinction is load-bearing. Every validated Variant A run (DEV and
# HOLDOUT) used config fingerprint ``af9eb2d0…``, which is the UNSET variant.
# ``ReasoningEffort.NONE`` is a *different* request: since the openrouter
# entry of ``REASONING_TRANSPORT`` gained "none", that enum would send an
# explicit ``reasoning.effort="none"`` field and produce config fingerprint
# ``caa89baa…``, which no benchmark ever measured.
SELECTOR_REASONING: Optional[ReasoningEffort] = None
SELECTOR_REASONING_LABEL = "none (unset; no reasoning field transmitted)"
SELECTOR_CONFIG_FINGERPRINT = (
    "af9eb2d0767d37cd632799cbae39e7938585b243ceb4c7a1527b8028cd489a6e"
)

# The selector contract (output schema + candidate prompt-view fields +
# serialized probe payload + parser) is prompt-independent.
SERIALIZER_CONTRACT_FINGERPRINT = (
    "d50fbee416ca98783e499454fe840c1fdc2ff41b5fb7445cbac20f6a72c7f5a9"
)

# ── Frozen pipeline component identities ────────────────────────────────────
CANDIDATE_ORDER = "production"  # ORIGINAL retrieval order
BENCHMARK_ONLY_ORDERS = ("neutral", "permute:<seed>")

INTENT_ANALYZER_IDENTITY = {
    "component": "services.intent_analyzer",
    "version": "v2",
    "max_intents": 2,
    "max_previous_user_turns": 2,
    "bot_messages_in_context": False,
    "regex_split_fallback": False,
    "speculative_selector_call": False,
    "strict_json": True,
    # Resolved from configuration, never hardcoded here — see
    # ``intent_analyzer_config_identity``.
    "config_resolution": (
        "DB active config (services.ai_registry.load_active_config), "
        "bootstrapped from env via services.llm_config.resolve_llm_config_set; "
        "env fallback LLM_INTENT_ANALYZER_* then LLM_PROVIDER"
    ),
}

CALENDAR_IDENTITY = {
    "component": "services.calendar_retrieval + services.calendar_utils",
    "version": "v2",
    "sort_key": "period/event/date then record id (Phase 3 order)",
    "candidates_precede_qna": True,
}

CANDIDATE_ELIGIBILITY_IDENTITY = {
    "component": "services.candidate_eligibility",
    "version": "v2",
    "default_max_candidates": 32,
    "max_candidates_env": "SELECTOR_MAX_CANDIDATES",
    "retrieval_score_decimals": 5,
    "order": CANDIDATE_ORDER,
}

DEGRADED_MODE_IDENTITY = {
    "component": "services.answer_pipeline.answer_in_degraded_mode",
    "deterministic": True,
    "chain": "Calendar -> Meili >=0.90 -> Qdrant >0.75",
    "llm_generated": False,
    "entered_only_on": (
        "provider/model failure",
        "circuit breaker open",
        "admin emergency LLM disable",
    ),
    "never_entered_on": ("semantic NONE", "NO_ELIGIBLE_CANDIDATES"),
    "is_normal_pilot_behaviour": False,
}

CIRCUIT_BREAKER_IDENTITY = {
    "component": "services.circuit_breaker",
    "default_failure_threshold": 3,
    "default_cooldown_seconds": 60.0,
    "failure_threshold_env": "LLM_CIRCUIT_BREAKER_FAILURE_THRESHOLD",
    "cooldown_seconds_env": "LLM_CIRCUIT_BREAKER_COOLDOWN_SECONDS",
    "invalid_output_is_neutral": True,
}

REGISTRY_IDENTITY = {
    "component": "services.ai_registry",
    "snapshot_schema": 1,
    "config_versioning": "immutable ai_config_version rows, append-only audit",
    "optimistic_concurrency": True,
}

PROVIDER_POLICY = {
    "inference_provider": "openrouter",
    "openrouter_only": True,
    "direct_openai_calls_allowed": False,
    "direct_gemini_calls_allowed": False,
    "applies_to": ("selector", "intent_analyzer"),
}

LLM_DEFAULT_ENABLED = True  # §10: the pilot's normal path runs the LLM.

# ── Known limitations (engineering-facing ids) ──────────────────────────────
KNOWN_ISSUE_IDS = (
    "IP-KI-1-generic-vs-specific-qualifier",
    "IP-KI-2-expected-none-undervalidated",
    "IP-KI-3-semantic-gold-is-model-adjudicated",
)

KNOWN_ISSUES = {
    "IP-KI-1-generic-vs-specific-qualifier": {
        "summary": (
            "A generic intent can be routed to a more specific candidate that "
            "assumes a qualifier the user never stated."
        ),
        "class": "general vs specific qualifier",
        "severity": "known-open",
        "user_facing_doc": False,
    },
    "IP-KI-2-expected-none-undervalidated": {
        "summary": (
            "Expected-NONE behaviour is not validated on a sufficiently large "
            "sample; NONE recall remains unmeasured."
        ),
        "class": "expected NONE",
        "severity": "known-open",
        "user_facing_doc": True,
    },
    "IP-KI-3-semantic-gold-is-model-adjudicated": {
        "summary": (
            "Semantic Gold was produced by blind model adjudication "
            "(adjudicator: GPT-5.6 Sol), not by independent human review."
        ),
        "class": "validation provenance",
        "severity": "blocks-public-production",
        "user_facing_doc": True,
    },
}

# ── Backlog / deferred phases ───────────────────────────────────────────────
BACKLOG_STATUS = "BACKLOG_POST_INTERNAL_PILOT"
BACKLOG_PHASES = {
    "7C": {"name": "Metadata Necessity", "status": BACKLOG_STATUS},
    "7D": {"name": "Exact Alias Experiment", "status": BACKLOG_STATUS},
    "7E": {"name": "Candidate Budget", "status": BACKLOG_STATUS},
    "7F": {"name": "Bot Context Necessity", "status": BACKLOG_STATUS},
}

PHASE_7G = {
    "name": "PUBLIC_PRODUCTION_FINAL_FREEZE",
    "status": "DEFERRED_UNTIL_INTERNAL_PILOT_DATA",
    "runs_before_internal_pilot": False,
}

# ── Validation provenance ───────────────────────────────────────────────────
VALIDATION_PROVENANCE = {
    "semantic_dev": {
        "production_4o_mini": "51/77",
        "variant_a_4o_mini": "64/77",
        "variant_b_4o_mini": "56/77",
        "variant_c_4o_mini": "63/77",
        "variant_c_luna": "64/77",
    },
    "variant_a_holdout": {
        "variant_a": "30/38",
        "production": "24/38",
        "only_a_correct": 6,
        "only_production_correct": 0,
        "historical_gate_status": "FAIL",
        "historical_gate_reason": "471/472 hard gate",
    },
    "gold_provenance": {
        "method": "blind model adjudication",
        "adjudicator": "GPT-5.6 Sol",
        "independent_human_gold": False,
    },
    "luna": {
        "full_dev": "COMPLETE",
        "variant_c_luna": "64/77",
        "m1": "FAIL",
        "status": "research candidate",
        "auto_promoted_to_pilot_selector": False,
    },
    "selection_rationale": (
        "strongest validated low-cost baseline",
        "major false-NONE reduction",
        "no observed HOLDOUT corruption vs Production",
        "Luna did not improve aggregate DEV accuracy",
        "public-production validation deferred to pilot data",
    ),
}

OPERATIONAL_NOTES = {
    "upstream_rate_limit": {
        "observed": "429 rate_limit_exceeded",
        "seen_in": "Luna stronger-selector benchmark experiment",
        "scope": "benchmark tooling only",
        "benchmark_only_mechanisms": (
            "benchmarks.selector_v2.model_experiment.PacedBackend "
            "(inter-call sleep pacing)",
            "benchmarks.selector_v2.model_experiment.FailFastBackend "
            "(stop after N consecutive operational errors)",
            "benchmarks.selector_v2.cli --fail-fast-after / --retry-errors",
        ),
        "shared_runtime_retry_surface": (
            "services.llm_provider: SDK max_retries / timeout passthrough from "
            "capability config (unchanged by the Luna experiment)"
        ),
        "runtime_request_semantics_changed": False,
        "retry_policy_redesigned_in_this_task": False,
    }
}

# ── Observability contract ──────────────────────────────────────────────────
# Verified against services/decision_trace.py at this commit rather than
# asserted. "verified" fields are emitted today; "gaps" are required by the
# pilot observability contract but NOT currently emitted, and must be closed
# before the pilot rather than assumed.
OBSERVABILITY_CONTRACT = {
    "verified_source": "services.decision_trace",
    "verified": {
        "correlation": ("request_id",),
        "intent": (
            "execution_mode (SINGLE/MULTI)",
            "intent_analyzer_status (success/failure)",
            "context_used",
            "calendar_relevant",
        ),
        "retrieval": ("candidate_count",),
        "selector": (
            "selected_candidate_ref_or_NONE",
            "selected_candidate_source",
            "provider",
            "requested_model",
            "actual_model",
            "status (success/failure)",
        ),
        "degraded": ("degraded (list + request summary)", "degraded_reason"),
        "latency": ("total_latency_ms", "intent latency_ms", "selector latency_ms"),
        "errors": (
            "model_error",
            "timeout",
            "pipeline_error",
            "ai_config_error",
            "circuit (breaker state entries)",
        ),
    },
    "gaps": {
        "session_correlation": (
            "decision_trace carries request_id but no session/conversation id. "
            "conversation_id exists only in routers/chat.py, so request->session "
            "correlation is not available from telemetry alone."
        ),
        "wall_clock_timestamp": (
            "The trace records elapsed time (started_at / total_latency_ms) but "
            "no wall-clock timestamp field; it is currently only the log "
            "record's own timestamp."
        ),
        "retrieval_latency": (
            "No retrieval_ms is recorded. Intent, selector and total latency "
            "are, so retrieval time is only inferable by subtraction."
        ),
        "source_availability": (
            "Only proxies exist (meili_fallback_used, qdrant_fallback_used, "
            "selected_source); explicit per-source availability is not emitted."
        ),
    },
    "gap_status": "MUST_CLOSE_BEFORE_PILOT",
    "privacy": {
        "raw_user_content_required_in_telemetry": False,
        "note": (
            "Existing PII-free request-scoped decision trace is preserved; "
            "this freeze adds no new mandatory raw-content field. Closing the "
            "gaps above must not introduce one."
        ),
    },
}

PILOT_DATA_POLICY = {
    "operational_telemetry": "automatic system metrics; PII-free decision trace",
    "conversation_history": (
        "the existing application's permitted chat-history behaviour; "
        "no new collection system is introduced by this freeze"
    ),
    "post_pilot_benchmark_requirements": (
        "PII anonymization / removal",
        "deduplication",
        "sampling",
        "independent human adjudication",
    ),
    "validation_artifact_without_those_steps": "FORBIDDEN",
}


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical(value) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def _jsonable(value):
    """Tuples -> lists so the manifest round-trips through JSON unchanged."""
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def intent_analyzer_config_identity(
    environ: Optional[Mapping[str, str]] = None,
) -> dict:
    """Derive the Intent Analyzer identity from configuration.

    Never hardcodes a model: the values come from the same resolver the
    runtime uses. The DB active config is bootstrapped from exactly this
    env-effective config, so this is the frozen identity as long as an
    operator has not created a newer registry version.
    """
    env = {"LLM_PROVIDER": PROVIDER_POLICY["inference_provider"]}
    if environ is not None:
        env = dict(environ)
    config = resolve_capability_config(
        LLMCapability.INTENT_ANALYZER, environ=env
    )
    identity = dict(INTENT_ANALYZER_IDENTITY)
    identity["config"] = config.to_dict()
    identity["config_fingerprint"] = config.fingerprint
    return identity


def selector_config_identity(
    environ: Optional[Mapping[str, str]] = None,
) -> dict:
    env = {"LLM_PROVIDER": SELECTOR_PROVIDER}
    if environ is not None:
        env = dict(environ)
    config = resolve_capability_config(LLMCapability.SELECTOR, environ=env)
    return {"config": config.to_dict(), "config_fingerprint": config.fingerprint}


def seeded_llm_enabled_default() -> Optional[bool]:
    """Read DEFAULT_SYSTEM_CONFIG["LLM_ENABLED"] without importing the DB layer.

    ``scripts.init_system`` pulls in ``core.database`` (SQLAlchemy engines), so
    preflight parses the literal instead — keeping the check runnable with no
    database and no infrastructure.
    """
    path = repo_root() / "backend" / "scripts" / "init_system.py"
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return None
    # Module-level string constants, so a dict whose values are names (e.g.
    # {"LLM_ENABLED": LLM_ENABLED_SAFE_DEFAULT}) still resolves.
    constants: dict = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = node.value.value

    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "DEFAULT_SYSTEM_CONFIG" not in names or not isinstance(node.value, ast.Dict):
            continue
        for key, value in zip(node.value.keys, node.value.values):
            if not (isinstance(key, ast.Constant) and key.value == "LLM_ENABLED"):
                continue
            if isinstance(value, ast.Constant):
                raw = value.value
            elif isinstance(value, ast.Name) and value.id in constants:
                raw = constants[value.id]
            else:
                return None
            return str(raw).strip().lower() in ("1", "true", "yes", "on")
    return None


def defines_symbol(path: Path, symbol: str) -> bool:
    """True if ``path`` defines ``symbol`` at module level, without importing.

    Importing ``services.answer_pipeline`` loads the embedding model, so
    preflight inspects source instead of executing it.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return False
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == symbol:
                return True
    return False


def benchmark_only_order_names() -> tuple[str, ...]:
    """Candidate-order names referenced anywhere under ``services/``.

    The pilot must use the ORIGINAL retrieval order; the neutral and permuted
    orders are benchmark tooling and must not appear in the runtime package.
    """
    found: set[str] = set()
    services_dir = repo_root() / "backend" / "services"
    for path in sorted(services_dir.glob("*.py")):
        if path.name == Path(__file__).name:
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for marker in ("NEUTRAL_ORDER", "order_candidates", "permute:"):
            if marker in source:
                found.add(marker)
    return tuple(sorted(found))


def build_freeze_manifest(
    *,
    git_commit: str,
    created_at: str,
    environ: Optional[Mapping[str, str]] = None,
) -> dict:
    """Assemble the machine-readable freeze manifest.

    Contains no secrets: only component identity, parameters and fingerprints.
    """
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "milestone": MILESTONE,
        "not_milestone": NOT_MILESTONE,
        "milestone_definition": (
            "Single-server Docker environment in which AUZEF staff exercise "
            "the chatbot as real users and generate real usage data."
        ),
        "git_commit": git_commit,
        "selector_prompt_version": SELECTOR_PROMPT_VERSION,
        "selector_prompt_fingerprint": SELECTOR_PROMPT_FINGERPRINT,
        "selector_provider": SELECTOR_PROVIDER,
        "selector_model": SELECTOR_MODEL,
        "selector_reasoning": "none",
        "selector_reasoning_semantics": SELECTOR_REASONING_LABEL,
        "selector_temperature": SELECTOR_TEMPERATURE,
        "selector_max_tokens": SELECTOR_MAX_TOKENS,
        "selector_config_fingerprint": SELECTOR_CONFIG_FINGERPRINT,
        "selector_contract_fingerprint": SERIALIZER_CONTRACT_FINGERPRINT,
        "intent_analyzer": intent_analyzer_config_identity(environ),
        "calendar": CALENDAR_IDENTITY,
        "candidate_eligibility": CANDIDATE_ELIGIBILITY_IDENTITY,
        "candidate_order": CANDIDATE_ORDER,
        "benchmark_only_orders": BENCHMARK_ONLY_ORDERS,
        "degraded_mode": DEGRADED_MODE_IDENTITY,
        "circuit_breaker": CIRCUIT_BREAKER_IDENTITY,
        "registry": REGISTRY_IDENTITY,
        "provider_policy": PROVIDER_POLICY,
        "llm_default_enabled": LLM_DEFAULT_ENABLED,
        "known_issue_ids": KNOWN_ISSUE_IDS,
        "known_issues": KNOWN_ISSUES,
        "backlog_phases": BACKLOG_PHASES,
        "phase_7g": PHASE_7G,
        "validation_provenance": VALIDATION_PROVENANCE,
        "operational_notes": OPERATIONAL_NOTES,
        "observability_contract": OBSERVABILITY_CONTRACT,
        "pilot_data_policy": PILOT_DATA_POLICY,
        "created_at": created_at,
    }
    manifest = _jsonable(manifest)
    manifest["freeze_fingerprint"] = freeze_fingerprint(manifest)
    return manifest


# Excluded from the hashed payload so the fingerprint is reproducible across
# regenerations. Documented in INTERNAL_PILOT_FREEZE.md.
#
# ``git_commit`` is provenance, not configuration: the fingerprint identifies
# the frozen *behaviour*, so regenerating the manifest from a later commit
# that changes nothing frozen must yield the same fingerprint. Were it hashed,
# every commit would silently invalidate the published value.
FINGERPRINT_EXCLUDED_FIELDS = ("created_at", "git_commit", "freeze_fingerprint")


def freeze_fingerprint(manifest: Mapping) -> str:
    """Deterministic SHA256 over the manifest minus volatile fields."""
    payload = {
        key: value
        for key, value in manifest.items()
        if key not in FINGERPRINT_EXCLUDED_FIELDS
    }
    return _sha256(_canonical(_jsonable(payload)))


# ── Preflight ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    expected: str
    actual: str
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "status": "PASS" if self.passed else "FAIL",
            "expected": self.expected,
            "actual": self.actual,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class PreflightReport:
    checks: Sequence[CheckResult] = field(default_factory=tuple)

    @property
    def failures(self) -> list[CheckResult]:
        return [check for check in self.checks if not check.passed]

    @property
    def passed(self) -> bool:
        return not self.failures

    @property
    def status(self) -> str:
        return "PASS" if self.passed else "FAIL"

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "checks": [check.to_dict() for check in self.checks],
            "failed_checks": [check.name for check in self.failures],
        }


def _normalize_prompt(text: str) -> str:
    """Same normalization the benchmark prompt contract uses."""
    lines = [
        line.rstrip()
        for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    ]
    return "\n".join(lines).strip("\n")


def runtime_selector_prompt_fingerprint() -> str:
    from services.selector import SELECTOR_SYSTEM_PROMPT

    return _sha256(_normalize_prompt(SELECTOR_SYSTEM_PROMPT))


def run_preflight(
    *,
    environ: Optional[Mapping[str, str]] = None,
    llm_enabled_default: Optional[bool] = None,
    selector_prompt_fingerprint: Optional[str] = None,
) -> PreflightReport:
    """Compare live runtime configuration against the frozen baseline.

    Never mutates configuration. A mismatch is reported as FAIL; resolving it
    is an operator decision.
    """
    checks: list[CheckResult] = []

    config = None
    resolve_error = ""
    try:
        config = resolve_capability_config(
            LLMCapability.SELECTOR,
            environ=dict(environ) if environ is not None else None,
        )
    except (ValueError, RuntimeError) as exc:
        resolve_error = str(exc)

    checks.append(
        CheckResult(
            "selector_config_resolvable",
            config is not None,
            "resolvable selector capability config",
            "resolved" if config is not None else f"error: {resolve_error}",
            detail=(
                ""
                if config is not None
                else "LLM_PROVIDER (or LLM_SELECTOR_PROVIDER) is unset in this "
                "environment; the config-dependent checks below cannot be "
                "evaluated. Run preflight on the pilot host with its real env."
            ),
        )
    )

    # A missing provider must not hide the remaining mismatches, so the
    # config-independent checks always run. Unresolvable config is reported as
    # "unresolved" per check rather than short-circuiting the whole report.
    def _config_check(name, ok, expected, actual, detail=""):
        if config is None:
            checks.append(
                CheckResult(name, False, expected, "unresolved", detail=detail)
            )
        else:
            checks.append(CheckResult(name, ok(), expected, actual(), detail=detail))

    _config_check(
        "selector_provider_matches",
        lambda: config.provider == SELECTOR_PROVIDER,
        SELECTOR_PROVIDER,
        lambda: config.provider,
    )
    _config_check(
        "selector_model_matches",
        lambda: config.model == SELECTOR_MODEL,
        SELECTOR_MODEL,
        lambda: config.model,
    )
    _config_check(
        "selector_reasoning_matches",
        lambda: config.reasoning_effort is SELECTOR_REASONING,
        SELECTOR_REASONING_LABEL,
        lambda: "unset"
        if config.reasoning_effort is None
        else config.reasoning_effort.value,
        detail=(
            "'none' means no reasoning field is transmitted (unset). "
            "ReasoningEffort.NONE is a different, unvalidated request."
        ),
    )
    _config_check(
        "selector_temperature_matches",
        lambda: float(config.temperature) == SELECTOR_TEMPERATURE,
        str(SELECTOR_TEMPERATURE),
        lambda: str(config.temperature),
    )
    _config_check(
        "selector_max_tokens_matches",
        lambda: int(config.max_tokens) == SELECTOR_MAX_TOKENS,
        str(SELECTOR_MAX_TOKENS),
        lambda: str(config.max_tokens),
    )
    _config_check(
        "selector_config_fingerprint_matches",
        lambda: config.fingerprint == SELECTOR_CONFIG_FINGERPRINT,
        SELECTOR_CONFIG_FINGERPRINT,
        lambda: config.fingerprint,
    )

    actual_prompt_fp = (
        selector_prompt_fingerprint
        if selector_prompt_fingerprint is not None
        else runtime_selector_prompt_fingerprint()
    )
    checks.append(
        CheckResult(
            "selector_prompt_fingerprint_matches",
            actual_prompt_fp == SELECTOR_PROMPT_FINGERPRINT,
            f"{SELECTOR_PROMPT_FINGERPRINT} ({SELECTOR_PROMPT_VERSION})",
            actual_prompt_fp,
            detail=(
                "The runtime still serves the 'production' selector prompt. "
                "Landing variant_a_v1 in services.selector is a prerequisite "
                "for the internal pilot and is NOT done by this freeze."
            ),
        )
    )

    enabled = (
        llm_enabled_default
        if llm_enabled_default is not None
        else seeded_llm_enabled_default()
    )
    checks.append(
        CheckResult(
            "llm_enabled_seeded_default",
            enabled is True,
            "true",
            "unknown" if enabled is None else str(enabled).lower(),
            detail=(
                "Reads DEFAULT_SYSTEM_CONFIG['LLM_ENABLED'] in "
                "scripts/init_system.py — the SEEDED default, not the live "
                "SystemConfig row. An operator may have enabled the LLM via "
                "admin while the seed still says false; the acceptance plan "
                "checks the live value separately (Stage A8). The pilot's "
                "normal request path runs the LLM; degraded mode is an "
                "exception path, not the default operating mode."
            ),
        )
    )

    leaked_orders = benchmark_only_order_names()
    order_is_clean = not leaked_orders
    checks.append(
        CheckResult(
            "candidate_order_expected",
            order_is_clean,
            f"{CANDIDATE_ORDER} (ORIGINAL retrieval order); no reordering in services/",
            "production order preserved"
            if order_is_clean
            else f"benchmark reordering leaked into services/: {', '.join(leaked_orders)}",
            detail=(
                "Neutral/permuted candidate order exists only in "
                "benchmarks/selector_v2/contract.py, never in services/."
            ),
        )
    )

    degraded_available = defines_symbol(
        repo_root() / "backend" / "services" / "answer_pipeline.py",
        "answer_in_degraded_mode",
    )
    checks.append(
        CheckResult(
            "degraded_mode_available",
            degraded_available,
            "services.answer_pipeline.answer_in_degraded_mode defined",
            "available" if degraded_available else "missing",
            detail=(
                "Checked by source inspection: importing answer_pipeline loads "
                "the embedding model, which preflight must not do."
            ),
        )
    )

    _config_check(
        "provider_policy_openrouter_only",
        lambda: config.provider == PROVIDER_POLICY["inference_provider"],
        "openrouter",
        lambda: config.provider,
        detail="Direct OpenAI/Gemini inference is not permitted in the pilot.",
    )

    return PreflightReport(tuple(checks))


# ── Amendment 1: acceptance-discovered Intent Analyzer contract fix ─────────
#
# The original freeze (parent fingerprint below) stays immutable: it records
# what was decided at freeze time, including the observability gaps. This
# amendment records a BUG FIX found by executing acceptance — not research,
# not a model/selector/policy change.
#
# The analyzer prompt declared its output schema with string type labels
# ("intent_count": "1 or 2"). gpt-4o-mini mirrored that and returned "1" as a
# string, which the strict Literal[1, 2] contract rejects, degrading 14/14
# live requests. A second instance of the same class was then found by live
# probing: the prompt never stated that resolved_text must equal
# normalized_text when context_used is false. Both were fixed in the PROMPT.
# The parser and the contract were NOT weakened.

AMENDMENT_ID = "INTERNAL_PILOT_FREEZE_AMENDMENT_1"
PARENT_FREEZE_FINGERPRINT = (
    "c7081ff54d959d183e7964498720dce7866461e72c3fa407c7346ea3cf32c5c6"
)


def intent_analyzer_prompt_fingerprint() -> str:
    """Fingerprint the analyzer system prompt + its typed output-schema shape.

    The user payload varies per turn, so only the turn-independent parts are
    hashed: the system prompt text and the JSON types of the output schema.
    """
    import json as _json

    from services.intent_analyzer import build_intent_analyzer_prompt

    system, user = build_intent_analyzer_prompt("probe", ())
    schema = _json.loads(user)["output_schema"]

    def shape(value):
        if isinstance(value, dict):
            return {key: shape(item) for key, item in sorted(value.items())}
        if isinstance(value, list):
            return [shape(item) for item in value]
        return type(value).__name__

    return _sha256(_canonical({"system": system, "output_schema_types": shape(schema)}))


def build_freeze_amendment(*, git_commit: str, created_at: str) -> dict:
    amendment = {
        "schema_version": SCHEMA_VERSION,
        "amendment_id": AMENDMENT_ID,
        "milestone": MILESTONE,
        "parent_freeze_fingerprint": PARENT_FREEZE_FINGERPRINT,
        "parent_is_immutable": True,
        "git_commit": git_commit,
        "classification": {
            "bug_fix": True,
            "research_change": False,
            "selector_change": False,
            "model_change": False,
            "policy_change": False,
        },
        "rationale": (
            "acceptance-discovered contract bug: the Intent Analyzer prompt "
            "emitted a string intent_count while the strict runtime contract "
            "requires a JSON integer; a second instance of the same class "
            "(resolved_text rewritten when context_used is false) was found by "
            "live probing and fixed in the same way"
        ),
        "changed": {
            "component": "services.intent_analyzer.build_intent_analyzer_prompt",
            "what": "prompt instruction + typed output-schema example",
            "intent_analyzer_prompt_fingerprint": intent_analyzer_prompt_fingerprint(),
            "intent_analyzer_config_fingerprint": (
                intent_analyzer_config_identity()["config_fingerprint"]
            ),
        },
        "unchanged": {
            "selector_prompt_version": SELECTOR_PROMPT_VERSION,
            "selector_prompt_fingerprint": SELECTOR_PROMPT_FINGERPRINT,
            "selector_provider": SELECTOR_PROVIDER,
            "selector_model": SELECTOR_MODEL,
            "selector_temperature": SELECTOR_TEMPERATURE,
            "selector_max_tokens": SELECTOR_MAX_TOKENS,
            "selector_reasoning": "none (unset)",
            "selector_config_fingerprint": SELECTOR_CONFIG_FINGERPRINT,
            "selector_contract_fingerprint": SERIALIZER_CONTRACT_FINGERPRINT,
            "candidate_order": CANDIDATE_ORDER,
            "parser_strictness": "unchanged (strict=True, extra=forbid)",
            "analyzer_policy": (
                "SINGLE default, MULTI max 2, ambiguity -> SINGLE, max 2 "
                "previous USER turns, bot messages excluded, calendar_relevant "
                "semantics unchanged, failure fallback unchanged"
            ),
        },
        "live_validation": {
            "provider": SELECTOR_PROVIDER,
            "model": SELECTOR_MODEL,
            "probes": 8,
            "invalid_output": 0,
            "integer_intent_count": 8,
            "quoted_string_intent_count": 0,
        },
        "created_at": created_at,
    }
    amendment = _jsonable(amendment)
    amendment["amendment_fingerprint"] = amendment_fingerprint(amendment)
    return amendment


AMENDMENT_EXCLUDED_FIELDS = ("created_at", "git_commit", "amendment_fingerprint")


def amendment_fingerprint(amendment: Mapping) -> str:
    payload = {
        key: value
        for key, value in amendment.items()
        if key not in AMENDMENT_EXCLUDED_FIELDS
    }
    return _sha256(_canonical(_jsonable(payload)))


# ── Amendment 2: MULTI + context semantic restoration ───────────────────────
#
# Re-acceptance measured two capabilities as systematically dead in live
# traffic: MULTI 0/44 and context_used 0/44. Context input plumbing was
# proven correct first (previous USER turns are supplied, bot messages
# excluded, max 2, chronological), so the cause was prompt instruction
# behaviour, not assembly.
#
# Amendment 1's fix for the resolved_text contract had added an emphatic
# "copy normalized_text verbatim" instruction. Unscoped, it read as
# unconditional and suppressed the context branch; "ambiguity -> SINGLE" led
# the prompt and suppressed MULTI. Both are clarified here. No policy, no
# parser strictness and no contract changed.

AMENDMENT_2_ID = "INTERNAL_PILOT_FREEZE_AMENDMENT_2"
AMENDMENT_1_FINGERPRINT = (
    "9d221c3cf94ffd5039670e74b6272b078106f793c06f98fe649557ca21ec8485"
)


def build_freeze_amendment_2(*, git_commit: str, created_at: str,
                             live_screen: Optional[Mapping] = None) -> dict:
    amendment = {
        "schema_version": SCHEMA_VERSION,
        "amendment_id": AMENDMENT_2_ID,
        "milestone": MILESTONE,
        "parent_amendment_id": AMENDMENT_ID,
        "parent_amendment_fingerprint": AMENDMENT_1_FINGERPRINT,
        "historical_parent_freeze_fingerprint": PARENT_FREEZE_FINGERPRINT,
        "parents_are_immutable": True,
        "git_commit": git_commit,
        "classification": {
            "intent_analyzer_semantic_bug_fix": True,
            "selector_change": False,
            "model_change": False,
            "retrieval_change": False,
            "calendar_policy_change": False,
            "parser_contract_change": False,
            "analyzer_policy_change": False,
        },
        "rationale": (
            "re-acceptance measured MULTI 0/44 and context_used 0/44. Context "
            "input plumbing was proven correct first, so the cause was prompt "
            "instruction behaviour: amendment 1's verbatim-copy rule was "
            "unscoped and suppressed the context branch, and 'ambiguity -> "
            "SINGLE' led the prompt and suppressed MULTI. Both clarified; the "
            "strict contract and the frozen policy are untouched."
        ),
        "changed": {
            "component": "services.intent_analyzer.build_intent_analyzer_prompt",
            "what": (
                "explicit context decision order; verbatim equality scoped to "
                "context_used=false; intent_count decoupled from whether the "
                "text changed; SINGLE boundaries enumerated"
            ),
            "intent_analyzer_prompt_fingerprint": intent_analyzer_prompt_fingerprint(),
            "context_assembly_changed": False,
        },
        "preserved": {
            "analyzer_policy": (
                "SINGLE/MULTI, max 2 intents, MULTI only for genuinely "
                "independent goals, ambiguity -> SINGLE, current USER message "
                "plus max 2 previous USER turns, bot messages excluded"
            ),
            "parser_strictness": "strict=True, extra=forbid, Literal[1, 2]",
            "intent_count_must_be_json_integer": True,
            "context_used_false_implies_verbatim_resolved_text": True,
            "selector_prompt_version": SELECTOR_PROMPT_VERSION,
            "selector_prompt_fingerprint": SELECTOR_PROMPT_FINGERPRINT,
            "selector_provider": SELECTOR_PROVIDER,
            "selector_model": SELECTOR_MODEL,
            "selector_temperature": SELECTOR_TEMPERATURE,
            "selector_max_tokens": SELECTOR_MAX_TOKENS,
            "selector_reasoning": "none (unset)",
            "selector_config_fingerprint": SELECTOR_CONFIG_FINGERPRINT,
            "candidate_order": CANDIDATE_ORDER,
        },
        "live_semantic_screen": dict(live_screen) if live_screen else None,
        "created_at": created_at,
    }
    amendment = _jsonable(amendment)
    amendment["amendment_fingerprint"] = amendment_2_fingerprint(amendment)
    return amendment


AMENDMENT_2_EXCLUDED_FIELDS = ("created_at", "git_commit", "amendment_fingerprint",
                               "live_semantic_screen")


def amendment_2_fingerprint(amendment: Mapping) -> str:
    """Fingerprint the frozen declaration, not the measurement that validated it."""
    payload = {key: value for key, value in amendment.items()
               if key not in AMENDMENT_2_EXCLUDED_FIELDS}
    return _sha256(_canonical(_jsonable(payload)))


# ── Amendment 3: context resolution final fix ───────────────────────────────
#
# Amendment 2 restored MULTI (0/3 -> 4/4) but the context branch still failed
# its screen at 1/4 with two classes:
#   CONTEXT_CLAIMED_WITHOUT_RESOLUTION - context_used=true while resolved_text
#       stayed referential, correctly rejected by the strict parser;
#   CONTEXT_NOT_DETECTED - a short reference-dependent question treated as
#       self-contained.
# Fixed in the prompt only: the flag and the rewrite are bound into one
# decision, and a generic self-contained test describes when a short question
# is reference-dependent. MULTI wording untouched; anti-over-trigger kept so
# the 4/4 control result is protected.

AMENDMENT_3_ID = "INTERNAL_PILOT_FREEZE_AMENDMENT_3"
AMENDMENT_2_FINGERPRINT = (
    "2c9f2ea99ae9a44f88ae1623a2bb753abf212c17d5a6bef59ec56f81a99fed50"
)


def build_freeze_amendment_3(*, git_commit: str, created_at: str,
                             live_screen: Optional[Mapping] = None) -> dict:
    amendment = {
        "schema_version": SCHEMA_VERSION,
        "amendment_id": AMENDMENT_3_ID,
        "milestone": MILESTONE,
        "parent_amendment_id": AMENDMENT_2_ID,
        "parent_amendment_fingerprint": AMENDMENT_2_FINGERPRINT,
        "amendment_1_fingerprint": AMENDMENT_1_FINGERPRINT,
        "historical_parent_freeze_fingerprint": PARENT_FREEZE_FINGERPRINT,
        "parents_are_immutable": True,
        "git_commit": git_commit,
        "classification": {
            "intent_analyzer_context_semantic_bug_fix": True,
            "model_change": False,
            "selector_change": False,
            "retrieval_change": False,
            "calendar_change": False,
            "parser_contract_change": False,
            "context_assembly_change": False,
            "multi_rule_change": False,
        },
        "rationale": (
            "amendment 2 restored MULTI but left context at 1/4 with two "
            "classes: CONTEXT_CLAIMED_WITHOUT_RESOLUTION (flag set while "
            "resolved_text stayed referential) and CONTEXT_NOT_DETECTED "
            "(short reference-dependent question treated as self-contained). "
            "Prompt-only fix: the flag and the rewrite are one decision, plus "
            "a generic self-contained test for short questions."
        ),
        "changed": {
            "component": "services.intent_analyzer.build_intent_analyzer_prompt",
            "what": (
                "context branch only: flag bound to the rewrite; generic "
                "reference-dependency test for short questions; "
                "anti-over-trigger rule retained"
            ),
            "intent_analyzer_prompt_fingerprint": intent_analyzer_prompt_fingerprint(),
            "multi_rules_touched": False,
            "context_assembly_changed": False,
        },
        "preserved": {
            "analyzer_policy": (
                "ambiguity -> SINGLE, MULTI max 2, calendar semantics, "
                "normalization policy, bot exclusion, max 2 previous USER turns"
            ),
            "parser_strictness": "strict=True, extra=forbid, Literal[1, 2]",
            "context_used_false_implies_verbatim_resolved_text": True,
            "context_used_true_requires_real_resolution": True,
            "selector_prompt_version": SELECTOR_PROMPT_VERSION,
            "selector_prompt_fingerprint": SELECTOR_PROMPT_FINGERPRINT,
            "selector_provider": SELECTOR_PROVIDER,
            "selector_model": SELECTOR_MODEL,
            "selector_config_fingerprint": SELECTOR_CONFIG_FINGERPRINT,
            "candidate_order": CANDIDATE_ORDER,
        },
        "live_semantic_screen": dict(live_screen) if live_screen else None,
        "created_at": created_at,
    }
    amendment = _jsonable(amendment)
    amendment["amendment_fingerprint"] = amendment_3_fingerprint(amendment)
    return amendment


AMENDMENT_3_EXCLUDED_FIELDS = AMENDMENT_2_EXCLUDED_FIELDS


def amendment_3_fingerprint(amendment: Mapping) -> str:
    payload = {key: value for key, value in amendment.items()
               if key not in AMENDMENT_3_EXCLUDED_FIELDS}
    return _sha256(_canonical(_jsonable(payload)))


# Amendment 4 is the final narrow context-detection correction. Amendment 3
# closed claimed-without-resolution; one bare plural set referent still looked
# grammatically complete and was therefore not resolved. Parser, context
# assembly, MULTI rules, selector and calendar policy remain unchanged.

AMENDMENT_4_ID = "INTERNAL_PILOT_FREEZE_AMENDMENT_4"
AMENDMENT_3_FINGERPRINT = (
    "429dc8c0f44f3969a09ef04b6a46363959aa4c37354a3f7394a8896d6511bbce"
)


def build_freeze_amendment_4(*, git_commit: str, created_at: str,
                             live_screen: Optional[Mapping] = None) -> dict:
    amendment = {
        "schema_version": SCHEMA_VERSION,
        "amendment_id": AMENDMENT_4_ID,
        "milestone": MILESTONE,
        "parent_amendment_id": AMENDMENT_3_ID,
        "parent_amendment_fingerprint": AMENDMENT_3_FINGERPRINT,
        "historical_parent_freeze_fingerprint": PARENT_FREEZE_FINGERPRINT,
        "parents_are_immutable": True,
        "git_commit": git_commit,
        "classification": {
            "intent_analyzer_context_detection_bug_fix": True,
            "model_change": False,
            "selector_change": False,
            "retrieval_change": False,
            "calendar_change": False,
            "parser_contract_change": False,
            "context_assembly_change": False,
            "multi_rule_change": False,
        },
        "rationale": (
            "amendment 3 restored 3/4 context-required probes and eliminated "
            "CONTEXT_CLAIMED_WITHOUT_RESOLUTION. The remaining "
            "BARE_PLURAL_REFERENT_NOT_RESOLVED class treated a grammatically "
            "complete generic set question as semantically self-contained. "
            "Prompt-only fix: semantic self-containment now requires an "
            "explicit subject/process for generic category or set referents."
        ),
        "changed": {
            "component": "services.intent_analyzer.build_intent_analyzer_prompt",
            "what": (
                "context branch only: generic bare category/set referents require "
                "an explicit subject or a unique previous USER referent; "
                "anti-over-trigger and ambiguity guards retained"
            ),
            "intent_analyzer_prompt_fingerprint": intent_analyzer_prompt_fingerprint(),
            "multi_rules_touched": False,
            "context_assembly_changed": False,
        },
        "preserved": {
            "analyzer_policy": (
                "ambiguity -> SINGLE, MULTI max 2, calendar semantics, "
                "normalization policy, bot exclusion, max 2 previous USER turns"
            ),
            "parser_strictness": "strict=True, extra=forbid, Literal[1, 2]",
            "context_used_false_implies_verbatim_resolved_text": True,
            "context_used_true_requires_real_resolution": True,
            "anti_over_trigger_guard": True,
            "no_invented_referent_guard": True,
            "selector_prompt_version": SELECTOR_PROMPT_VERSION,
            "selector_prompt_fingerprint": SELECTOR_PROMPT_FINGERPRINT,
            "selector_provider": SELECTOR_PROVIDER,
            "selector_model": SELECTOR_MODEL,
            "selector_config_fingerprint": SELECTOR_CONFIG_FINGERPRINT,
            "candidate_order": CANDIDATE_ORDER,
        },
        "live_semantic_screen": dict(live_screen) if live_screen else None,
        "created_at": created_at,
    }
    amendment = _jsonable(amendment)
    amendment["amendment_fingerprint"] = amendment_4_fingerprint(amendment)
    return amendment


AMENDMENT_4_EXCLUDED_FIELDS = AMENDMENT_3_EXCLUDED_FIELDS


def amendment_4_fingerprint(amendment: Mapping) -> str:
    payload = {key: value for key, value in amendment.items()
               if key not in AMENDMENT_4_EXCLUDED_FIELDS}
    return _sha256(_canonical(_jsonable(payload)))
