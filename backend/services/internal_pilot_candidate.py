"""INTERNAL_PILOT candidate contract (freeze amendment 5) and its DB-aware preflight.

Amendments 1-4 and the original freeze stay immutable history. Amendment 5 pins
the pilot *candidate*: Luna as Intent Analyzer (explicit reasoning "none",
max_tokens 600), the unchanged gpt-4o-mini selector served with the
``variant_a_v2`` prompt, the unchanged strict parser, and a 500-character chat
input limit shared by the widget and the backend.

Why a separate preflight
------------------------
``internal_pilot_freeze.run_preflight`` and
``internal_pilot_runtime.run_runtime_preflight`` resolve capability config
from *environment variables*. The runtime, however, serves the **managed
config stored in the database** (``services.ai_registry.load_active_config``).
Neither older preflight ever looked at it, so switching the analyzer back to
gpt-4o-mini, or its budget back to 300, from the admin API passed silently.
``run_candidate_preflight`` compares the DB active config for both
capabilities, the served selector prompt, the live ``LLM_ENABLED`` value, both
input limits and the strict-parser contract against this amendment.

Nothing here mutates configuration. Collecting live state only reads.
"""
from __future__ import annotations

import ast
import inspect
import io
import re
import tokenize
from dataclasses import dataclass, field
from typing import Mapping, Optional

from services.internal_pilot_freeze import (
    AMENDMENT_4_ID,
    CheckResult,
    MILESTONE,
    PARENT_FREEZE_FINGERPRINT,
    PROVIDER_POLICY,
    PreflightReport,
    SCHEMA_VERSION,
    SELECTOR_CONFIG_FINGERPRINT,
    SELECTOR_MAX_TOKENS,
    SELECTOR_MODEL,
    SELECTOR_PROVIDER,
    SELECTOR_TEMPERATURE,
    _canonical,
    _jsonable,
    _sha256,
    intent_analyzer_prompt_fingerprint,
    repo_root,
)
from services.llm_config import EffectiveLLMConfig, LLMCapability, ReasoningEffort

AMENDMENT_5_ID = "INTERNAL_PILOT_FREEZE_AMENDMENT_5"
AMENDMENT_4_FINGERPRINT = (
    "f92d8eb2a7e809ae15e256280cbe7c707480e2b261553df15aa62707a8cfee98"
)

# ── Intent Analyzer (managed config, DB active version) ─────────────────────
ANALYZER_PROVIDER = "openrouter"
ANALYZER_MODEL = "openai/gpt-6-luna"
# Explicit "none": the adapter sends reasoning.effort="none". Every Luna
# measurement used this request; an UNSET value (no reasoning field) is a
# different, unmeasured request with a different config fingerprint.
ANALYZER_REASONING = ReasoningEffort.NONE
ANALYZER_TEMPERATURE = 0.0
ANALYZER_MAX_TOKENS = 600
ANALYZER_CONFIG_FINGERPRINT = (
    "0181aa63cac4912085dff1934c09674c59d80815cdfdefc8a2f5c2d4a279e835"
)

# Rollback target for the analyzer: gpt-4o-mini at the SAME 600 budget. The
# registry's original version 1 (gpt-4o-mini @300) truncates 500-character
# inputs and is therefore not a valid rollback under the pilot input policy.
ROLLBACK_ANALYZER_MODEL = "openai/gpt-4o-mini"
ROLLBACK_ANALYZER_CONFIG_FINGERPRINT = (
    "512fe08df368e629a4960ea8f55bfa19874f49cf12f12b58ed8a681b00ab5ee3"
)

LUNA_REGISTRY_ENTRY = {
    "display_name": "GPT-6 Luna (OpenRouter)",
    "provider": ANALYZER_PROVIDER,
    "model_identifier": ANALYZER_MODEL,
    "allowed_capabilities": ["intent_analyzer"],
    "supports_structured_output": True,
    "supports_reasoning_effort": True,
    "allowed_reasoning_efforts": ["none"],
}
LUNA_QUALIFICATION_REFERENCE = (
    "LUNA_QUALIFICATION_REPORT 2026-09-25 + PILOT_CANDIDATE_VALIDATION 2026-09-27"
)

