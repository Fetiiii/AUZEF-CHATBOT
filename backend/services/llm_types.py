"""Typed internal outcomes for LLM calls and answer-pipeline parsing."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Annotated, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class LLMOutcomeStatus(str, Enum):
    SUCCESS = "success"
    SEMANTIC_NONE = "semantic_none"
    INVALID_OUTPUT = "invalid_output"
    MODEL_ERROR = "model_error"
    TIMEOUT = "timeout"


class LLMParseStatus(str, Enum):
    SUCCESS = "success"
    SEMANTIC_NONE = "semantic_none"
    INVALID_OUTPUT = "invalid_output"
    FALLBACK = "fallback"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True)
class LLMResponseMetadata:
    requested_model: str
    actual_model: Optional[str] = None
    provider_response_id: Optional[str] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    finish_reason: Optional[str] = None
    retry_count: Optional[int] = None

    def to_trace_dict(self) -> dict:
        return {
            "requested_model": self.requested_model,
            "actual_model": self.actual_model,
            "provider_response_id": self.provider_response_id,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "finish_reason": self.finish_reason,
            "retry_count": self.retry_count,
        }


@dataclass(frozen=True)
class LLMInvocationResult:
    status: LLMOutcomeStatus
    text: Optional[str]
    latency_ms: float
    metadata: LLMResponseMetadata
    error_type: Optional[str] = None


class SelectionOutcome(str, Enum):
    """Per-intent Selector V2 outcome.

    ``NO_ELIGIBLE_CANDIDATES`` is deliberately separate from ``SEMANTIC_NONE``:
    the former means the selector was never called; the latter means a valid
    selector response explicitly rejected every eligible candidate.
    """

    SELECTED = "selected"
    SEMANTIC_NONE = "semantic_none"
    NO_ELIGIBLE_CANDIDATES = "no_eligible_candidates"
    INVALID_OUTPUT = "invalid_output"
    MODEL_ERROR = "model_error"
    TIMEOUT = "timeout"


SELECTION_ERROR_OUTCOMES = frozenset({
    SelectionOutcome.INVALID_OUTPUT,
    SelectionOutcome.MODEL_ERROR,
    SelectionOutcome.TIMEOUT,
})


CandidateRefText = Annotated[str, Field(min_length=1, max_length=64)]


class SelectorDecision(BaseModel):
    """Strict Selector V2 output: SELECT one candidate_ref or NONE.

    ``candidate_ref: null`` is accepted for NONE because strict JSON-schema
    producers emit every declared key. No confidence, reason, explanation or
    other field is accepted.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    decision: Literal["SELECT", "NONE"]
    candidate_ref: Optional[CandidateRefText] = None

    @model_validator(mode="after")
    def ref_matches_decision(self) -> "SelectorDecision":
        if self.decision == "SELECT" and self.candidate_ref is None:
            raise ValueError("SELECT requires candidate_ref")
        if self.decision == "NONE" and self.candidate_ref is not None:
            raise ValueError("NONE must not carry candidate_ref")
        return self


@dataclass(frozen=True)
class SelectorResult:
    status: LLMOutcomeStatus
    parse_status: LLMParseStatus
    answer: Optional[str]
    decision: Optional[str] = None
    selected_candidate_ref: Optional[str] = None
    selected_kind: Optional[str] = None
    selected_qna_id: object = None
    selected_calendar_id: Optional[int] = None
    selected_candidate_source: Optional[str] = None
    # Safe internal code only (never raw provider text).
    invalid_reason: Optional[str] = None
    invocation: Optional[LLMInvocationResult] = None


IntentText = Annotated[str, Field(min_length=1, max_length=2000)]


class IntentItem(BaseModel):
    """One current-turn intent after minimal normalization/context resolution."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)

    source_text: IntentText
    normalized_text: IntentText
    resolved_text: IntentText
    context_used: bool
    calendar_relevant: bool


class IntentAnalysis(BaseModel):
    """Strict V2 analyzer contract: exactly one or two intents."""

    model_config = ConfigDict(extra="forbid", strict=True)

    intent_count: Literal[1, 2]
    intents: Annotated[list[IntentItem], Field(min_length=1, max_length=2)]

    @model_validator(mode="after")
    def count_matches_items(self) -> "IntentAnalysis":
        if self.intent_count != len(self.intents):
            raise ValueError("intent_count must equal intents length")
        return self


@dataclass(frozen=True)
class IntentAnalyzerResult:
    analysis: IntentAnalysis
    status: LLMOutcomeStatus = LLMOutcomeStatus.SUCCESS
    parse_status: LLMParseStatus = LLMParseStatus.SUCCESS
    fallback_to_single: bool = False
    invocation: Optional[LLMInvocationResult] = None
