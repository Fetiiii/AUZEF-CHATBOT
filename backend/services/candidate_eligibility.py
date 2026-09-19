"""Unified typed selector candidates and objective (hard-rule) eligibility.

Eligibility answers only "may the selector evaluate this candidate at all?".
It never answers "is this candidate a good answer?" — that is the Selector's
job. Consequently this module applies no retrieval-score threshold, no rank
cut, no keyword/general-vs-specific rule and no exact-alias shortcut.

Calendar candidates arrive already routed/filtered by Calendar V2 (relevance,
year, term and meaningful event match); those safety decisions are not
re-evaluated here or delegated to the LLM.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Iterable, Mapping, Optional, Sequence

from services.calendar_utils import format_calendar_answer
from services.routing_guards import RoutingGuardPolicy


# Phase 3 retrieval ceiling: Qdrant 24 + Meili 5 + Calendar hard max 3.
# The default budget keeps that full coverage; tighter K is a Phase 7 experiment.
DEFAULT_SELECTOR_MAX_CANDIDATES = 32
SELECTOR_MAX_CANDIDATES_ENV = "SELECTOR_MAX_CANDIDATES"

# Birbirine çok yakın retrieval skorları sağlayıcı/float ayrıntıları yüzünden
# son basamaklarda oynayabilir. Bu skorları aynı kovaya alıp QnA kimliğiyle
# bağlamak, aynı aday kümesinin prompt'ta aynı sırayı almasını sağlar.
RETRIEVAL_SCORE_DECIMALS = 5


class CandidateKind(str, Enum):
    QNA = "QNA"
    CALENDAR = "CALENDAR"


class ExclusionReason(str, Enum):
    MISSING_QNA_ID = "missing_qna_id"
    MISSING_QUESTION = "missing_question"
    MISSING_ANSWER = "missing_answer"
    GUARD_EVALUATION_ERROR = "guard_evaluation_error"
    INACTIVE_OR_MISSING = "inactive_or_missing"
    ACTIVITY_LOOKUP_FAILED = "activity_lookup_failed"
    CALENDAR_MISSING_ID = "calendar_missing_id"
    CALENDAR_INCOMPLETE = "calendar_incomplete"
    # Routing guard reasons are reused verbatim from RoutingGuardPolicy:
    # not_yet_valid, expired, unsupported_selector_mode.


@dataclass(frozen=True)
class SelectorCandidate:
    """One curated candidate, independent of where it was retrieved from.

    ``candidate_ref`` is stable (``qna:<id>`` / ``calendar:<id>``), unique,
    internal and never user-facing. Retrieval provenance (source, stage,
    score, alias flag) is kept for trace only and is never serialized into the
    selector prompt.
    """

    candidate_ref: str
    kind: CandidateKind
    canonical_text: str
    answer_text: str
    qna_id: Optional[int] = None
    calendar_id: Optional[int] = None
    source: Optional[str] = None
    retrieval_stage: Optional[str] = None
    score: Optional[float] = None
    alias_match: bool = False

    def prompt_view(self) -> dict:
        """The only candidate fields the selector model may see."""
        return {
            "candidate_ref": self.candidate_ref,
            "kind": self.kind.value,
            "canonical_text": self.canonical_text,
            "answer_text": self.answer_text,
        }

    def trace_view(self, order: int) -> dict:
        """Text-free internal provenance for the decision trace."""
        return {
            "order": order,
            "candidate_ref": self.candidate_ref,
            "kind": self.kind.value,
            "qna_id": self.qna_id,
            "calendar_id": self.calendar_id,
            "source": self.source,
            "retrieval_stage": self.retrieval_stage,
            "score": self.score,
            "alias_match": self.alias_match,
        }


@dataclass(frozen=True)
class EligibilityExclusion:
    candidate_ref: Optional[str]
    kind: CandidateKind
    reason: str


@dataclass(frozen=True)
class CandidateSetBuild:
    """Eligible, budgeted selector input plus its PII-free trace snapshot."""

    candidates: tuple[SelectorCandidate, ...]
    exclusions: tuple[EligibilityExclusion, ...]
    trace_snapshot: dict


ActiveQnALookup = Callable[[Sequence[int]], Iterable[int]]


def selector_max_candidates(environ: Optional[Mapping[str, str]] = None) -> int:
    env = os.environ if environ is None else environ
    raw = env.get(SELECTOR_MAX_CANDIDATES_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_SELECTOR_MAX_CANDIDATES
    try:
        value = int(raw)
    except ValueError:
        raise RuntimeError(
            f"{SELECTOR_MAX_CANDIDATES_ENV} pozitif tam sayı olmalı, alınan: {raw!r}"
        ) from None
    if value < 1:
        raise RuntimeError(
            f"{SELECTOR_MAX_CANDIDATES_ENV} pozitif tam sayı olmalı, alınan: {value}"
        )
    return value


def normalized_record_id(value) -> Optional[int]:
    """Integer record identity, or None when structurally unusable."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        text = str(value).strip()
        return int(text) if text.isdigit() else None