# ── Selector (model/config unchanged; prompt variant_a_v2) ──────────────────
SELECTOR_PROMPT_VERSION = "variant_a_v2"
SELECTOR_PROMPT_FINGERPRINT = (
    "d9c8be9b6b519265f3cfff6d5f9c697844b9832e3f05bc0192a7a96fd18018b1"
)
ROLLBACK_SELECTOR_PROMPT_VERSION = "variant_a_v1"
ROLLBACK_SELECTOR_PROMPT_FINGERPRINT = (
    "1aed568885db02f474534224695a45eb6f95835bc31f94e877af9659efe94a1e"
)

# ── Input policy ────────────────────────────────────────────────────────────
CHAT_INPUT_MAX_CHARS = 500

# ── Strict parser contract ──────────────────────────────────────────────────
PARSER_STRICTNESS = "strict=True, extra=forbid, Literal[1, 2]"
_PARSER_FUNCTIONS = (
    "parse_intent_analysis",
    "_collapse",
    "_tokens",
    "_token_supported",
    "_tokens_are_supported",
    "_uses_context_tokens",
)


def expected_analyzer_config() -> EffectiveLLMConfig:
    return EffectiveLLMConfig(
        capability=LLMCapability.INTENT_ANALYZER,
        provider=ANALYZER_PROVIDER,
        model=ANALYZER_MODEL,
        reasoning_effort=ANALYZER_REASONING,
        temperature=ANALYZER_TEMPERATURE,
        max_tokens=ANALYZER_MAX_TOKENS,
    )


def parser_contract_fingerprint() -> str:
    """Identity of the strict Intent Analyzer parser: schema + invariant code.

    The code is hashed as its token stream without comments or layout
    tokens, so comments and formatting do not move it (and neither does the
    Python version: the pilot container and a developer host differ), while
    any change to a validation rule, the output schema or the strict/extra
    model configuration does.
    """
    from services import intent_analyzer
    from services.llm_types import IntentAnalysis, IntentItem

    code = {}
    for name in _PARSER_FUNCTIONS:
        code[name] = _code_tokens(inspect.getsource(getattr(intent_analyzer, name)))
    payload = {
        "schema": IntentAnalysis.model_json_schema(),
        "model_config": {
            "IntentAnalysis": {k: IntentAnalysis.model_config.get(k)
                               for k in ("strict", "extra")},
            "IntentItem": {k: IntentItem.model_config.get(k)
                           for k in ("strict", "extra", "str_strip_whitespace")},
        },
        "max_previous_user_turns": intent_analyzer.MAX_PREVIOUS_USER_TURNS,
        "code": code,
    }
    return _sha256(_canonical(payload))


_LAYOUT_TOKENS = {tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT,
                  tokenize.DEDENT, tokenize.ENCODING, tokenize.ENDMARKER}


def _code_tokens(source: str) -> str:
    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    return " ".join(tok.string for tok in tokens if tok.type not in _LAYOUT_TOKENS)


# Recorded when amendment 5 was cut; the preflight fails if the parser moves.
PARSER_CONTRACT_FINGERPRINT = (
    "1ab2f476efa039392815b3db8bb12cb5570ebe9d898f0884e38ff4dd24b584bc"
)


# ── Input limits, read statically ───────────────────────────────────────────


