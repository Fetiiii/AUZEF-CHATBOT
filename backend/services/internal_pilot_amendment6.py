"""INTERNAL_PILOT freeze amendment 6: Luna + variant_a_v3_contract selector, Luna analyzer
without an unsupported temperature field, frozen KB/search expectations, trace v8.

Status: INTERNAL_PILOT_CANDIDATE (user product decision 2026-09-29). Not
PRODUCTION_QUALIFIED and not FINAL_SELECTOR_QUALIFIED: the fresh Luna holdout
qualification was blocked (SELECTOR_V3_LUNA_FRESH_SEALED_HOLDOUT_QUALIFICATION.md).

Amendments 1-5 and the original freeze stay immutable. This module does not
import or change amendment 5's constants; it reads amendment 5's committed
artifact only to inherit the analyzer prompt/parser identity it pinned.

Preflight scopes
----------------
``live``   runs where the backend's DB, Meili and Qdrant are reachable (inside
           the backend container): DB managed config for both capabilities, the
           served selector prompt, LLM_ENABLED, KB fingerprint, index counts,
           analyzer prompt + parser identity, backend input limit, trace schema.
``static`` runs in a repository checkout: the committed amendment-6 artifact
           equals the builder output, the widget input limit, and the
           ``pilot.env.example`` prompt pin. A missing file is a FAIL, never a
           silent PASS.
Nothing here mutates configuration.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

from services.internal_pilot_freeze import (
    CheckResult,
    MILESTONE,
    PARENT_FREEZE_FINGERPRINT,
    PROVIDER_POLICY,
    PreflightReport,
    SCHEMA_VERSION,
    _canonical,
    _jsonable,
    _sha256,
    intent_analyzer_prompt_fingerprint,
)
from services.llm_config import (
    DEFAULT_ATTEMPT_TIMEOUT_SECONDS,
    LOGICAL_DEADLINE_SECONDS,
    EffectiveLLMConfig,
    LLMCapability,
    ReasoningEffort,
)

AMENDMENT_6_ID = "INTERNAL_PILOT_FREEZE_AMENDMENT_6"
AMENDMENT_5_ID = "INTERNAL_PILOT_FREEZE_AMENDMENT_5"
AMENDMENT_5_FINGERPRINT = "2e991cbae613f92b72f874a4c413c53553dc825993ed38b03cf562d3a11575de"
STATUS = "INTERNAL_PILOT_CANDIDATE"

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
AMENDMENT_5_REL = ("deploy", "internal-pilot", "answer-pipeline-freeze-amendment-5.json")
AMENDMENT_6_REL = ("deploy", "internal-pilot", "answer-pipeline-freeze-amendment-6.json")
PILOT_ENV_EXAMPLE_REL = ("deploy", "internal-pilot", "pilot.env.example")
WIDGET_REL = ("chatbot-web", "src", "widget.js")

# ── Luna (one registry entry serves both capabilities) ─────────────────────
PROVIDER = "openrouter"
LUNA_MODEL = "openai/gpt-6-luna"
LUNA_REGISTRY_ENTRY = {
    "display_name": "GPT-6 Luna (OpenRouter)",
    "provider": PROVIDER,
    "model_identifier": LUNA_MODEL,
    "allowed_capabilities": ["intent_analyzer", "selector"],
    "supports_structured_output": True,
    "supports_reasoning_effort": True,
    "allowed_reasoning_efforts": ["none"],
    # OpenRouter /api/v1/models supported_parameters for openai/gpt-6-luna
    # (fetched 2026-09-29) do not include temperature.
    "supports_temperature": False,
}

# ── Intent Analyzer: amendment-5 candidate, transport-only change ──────────
ANALYZER_MAX_TOKENS = 600
ANALYZER_REASONING = ReasoningEffort.NONE
ANALYZER_CONFIG_FINGERPRINT_AM5 = "0181aa63cac4912085dff1934c09674c59d80815cdfdefc8a2f5c2d4a279e835"
# Same model/reasoning/budget; temperature=None because Luna does not accept it
# (it was sent as 0.0 before and never applied by the model).
ANALYZER_CONFIG_FINGERPRINT = "e74b94a3bf521115c98018e1ec6519a906eba157ded0f47928969c21cc95df23"
# Inherited unchanged from amendment 5 (verified against its committed artifact
# by the builder and the static preflight; the live scope has no repo files).
ANALYZER_PROMPT_FINGERPRINT = "0d2c008633c854995580966b40970062963d5e79fc1491eb1270d2ea1ced9936"
PARSER_CONTRACT_FINGERPRINT = "1ab2f476efa039392815b3db8bb12cb5570ebe9d898f0884e38ff4dd24b584bc"

# ── Selector: variant_a_v3_contract + Luna ─────────────────────────────────
SELECTOR_PROMPT_VERSION = "variant_a_v3_contract"
SELECTOR_PROMPT_FINGERPRINT = "cdeea79518c6b20a26e65ac628af3a03e92a4bbd6d14802cc709e647643c46c7"
SELECTOR_MAX_TOKENS = 32          # selector contract value (llm_config default, v3 run manifests)
SELECTOR_REASONING = ReasoningEffort.NONE
SELECTOR_CONFIG_FINGERPRINT = "f6fcb61426cf400fb7f679190480e97e3f2d0b8ea58aecfca94a3440ecb89346"

ROLLBACK = {
    "selector_model": {
        "model": "openai/gpt-4o-mini", "config_fingerprint":
            "af9eb2d0767d37cd632799cbae39e7938585b243ceb4c7a1527b8028cd489a6e",
        "mechanism": "POST /api/ai-config/config/rollback/<PRE_ACTIVATION_VERSION> printed by "
                     "deploy/internal-pilot/activate-pilot-luna-v3.py",
    },
    "selector_prompt": {
        "version": "variant_a_v1",
        "fingerprint": "1aed568885db02f474534224695a45eb6f95835bc31f94e877af9659efe94a1e",
        "mechanism": "SELECTOR_PROMPT_VERSION=variant_a_v1 and restart",
    },
    "analyzer": {"model": "openai/gpt-4o-mini", "max_tokens": 600,
                 "config_fingerprint": "512fe08df368e629a4960ea8f55bfa19874f49cf12f12b58ed8a681b00ab5ee3"},
}

# ── Frozen data / search expectations (KB Freeze v1 + search rebuild) ──────
KB_FINGERPRINT = "b53e0458a0baec83518a989a1515f197ce83568f939ec43c8a3d2e861bf903ab"
KB_ACTIVE_QNA = 322
KB_ACTIVE_QUERIES = 2667
MEILI_INDEX = "auzef_qna_index"
MEILI_DOCUMENTS = 322
QDRANT_COLLECTION = "auzef_qna_vectors"
QDRANT_POINTS = 2989
DECISION_TRACE_SCHEMA = 8
CHAT_INPUT_MAX_CHARS = 500


def expected_config(capability: LLMCapability) -> EffectiveLLMConfig:
    if capability is LLMCapability.SELECTOR:
        return EffectiveLLMConfig(capability, PROVIDER, LUNA_MODEL, SELECTOR_REASONING, None, SELECTOR_MAX_TOKENS)
    return EffectiveLLMConfig(capability, PROVIDER, LUNA_MODEL, ANALYZER_REASONING, None, ANALYZER_MAX_TOKENS)


def amendment_5_identity(root: Optional[Path] = None) -> dict:
    """Analyzer prompt + parser identity pinned by the committed amendment 5."""
    path = (root or REPO_ROOT).joinpath(*AMENDMENT_5_REL)
    stored = json.loads(path.read_text(encoding="utf-8"))
    return {"analyzer_prompt_fingerprint": stored["intent_analyzer"]["prompt_fingerprint"],
            "parser_contract_fingerprint": stored["parser"]["contract_fingerprint"],
            "parser_strictness": stored["parser"]["strictness"],
            "amendment_fingerprint": stored["amendment_fingerprint"]}


EXCLUDED_FIELDS = ("created_at", "git_commit", "amendment_fingerprint", "gate_evidence")


def build_freeze_amendment_6(*, git_commit: str, created_at: str, gate_evidence: Optional[Mapping] = None,
                             root: Optional[Path] = None) -> dict:
    am5 = amendment_5_identity(root)
    if am5["amendment_fingerprint"] != AMENDMENT_5_FINGERPRINT:
        raise ValueError("amendment 5 artifact changed; amendments are immutable")
    if (am5["analyzer_prompt_fingerprint"], am5["parser_contract_fingerprint"]) != (
            ANALYZER_PROMPT_FINGERPRINT, PARSER_CONTRACT_FINGERPRINT):
        raise ValueError("inherited analyzer prompt/parser identity differs from amendment 5")
    amendment = {
        "schema_version": SCHEMA_VERSION,
        "amendment_id": AMENDMENT_6_ID,
        "milestone": MILESTONE,
        "status": STATUS,
        "not_qualified_as": ["PRODUCTION_QUALIFIED", "FINAL_SELECTOR_QUALIFIED"],
        "parent_amendment_id": AMENDMENT_5_ID,
        "parent_amendment_fingerprint": AMENDMENT_5_FINGERPRINT,
        "historical_parent_freeze_fingerprint": PARENT_FREEZE_FINGERPRINT,
        "parents_are_immutable": True,
        "git_commit": git_commit,
        "classification": {
            "selector_model_change": True,
            "selector_prompt_change": True,
            "selector_config_change": True,
            "intent_analyzer_model_change": False,
            "intent_analyzer_prompt_change": False,
            "intent_analyzer_budget_change": False,
            "intent_analyzer_transport_change": True,
            "registry_capability_change": True,
            "parser_contract_change": False,
            "retrieval_change": False,
            "eligibility_or_threshold_change": False,
            "candidate_budget_change": False,
            "provider_retry_or_deadline_change": False,
            "input_policy_change": False,
            "kb_or_search_pin_added": True,
            "calendar_candidate_policy_change": True,
            "decision_trace_schema_change": True,
        },
        "rationale": (
            "User product decision 2026-09-29: internal pilot selector = variant_a_v3_contract + "
            "openai/gpt-6-luna (reasoning none) as INTERNAL_PILOT_CANDIDATE. Evidence: development "
            "ablation (SELECTOR_V3_MODEL_ABLATION_4OMINI_VS_LUNA.md: 111/114 vs 101/114, premise "
            "20/21, F05 2/24 unsafe runs, heavier latency tail); independent fresh qualification "
            "blocked for insufficient fresh QnA. Luna does not accept temperature (OpenRouter "
            "supported_parameters), so the registry declares supports_temperature=false and the "
            "adapter omits the field for both capabilities; the analyzer's model, prompt, budget, "
            "reasoning and parser are unchanged (transport-only identity change)."
        ),
        "registry_entry": dict(LUNA_REGISTRY_ENTRY),
        "intent_analyzer": {
            "provider": PROVIDER, "model": LUNA_MODEL, "reasoning_effort": ANALYZER_REASONING.value,
            "temperature": None, "temperature_sent": False, "max_tokens": ANALYZER_MAX_TOKENS,
            "timeout_seconds": None, "max_retries": None,
            "config_fingerprint": ANALYZER_CONFIG_FINGERPRINT,
            "config_fingerprint_amendment_5": ANALYZER_CONFIG_FINGERPRINT_AM5,
            "identity_change": "transport only: temperature field no longer sent (never applied by the model)",
            "prompt_fingerprint": am5["analyzer_prompt_fingerprint"],
            "qualification_reference": "LUNA_QUALIFICATION_REPORT 2026-09-25 + PILOT_CANDIDATE_VALIDATION 2026-09-27",
        },
        "selector": {
            "provider": PROVIDER, "model": LUNA_MODEL, "reasoning_effort": SELECTOR_REASONING.value,
            "temperature": None, "temperature_sent": False, "max_tokens": SELECTOR_MAX_TOKENS,
            "timeout_seconds": None, "max_retries": None,
            "config_fingerprint": SELECTOR_CONFIG_FINGERPRINT,
            "prompt_version": SELECTOR_PROMPT_VERSION, "prompt_fingerprint": SELECTOR_PROMPT_FINGERPRINT,
            "prompt_version_env": "SELECTOR_PROMPT_VERSION",
            "evidence": ["SELECTOR_V3_MODEL_ABLATION_4OMINI_VS_LUNA.md",
                         "SELECTOR_V3_LUNA_FRESH_SEALED_HOLDOUT_QUALIFICATION.md (BLOCKED)"],
        },
        "parser": {"strictness": am5["parser_strictness"],
                   "contract_fingerprint": am5["parser_contract_fingerprint"], "parser_v3": False},
        "deadlines": {
            "attempt_timeout_seconds": {cap.value: DEFAULT_ATTEMPT_TIMEOUT_SECONDS[cap] for cap in LLMCapability},
            "logical_deadline_seconds": {cap.value: LOGICAL_DEADLINE_SECONDS[cap] for cap in LLMCapability},
        },
        "provider_policy": dict(PROVIDER_POLICY),
        "input_policy": {"widget_max_chars": CHAT_INPUT_MAX_CHARS, "backend_max_chars": CHAT_INPUT_MAX_CHARS},
        "knowledge_base": {"dataset_fingerprint": KB_FINGERPRINT, "active_qna": KB_ACTIVE_QNA,
                           "active_queries": KB_ACTIVE_QUERIES,
                           "source": "KB_FREEZE_V1.md (local/dev); pilot DB needs the same dataset"},
        "search": {"meili_index": MEILI_INDEX, "meili_documents": MEILI_DOCUMENTS,
                   "qdrant_collection": QDRANT_COLLECTION, "qdrant_points": QDRANT_POINTS,
                   "source": "KB_FREEZE_V1_SEARCH_REBUILD_AND_RETRIEVAL_REBASELINE.md"},
        "answer_pipeline": {"decision_trace_schema_version": DECISION_TRACE_SCHEMA, "candidate_order": "production"},
        "calendar": {
            "implicit_term_policy": "question without a term: current-term and GENERAL rows only; another "
                                    "term's row is a fallback only when neither matches the event",
            "explicit_term_policy": "unchanged (named term + GENERAL)",
            "reference": "P-01 (smoke 2026-09-29: GUZ current, 'Final sınavları ne zaman?' offered the BAHAR final)",
            "trace_field": "calendar_other_term_suppressed_count",
        },
        "rollback": ROLLBACK,
        "pilot_watchlist": ["premise_correction", "wrong_specific_general_to_specific", "false_none",
                            "unsafe_select", "semantic_instability_reask", "selector_latency_tail",
                            "provider_retry_deadline_proximity"],
        "gate_evidence": dict(gate_evidence) if gate_evidence else None,
        "created_at": created_at,
    }
    amendment = _jsonable(amendment)
    amendment["amendment_fingerprint"] = amendment_6_fingerprint(amendment)
    return amendment


def amendment_6_fingerprint(amendment: Mapping) -> str:
    return _sha256(_canonical(_jsonable({k: v for k, v in amendment.items() if k not in EXCLUDED_FIELDS})))


# ── live state ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LiveState:
    analyzer: Optional[object] = None
    selector: Optional[object] = None
    config_version: Optional[int] = None
    config_error: str = ""
    llm_enabled: Optional[bool] = None
    selector_prompt_version: str = "unresolved"
    selector_prompt_fingerprint: str = "unresolved"
    kb: dict = field(default_factory=dict)
    meili_documents: Optional[int] = None
    qdrant_points: Optional[int] = None
    search_error: str = ""
    analyzer_prompt_fingerprint: str = ""
    parser_fingerprint: str = ""
    backend_max_chars: Optional[int] = None
    trace_schema: Optional[int] = None


def _kb_fingerprint(db) -> dict:
    from sqlalchemy import text

    from core.database import execute_admin_sql

    rows = execute_admin_sql(db, text("SELECT id, question, answer, tags, queries FROM qna_search_view "
                                      "WHERE status = 1 ORDER BY id")).mappings().all()
    canon = [{"id": r["id"], "question": r["question"], "answer": r["answer"], "tags": sorted(r["tags"] or []),
              "queries": sorted(r["queries"] or [])} for r in rows]
    payload = json.dumps(canon, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {"fingerprint": hashlib.sha256(payload).hexdigest(), "active_qna": len(rows),
            "active_queries": sum(len(r["queries"] or []) for r in rows)}


def _search_counts() -> tuple[Optional[int], Optional[int], str]:
    import os

    errors, meili, qdrant = [], None, None
    try:
        import meilisearch

        client = meilisearch.Client(os.environ["MEILI_URL"], os.environ.get("MEILI_MASTER_KEY"))
        meili = client.index(MEILI_INDEX).get_stats().number_of_documents
    except Exception as exc:  # reported, never raised
        errors.append(f"meili: {type(exc).__name__}")
    try:
        from qdrant_client import QdrantClient

        client = QdrantClient(host=os.environ["QDRANT_HOST"], port=int(os.environ["QDRANT_PORT"]),
                              check_compatibility=False)
        qdrant = client.count(QDRANT_COLLECTION, exact=True).count
    except Exception as exc:
        errors.append(f"qdrant: {type(exc).__name__}")
    return meili, qdrant, "; ".join(errors)


def _backend_chat_max_chars() -> Optional[int]:
    """``max_length`` of ``WidgetChatRequest.message``, read statically from the
    backend package this process runs (works in the container layout)."""
    import ast

    from core.limits import CHAT_MESSAGE_MAX_CHARS

    path = BACKEND_DIR / "routers" / "chat.py"
    if not path.exists():
        return None
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ClassDef) and node.name == "WidgetChatRequest":
            for stmt in node.body:
                if (isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
                        and stmt.target.id == "message" and isinstance(stmt.value, ast.Call)):
                    for kw in stmt.value.keywords:
                        if kw.arg == "max_length":
                            if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, int):
                                return kw.value.value
                            if isinstance(kw.value, ast.Name) and kw.value.id == "CHAT_MESSAGE_MAX_CHARS":
                                return CHAT_MESSAGE_MAX_CHARS
    return None


def collect_live_state(db, environ: Optional[Mapping[str, str]] = None) -> LiveState:
    from core.database import SystemConfig
    from services import ai_registry
    from services.decision_trace import DecisionTrace
    from services.internal_pilot_candidate import parser_contract_fingerprint
    from services.selector_prompt_catalog import UnknownSelectorPromptVersion, resolve_selector_prompt

    analyzer = selector = version = None
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
    meili, qdrant, search_error = _search_counts()
    return LiveState(
        analyzer=analyzer, selector=selector, config_version=version, config_error=config_error,
        llm_enabled=llm_enabled, selector_prompt_version=prompt_version,
        selector_prompt_fingerprint=prompt_fp, kb=_kb_fingerprint(db), meili_documents=meili,
        qdrant_points=qdrant, search_error=search_error,
        analyzer_prompt_fingerprint=intent_analyzer_prompt_fingerprint(),
        parser_fingerprint=parser_contract_fingerprint(), backend_max_chars=_backend_chat_max_chars(),
        trace_schema=DecisionTrace(endpoint="preflight").schema_version,
    )


def run_live_preflight(state: LiveState) -> PreflightReport:
    checks: list[CheckResult] = []
    add = checks.append
    add(CheckResult("managed_config_loaded", state.analyzer is not None and state.selector is not None,
                    "DB active managed config for both capabilities",
                    f"version {state.config_version}" if state.analyzer is not None
                    else (state.config_error or "missing")))
    for prefix, assignment, cap, fp in (
            ("analyzer", state.analyzer, LLMCapability.INTENT_ANALYZER, ANALYZER_CONFIG_FINGERPRINT),
            ("selector", state.selector, LLMCapability.SELECTOR, SELECTOR_CONFIG_FINGERPRINT)):
        exp = expected_config(cap)
        if assignment is None:
            add(CheckResult(f"{prefix}_assignment", False, "resolved", "unresolved"))
            continue
        cfg = assignment.effective_config()
        model = assignment.model
        add(CheckResult(f"{prefix}_model_matches", model.provider == PROVIDER and model.model_identifier == LUNA_MODEL,
                        f"{PROVIDER}/{LUNA_MODEL}", f"{model.provider}/{model.model_identifier}"))
        add(CheckResult(f"{prefix}_reasoning_is_explicit_none", cfg.reasoning_effort == exp.reasoning_effort,
                        '"none" (transmitted)', "unset" if cfg.reasoning_effort is None else cfg.reasoning_effort.value))
        add(CheckResult(f"{prefix}_max_tokens_matches", cfg.max_tokens == exp.max_tokens,
                        str(exp.max_tokens), str(cfg.max_tokens)))
        add(CheckResult(f"{prefix}_temperature_not_sent", cfg.temperature is None,
                        "None (registry supports_temperature=false)", str(cfg.temperature)))
        add(CheckResult(f"{prefix}_no_timeout_or_retry_override",
                        cfg.timeout_seconds is None and cfg.max_retries is None, "None/None",
                        f"{cfg.timeout_seconds}/{cfg.max_retries}"))
        add(CheckResult(f"{prefix}_config_fingerprint_matches", cfg.fingerprint == fp, fp, cfg.fingerprint))
        add(CheckResult(f"{prefix}_model_qualified_and_enabled",
                        model.enabled and model.qualification_status.value == "QUALIFIED", "enabled, QUALIFIED",
                        f"{'enabled' if model.enabled else 'disabled'}, {model.qualification_status.value}"))
        add(CheckResult(f"{prefix}_registry_capabilities_match",
                        sorted(model.allowed_capabilities) == sorted(LUNA_REGISTRY_ENTRY["allowed_capabilities"])
                        and model.supports_temperature is False
                        and list(model.allowed_reasoning_efforts) == LUNA_REGISTRY_ENTRY["allowed_reasoning_efforts"],
                        json.dumps({k: LUNA_REGISTRY_ENTRY[k] for k in ("allowed_capabilities", "allowed_reasoning_efforts",
                                                                          "supports_temperature")}),
                        json.dumps({"allowed_capabilities": sorted(model.allowed_capabilities),
                                    "allowed_reasoning_efforts": list(model.allowed_reasoning_efforts),
                                    "supports_temperature": model.supports_temperature})))
    add(CheckResult("selector_prompt_version_matches", state.selector_prompt_version == SELECTOR_PROMPT_VERSION,
                    SELECTOR_PROMPT_VERSION, state.selector_prompt_version,
                    detail="Resolved through the runtime catalog from SELECTOR_PROMPT_VERSION."))
    add(CheckResult("selector_prompt_fingerprint_matches",
                    state.selector_prompt_fingerprint == SELECTOR_PROMPT_FINGERPRINT,
                    SELECTOR_PROMPT_FINGERPRINT, state.selector_prompt_fingerprint))
    add(CheckResult("analyzer_prompt_fingerprint_matches",
                    state.analyzer_prompt_fingerprint == ANALYZER_PROMPT_FINGERPRINT,
                    ANALYZER_PROMPT_FINGERPRINT, state.analyzer_prompt_fingerprint))
    add(CheckResult("strict_parser_contract_unchanged",
                    state.parser_fingerprint == PARSER_CONTRACT_FINGERPRINT,
                    PARSER_CONTRACT_FINGERPRINT, state.parser_fingerprint))
    add(CheckResult("deadlines_match",
                    LOGICAL_DEADLINE_SECONDS[LLMCapability.SELECTOR] == 15.0
                    and LOGICAL_DEADLINE_SECONDS[LLMCapability.INTENT_ANALYZER] == 30.0
                    and DEFAULT_ATTEMPT_TIMEOUT_SECONDS[LLMCapability.SELECTOR] == 10.0
                    and DEFAULT_ATTEMPT_TIMEOUT_SECONDS[LLMCapability.INTENT_ANALYZER] == 20.0,
                    "selector 10/15 s, analyzer 20/30 s",
                    f"selector {DEFAULT_ATTEMPT_TIMEOUT_SECONDS[LLMCapability.SELECTOR]}/"
                    f"{LOGICAL_DEADLINE_SECONDS[LLMCapability.SELECTOR]}, analyzer "
                    f"{DEFAULT_ATTEMPT_TIMEOUT_SECONDS[LLMCapability.INTENT_ANALYZER]}/"
                    f"{LOGICAL_DEADLINE_SECONDS[LLMCapability.INTENT_ANALYZER]}"))
    providers = [x.model.provider for x in (state.analyzer, state.selector) if x is not None]
    add(CheckResult("provider_policy_openrouter_only",
                    len(providers) == 2 and all(p == PROVIDER_POLICY["inference_provider"] for p in providers),
                    PROVIDER_POLICY["inference_provider"], ", ".join(providers) or "unresolved"))
    add(CheckResult("llm_enabled_live_is_true", state.llm_enabled is True, "true",
                    "missing" if state.llm_enabled is None else str(state.llm_enabled).lower()))
    add(CheckResult("kb_fingerprint_matches", state.kb.get("fingerprint") == KB_FINGERPRINT,
                    KB_FINGERPRINT, str(state.kb.get("fingerprint"))))
    add(CheckResult("kb_counts_match",
                    state.kb.get("active_qna") == KB_ACTIVE_QNA and state.kb.get("active_queries") == KB_ACTIVE_QUERIES,
                    f"{KB_ACTIVE_QNA}/{KB_ACTIVE_QUERIES}",
                    f"{state.kb.get('active_qna')}/{state.kb.get('active_queries')}"))
    add(CheckResult("meili_documents_match", state.meili_documents == MEILI_DOCUMENTS, str(MEILI_DOCUMENTS),
                    str(state.meili_documents), detail=state.search_error))
    add(CheckResult("qdrant_points_match", state.qdrant_points == QDRANT_POINTS, str(QDRANT_POINTS),
                    str(state.qdrant_points), detail=state.search_error))
    add(CheckResult("backend_chat_input_max_matches", state.backend_max_chars == CHAT_INPUT_MAX_CHARS,
                    str(CHAT_INPUT_MAX_CHARS), str(state.backend_max_chars)))
    add(CheckResult("decision_trace_schema_matches", state.trace_schema == DECISION_TRACE_SCHEMA,
                    str(DECISION_TRACE_SCHEMA), str(state.trace_schema)))
    return PreflightReport(tuple(checks))


# ── static (repository) checks ─────────────────────────────────────────────

_WIDGET_INPUT = re.compile(r'<textarea id="w-input"[^>]*\bmaxlength="(\d+)"')
_ENV_PROMPT = re.compile(r"^SELECTOR_PROMPT_VERSION=(\S+)\s*$", re.M)


def run_static_preflight(root: Optional[Path] = None) -> PreflightReport:
    root = root or REPO_ROOT
    checks: list[CheckResult] = []
    add = checks.append

    def _read(rel):
        path = root.joinpath(*rel)
        return path.read_text(encoding="utf-8") if path.exists() else None

    am6_text = _read(AMENDMENT_6_REL)
    if am6_text is None:
        add(CheckResult("amendment_6_artifact_present", False, "/".join(AMENDMENT_6_REL), "missing",
                        detail="Static scope needs a repository checkout."))
    else:
        stored = json.loads(am6_text)
        rebuilt = build_freeze_amendment_6(git_commit=stored.get("git_commit", ""),
                                           created_at=stored.get("created_at", ""),
                                           gate_evidence=stored.get("gate_evidence"), root=root)
        add(CheckResult("amendment_6_matches_builder",
                        stored.get("amendment_fingerprint") == rebuilt["amendment_fingerprint"]
                        == amendment_6_fingerprint(stored),
                        rebuilt["amendment_fingerprint"], str(stored.get("amendment_fingerprint"))))
    widget = _read(WIDGET_REL)
    match = _WIDGET_INPUT.search(widget) if widget else None
    add(CheckResult("widget_chat_input_max_matches", bool(match) and int(match.group(1)) == CHAT_INPUT_MAX_CHARS,
                    str(CHAT_INPUT_MAX_CHARS), match.group(1) if match else "widget.js missing or no maxlength"))
    env_text = _read(PILOT_ENV_EXAMPLE_REL)
    env_match = _ENV_PROMPT.search(env_text) if env_text else None
    add(CheckResult("pilot_env_example_pins_selector_prompt",
                    bool(env_match) and env_match.group(1) == SELECTOR_PROMPT_VERSION, SELECTOR_PROMPT_VERSION,
                    env_match.group(1) if env_match else "pilot.env.example missing or no SELECTOR_PROMPT_VERSION"))
    return PreflightReport(tuple(checks))