def score_bucket(value) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(score):
        return 0.0
    return round(score, RETRIEVAL_SCORE_DECIMALS)


def qna_ref(qna_id: int) -> str:
    return f"qna:{qna_id}"


def calendar_ref(calendar_id: int) -> str:
    return f"calendar:{calendar_id}"


def _text(value) -> str:
    return str(value or "").strip()


def _calendar_sort_key(entry) -> tuple:
    # Phase 3 order retained: period/event/date then record id.
    calendar_id = normalized_record_id(getattr(entry, "id", None))
    return (
        _text(entry.period).casefold(),
        _text(entry.event).casefold(),
        str(entry.start_date or ""),
        str(entry.end_date or ""),
        (0, calendar_id) if calendar_id is not None else (1, 0),
    )


def calendar_candidate(entry) -> Optional[SelectorCandidate]:
    """Deterministic Calendar candidate; the answer is stored data, not LLM text."""
    calendar_id = normalized_record_id(getattr(entry, "id", None))
    if calendar_id is None:
        return None
    return SelectorCandidate(
        candidate_ref=calendar_ref(calendar_id),
        kind=CandidateKind.CALENDAR,
        canonical_text=f"{_text(entry.period)} {_text(entry.event)}".strip(),
        answer_text=format_calendar_answer(
            entry.period, entry.event, entry.start_date, entry.end_date
        ),
        calendar_id=calendar_id,
        source="academic_calendar",
        retrieval_stage="calendar",
    )


def _qna_candidate(hit: dict, qna_id: int) -> SelectorCandidate:
    return SelectorCandidate(
        candidate_ref=qna_ref(qna_id),
        kind=CandidateKind.QNA,
        canonical_text=_text(hit.get("question")),
        answer_text=str(hit.get("answer") or ""),
        qna_id=qna_id,
        source=hit.get("source"),
        retrieval_stage="context" if hit.get("_stage") == 1 else "current",
        score=score_bucket(hit.get("score")),
        alias_match=bool(hit.get("matched_query")),
    )


