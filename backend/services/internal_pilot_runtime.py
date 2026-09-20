"""Internal-pilot RUNTIME preflight: does the runtime serve the frozen baseline?

Two distinct axes, deliberately not merged (see the freeze document §31):

``Internal Pilot Freeze``
    Is the frozen decision recorded correctly, deterministically and
    reproducibly? Owned by ``services.internal_pilot_freeze``. Its manifest
    and fingerprint are **authoritative and immutable** — they describe what
    was decided, including the observability gaps that existed at freeze
    time. Closing a gap does not rewrite history, so nothing here touches
    that manifest or its fingerprint.

``Internal Pilot Runtime Preflight``
    Does the *current runtime*, configured as the pilot will configure it,
    actually behave that way? That is this module.

Everything here resolves real runtime behaviour rather than trusting
configuration strings: the selector prompt comes from the runtime catalog
resolver, the reasoning omission is checked on the built provider request
payload, and the trace capabilities are checked on a real ``DecisionTrace``.

Nothing here mutates configuration or touches a database.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Mapping, Optional

from services.internal_pilot_freeze import (
    CheckResult,
    PreflightReport,
    SELECTOR_CONFIG_FINGERPRINT,
    SELECTOR_MAX_TOKENS,
    SELECTOR_MODEL,
    SELECTOR_PROMPT_FINGERPRINT,
    SELECTOR_PROVIDER,
    SELECTOR_REASONING_LABEL,
    SELECTOR_TEMPERATURE,
    repo_root,
)
from services.llm_config import (
    LLMCapability,
    reasoning_request_fields,
    resolve_capability_config,
)
from services.selector_prompt_catalog import (
    UnknownSelectorPromptVersion,
    resolve_selector_prompt,
)

#: The pilot's desired, non-secret configuration. Preflight validates what the
#: pilot stack will resolve, not whatever happens to be in the caller's shell.
PILOT_ENV_FILE = Path("deploy") / "internal-pilot" / "pilot.env.example"

PILOT_PROMPT_VERSION = "variant_a_v1"
REQUIRED_TRACE_REQUEST_FIELDS = ("request_id", "conversation_id", "timestamp")
REQUIRED_TRACE_SOURCES = ("calendar", "meili", "qdrant")

#: Packages that runtime code must never import: the pilot has to run without
#: the benchmark tooling present at all.
RUNTIME_PACKAGES = ("services", "core", "routers", "admin", "scripts")
FORBIDDEN_RUNTIME_IMPORT = "benchmarks"


def load_pilot_env(path: Optional[Path] = None) -> dict:
    """Parse the pilot env artifact into a plain mapping (no secrets in it)."""
    target = repo_root() / PILOT_ENV_FILE if path is None else path
    env: dict = {}
    for line in target.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        env[key.strip()] = value.strip()
    return env


def runtime_modules_importing_benchmarks() -> list[str]:
    """Runtime files that import the benchmark package, by static analysis.

    Static rather than dynamic: an import that only happens on some code path
    would still make the pilot depend on tooling it must not need.
    """
    offenders: list[str] = []
    backend = repo_root() / "backend"
    for package in RUNTIME_PACKAGES:
        for path in sorted((backend / package).rglob("*.py")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (OSError, SyntaxError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                if any(
                    name == FORBIDDEN_RUNTIME_IMPORT
                    or name.startswith(FORBIDDEN_RUNTIME_IMPORT + ".")
                    for name in names
                ):
                    offenders.append(str(path.relative_to(repo_root())))
                    break
    return offenders


def selector_request_reasoning_fields(config) -> dict:
    """The provider request fields a selector call would actually carry.

    Checked at payload level, not by comparing config fingerprints: the whole
    point is that no ``reasoning`` key reaches the wire.
    """
    return reasoning_request_fields(config.provider, config.reasoning_effort)


def _trace_capabilities() -> dict:
    """Inspect a real DecisionTrace payload.

    ``decision_trace`` imports only ``llm_config`` and ``llm_types``, so this
    is cheap — unlike ``answer_pipeline``, which loads the embedding model.
    """
    from services.decision_trace import DecisionTrace, SourceAvailability

    trace = DecisionTrace(endpoint="preflight", conversation_id=1)
    payload = trace.to_dict()
    return {
        "request": payload.get("request", {}),
        "source_availability": payload.get("source_availability", {}),
        "schema_version": payload.get("schema_version"),
        "states": {item.value for item in SourceAvailability},
        "retrieval_ms_supported": _snapshot_supports_retrieval_ms(),
    }


def _snapshot_supports_retrieval_ms() -> bool:
    """Does the candidate-set snapshot carry retrieval_ms and availability?"""
    from services.candidate_eligibility import build_candidate_set

    build = build_candidate_set(
        calendar_entries=(),
        qna_hits=(),
        routing_policy=None,
        active_qna_lookup=None,
        max_candidates=8,
        retrieval_ms=1.5,
        qdrant_available=True,
        meili_available=False,
    )
    snapshot = build.trace_snapshot
    return (
        snapshot.get("retrieval_ms") == 1.5
        and snapshot.get("qdrant_availability") == "available"
        and snapshot.get("meili_availability") == "unavailable"
    )


def run_runtime_preflight(
    environ: Optional[Mapping[str, str]] = None,
) -> PreflightReport:
    """Validate the runtime against the frozen pilot baseline.

    ``environ`` defaults to the committed pilot configuration artifact.
    """
    env = dict(load_pilot_env() if environ is None else environ)
    checks: list[CheckResult] = []

    # ── BLOCKER-1: selector prompt ──────────────────────────────────────────
    try:
        prompt = resolve_selector_prompt(env)
        prompt_version, prompt_fp = prompt.version, prompt.fingerprint
        prompt_error = ""
    except UnknownSelectorPromptVersion as exc:
        prompt_version, prompt_fp, prompt_error = "unresolved", "unresolved", str(exc)

    checks.append(
        CheckResult(
            "selector_prompt_version_is_variant_a",
            prompt_version == PILOT_PROMPT_VERSION,
            PILOT_PROMPT_VERSION,
            prompt_version,
            detail=prompt_error,
        )
    )
    checks.append(
        CheckResult(
            "selector_prompt_fingerprint_matches",
            prompt_fp == SELECTOR_PROMPT_FINGERPRINT,
            SELECTOR_PROMPT_FINGERPRINT,
            prompt_fp,
            detail="Resolved through the real runtime catalog, not the env string.",
        )
    )

    offenders = runtime_modules_importing_benchmarks()
    checks.append(
        CheckResult(
            "runtime_does_not_import_benchmarks",
            not offenders,
            "no runtime module imports 'benchmarks'",
            "clean" if not offenders else ", ".join(offenders),
        )
    )

    # ── Selector capability config ──────────────────────────────────────────
    config = None
    resolve_error = ""
    try:
        config = resolve_capability_config(LLMCapability.SELECTOR, environ=env)
    except (ValueError, RuntimeError) as exc:
        resolve_error = str(exc)

    checks.append(
        CheckResult(
            "selector_config_resolvable",
            config is not None,
            "resolvable selector capability config",
            "resolved" if config is not None else f"error: {resolve_error}",
        )
    )

    def _cfg(name, ok, expected, actual, detail=""):
        if config is None:
            checks.append(CheckResult(name, False, expected, "unresolved", detail))
        else:
            checks.append(CheckResult(name, ok(), expected, actual(), detail))

    _cfg("selector_provider_matches", lambda: config.provider == SELECTOR_PROVIDER,
         SELECTOR_PROVIDER, lambda: config.provider)
    _cfg("selector_model_matches", lambda: config.model == SELECTOR_MODEL,
         SELECTOR_MODEL, lambda: config.model)
    _cfg("selector_temperature_matches",
         lambda: float(config.temperature) == SELECTOR_TEMPERATURE,
         str(SELECTOR_TEMPERATURE), lambda: str(config.temperature))
    _cfg("selector_max_tokens_matches",
         lambda: int(config.max_tokens) == SELECTOR_MAX_TOKENS,
         str(SELECTOR_MAX_TOKENS), lambda: str(config.max_tokens))
    _cfg("selector_config_fingerprint_matches",
         lambda: config.fingerprint == SELECTOR_CONFIG_FINGERPRINT,
         SELECTOR_CONFIG_FINGERPRINT, lambda: config.fingerprint)
    _cfg("selector_reasoning_is_unset",
         lambda: config.reasoning_effort is None,
         SELECTOR_REASONING_LABEL,
         lambda: "unset" if config.reasoning_effort is None
         else config.reasoning_effort.value,
         detail="ReasoningEffort.NONE is a different, unvalidated request.")

    reasoning_fields = (
        selector_request_reasoning_fields(config) if config is not None else {"unresolved": True}
    )
    emitted_reasoning = bool(reasoning_fields)
    checks.append(
        CheckResult(
            "selector_request_omits_reasoning_key",
            config is not None and not emitted_reasoning,
            "no reasoning key in the provider request payload",
            "omitted" if config is not None and not emitted_reasoning
            else f"emitted: {sorted(reasoning_fields)}",
            detail="Checked on the built request payload, not on config equality.",
        )
    )

    # ── BLOCKER-2: initial LLM state ────────────────────────────────────────
    from scripts.init_system import llm_enabled_default

    try:
        desired = llm_enabled_default(env)
        seed_error = ""
    except RuntimeError as exc:
        desired, seed_error = "invalid", str(exc)
    checks.append(
        CheckResult(
            "pilot_fresh_llm_enabled_is_true",
            desired == "true",
            "true",
            desired,
            detail=seed_error or (
                "Seeded on a fresh install only; an existing admin value "
                "(including emergency off) is never overwritten."
            ),
        )
    )

    # ── BLOCKER-3: observability ────────────────────────────────────────────
    caps = _trace_capabilities()
    request_fields = caps["request"]
    missing = [f for f in REQUIRED_TRACE_REQUEST_FIELDS if f not in request_fields]
    checks.append(
        CheckResult(
            "trace_has_correlation_and_timestamp",
            not missing,
            ", ".join(REQUIRED_TRACE_REQUEST_FIELDS),
            "all present" if not missing else f"missing: {', '.join(missing)}",
        )
    )

    timestamp = request_fields.get("timestamp") or ""
    tz_aware = timestamp.endswith("Z") and "T" in timestamp
    checks.append(
        CheckResult(
            "trace_timestamp_is_utc_iso8601",
            tz_aware,
            "timezone-aware UTC ISO-8601 (…Z)",
            timestamp or "missing",
        )
    )

    missing_sources = [s for s in REQUIRED_TRACE_SOURCES
                       if s not in caps["source_availability"]]
    checks.append(
        CheckResult(
            "trace_has_source_availability",
            not missing_sources,
            ", ".join(REQUIRED_TRACE_SOURCES),
            "all present" if not missing_sources
            else f"missing: {', '.join(missing_sources)}",
        )
    )

    expected_states = {"available", "unavailable", "skipped"}
    checks.append(
        CheckResult(
            "source_availability_states_are_typed",
            caps["states"] == expected_states,
            ", ".join(sorted(expected_states)),
            ", ".join(sorted(caps["states"])),
            detail="0 results is 'available', never 'unavailable'.",
        )
    )

    checks.append(
        CheckResult(
            "trace_has_retrieval_ms",
            caps["retrieval_ms_supported"],
            "retrieval_ms + per-source availability on the retrieval snapshot",
            "present" if caps["retrieval_ms_supported"] else "missing",
            detail=(
                "retrieval_ms covers the QnA retrieval leg (Qdrant + Meili). "
                "Calendar resolves upstream and selector latency is separate."
            ),
        )
    )

    checks.append(
        CheckResult(
            "trace_schema_version_bumped",
            (caps["schema_version"] or 0) >= 7,
            ">= 7",
            str(caps["schema_version"]),
            detail="v7 adds timestamp, source availability and retrieval_ms.",
        )
    )

    return PreflightReport(tuple(checks))
