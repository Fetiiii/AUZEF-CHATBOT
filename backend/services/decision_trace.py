"""PII-safe request-scoped observability for the answer pipeline."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from services.llm_config import EffectiveLLMConfigSet
from services.llm_types import IntentAnalyzerResult, SelectorResult

logger = logging.getLogger("auzef")


@dataclass
class DecisionTrace:
    # v4: Selector V2 — typed candidate refs, eligibility, SELECT/NONE decision,
    # NO_ELIGIBLE_CANDIDATES, selection outcome and guard-safe suggestions.
    schema_version: int = field(default=4, init=False)
    endpoint: str
    conversation_id: Optional[int] = None
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    started_at: float = field(default_factory=time.perf_counter, repr=False)
    llm_enabled: Optional[bool] = None
    deployment_version: Optional[str] = field(
        default_factory=lambda: os.getenv("DEPLOYMENT_VERSION") or os.getenv("APP_VERSION")
    )
    git_version: Optional[str] = field(default_factory=lambda: os.getenv("GIT_SHA"))
    context: dict = field(default_factory=dict)
    ai_config_fingerprint: Optional[str] = None
    effective_configs: dict = field(default_factory=dict)
    intent_analyzer: Optional[dict] = None
    calendar_routes: list[dict] = field(default_factory=list)
    retrieval: list[dict] = field(default_factory=list)
    selectors: list[dict] = field(default_factory=list)
    fallback: dict = field(default_factory=lambda: {
        "fallback_entered": False,
        "fallback_reason": None,
        "calendar_fallback_used": False,
        "meili_fallback_used": False,
        "qdrant_fallback_used": False,
        "selected_qna_id": None,
        "selected_source": None,
    })
    selection: dict = field(default_factory=lambda: {
        "aggregate_outcome": None,
        "intent_outcomes": [],
        "error_fallback_allowed": False,
    })
    suggestions: dict = field(default_factory=lambda: {
        "suggestions_evaluated": False,
        "retrieved_count": 0,
        "offered_count": 0,
        "offered_qna_ids": [],
        "exclusion_reasons": {},
    })
    final: dict = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def set_context(self, *, enabled: bool, messages: tuple[dict, ...]) -> None:
        # Deliberately retain only aggregate counts, never raw content.
        with self._lock:
            self.context = {
                "context_enabled": enabled,
                "context_message_count": len(messages),
                "context_char_count": sum(
                    len(str(item.get("content") or "")) for item in messages
                ),
            }

    def set_llm(self, *, enabled: bool, configs: Optional[EffectiveLLMConfigSet]) -> None:
        with self._lock:
            self.llm_enabled = enabled
            if configs is not None:
                self.effective_configs = configs.to_dict()
                self.ai_config_fingerprint = configs.fingerprint

    def record_intent_analyzer(
        self,
        result: IntentAnalyzerResult,
        config: dict,
        *,
        config_fingerprint: str,
        current_input_length: int,
        previous_user_context_count: int,
    ) -> None:
        invocation = result.invocation
        with self._lock:
            self.intent_analyzer = {
                "provider": config.get("provider"),
                "requested_model": config.get("model"),
                "actual_model": (
                    invocation.metadata.actual_model if invocation else None
                ),
                "effective_config": config,
                "config_fingerprint": config_fingerprint,
                "current_input_length": current_input_length,
                "previous_user_context_count": previous_user_context_count,
                "intent_count": result.analysis.intent_count,
                "intents": [
                    {
                        "position": index,
                        "source_length": len(item.source_text),
                        "normalized_length": len(item.normalized_text),
                        "resolved_length": len(item.resolved_text),
                        "context_used": item.context_used,
                        "calendar_relevant": item.calendar_relevant,
                    }
                    for index, item in enumerate(result.analysis.intents, start=1)
                ],
                "call_status": (
                    invocation.status.value if invocation else result.status.value
                ),
                "outcome_status": result.status.value,
                "parse_status": result.parse_status.value,
                "fallback_to_single": result.fallback_to_single,
                "latency_ms": invocation.latency_ms if invocation else None,
                "provider_metadata": (
                    invocation.metadata.to_trace_dict() if invocation else None
                ),
                "usage": _usage(invocation),
                "retry_count": _retry_count(invocation),
            }

    def record_retrieval(self, snapshot: dict) -> None:
        with self._lock:
            self.retrieval.append(snapshot)

    def record_calendar_route(self, snapshot: dict, *, purpose: str) -> None:
        # The snapshot contains only config/status/counts and curated Calendar
        # identifiers; resolved user text and aliases are deliberately absent.
        with self._lock:
            self.calendar_routes.append({**snapshot, "purpose": purpose})

    def record_selector(
        self,
        result: SelectorResult,
        *,
        outcome: str,
        config: dict,
        config_fingerprint: str,
        candidate_refs: list,
        candidate_kinds: list,
        candidate_qna_ids: list,
        purpose: str,
        used_in_final: bool,
    ) -> None:
        invocation = result.invocation
        with self._lock:
            self.selectors.append({
                "purpose": purpose,
                "selector_called": True,
                "selector_status": outcome,
                "selector_decision": result.decision,
                "used_in_final": used_in_final,
                "provider": config.get("provider"),
                "model": config.get("model"),
                "requested_model": (
                    invocation.metadata.requested_model if invocation else config.get("model")
                ),
                "actual_model": invocation.metadata.actual_model if invocation else None,
                "effective_config": config,
                "config_fingerprint": config_fingerprint,
                "candidate_count": len(candidate_refs),
                "candidate_refs": list(candidate_refs),
                "candidate_kinds": list(candidate_kinds),
                "candidate_qna_ids": list(candidate_qna_ids),
                "selected_candidate_ref": result.selected_candidate_ref,
                "selected_kind": result.selected_kind,
                "selected_qna_id": result.selected_qna_id,
                "selected_calendar_id": result.selected_calendar_id,
                "selected_candidate_source": result.selected_candidate_source,
                # Safe internal code only; raw provider text is never logged.
                "invalid_reason": result.invalid_reason,
                "call_status": (
                    invocation.status.value if invocation else result.status.value
                ),
                "parse_status": result.parse_status.value,
                "semantic_none": outcome == "semantic_none",
                "invalid_output": outcome == "invalid_output",
                "model_error": outcome == "model_error",
                "timeout": outcome == "timeout",
                "latency_ms": invocation.latency_ms if invocation else None,
                "provider_metadata": (
                    invocation.metadata.to_trace_dict() if invocation else None
                ),
                "usage": _usage(invocation),
                "retry_count": _retry_count(invocation),
            })

    def record_selector_skipped(self, *, purpose: str, outcome: str) -> None:
        """Selector deliberately not called (zero eligible candidates)."""
        with self._lock:
            self.selectors.append(_not_called_entry(purpose, outcome))

    def record_selector_pipeline_error(self, *, purpose: str) -> None:
        """Unexpected retrieval/eligibility/selector pipeline exception."""
        with self._lock:
            entry = _not_called_entry(purpose, "model_error")
            entry["model_error"] = True
            entry["pipeline_error"] = True
            self.selectors.append(entry)

    def record_selection_outcome(
        self,
        aggregate_outcome: str,
        *,
        fallback_allowed: bool,
        intent_outcomes: Optional[list] = None,
    ) -> None:
        with self._lock:
            self.selection = {
                "aggregate_outcome": aggregate_outcome,
                "intent_outcomes": list(intent_outcomes or []),
                "error_fallback_allowed": fallback_allowed,
            }

    def record_suggestions(
        self,
        *,
        retrieved_count: int,
        offered_qna_ids: list,
        exclusion_reasons: dict,
    ) -> None:
        # Suggestions are non-answers; titles themselves are never logged.
        with self._lock:
            self.suggestions = {
                "suggestions_evaluated": True,
                "retrieved_count": retrieved_count,
                "offered_count": len(offered_qna_ids),
                "offered_qna_ids": list(offered_qna_ids),
                "exclusion_reasons": dict(exclusion_reasons),
            }

    def record_fallback(
        self,
        *,
        reason: str,
        selected_source: Optional[str] = None,
        selected_qna_id=None,
    ) -> None:
        with self._lock:
            self.fallback["fallback_entered"] = True
            self.fallback["fallback_reason"] = reason
            self.fallback["selected_source"] = selected_source
            self.fallback["selected_qna_id"] = selected_qna_id
            if selected_source == "academic_calendar":
                self.fallback["calendar_fallback_used"] = True
            elif selected_source == "meilisearch":
                self.fallback["meili_fallback_used"] = True
            elif selected_source == "qdrant_vector":
                self.fallback["qdrant_fallback_used"] = True

    def finalize(
        self,
        *,
        outcome: str,
        source: str,
        qna_ids: Optional[list] = None,
        answer_count: Optional[int] = None,
    ) -> None:
        with self._lock:
            fallback_qna_id = self.fallback.get("selected_qna_id")
            preserved_qna_ids = self.final.get(
                "final_qna_ids",
                [fallback_qna_id] if fallback_qna_id is not None else [],
            )
            preserved_answer_count = self.final.get("answer_count", 0)
            self.final = {
                "final_outcome": outcome,
                "final_source": source,
                "final_qna_ids": list(
                    preserved_qna_ids if qna_ids is None else qna_ids
                ),
                "answer_count": (
                    preserved_answer_count if answer_count is None else answer_count
                ),
                "total_latency_ms": round((time.perf_counter() - self.started_at) * 1000, 3),
            }

    def to_dict(self) -> dict:
        with self._lock:
            return {
                "event": "answer_pipeline_decision_trace",
                "schema_version": self.schema_version,
                "request": {
                    "request_id": self.request_id,
                    "conversation_id": self.conversation_id,
                    "endpoint": self.endpoint,
                    "llm_enabled": self.llm_enabled,
                    "deployment_version": self.deployment_version,
                    "git_version": self.git_version,
                    "ai_config_fingerprint": self.ai_config_fingerprint,
                    "effective_configs": self.effective_configs,
                },
                "context": self.context,
                "intent_analyzer": self.intent_analyzer,
                "calendar_routes": list(self.calendar_routes),
                "retrieval": list(self.retrieval),
                "selectors": list(self.selectors),
                "selection": dict(self.selection),
                "fallback": dict(self.fallback),
                "suggestions": dict(self.suggestions),
                "final": dict(self.final),
            }


def _not_called_entry(purpose: str, outcome: str) -> dict:
    return {
        "purpose": purpose,
        "selector_called": False,
        "selector_status": outcome,
        "selector_decision": None,
        "used_in_final": False,
        "candidate_count": 0,
        "candidate_refs": [],
        "candidate_kinds": [],
        "candidate_qna_ids": [],
        "selected_candidate_ref": None,
        "selected_kind": None,
        "selected_qna_id": None,
        "selected_calendar_id": None,
        "invalid_reason": None,
        "semantic_none": False,
        "invalid_output": False,
        "model_error": False,
        "timeout": False,
        "latency_ms": None,
        "usage": None,
    }


def _usage(invocation) -> Optional[dict]:
    if invocation is None:
        return None
    meta = invocation.metadata
    if meta.input_tokens is None and meta.output_tokens is None:
        return None
    return {"input_tokens": meta.input_tokens, "output_tokens": meta.output_tokens}


def _retry_count(invocation) -> Optional[int]:
    return invocation.metadata.retry_count if invocation is not None else None


def emit_decision_trace(trace: DecisionTrace) -> None:
    """Emit exactly one structured payload without affecting the response path."""
    try:
        payload = json.dumps(
            trace.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        logger.info("decision_trace=%s", payload)
    except Exception:
        logger.exception("Decision trace emit edilemedi")