def backend_chat_max_chars() -> Optional[int]:
    """The ``max_length`` of ``WidgetChatRequest.message`` in routers/chat.py.

    Static: importing ``routers.chat`` loads the answer pipeline (and with it
    the embedding model), which a preflight must not do. Only the constant
    name ``CHAT_MESSAGE_MAX_CHARS`` (resolved from ``core.limits``) or an
    integer literal is accepted.
    """
    from core.limits import CHAT_MESSAGE_MAX_CHARS

    path = repo_root() / "backend" / "routers" / "chat.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ClassDef) and node.name == "WidgetChatRequest"):
            continue
        for stmt in node.body:
            if not (isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
                    and stmt.target.id == "message" and isinstance(stmt.value, ast.Call)):
                continue
            for keyword in stmt.value.keywords:
                if keyword.arg != "max_length":
                    continue
                value = keyword.value
                if isinstance(value, ast.Constant) and isinstance(value.value, int):
                    return value.value
                if isinstance(value, ast.Name) and value.id == "CHAT_MESSAGE_MAX_CHARS":
                    return CHAT_MESSAGE_MAX_CHARS
    return None


_WIDGET_INPUT = re.compile(r'<textarea id="w-input"[^>]*\bmaxlength="(\d+)"')


def widget_chat_max_chars() -> Optional[int]:
    path = repo_root() / "chatbot-web" / "src" / "widget.js"
    match = _WIDGET_INPUT.search(path.read_text(encoding="utf-8"))
    return int(match.group(1)) if match else None


# ── Amendment 5 ─────────────────────────────────────────────────────────────

AMENDMENT_5_EXCLUDED_FIELDS = (
    "created_at", "git_commit", "amendment_fingerprint", "gate_evidence",
)


def build_freeze_amendment_5(*, git_commit: str, created_at: str,
                             gate_evidence: Optional[Mapping] = None) -> dict:
    amendment = {
        "schema_version": SCHEMA_VERSION,
        "amendment_id": AMENDMENT_5_ID,
        "milestone": MILESTONE,
        "parent_amendment_id": AMENDMENT_4_ID,
        "parent_amendment_fingerprint": AMENDMENT_4_FINGERPRINT,
        "historical_parent_freeze_fingerprint": PARENT_FREEZE_FINGERPRINT,
        "parents_are_immutable": True,
        "git_commit": git_commit,
        "classification": {
            "pilot_candidate_contract": True,
            "intent_analyzer_model_change": True,
            "intent_analyzer_prompt_change": True,
            "intent_analyzer_budget_change": True,
            "selector_prompt_change": True,
            "selector_model_change": False,
            "selector_config_change": False,
            "parser_contract_change": False,
            "retrieval_change": False,
            "eligibility_or_threshold_change": False,
            "candidate_budget_change": False,
            "provider_retry_or_deadline_change": False,
            "input_policy_change": True,
        },
        "rationale": (
            "Pilot candidate per PILOT_CANDIDATE_VALIDATION (2026-09-27): Luna "
            "analyzer with explicit reasoning none and a 600-token budget sized "
            "for a 500-character input limit; analyzer prompt states the strict "
            "parser's source/segment/word-provenance contract; selector "
            "variant_a_v2 adds narrow premise-correction, over-specific, "
            "relative-time and safe fallback rules. Parser unchanged."
        ),
        "intent_analyzer": {
            "provider": ANALYZER_PROVIDER,
            "model": ANALYZER_MODEL,
            "reasoning_effort": ANALYZER_REASONING.value,
            "reasoning_transmitted": True,
            "temperature": ANALYZER_TEMPERATURE,
            "max_tokens": ANALYZER_MAX_TOKENS,
            "timeout_seconds": None,
            "max_retries": None,
            "config_fingerprint": ANALYZER_CONFIG_FINGERPRINT,
            "prompt_fingerprint": intent_analyzer_prompt_fingerprint(),
            "config_source": "DB active managed config (services.ai_registry)",
            "registry_entry": dict(LUNA_REGISTRY_ENTRY),
            "qualification_reference": LUNA_QUALIFICATION_REFERENCE,
        },
        "selector": {
            "provider": SELECTOR_PROVIDER,
            "model": SELECTOR_MODEL,
            "temperature": SELECTOR_TEMPERATURE,
            "max_tokens": SELECTOR_MAX_TOKENS,
            "reasoning_effort": None,
            "config_fingerprint": SELECTOR_CONFIG_FINGERPRINT,
            "prompt_version": SELECTOR_PROMPT_VERSION,
            "prompt_fingerprint": SELECTOR_PROMPT_FINGERPRINT,
            "prompt_version_env": "SELECTOR_PROMPT_VERSION",
        },
        "parser": {
            "strictness": PARSER_STRICTNESS,
            "contract_fingerprint": PARSER_CONTRACT_FINGERPRINT,
            "parser_v3": False,
        },
        "input_policy": {
            "widget_max_chars": CHAT_INPUT_MAX_CHARS,
            "backend_max_chars": CHAT_INPUT_MAX_CHARS,
            "over_limit_behaviour": "HTTP 422 request validation",
        },
        "rollback": {
            "intent_analyzer_model": ROLLBACK_ANALYZER_MODEL,
            "intent_analyzer_max_tokens": ANALYZER_MAX_TOKENS,
            "intent_analyzer_config_fingerprint": ROLLBACK_ANALYZER_CONFIG_FINGERPRINT,
            "intent_analyzer_mechanism": (
                "POST /api/ai-config/config/rollback/<ROLLBACK_VERSION> printed by "
                "deploy/internal-pilot/activate-luna.py; never version 1 (gpt-4o-mini @300)"
            ),
            "selector_prompt_version": ROLLBACK_SELECTOR_PROMPT_VERSION,
            "selector_prompt_fingerprint": ROLLBACK_SELECTOR_PROMPT_FINGERPRINT,
            "selector_mechanism": "SELECTOR_PROMPT_VERSION=variant_a_v1 and restart",
        },
        "preserved": {
            "selector_config_fingerprint": SELECTOR_CONFIG_FINGERPRINT,
            "provider_policy": dict(PROVIDER_POLICY),
            "candidate_order": "production",
            "attempt_timeout_seconds": {"intent_analyzer": 20.0, "selector": 10.0},
            "logical_deadline_seconds": {"intent_analyzer": 30.0, "selector": 15.0},
        },
        "gate_evidence": dict(gate_evidence) if gate_evidence else None,
        "created_at": created_at,
    }
    amendment = _jsonable(amendment)
    amendment["amendment_fingerprint"] = amendment_5_fingerprint(amendment)
    return amendment


