"""Controlled Intent Analyzer diagnosis (test artifact only).

Runs INSIDE the backend container (stdin), using the same provider/config
snapshot the request path resolves, the same prompt builder, the same
provider._invoke and the same strict parser. Nothing is logged through the
application logger; raw model text is printed as JSONL to stdout for the
caller to store under outputs/. No API key, header or client object is
serialized.

Per-case diagnostic overrides (never persisted, never touch the registry):
  max_tokens_override        int, output budget for this call
  model_override             OpenRouter model id (same client/credentials)
  reasoning_effort_override  none|low|medium|high (with model_override)
  system_append              experiment-only addendum to the system prompt
Each row records logical latency (incl. body-level retries), the final
physical attempt latency, retry_count and failure_category.

Usage (host):
  docker exec -i auzef_backend python - CASES_JSON < analyzer-diagnose.py
"""
import json
import re
import sys
from dataclasses import replace

from pydantic import ValidationError

from core.database import SessionLocal
from core.deps import get_llm_provider
from services.intent_analyzer import (
    _collapse, _token_supported, _tokens, build_intent_analyzer_prompt, parse_intent_analysis,
)
from services.llm_types import IntentAnalysis, LLMOutcomeStatus
from services.llm_config import LLMCapability, ReasoningEffort

PARSER_CLASSES = {
    "empty analyzer output": "EMPTY_OUTPUT",
    "single intent source must preserve the whole current turn": "SOURCE_TEXT_MISMATCH",
    "multi intent source is not a current-turn segment": "SOURCE_TEXT_MISMATCH",
    "duplicate source intent": "SOURCE_TEXT_MISMATCH",
    "normalized intent contains unsupported semantic expansion": "NORMALIZED_TEXT_UNSUPPORTED_EXPANSION",
    "context_used does not reflect context resolution": "CONTEXT_USED_INCONSISTENCY",
    "resolved intent changed without context": "CONTEXT_USED_INCONSISTENCY",
    "resolved intent contains unsupported semantic expansion": "RESOLVED_TEXT_UNSUPPORTED_EXPANSION",
    "resolved intent does not use previous-user information": "RESOLVED_TEXT_DID_NOT_USE_CONTEXT",
}


def unsupported(text, sources):
    """Read-only reuse of the parser's token rule: which tokens fail support."""
    source_tokens = [token for value in sources for token in _tokens(value)]
    return [token for token in _tokens(text) if not _token_supported(token, source_tokens)]


def truncation_point(raw):
    """Last JSON key the model started writing before the cut."""
    keys = [(m.start(), m.group(1)) for m in re.finditer(r'"(intent_count|intents|source_text|normalized_text|resolved_text|context_used|calendar_relevant)"\s*:', raw)]
    intent_objects = raw.count('"source_text"')
    return {"last_key_started": keys[-1][1] if keys else None,
            "intent_objects_started": intent_objects,
            "intent_count_prefix": (re.search(r'"intent_count"\s*:\s*(\d)', raw) or [None, None])[1],
            "chars_after_last_key": len(raw) - keys[-1][0] if keys else None}


def invariant_detail(parsed, current, previous):
    details = []
    for index, item in enumerate(parsed.get("intents") or [], start=1):
        if not isinstance(item, dict):
            continue
        source = str(item.get("source_text", ""))
        normalized = str(item.get("normalized_text", ""))
        resolved = str(item.get("resolved_text", ""))
        details.append({
            "position": index,
            "context_used": item.get("context_used"),
            "source_equals_current": _collapse(source) == _collapse(current),
            "source_in_current": _collapse(source) in _collapse(current),
            "source_chars": len(source), "normalized_chars": len(normalized), "resolved_chars": len(resolved),
            "resolved_equals_normalized": _collapse(resolved) == _collapse(normalized),
            "normalized_unsupported_tokens": unsupported(normalized, [source]),
            "resolved_unsupported_tokens": unsupported(resolved, [normalized, *previous]),
            "resolved_tokens_from_context": [
                token for token in _tokens(resolved)
                if not _token_supported(token, _tokens(normalized))
                and _token_supported(token, [t for p in previous for t in _tokens(p)])],
        })
    return details