def build_candidate_set(
    *,
    calendar_entries: Sequence,
    qna_hits: Sequence[dict],
    routing_policy: Optional[RoutingGuardPolicy],
    active_qna_lookup: Optional[ActiveQnALookup],
    max_candidates: int,
    qdrant_candidate_count: int = 0,
    meili_candidate_count: int = 0,
) -> CandidateSetBuild:
    """Merge, dedupe, apply objective eligibility and the candidate budget.

    ``qna_hits`` must already be in deterministic retrieval order. Calendar
    entries precede QnA (Phase 3 order). ``active_qna_lookup`` returns the
    subset of the given QnA ids that exist with ``status=1``; when it is
    missing or fails, every QnA candidate is excluded (fail closed).
    """
    exclusions: list[EligibilityExclusion] = []

    # 1) Structural identity + dedupe. QnA and Calendar refs live in distinct
    #    namespaces, so they can never be counted as duplicates of each other.
    structural: list[SelectorCandidate] = []
    seen_refs: set[str] = set()
    seen_calendar_content: set[tuple] = set()
    for entry in sorted(calendar_entries, key=_calendar_sort_key):
        candidate = calendar_candidate(entry)
        if candidate is None:
            exclusions.append(EligibilityExclusion(
                None, CandidateKind.CALENDAR, ExclusionReason.CALENDAR_MISSING_ID.value
            ))
            continue
        # Identity is calendar_id; rows with identical period/event/dates are
        # also one canonical record (Phase 3 behavior: lowest id is kept).
        content_key = (
            str(entry.period or ""), str(entry.event or ""),
            str(entry.start_date or ""), str(entry.end_date or ""),
        )
        if candidate.candidate_ref in seen_refs or content_key in seen_calendar_content:
            continue
        seen_refs.add(candidate.candidate_ref)
        seen_calendar_content.add(content_key)
        if not (_text(entry.event) and _text(entry.start_date) and _text(entry.end_date)):
            exclusions.append(EligibilityExclusion(
                candidate.candidate_ref,
                CandidateKind.CALENDAR,
                ExclusionReason.CALENDAR_INCOMPLETE.value,
            ))
            continue
        structural.append(candidate)

    qna_by_ref: dict[str, SelectorCandidate] = {}
    qna_order: list[str] = []
    for hit in qna_hits:
        qna_id = normalized_record_id(hit.get("qna_id"))
        if qna_id is None:
            exclusions.append(EligibilityExclusion(
                None, CandidateKind.QNA, ExclusionReason.MISSING_QNA_ID.value
            ))
            continue
        ref = qna_ref(qna_id)
        candidate = _qna_candidate(hit, qna_id)
        existing = qna_by_ref.get(ref)
        if existing is None:
            qna_by_ref[ref] = candidate
            qna_order.append(ref)
        elif not (existing.canonical_text and existing.answer_text.strip()) and (
            candidate.canonical_text and candidate.answer_text.strip()
        ):
            # Same record from another point: keep order, prefer usable content.
            qna_by_ref[ref] = candidate
    before_eligibility = len(structural) + len(qna_order)

    # 2) QnA content usability + routing guard (fail closed per candidate).
    guard_passed: list[SelectorCandidate] = []
    for ref in qna_order:
        candidate = qna_by_ref[ref]
        if not candidate.canonical_text:
            exclusions.append(EligibilityExclusion(
                ref, CandidateKind.QNA, ExclusionReason.MISSING_QUESTION.value
            ))
            continue
        if not candidate.answer_text.strip():
            exclusions.append(EligibilityExclusion(
                ref, CandidateKind.QNA, ExclusionReason.MISSING_ANSWER.value
            ))
            continue
        if routing_policy is not None:
            try:
                decision = routing_policy.decision(candidate.qna_id)
            except Exception:
                exclusions.append(EligibilityExclusion(
                    ref, CandidateKind.QNA, ExclusionReason.GUARD_EVALUATION_ERROR.value
                ))
                continue
            if not decision.selector_allowed:
                exclusions.append(EligibilityExclusion(
                    ref, CandidateKind.QNA, decision.reason or "guard_rejected"
                ))
                continue
        guard_passed.append(candidate)

    # 3) Activity: one bounded lookup for the remaining QnA ids.
    active_ids: set[int] = set()
    lookup_failed = False
    if guard_passed:
        if active_qna_lookup is None:
            lookup_failed = True
        else:
            try:
                active_ids = {
                    int(value)
                    for value in active_qna_lookup([c.qna_id for c in guard_passed])
                }
            except Exception:
                lookup_failed = True
    eligible_qna: list[SelectorCandidate] = []
    for candidate in guard_passed:
        if lookup_failed:
            reason = ExclusionReason.ACTIVITY_LOOKUP_FAILED.value
        elif candidate.qna_id not in active_ids:
            reason = ExclusionReason.INACTIVE_OR_MISSING.value
        else:
            eligible_qna.append(candidate)
            continue
        exclusions.append(
            EligibilityExclusion(candidate.candidate_ref, CandidateKind.QNA, reason)
        )

    eligible = [*structural, *eligible_qna]

    # 4) Deterministic budget. Order is not semantic truth; it is only stable.
    selected = eligible[:max_candidates]
    truncated = eligible[max_candidates:]

    reasons: dict[str, int] = {}
    for item in exclusions:
        reasons[item.reason] = reasons.get(item.reason, 0) + 1
    calendar_count = sum(1 for c in selected if c.kind is CandidateKind.CALENDAR)
    snapshot = {
        "calendar_candidate_count": len(calendar_entries),
        "qdrant_candidate_count": qdrant_candidate_count,
        "meili_candidate_count": meili_candidate_count,
        "retrieved_candidate_count": len(calendar_entries) + len(qna_hits),
        "candidate_count_before_eligibility": before_eligibility,
        "candidate_count_after_eligibility": len(eligible),
        "excluded_candidate_count": len(exclusions),
        "excluded_candidate_refs": [
            item.candidate_ref for item in exclusions if item.candidate_ref is not None
        ],
        "eligibility_exclusion_reasons": reasons,
        "candidate_budget": max_candidates,
        "candidate_truncated": bool(truncated),
        "truncated_candidate_refs": [c.candidate_ref for c in truncated],
        "selector_candidate_count": len(selected),
        "selector_calendar_candidate_count": calendar_count,
        "selector_qna_candidate_count": len(selected) - calendar_count,
        "selector_candidate_refs": [c.candidate_ref for c in selected],
        "selector_candidate_kinds": [c.kind.value for c in selected],
        "candidate_qna_ids": [c.qna_id for c in selected if c.qna_id is not None],
        "candidate_order": [
            candidate.trace_view(position)
            for position, candidate in enumerate(selected, start=1)
        ],
    }
    return CandidateSetBuild(
        candidates=tuple(selected),
        exclusions=tuple(exclusions),
        trace_snapshot=snapshot,
    )