def amendment_5_fingerprint(amendment: Mapping) -> str:
    payload = {key: value for key, value in amendment.items()
               if key not in AMENDMENT_5_EXCLUDED_FIELDS}
    return _sha256(_canonical(_jsonable(payload)))


# ── Live state + preflight ──────────────────────────────────────────────────


@dataclass(frozen=True)
class LiveCandidateState:
    """Everything the candidate preflight compares, gathered read-only."""

    analyzer: Optional[object] = None      # ai_registry.CapabilityAssignment
    selector: Optional[object] = None      # ai_registry.CapabilityAssignment
    config_version: Optional[int] = None
    config_error: str = ""
    llm_enabled: Optional[bool] = None
    selector_prompt_version: str = "unresolved"
    selector_prompt_fingerprint: str = "unresolved"
    backend_max_chars: Optional[int] = None
    widget_max_chars: Optional[int] = None
    parser_fingerprint: str = ""
    extra: dict = field(default_factory=dict)


def collect_live_state(db, environ: Optional[Mapping[str, str]] = None) -> LiveCandidateState:
    """Read the live runtime: DB active config, LLM_ENABLED, served prompt.

    ``environ`` defaults to the process environment, i.e. what the running
    backend actually uses to pick the selector prompt.
    """
    from core.database import SystemConfig
    from services import ai_registry
    from services.selector_prompt_catalog import (
        UnknownSelectorPromptVersion,
        resolve_selector_prompt,
    )

    analyzer = selector = None
    version = None
    config_error = ""
    try:
        active = ai_registry.load_active_config(db)
        if active is None:
            config_error = "no managed config (registry never bootstrapped)"
        else:
            version = active.version_id
            analyzer = active.assignments[LLMCapability.INTENT_ANALYZER]
            selector = active.assignments[LLMCapability.SELECTOR]
    except ai_registry.AIConfigError as exc:
        config_error = f"{exc.code}: {exc.message}"

    row = db.query(SystemConfig).filter(SystemConfig.key == "LLM_ENABLED").first()
    llm_enabled = None if row is None else (row.value or "").strip().lower() == "true"

    try:
        prompt = resolve_selector_prompt(environ)
        prompt_version, prompt_fp = prompt.version, prompt.fingerprint
    except UnknownSelectorPromptVersion:
        prompt_version = prompt_fp = "unresolved"

    return LiveCandidateState(
        analyzer=analyzer,
        selector=selector,
        config_version=version,
        config_error=config_error,
        llm_enabled=llm_enabled,
        selector_prompt_version=prompt_version,
        selector_prompt_fingerprint=prompt_fp,
        backend_max_chars=backend_chat_max_chars(),
        widget_max_chars=widget_chat_max_chars(),
        parser_fingerprint=parser_contract_fingerprint(),
    )


