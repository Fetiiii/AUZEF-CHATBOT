"""Typed internal outcomes for LLM calls and V1 parsing."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


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


@dataclass(frozen=True)
class SelectorResult:
    status: LLMOutcomeStatus
    parse_status: LLMParseStatus
    answer: Optional[str]
    selected_index: Optional[int]
    selected_qna_id: object = None
    selected_candidate_source: Optional[str] = None
    raw_numeric_value: Optional[int] = None
    invocation: Optional[LLMInvocationResult] = None


@dataclass(frozen=True)
class SplitterResult:
    subquestions: list[str] = field(default_factory=list)
    status: LLMOutcomeStatus = LLMOutcomeStatus.SUCCESS
    parse_status: LLMParseStatus = LLMParseStatus.SUCCESS
    fallback_used: bool = False
    invocation: Optional[LLMInvocationResult] = None