def diagnose(provider, case, config, arm):
    current = case["question"].strip()
    previous = [t.strip() for t in case.get("previous", []) if t.strip()][-2:]
    system, user = build_intent_analyzer_prompt(current, previous)
    # Experiment-only prompt variant: an addendum appended to the production
    # system prompt inside this harness. build_intent_analyzer_prompt is unchanged.
    if case.get("system_append"):
        system = system + "\n" + case["system_append"]
    invocation = provider._invoke(system, user, config)
    meta = invocation.metadata
    raw = invocation.text
    row = {
        "case_id": case["case_id"], "scenario": case["scenario"], "arm": arm,
        "prompt_variant": case.get("prompt_variant", "P0_production"),
        "repeat": case.get("repeat"),
        "current_input_length": len(current),
        "previous_user_turn_count": len(previous),
        "previous_context_chars": sum(len(t) for t in previous),
        "provider": getattr(provider, "provider_name", None),
        "requested_model": meta.requested_model if meta else None,
        "actual_model": meta.actual_model if meta else None,
        "configured_max_tokens": config.max_tokens,
        "temperature": config.temperature,
        "reasoning_effort": getattr(config.reasoning_effort, "value", config.reasoning_effort),
        "input_tokens": meta.input_tokens if meta else None,
        "output_tokens": meta.output_tokens if meta else None,
        "finish_reason": meta.finish_reason if meta else None,
        "invocation_status": invocation.status.value,
        # Logical latency: includes body-level retries and backoff.
        "latency_ms": round(invocation.latency_ms, 1) if invocation.latency_ms is not None else None,
        # Present only on an adapter with body-level error handling (None otherwise).
        "failure_category": invocation.failure_category,
        "error_type": invocation.error_type,
        "retry_count": meta.retry_count if meta else None,
        "final_attempt_latency_ms": (round(meta.attempt_latency_ms, 1)
                                     if getattr(meta, "attempt_latency_ms", None) else None),
        "raw_output": raw,
        "raw_output_chars": len(raw) if raw is not None else None,
        "raw_starts_with_brace": bool(raw) and raw.lstrip().startswith("{"),
        "raw_has_code_fence": bool(raw) and "```" in raw,
    }
    json_ok = schema_ok = False
    schema_error = parser_error = None
    if invocation.status is LLMOutcomeStatus.SUCCESS and raw and raw.strip():
        try:
            parsed = json.loads(raw)
            json_ok = True
            if isinstance(parsed, dict):
                row["declared_intent_count"] = parsed.get("intent_count")
                row["invariant_detail"] = invariant_detail(parsed, current, previous)
        except ValueError as exc:
            row["json_error"] = f"{type(exc).__name__}: {exc}"
            row["truncation_point"] = truncation_point(raw)
        try:
            IntentAnalysis.model_validate_json(raw)
            schema_ok = True
        except ValidationError as exc:
            schema_error = [
                {"loc": list(err.get("loc", ())), "type": err.get("type"), "msg": err.get("msg")}
                for err in exc.errors()
            ][:5]
    try:
        if invocation.status is not LLMOutcomeStatus.SUCCESS:
            raise RuntimeError(invocation.status.value)
        parse_intent_analysis(raw or "", current_user_turn=current, previous_user_turns=previous)
        final, fallback = "success", False
    except ValueError as exc:
        parser_error, final, fallback = str(exc), "invalid_output", True
    except RuntimeError as exc:
        parser_error, final, fallback = None, str(exc), True
    if final == "success":
        failure = None
    elif invocation.status is not LLMOutcomeStatus.SUCCESS:
        failure = "OTHER"
    elif not raw or not raw.strip():
        failure = "EMPTY_OUTPUT"
    elif not json_ok and row["finish_reason"] == "length":
        failure = "TRUNCATED_OUTPUT"
    elif not json_ok:
        failure = "MALFORMED_JSON"
    elif not schema_ok:
        failure = "SCHEMA_VALIDATION"
    else:
        failure = PARSER_CLASSES.get(parser_error, "OTHER")
    row.update({"json_parse_ok": json_ok, "schema_validation_ok": schema_ok,
                "schema_errors": schema_error, "parser_invariant_error": parser_error,
                "final_outcome_status": final, "fallback_to_single": fallback,
                "failure_class": failure})
    return row


def main():
    cases = json.loads(sys.argv[1])
    db = SessionLocal()
    try:
        provider = get_llm_provider(db)
    finally:
        db.close()
    if provider is None:
        raise SystemExit("no active LLM provider")
    config = provider.effective_config(LLMCapability.INTENT_ANALYZER)
    # Production request path: ManagedLLMProvider delegates analyze_intents to
    # the config-bound client for this capability; call that same client.
    clients = getattr(provider, "_clients", None)
    if clients is not None:
        provider = clients[LLMCapability.INTENT_ANALYZER]
        assert provider.effective_config(LLMCapability.INTENT_ANALYZER) == config
    for case in cases:
        # Optional per-case budget override for the token-budget experiment.
        # Diagnostic only: the production config object is never mutated.
        budget = case.get("max_tokens_override")
        call_config = replace(config, max_tokens=int(budget)) if budget else config
        arm = f"max_tokens_{call_config.max_tokens}" if budget else "production_config"
        # Model-comparison override (experiment only): same OpenRouter client,
        # different model id / reasoning effort. Production config untouched.
        if case.get("model_override"):
            effort = case.get("reasoning_effort_override")
            call_config = replace(call_config, model=case["model_override"],
                                  reasoning_effort=ReasoningEffort(effort) if effort else None)
            arm = f"model={call_config.model},reasoning={effort}"
        print("ANALYZER_DIAG " + json.dumps(diagnose(provider, case, call_config, arm),
                                            ensure_ascii=False), flush=True)


main()