def run_candidate_preflight(state: LiveCandidateState) -> PreflightReport:
    """Compare live state against amendment 5. Any mismatch is a FAIL."""
    checks: list[CheckResult] = []
    add = checks.append

    add(CheckResult(
        "managed_config_loaded",
        state.analyzer is not None and state.selector is not None,
        "DB active managed config for intent_analyzer and selector",
        f"version {state.config_version}" if state.analyzer is not None
        else (state.config_error or "missing"),
    ))

    def _cap(prefix, assignment, name, ok, expected, actual, detail=""):
        full = f"{prefix}_{name}"
        if assignment is None:
            add(CheckResult(full, False, expected, "unresolved", detail))
        else:
            add(CheckResult(full, ok(assignment), expected, actual(assignment), detail))

    a = state.analyzer
    _cap("analyzer", a, "provider_matches", lambda x: x.model.provider == ANALYZER_PROVIDER,
         ANALYZER_PROVIDER, lambda x: x.model.provider)
    _cap("analyzer", a, "model_matches",
         lambda x: x.model.model_identifier == ANALYZER_MODEL,
         ANALYZER_MODEL, lambda x: x.model.model_identifier)
    _cap("analyzer", a, "reasoning_is_explicit_none",
         lambda x: x.params.reasoning_effort == ANALYZER_REASONING.value,
         '"none" (reasoning.effort="none" is transmitted)',
         lambda x: "unset" if x.params.reasoning_effort is None else x.params.reasoning_effort,
         detail="Unset reasoning is a different, unmeasured Luna request.")
    _cap("analyzer", a, "max_tokens_matches",
         lambda x: x.params.max_tokens == ANALYZER_MAX_TOKENS,
         str(ANALYZER_MAX_TOKENS), lambda x: str(x.params.max_tokens),
         detail="300 truncates 500-character inputs.")
    _cap("analyzer", a, "temperature_matches",
         lambda x: float(x.params.temperature) == ANALYZER_TEMPERATURE,
         str(ANALYZER_TEMPERATURE), lambda x: str(x.params.temperature))
    _cap("analyzer", a, "config_fingerprint_matches",
         lambda x: x.effective_config().fingerprint == ANALYZER_CONFIG_FINGERPRINT,
         ANALYZER_CONFIG_FINGERPRINT, lambda x: x.effective_config().fingerprint)
    _cap("analyzer", a, "model_is_qualified_and_enabled",
         lambda x: x.model.enabled and x.model.qualification_status.value == "QUALIFIED",
         "enabled, QUALIFIED",
         lambda x: f"{'enabled' if x.model.enabled else 'disabled'}, "
                   f"{x.model.qualification_status.value}")
    _cap("analyzer", a, "model_restricted_to_analyzer",
         lambda x: list(x.model.allowed_capabilities) == ["intent_analyzer"],
         '["intent_analyzer"]', lambda x: str(list(x.model.allowed_capabilities)),
         detail="Luna must not be assignable to the selector.")

    s = state.selector
    _cap("selector", s, "provider_matches", lambda x: x.model.provider == SELECTOR_PROVIDER,
         SELECTOR_PROVIDER, lambda x: x.model.provider)
    _cap("selector", s, "model_matches", lambda x: x.model.model_identifier == SELECTOR_MODEL,
         SELECTOR_MODEL, lambda x: x.model.model_identifier)
    _cap("selector", s, "max_tokens_matches",
         lambda x: x.params.max_tokens == SELECTOR_MAX_TOKENS,
         str(SELECTOR_MAX_TOKENS), lambda x: str(x.params.max_tokens))
    _cap("selector", s, "reasoning_is_unset",
         lambda x: x.params.reasoning_effort is None,
         "unset", lambda x: str(x.params.reasoning_effort))
    _cap("selector", s, "config_fingerprint_matches",
         lambda x: x.effective_config().fingerprint == SELECTOR_CONFIG_FINGERPRINT,
         SELECTOR_CONFIG_FINGERPRINT, lambda x: x.effective_config().fingerprint)

    add(CheckResult(
        "selector_prompt_version_matches",
        state.selector_prompt_version == SELECTOR_PROMPT_VERSION,
        SELECTOR_PROMPT_VERSION, state.selector_prompt_version,
        detail="Resolved through the runtime catalog from SELECTOR_PROMPT_VERSION.",
    ))
    add(CheckResult(
        "selector_prompt_fingerprint_matches",
        state.selector_prompt_fingerprint == SELECTOR_PROMPT_FINGERPRINT,
        SELECTOR_PROMPT_FINGERPRINT, state.selector_prompt_fingerprint,
    ))

    policy = PROVIDER_POLICY["inference_provider"]
    providers = [x.model.provider for x in (a, s) if x is not None]
    add(CheckResult(
        "provider_policy_openrouter_only",
        len(providers) == 2 and all(p == policy for p in providers),
        policy, ", ".join(providers) or "unresolved",
    ))

    add(CheckResult(
        "llm_enabled_live_is_true",
        state.llm_enabled is True,
        "true",
        "missing" if state.llm_enabled is None else str(state.llm_enabled).lower(),
        detail="Live SystemConfig value, not the seed default.",
    ))

    add(CheckResult(
        "backend_chat_input_max_matches",
        state.backend_max_chars == CHAT_INPUT_MAX_CHARS,
        str(CHAT_INPUT_MAX_CHARS), str(state.backend_max_chars),
    ))
    add(CheckResult(
        "widget_chat_input_max_matches",
        state.widget_max_chars == CHAT_INPUT_MAX_CHARS,
        str(CHAT_INPUT_MAX_CHARS), str(state.widget_max_chars),
    ))
    add(CheckResult(
        "input_limits_are_equal",
        state.backend_max_chars is not None
        and state.backend_max_chars == state.widget_max_chars,
        "widget max == backend max",
        f"widget {state.widget_max_chars}, backend {state.backend_max_chars}",
    ))

    add(CheckResult(
        "analyzer_prompt_fingerprint_matches",
        intent_analyzer_prompt_fingerprint() == _frozen_analyzer_prompt_fingerprint(),
        _frozen_analyzer_prompt_fingerprint(),
        intent_analyzer_prompt_fingerprint(),
    ))
    add(CheckResult(
        "strict_parser_contract_unchanged",
        state.parser_fingerprint == PARSER_CONTRACT_FINGERPRINT,
        PARSER_CONTRACT_FINGERPRINT, state.parser_fingerprint,
        detail="Parser V3 / relaxed invariants are out of the pilot scope.",
    ))
    return PreflightReport(tuple(checks))


AMENDMENT_5_PATH = ("deploy", "internal-pilot", "answer-pipeline-freeze-amendment-5.json")


def _frozen_analyzer_prompt_fingerprint() -> str:
    """The analyzer prompt fingerprint recorded in the committed amendment 5."""
    import json

    path = repo_root().joinpath(*AMENDMENT_5_PATH)
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
        return stored["intent_analyzer"]["prompt_fingerprint"]
    except (OSError, ValueError, KeyError):
        return "missing amendment-5 artifact"
