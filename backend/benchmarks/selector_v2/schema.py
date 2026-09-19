"""Typed, versioned benchmark contracts: cases, frozen candidates, results.

A *case* is one resolved intent with its expected selector decision. The
candidate set it is evaluated against lives in a separate frozen *snapshot*,
so every model/config sees exactly the same candidates.

Several acceptable refs mean "any of these is correct" (one intent, several
equivalent curated answers) — never multi-intent.
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from benchmarks.selector_v2 import BENCHMARK_VERSION
from services.candidate_eligibility import CandidateKind, SelectorCandidate

CANDIDATE_REF_RE = re.compile(r"^(qna|calendar):([1-9][0-9]*)$")


def parse_candidate_ref(ref: str) -> tuple[str, int]:
    match = CANDIDATE_REF_RE.match(ref or "")
    if match is None:
        raise ValueError(f"invalid candidate_ref: {ref!r} (expected qna:<id> or calendar:<id>)")
    return match.group(1), int(match.group(2))


class EvaluationStatus(str, Enum):
    """Whether a case may enter the selector benchmark at all."""

    SELECTOR_EVALUABLE = "SELECTOR_EVALUABLE"
    # Needs something a selector-only run must not do (context resolution,
    # splitting a raw multi-intent message without a reviewed decomposition).
    NOT_SELECTOR_EVALUABLE = "NOT_SELECTOR_EVALUABLE"
    # Reviewer removed the case from evaluation (e.g. ambiguous).
    EXCLUDED = "EXCLUDED"
    # Waiting on content/source work; not scoreable yet.
    HOLD = "HOLD"


class PoolStatus(str, Enum):
    """Where the expected candidate(s) ended up in the frozen snapshot."""

    IN_POOL = "IN_POOL"
    RETRIEVAL_MISS = "RETRIEVAL_MISS"        # never retrieved
    ELIGIBILITY_MISS = "ELIGIBILITY_MISS"    # retrieved, removed by eligibility
    BUDGET_MISS = "BUDGET_MISS"              # eligible, cut by the candidate budget
    NOT_APPLICABLE = "NOT_APPLICABLE"        # expected NONE: nothing to find
    EMPTY_POOL = "EMPTY_POOL"                # expected NONE, zero eligible: selector never called
    NOT_SNAPSHOTTED = "NOT_SNAPSHOTTED"      # case not selector-evaluable


SELECTOR_POOL_STATUSES = frozenset({PoolStatus.IN_POOL, PoolStatus.NOT_APPLICABLE})
MISS_STATUSES = frozenset({
    PoolStatus.RETRIEVAL_MISS, PoolStatus.ELIGIBILITY_MISS, PoolStatus.BUDGET_MISS,
})


class BenchmarkCase(BaseModel):
    """External dataset contract (one JSON object per line)."""

    model_config = ConfigDict(extra="forbid")

    benchmark_version: str = BENCHMARK_VERSION
    case_id: str = Field(min_length=1, max_length=64)
    source_case_id: Optional[str] = None
    intent_index: Optional[int] = None
    intent_text: Optional[str] = None
    expected_decision: Optional[Literal["SELECT", "NONE"]] = None
    acceptable_candidate_refs: list[str] = Field(default_factory=list)
    evaluation_status: EvaluationStatus = EvaluationStatus.SELECTOR_EVALUABLE
    status_reason: Optional[str] = None
    # Free-form reviewer decision label (CHANGE_GOLD, EXCLUDE_AMBIGUOUS, ...);
    # the loader never branches on its exact spelling.
    review_decision: Optional[str] = None
    # Primary metric membership: single resolved intent taken verbatim from
    # the dataset. Derived/reformulated intents are reported separately.
    primary: bool = True
    # None = dataset does not state Calendar relevance -> Calendar route
    # stays closed (no Calendar candidates are invented).
    calendar_relevant: Optional[bool] = None
    tags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> "BenchmarkCase":
        seen = set()
        for ref in self.acceptable_candidate_refs:
            parse_candidate_ref(ref)
            if ref in seen:
                raise ValueError(f"duplicate acceptable ref {ref}")
            seen.add(ref)
        if self.evaluation_status is EvaluationStatus.SELECTOR_EVALUABLE:
            if not (self.intent_text or "").strip():
                raise ValueError("selector-evaluable case requires intent_text")
            if self.expected_decision is None:
                raise ValueError("selector-evaluable case requires expected_decision")
        elif not self.status_reason:
            raise ValueError("non-evaluable case requires status_reason")
        if self.expected_decision == "SELECT" and not self.acceptable_candidate_refs:
            raise ValueError("SELECT requires at least one acceptable_candidate_ref")
        if self.expected_decision == "NONE" and self.acceptable_candidate_refs:
            raise ValueError("NONE must not carry acceptable_candidate_refs")
        return self

    @property
    def multi_acceptable(self) -> bool:
        return len(self.acceptable_candidate_refs) > 1


class FrozenCandidate(BaseModel):
    """One candidate exactly as the selector must see it, plus diagnostics.

    Only ``candidate_ref``/``kind``/``canonical_text``/``answer_text`` reach the
    model; ``order``/``source``/``retrieval_stage``/``score``/``alias_match``
    are kept for offline analysis and never serialized into a prompt.
    """

    model_config = ConfigDict(extra="forbid")

    candidate_ref: str
    kind: Literal["QNA", "CALENDAR"]
    canonical_text: str
    answer_text: str
    order: int
    source: Optional[str] = None
    retrieval_stage: Optional[str] = None
    score: Optional[float] = None
    alias_match: bool = False

    @model_validator(mode="after")
    def _check(self) -> "FrozenCandidate":
        prefix, _ = parse_candidate_ref(self.candidate_ref)
        if (prefix == "qna") != (self.kind == "QNA"):
            raise ValueError(f"{self.candidate_ref} does not match kind {self.kind}")
        return self

    @classmethod
    def from_selector_candidate(cls, candidate: SelectorCandidate, order: int) -> "FrozenCandidate":
        return cls(
            candidate_ref=candidate.candidate_ref,
            kind=candidate.kind.value,
            canonical_text=candidate.canonical_text,
            answer_text=candidate.answer_text,
            order=order,
            source=candidate.source,
            retrieval_stage=candidate.retrieval_stage,
            score=candidate.score,
            alias_match=candidate.alias_match,
        )

    def to_selector_candidate(self) -> SelectorCandidate:
        """Production candidate type WITHOUT any retrieval provenance."""
        prefix, record_id = parse_candidate_ref(self.candidate_ref)
        return SelectorCandidate(
            candidate_ref=self.candidate_ref,
            kind=CandidateKind(self.kind),
            canonical_text=self.canonical_text,
            answer_text=self.answer_text,
            qna_id=record_id if prefix == "qna" else None,
            calendar_id=record_id if prefix == "calendar" else None,
        )


class CaseSnapshot(BaseModel):
    """A case plus its frozen candidate set and retrieval diagnostics."""

    model_config = ConfigDict(extra="forbid")

    case: BenchmarkCase
    pool_status: PoolStatus
    candidates: list[FrozenCandidate] = Field(default_factory=list)
    retrieved_refs: list[str] = Field(default_factory=list)
    excluded: list[dict] = Field(default_factory=list)
    truncated_refs: list[str] = Field(default_factory=list)
    retrieval_counts: dict = Field(default_factory=dict)
    derived_tags: list[str] = Field(default_factory=list)

    @property
    def selector_evaluable(self) -> bool:
        return (
            self.case.evaluation_status is EvaluationStatus.SELECTOR_EVALUABLE
            and self.pool_status in SELECTOR_POOL_STATUSES
            and bool(self.candidates)
        )

    @property
    def all_tags(self) -> list[str]:
        return sorted({*self.case.tags, *self.derived_tags})

    def selector_candidates(self) -> list[SelectorCandidate]:
        return [c.to_selector_candidate() for c in sorted(self.candidates, key=lambda c: c.order)]


class Outcome(str, Enum):
    """Per-case benchmark outcome (exhaustive, mutually exclusive)."""

    CORRECT_SELECT = "CORRECT_SELECT"
    WRONG_SELECT = "WRONG_SELECT"      # expected SELECT, selected a non-acceptable ref
    FALSE_NONE = "FALSE_NONE"          # expected SELECT, valid semantic NONE
    CORRECT_NONE = "CORRECT_NONE"
    FALSE_SELECT = "FALSE_SELECT"      # expected NONE, selected something
    INVALID_OUTPUT = "INVALID_OUTPUT"
    MODEL_ERROR = "MODEL_ERROR"
    TIMEOUT = "TIMEOUT"


CORRECT_OUTCOMES = frozenset({Outcome.CORRECT_SELECT, Outcome.CORRECT_NONE})


class BenchmarkResult(BaseModel):
    """One selector invocation result (future live schema, §36)."""

    model_config = ConfigDict(extra="forbid")

    result_key: str
    case_id: str
    run_id: str
    run_mode: str                       # "DRY_RUN_FAKE:<policy>" | "LIVE"
    expected_decision: Literal["SELECT", "NONE"]
    expected_refs: list[str]
    decision: Optional[Literal["SELECT", "NONE"]] = None
    selected_candidate_ref: Optional[str] = None
    outcome: Outcome
    correct: bool
    selector_status: str                # production LLMOutcomeStatus value
    invalid_reason: Optional[str] = None
    provider: str
    model: str
    actual_model: Optional[str] = None
    reasoning_effort: Optional[str] = None
    config_fingerprint: str
    selector_contract_fingerprint: str
    snapshot_fingerprint: str
    candidate_order: str = "production"
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    latency_ms: Optional[float] = None
    finish_reason: Optional[str] = None
    error_type: Optional[str] = None
    failure_category: Optional[str] = None
    # Structured final response only (bounded); never reasoning/CoT.
    raw_response: Optional[str] = None
    attempt: int = 1
