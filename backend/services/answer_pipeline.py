"""Cevap üretim pipeline'ı: Intent Analyzer + eligibility + Selector V2
+ resmi degraded mode.

Normal LLM modunda current turn 1 veya en fazla 2 resolved intent'e ayrılır;
her intent için typed aday kümesi kurulur, objektif eligibility uygulanır ve
Selector V2 strict ``SELECT``/``NONE`` kararı verir. LLM asla son kullanıcı
cevabı üretmez.

Semantic ``NONE`` ve ``NO_ELIGIBLE_CANDIDATES`` finaldir: degraded yol açılmaz.
LLM kullanılamıyorsa tek deterministik degraded servis
(``answer_in_degraded_mode``: Calendar → Meili ≥0.90 → Qdrant >0.75) çalışır:

- admin LLM OFF / sağlayıcı yok  → ham current turn (ADMIN_DEGRADED)
- Intent Analyzer hata/geçersiz çıktı ya da circuit OPEN → ham current turn
- Selector hata/geçersiz çıktı ya da circuit OPEN → YALNIZ o intent'in
  ``resolved_text``'i (diğer intent'lerin kararları korunur)

Altyapı hataları capability/config bazlı circuit breaker'a yazılır.
"""
import os
import logging
import time
from dataclasses import dataclass, replace
from typing import Optional

from sqlalchemy.orm import Session

from services.calendar_utils import format_calendar_answer
from services.calendar_retrieval import (
    failed_calendar_result,
    normalize_calendar_text,
    retrieve_calendar_candidates,
    skipped_calendar_result,
)
from core.database import QnA
from core.deps import (
    MEILI_PROVIDER,
    QDRANT_PROVIDER,
    get_llm_provider,
    is_llm_enabled,
    llm_config_problem,
    meili_is_available,
    meili_search_safe,
)
from services.candidate_eligibility import (
    CandidateSetBuild,
    ExclusionReason,
    build_candidate_set,
    normalized_record_id,
    score_bucket,
    selector_max_candidates,
)
from services.routing_guards import RoutingGuardPolicy
from services.decision_trace import DecisionTrace
from services.circuit_breaker import (
    LLM_ADMIN_MODE_TRACKER,
    LLM_CIRCUIT_BREAKER,
    CallOutcomeKind,
    breaker_key,
)
from services.llm_config import LLMCapability
from services.llm_types import (
    ExecutionMode,
    IntentResolution,
    LLMOutcomeStatus,
    LLMParseStatus,
    SelectionOutcome,
    SelectorResult,
)

logger = logging.getLogger("auzef")


@dataclass(frozen=True)
class PoolSelection:
    outcome: SelectionOutcome
    result: Optional[SelectorResult]
    build: CandidateSetBuild


@dataclass(frozen=True)
class DegradedAnswer:
    """One deterministic degraded-mode decision (never LLM-generated)."""

    answer: Optional[str]
    source: str
    qna_id: Optional[int] = None
    calendar_id: Optional[int] = None


@dataclass(frozen=True)
class IntentResult:
    position: int
    resolution: IntentResolution
    execution_mode: ExecutionMode
    answer: Optional[str] = None
    source: Optional[str] = None
    qna_id: object = None
    calendar_id: Optional[int] = None
    selection_outcome: Optional[SelectionOutcome] = None
    degraded_reason: Optional[str] = None


@dataclass(frozen=True)
class LLMAnswerResult:
    answer: Optional[str]
    source: str
    execution_mode: ExecutionMode
    intents: tuple
    selected_qna_ids: list
    answer_count: int = 0
    degraded_reason: Optional[str] = None


def _previous_user_turns(conversation_context: tuple[dict, ...]) -> tuple[str, ...]:
    """Return only the last two previous user turns for Intent Analyzer V2."""
    return tuple([
        item["content"].strip()
        for item in conversation_context
        if item.get("role") == "user" and item.get("content", "").strip()
    ][-2:])


def _normalized_qna_id(value):
    """Sayısal QnA kimliklerini sağlayıcıdan bağımsız olarak int'e çevirir."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        text = str(value).strip()
        return text or None


def _stable_id_key(value) -> tuple:
    normalized = _normalized_qna_id(value)
    if isinstance(normalized, int):
        return (0, normalized)
    return (1, str(normalized or ""))


_score_bucket = score_bucket


def _decorate_hits(hits: list, *, stage: int, provider_priority: int) -> list:
    return [
        {
            **hit,
            "_stage": stage,
            "_provider_priority": provider_priority,
        }
        for hit in hits
    ]


def _candidate_sort_key(candidate: dict) -> tuple:
    return (
        candidate["_stage"],
        candidate["_provider_priority"],
        -_score_bucket(candidate.get("score")),
        _stable_id_key(candidate.get("qna_id")),
        str(candidate.get("question") or "").casefold(),
        str(candidate.get("answer") or "").casefold(),
    )


def _threshold(name: str, default: str) -> float:
    """Eşiği ortamdan okur ve 0-1 aralığında olduğunu DOĞRULAR.

    Doğrulama şart: skorlar [0,1] aralığında geliyor, yani "0.90" yerine
    "90" yazılması hiçbir hit'in eşiği geçememesi demek. Bu SESSİZCE olur —
    bot her soruya "Bu konuda bilgim bulunmuyor." der, loga bir şey düşmez.
    Yanlış yapılandırmayla ayakta kalmaktansa açılışta düşmek doğrudur.
    """
    raw = os.getenv(name, default)
    try:
        value = float(raw)
    except ValueError:
        raise RuntimeError(f"{name} sayı olmalı, alınan: {raw!r}") from None
    if not 0.0 <= value <= 1.0:
        raise RuntimeError(
            f"{name} 0 ile 1 arasında olmalı (skorlar bu aralıkta), alınan: {value}. "
            f"Yüzde yazmak istiyorsanız {value / 100} kullanın."
        )
    return value


MEILI_THERESHOLD = _threshold("MEILI_THERESHOLD", "0.90")
QDRANT_THERESHOLD = _threshold("QDRANT_THERESHOLD", "0.75")



def is_date_query(query: str) -> bool:
    """Conservative deterministic Calendar gate for the LLM-off path."""
    q = normalize_calendar_text(query)
    date_patterns = ["ne zaman", "hangi tarih", "hangi gün", "tarihi ne", "tarihleri ne",
                     "kaçında", "kaçınca", "ayın kaçı", "ne vakit"]
    normalized_patterns = [normalize_calendar_text(pattern) for pattern in date_patterns]
    if any(pattern in q for pattern in normalized_patterns):
        return True
    return bool(set(q.split()) & {"tarih", "tarihi", "tarihler", "tarihleri"})


def _degraded_calendar(
    query: str,
    db: Session,
    trace: DecisionTrace | None = None,
    purpose: str = "fallback",
) -> tuple[Optional[str], Optional[int]]:
    """Deterministic Calendar V2 lookup (year/term/event safety, limit 1)."""
    try:
        result = retrieve_calendar_candidates(query, db, limit=1)
    except Exception:
        logger.exception("Calendar V2 retrieval hatası")
        result = failed_calendar_result()
    if trace is not None:
        trace.record_calendar_route(result.trace_snapshot, purpose=purpose)
    if not result.candidates:
        return None, None
    best = result.candidates[0]
    return (
        format_calendar_answer(best.period, best.event, best.start_date, best.end_date),
        normalized_record_id(getattr(best, "id", None)),
    )


def search_calendar(
    query: str,
    db: Session,
    use_llm: bool,
    trace: DecisionTrace | None = None,
) -> Optional[str]:
    """Compatibility boundary backed by deterministic Calendar V2 retrieval.

    ``use_llm`` remains in the public signature for callers/tests, but Calendar
    V2 never starts a Calendar-specific LLM call.
    """
    del use_llm
    answer, _calendar_id = _degraded_calendar(query, db, trace)
    return answer


def _active_qna_lookup(db: Session):
    """Bounded activity check: ids that exist with ``status=1``."""
    def lookup(qna_ids) -> set[int]:
        ids = sorted({int(value) for value in qna_ids})
        if not ids:
            return set()
        rows = (
            db.query(QnA.id)
            .filter(QnA.id.in_(ids), QnA.status == 1)
            .all()
        )
        return {int(row[0]) for row in rows}

    return lookup


def _build_candidate_pool_result(
    query: str,
    calendar_entries: list,
    routing_policy: RoutingGuardPolicy | None = None,
    *,
    active_qna_lookup=None,
    max_candidates: int | None = None,
) -> CandidateSetBuild:
    """Bir intent için Selector V2'ye verilecek eligible + bounded aday kümesi.

    Retrieval değişmedi: Qdrant (24) + Meili (5) QnA adayları ve Calendar V2'nin
    önceden filtrelediği en fazla üç takvim kaydı. QnA'lar ``qna_id``, takvim
    kayıtları ``calendar_id`` ile tekilleştirilir. QnA sırası retrieval aşaması,
    sağlayıcı, beş ondalığa yuvarlanmış skor ve QnA kimliğiyle belirlenir; bu
    sıra yalnız determinizm içindir, semantik doğruluk bildirmez ve prompt'a
    skor/rank/sağlayıcı olarak girmez."""
    qdrant_hits = []
    raw = []
    qdrant_available = True
    started = time.perf_counter()
    try:
        qdrant_hits = QDRANT_PROVIDER.search(query, limit=24)
        raw.extend(_decorate_hits(qdrant_hits, stage=0, provider_priority=0))
    except Exception:
        # Zero results and an outage are different facts; record the outage.
        qdrant_available = False
    # meili_search_safe stays the call site: it is the seam the pipeline tests
    # substitute. Availability comes from the circuit status that same helper
    # maintains, so a successful empty result stays "available".
    meili_hits = meili_search_safe(query, limit=5)
    meili_available = meili_is_available()
    raw.extend(_decorate_hits(meili_hits, stage=0, provider_priority=1))
    retrieval_ms = round((time.perf_counter() - started) * 1000, 3)
    return build_candidate_set(
        calendar_entries=calendar_entries,
        qna_hits=sorted(raw, key=_candidate_sort_key),
        routing_policy=routing_policy,
        active_qna_lookup=active_qna_lookup,
        max_candidates=(
            selector_max_candidates() if max_candidates is None else max_candidates
        ),
        qdrant_candidate_count=len(qdrant_hits),
        meili_candidate_count=len(meili_hits),
        retrieval_ms=retrieval_ms,
        qdrant_available=qdrant_available,
        meili_available=meili_available,
    )


def _build_candidate_pool(
    query: str,
    calendar_entries: list,
    routing_policy: RoutingGuardPolicy | None = None,
    *,
    active_qna_lookup=None,
) -> list:
    """Compatibility wrapper returning only the eligible typed candidates."""
    return list(
        _build_candidate_pool_result(
            query,
            calendar_entries,
            routing_policy,
            active_qna_lookup=active_qna_lookup,
        ).candidates
    )


_OUTCOME_BY_STATUS = {
    LLMOutcomeStatus.SUCCESS: SelectionOutcome.SELECTED,
    LLMOutcomeStatus.SEMANTIC_NONE: SelectionOutcome.SEMANTIC_NONE,
    LLMOutcomeStatus.INVALID_OUTPUT: SelectionOutcome.INVALID_OUTPUT,
    LLMOutcomeStatus.MODEL_ERROR: SelectionOutcome.MODEL_ERROR,
    LLMOutcomeStatus.TIMEOUT: SelectionOutcome.TIMEOUT,
}


def _availability_kind(status: LLMOutcomeStatus) -> CallOutcomeKind:
    """Only infrastructure failures count against capability availability."""
    if status in (LLMOutcomeStatus.MODEL_ERROR, LLMOutcomeStatus.TIMEOUT):
        return CallOutcomeKind.FAILURE
    if status is LLMOutcomeStatus.INVALID_OUTPUT:
        return CallOutcomeKind.NEUTRAL
    return CallOutcomeKind.SUCCESS


def _capability_permit(prov, capability: LLMCapability):
    config = prov.effective_config(capability)
    key = breaker_key(capability.value, config.provider, config.model, config.fingerprint)
    return config, LLM_CIRCUIT_BREAKER.acquire(key)


def _trace_circuit(
    trace: DecisionTrace | None,
    *,
    capability: LLMCapability,
    purpose: str,
    config,
    permit,
    record=None,
    status: LLMOutcomeStatus | None = None,
    failure_category: str | None = None,
    degraded_reason: str | None = None,
) -> None:
    if trace is None:
        return
    trace.record_circuit({
        "capability": capability.value,
        "purpose": purpose,
        "circuit_key": permit.key,
        "provider": config.provider,
        "model": config.model,
        "config_fingerprint": config.fingerprint,
        "circuit_state_before": permit.state_before.value,
        "circuit_state_after": (
            record.state_after.value if record is not None else permit.state_before.value
        ),
        "consecutive_failures_before": permit.failures_before,
        "consecutive_failures_after": (
            record.failures_after if record is not None else permit.failures_before
        ),
        "llm_call_skipped": not permit.allowed,
        "probe_attempted": permit.probe,
        "call_status": status.value if status is not None else None,
        "availability_outcome": (
            _availability_kind(status).value if status is not None else None
        ),
        "failure_kind": (
            status.value if status in (
                LLMOutcomeStatus.MODEL_ERROR, LLMOutcomeStatus.TIMEOUT
            ) else None
        ),
        "failure_category": failure_category,
        "transition": record.transition if record is not None else None,
        "degraded_reason": degraded_reason,
    })


def _select_from_pool(
    query: str,
    calendar_entries: list,
    prov,
    routing_policy: RoutingGuardPolicy | None = None,
    trace: DecisionTrace | None = None,
    trace_purpose: str = "selector",
    active_qna_lookup=None,
) -> PoolSelection:
    """Eligible aday kümesini kurar ve Selector V2'yi en fazla bir kez çağırır.

    Sıfır eligible aday → selector çağrılmaz (``NO_ELIGIBLE_CANDIDATES``,
    circuit'e dokunulmaz). Tek aday bile selector'dan geçer. Selector circuit
    OPEN ise çağrı yapılmaz (``CIRCUIT_OPEN``)."""
    build = _build_candidate_pool_result(
        query,
        calendar_entries,
        routing_policy,
        active_qna_lookup=active_qna_lookup,
    )
    if trace is not None:
        trace.record_retrieval({**build.trace_snapshot, "purpose": trace_purpose})
    if not build.candidates:
        return PoolSelection(SelectionOutcome.NO_ELIGIBLE_CANDIDATES, None, build)

    config, permit = _capability_permit(prov, LLMCapability.SELECTOR)
    if not permit.allowed:
        _trace_circuit(
            trace, capability=LLMCapability.SELECTOR, purpose=trace_purpose,
            config=config, permit=permit, degraded_reason="selector_circuit_open",
        )
        return PoolSelection(SelectionOutcome.CIRCUIT_OPEN, None, build)
    try:
        result = prov.ask_with_result(query, list(build.candidates))
    except Exception:
        record = LLM_CIRCUIT_BREAKER.record(permit, CallOutcomeKind.FAILURE)
        _trace_circuit(
            trace, capability=LLMCapability.SELECTOR, purpose=trace_purpose,
            config=config, permit=permit, record=record,
            status=LLMOutcomeStatus.MODEL_ERROR, failure_category="UNKNOWN",
        )
        raise
    if result.status is LLMOutcomeStatus.SUCCESS:
        # Provider doubles/custom providers must also respect the candidate set.
        allowed = {candidate.candidate_ref for candidate in build.candidates}
        if result.selected_candidate_ref not in allowed or not result.answer:
            result = replace(
                result,
                status=LLMOutcomeStatus.INVALID_OUTPUT,
                parse_status=LLMParseStatus.INVALID_OUTPUT,
                answer=None,
                decision=None,
                selected_candidate_ref=None,
                selected_kind=None,
                selected_qna_id=None,
                selected_calendar_id=None,
                selected_candidate_source=None,
                invalid_reason="unknown_candidate_ref",
            )
    record = LLM_CIRCUIT_BREAKER.record(permit, _availability_kind(result.status))
    _trace_circuit(
        trace, capability=LLMCapability.SELECTOR, purpose=trace_purpose,
        config=config, permit=permit, record=record, status=result.status,
        failure_category=(
            result.invocation.failure_category if result.invocation else None
        ),
    )
    outcome = _OUTCOME_BY_STATUS.get(result.status, SelectionOutcome.INVALID_OUTPUT)
    return PoolSelection(outcome, result, build)


def _degraded_request(
    query: str,
    db: Session,
    routing_policy: RoutingGuardPolicy | None,
    trace: DecisionTrace | None,
    *,
    mode: ExecutionMode,
    reason: str,
) -> LLMAnswerResult:
    """Whole current turn → deterministic path (no analyzer/selector call).

    Used for admin OFF and for analyzer failure/OPEN circuit: no LLM context
    resolution exists, so the raw current turn with the deterministic date gate
    is used (the pre-V2 behavior)."""
    degraded = answer_in_degraded_mode(
        query, db,
        routing_policy=routing_policy,
        calendar_gate="date_query",
        reason=reason,
        trace=trace,
        purpose="request",
        query_kind="raw_current_turn",
    )
    intent = IntentResult(
        position=1,
        resolution=(
            IntentResolution.DEGRADED_SELECTED
            if degraded.answer else IntentResolution.DEGRADED_NONE
        ),
        execution_mode=mode,
        answer=degraded.answer,
        source=degraded.source if degraded.answer else None,
        qna_id=degraded.qna_id,
        calendar_id=degraded.calendar_id,
        degraded_reason=reason,
    )
    return _compose((intent,), mode=mode, degraded_reason=reason)


def _compose(
    intents: tuple,
    *,
    mode: ExecutionMode,
    degraded_reason: Optional[str] = None,
) -> LLMAnswerResult:
    """Deterministic multi-answer composition: intent order, exact dedupe."""
    answers, sources, qna_ids = [], [], []
    for intent in intents:
        if intent.answer and intent.answer not in answers:
            answers.append(intent.answer)
            sources.append(intent.source)
            if intent.qna_id is not None:
                qna_ids.append(intent.qna_id)
    # Existing source vocabulary only: any selector answer → "llm",
    # otherwise the (first) deterministic source.
    source = "llm" if "llm" in sources else (sources[0] if sources else "none")
    return LLMAnswerResult(
        answer="\n\n".join(answers) if answers else None,
        source=source,
        execution_mode=mode,
        intents=tuple(intents),
        selected_qna_ids=qna_ids,
        answer_count=len(answers),
        degraded_reason=degraded_reason,
    )


def _llm_answer(
    query: str,
    db: Session,
    conversation_context: tuple[dict, ...] = (),
    routing_policy: RoutingGuardPolicy | None = None,
    trace: DecisionTrace | None = None,
) -> LLMAnswerResult:
    """Analyze the current turn, then run eligibility + Selector V2 per intent.

    Context is consumed only by the analyzer. Each intent is independent: a
    NONE is final for that intent only, and a selector failure degrades only
    that intent (using its own ``resolved_text``)."""
    prov = get_llm_provider(db)
    if prov is None:
        raise RuntimeError("LLM sağlayıcısı yok (anahtar DB'de/env'de bulunamadı)")
    if trace is not None:
        trace.set_llm(
            enabled=True,
            configs=getattr(prov, "configs", None),
            runtime=getattr(prov, "runtime", None),
        )

    previous_user_turns = _previous_user_turns(conversation_context)
    analyzer_config, permit = _capability_permit(prov, LLMCapability.INTENT_ANALYZER)
    if not permit.allowed:
        _trace_circuit(
            trace, capability=LLMCapability.INTENT_ANALYZER, purpose="request",
            config=analyzer_config, permit=permit,
            degraded_reason="intent_analyzer_circuit_open",
        )
        return _degraded_request(
            query, db, routing_policy, trace,
            mode=ExecutionMode.CIRCUIT_DEGRADED,
            reason="intent_analyzer_circuit_open",
        )
    try:
        analysis_result = prov.analyze_intents_with_result(query, previous_user_turns)
    except Exception:
        record = LLM_CIRCUIT_BREAKER.record(permit, CallOutcomeKind.FAILURE)
        _trace_circuit(
            trace, capability=LLMCapability.INTENT_ANALYZER, purpose="request",
            config=analyzer_config, permit=permit, record=record,
            status=LLMOutcomeStatus.MODEL_ERROR, failure_category="UNKNOWN",
            degraded_reason="intent_analyzer_model_error",
        )
        logger.exception("Intent Analyzer hattı hatası")
        return _degraded_request(
            query, db, routing_policy, trace,
            mode=ExecutionMode.REQUEST_DEGRADED,
            reason="intent_analyzer_model_error",
        )
    status = analysis_result.status
    record = LLM_CIRCUIT_BREAKER.record(permit, _availability_kind(status))
    analyzer_degraded_reason = (
        f"intent_analyzer_{status.value}"
        if status is not LLMOutcomeStatus.SUCCESS else None
    )
    _trace_circuit(
        trace, capability=LLMCapability.INTENT_ANALYZER, purpose="request",
        config=analyzer_config, permit=permit, record=record, status=status,
        failure_category=(
            analysis_result.invocation.failure_category
            if analysis_result.invocation else None
        ),
        degraded_reason=analyzer_degraded_reason,
    )
    if trace is not None:
        trace.record_intent_analyzer(
            analysis_result,
            analyzer_config.to_dict(),
            config_fingerprint=analyzer_config.fingerprint,
            current_input_length=len(query),
            previous_user_context_count=len(previous_user_turns),
        )
    if analyzer_degraded_reason is not None:
        # The analyzer contract did not complete: keep its lossless raw SINGLE
        # and answer the whole current turn deterministically. No selector.
        return _degraded_request(
            query, db, routing_policy, trace,
            mode=ExecutionMode.REQUEST_DEGRADED,
            reason=analyzer_degraded_reason,
        )

    active_lookup = _active_qna_lookup(db)
    results = []
    for position, intent in enumerate(analysis_result.analysis.intents, start=1):
        purpose = f"intent_{position}"
        if intent.calendar_relevant:
            try:
                calendar_result = retrieve_calendar_candidates(
                    intent.resolved_text,
                    db,
                )
            except Exception:
                logger.exception("Calendar V2 intent retrieval hatası")
                calendar_result = failed_calendar_result()
        else:
            # Important: no DB/config lookup occurs on the closed route.
            calendar_result = skipped_calendar_result(relevant=False)
        if trace is not None:
            trace.record_calendar_route(calendar_result.trace_snapshot, purpose=purpose)
        try:
            selection = _select_from_pool(
                intent.resolved_text,
                list(calendar_result.candidates),
                prov,
                routing_policy,
                trace,
                purpose,
                active_qna_lookup=active_lookup,
            )
            outcome = selection.outcome
        except Exception:
            logger.exception("Selector V2 intent hattı hatası")
            selection = None
            outcome = SelectionOutcome.MODEL_ERROR
            if trace is not None:
                trace.record_selector_pipeline_error(purpose=purpose)
        if trace is not None and selection is not None:
            if selection.result is None:
                trace.record_selector_skipped(purpose=purpose, outcome=outcome.value)
            else:
                selector_config = prov.effective_config(LLMCapability.SELECTOR)
                trace.record_selector(
                    selection.result,
                    outcome=outcome.value,
                    config=selector_config.to_dict(),
                    config_fingerprint=selector_config.fingerprint,
                    candidate_refs=selection.build.trace_snapshot["selector_candidate_refs"],
                    candidate_kinds=selection.build.trace_snapshot["selector_candidate_kinds"],
                    candidate_qna_ids=selection.build.trace_snapshot["candidate_qna_ids"],
                    purpose=purpose,
                    used_in_final=outcome is SelectionOutcome.SELECTED,
                )

        if outcome is SelectionOutcome.SELECTED:
            result = selection.result
            results.append(IntentResult(
                position, IntentResolution.SELECTED, ExecutionMode.NORMAL_LLM,
                answer=result.answer, source="llm",
                qna_id=result.selected_qna_id,
                calendar_id=result.selected_calendar_id,
                selection_outcome=outcome,
            ))
        elif outcome in (
            SelectionOutcome.SEMANTIC_NONE, SelectionOutcome.NO_ELIGIBLE_CANDIDATES
        ):
            # Final semantic decision for THIS intent: no degraded fallback.
            results.append(IntentResult(
                position,
                IntentResolution(outcome.value),
                ExecutionMode.NORMAL_LLM,
                selection_outcome=outcome,
            ))
        else:
            mode = (
                ExecutionMode.CIRCUIT_DEGRADED
                if outcome is SelectionOutcome.CIRCUIT_OPEN
                else ExecutionMode.REQUEST_DEGRADED
            )
            reason = (
                f"selector_{outcome.value}" if selection is not None
                else "selector_pipeline_error"
            )
            # Only this intent, only its resolved text (never the raw turn).
            degraded = answer_in_degraded_mode(
                intent.resolved_text, db,
                routing_policy=routing_policy,
                calendar_gate="intent_relevant" if intent.calendar_relevant else "closed",
                reason=reason,
                trace=trace,
                purpose=purpose,
                query_kind="resolved_intent",
            )
            results.append(IntentResult(
                position,
                (
                    IntentResolution.DEGRADED_SELECTED
                    if degraded.answer else IntentResolution.DEGRADED_NONE
                ),
                mode,
                answer=degraded.answer,
                source=degraded.source if degraded.answer else None,
                qna_id=degraded.qna_id,
                calendar_id=degraded.calendar_id,
                selection_outcome=outcome,
                degraded_reason=reason,
            ))

    modes = {item.execution_mode for item in results}
    request_mode = (
        ExecutionMode.CIRCUIT_DEGRADED if ExecutionMode.CIRCUIT_DEGRADED in modes
        else ExecutionMode.REQUEST_DEGRADED if ExecutionMode.REQUEST_DEGRADED in modes
        else ExecutionMode.NORMAL_LLM
    )
    reasons = [item.degraded_reason for item in results if item.degraded_reason]
    return _compose(
        tuple(results), mode=request_mode,
        degraded_reason=reasons[0] if reasons else None,
    )


def _fallback_safe_hits(
    hits: list,
    policy: RoutingGuardPolicy,
    active_lookup,
    reasons: dict,
) -> list:
    """Degraded QnA eligibility: fallback guard semantics + ``status=1``.

    ``semantic_selector_only``, expired and not-yet-valid guarded QnA cannot be
    a degraded answer. Guard or activity errors fail closed."""
    def exclude(reason: str) -> None:
        reasons[reason] = reasons.get(reason, 0) + 1

    allowed = []
    for hit in hits:
        qna_id = normalized_record_id(hit.get("qna_id"))
        if qna_id is None:
            exclude(ExclusionReason.MISSING_QNA_ID.value)
            continue
        try:
            permitted = policy.decision(qna_id).fallback_allowed
        except Exception:
            exclude(ExclusionReason.GUARD_EVALUATION_ERROR.value)
            continue
        if not permitted:
            exclude("guard_fallback_blocked")
            continue
        allowed.append((qna_id, hit))
    if not allowed:
        return []
    try:
        active = active_lookup([qna_id for qna_id, _ in allowed])
    except Exception:
        logger.exception("Degraded QnA aktiflik kontrolü başarısız; QnA bloke edildi")
        for _ in allowed:
            exclude(ExclusionReason.ACTIVITY_LOOKUP_FAILED.value)
        return []
    safe = []
    for qna_id, hit in allowed:
        if qna_id in active:
            safe.append({**hit, "qna_id": qna_id})
        else:
            exclude(ExclusionReason.INACTIVE_OR_MISSING.value)
    return safe


def answer_in_degraded_mode(
    query: str,
    db: Session,
    *,
    routing_policy: RoutingGuardPolicy | None = None,
    calendar_gate: str = "date_query",
    reason: str = "llm_disabled_or_unavailable",
    trace: DecisionTrace | None = None,
    purpose: str = "request",
    query_kind: str = "raw_current_turn",
) -> DegradedAnswer:
    """The single deterministic degraded-answer service. No LLM call.

    Order: Calendar (gate) → Meili (≥ MEILI_THERESHOLD) → Qdrant
    (> QDRANT_THERESHOLD) → no answer. Exactly one curated answer or none.

    ``calendar_gate``: ``date_query`` (deterministic date-question gate; used
    without analyzer), ``intent_relevant`` (analyzer said calendar_relevant)
    or ``closed``. Calendar always goes through Calendar V2 year/term/event
    rules; there is no random-event or all-rows fallback."""
    run = {
        "purpose": purpose,
        "degraded_reason": reason,
        "query_kind": query_kind,
        "calendar_gate": calendar_gate,
        "degraded_calendar_attempted": False,
        "degraded_meili_attempted": False,
        "degraded_qdrant_attempted": False,
        "degraded_selected_source": None,
        "degraded_selected_qna_id": None,
        "degraded_selected_calendar_id": None,
        "degraded_no_answer": False,
        "exclusion_reasons": {},
    }

    def finish(result: DegradedAnswer) -> DegradedAnswer:
        if result.answer:
            run["degraded_selected_source"] = result.source
            run["degraded_selected_qna_id"] = result.qna_id
            run["degraded_selected_calendar_id"] = result.calendar_id
        else:
            run["degraded_no_answer"] = True
        if trace is not None:
            trace.record_degraded(run)
        return result

    open_calendar = (
        calendar_gate == "intent_relevant"
        or (calendar_gate == "date_query" and is_date_query(query))
    )
    if open_calendar:
        run["degraded_calendar_attempted"] = True
        calendar_answer, calendar_id = _degraded_calendar(
            query, db, trace, purpose=f"degraded_{purpose}"
        )
        if calendar_answer:
            return finish(DegradedAnswer(
                calendar_answer, "academic_calendar", calendar_id=calendar_id
            ))

    policy = routing_policy or RoutingGuardPolicy.empty()
    active_lookup = _active_qna_lookup(db)
    run["degraded_meili_attempted"] = True
    hits = _fallback_safe_hits(
        meili_search_safe(query, limit=3), policy, active_lookup, run["exclusion_reasons"]
    )
    if hits and hits[0]["score"] >= MEILI_THERESHOLD:
        return finish(DegradedAnswer(hits[0]["answer"], "meilisearch", hits[0]["qna_id"]))

    try:
        run["degraded_qdrant_attempted"] = True
        qhits = _fallback_safe_hits(
            QDRANT_PROVIDER.search(query, limit=5), policy, active_lookup,
            run["exclusion_reasons"],
        )
        if qhits and qhits[0]["score"] > QDRANT_THERESHOLD:
            return finish(
                DegradedAnswer(qhits[0]["answer"], "qdrant_vector", qhits[0]["qna_id"])
            )
    except Exception:
        pass
    return finish(DegradedAnswer(None, "none"))


def _fallback_answer(
    query: str,
    db: Session,
    use_calendar: bool = True,
    routing_policy: RoutingGuardPolicy | None = None,
    trace: DecisionTrace | None = None,
    fallback_reason: str = "llm_disabled_or_unavailable",
) -> tuple:
    """Compatibility wrapper over ``answer_in_degraded_mode`` → (answer, source)."""
    result = answer_in_degraded_mode(
        query, db,
        routing_policy=routing_policy,
        calendar_gate="date_query" if use_calendar else "closed",
        reason=fallback_reason,
        trace=trace,
    )
    return (result.answer, result.source) if result.answer else (None, "none")


def answer_question(
    query: str,
    db: Session,
    conversation_context: tuple[dict, ...] = (),
    trace: DecisionTrace | None = None,
) -> tuple:
    """Bir soruya cevap üretir. (answer, source) döner; cevap yoksa (None, "none").

    - NORMAL_LLM: analyzer + selector; SELECT → curated cevap, semantic NONE /
      eligible aday yok → o intent için final cevapsız (degraded yol YOK).
    - REQUEST_DEGRADED / CIRCUIT_DEGRADED: yalnız etkilenen kısım (analyzer
      → tüm ham turn; selector → yalnız o intent'in resolved_text'i)
      deterministik degraded servise gider.
    - ADMIN_DEGRADED: admin LLM OFF ya da sağlayıcı yok; LLM çağrısı yok.
    Tek istek hatası DB'deki LLM_ENABLED ayarını asla değiştirmez."""
    try:
        routing_policy = RoutingGuardPolicy.load(db)
    except Exception:
        # Guard deposu okunamazken kontrollü bir QnA'yı yanlışlıkla guardsız
        # döndürmektense tüm QnA yollarını fail-closed kapat.
        logger.exception("Routing guard deposu okunamadı; QnA cevabı bloke edildi")
        if trace is not None:
            trace.set_llm(enabled=False, configs=None)
            trace.finalize(outcome="guard_store_error", source="none", qna_ids=[])
        return None, "none"

    llm_enabled = is_llm_enabled(db)
    # Admin OFF → ON (explicit operator action) resets this node's breakers.
    LLM_ADMIN_MODE_TRACKER.observe(llm_enabled, LLM_CIRCUIT_BREAKER)
    if trace is not None:
        trace.set_llm(enabled=llm_enabled, configs=None)
    if not llm_enabled:
        try:
            config_problem = llm_config_problem(db)
        except Exception:
            logger.exception("AI config durumu okunamadı")
            config_problem = "config_unavailable"
        # Admin ON but managed config invalid/unavailable: a typed config
        # failure (never semantic NONE, never a guessed model).
        result = _degraded_request(
            query, db, routing_policy, trace,
            mode=(
                ExecutionMode.CONFIG_DEGRADED if config_problem
                else ExecutionMode.ADMIN_DEGRADED
            ),
            reason=config_problem or "llm_disabled_or_unavailable",
        )
    else:
        try:
            result = _llm_answer(
                query, db, conversation_context, routing_policy, trace
            )
        except Exception as e:
            # Pipeline hatası (ör. sağlayıcı kurulamadı): request-level degraded.
            logger.error(f"LLM ana yol hatası (degraded yola düşülüyor): {e}")
            result = _degraded_request(
                query, db, routing_policy, trace,
                mode=ExecutionMode.REQUEST_DEGRADED,
                reason="llm_pipeline_error",
            )

    if trace is not None:
        trace.record_execution(
            execution_mode=result.execution_mode.value,
            degraded_reason=result.degraded_reason,
            intents=[
                {
                    "position": item.position,
                    "resolution": item.resolution.value,
                    "execution_mode": item.execution_mode.value,
                    "selection_outcome": (
                        item.selection_outcome.value if item.selection_outcome else None
                    ),
                    "degraded_reason": item.degraded_reason,
                    "answer_source": item.source,
                    "qna_id": item.qna_id,
                    "calendar_id": item.calendar_id,
                }
                for item in result.intents
            ],
        )
    if result.answer:
        if trace is not None:
            trace.finalize(
                outcome="answer",
                source=result.source,
                qna_ids=result.selected_qna_ids,
                answer_count=result.answer_count,
            )
        return result.answer, result.source
    return None, "none"


def guard_safe_suggestions(
    query: str,
    db: Session,
    *,
    limit: int = 20,
    trace: DecisionTrace | None = None,
) -> list[str]:
    """Non-answer "did you mean" titles that respect guard/activity rules.

    Suggestions are not answers and never override a selector decision. A
    title is offered only when its QnA may be surfaced outside the selector:
    guardless or fallback-allowed (so ``semantic_selector_only``, expired and
    not-yet-valid guarded QnA are hidden), existing with ``status=1``. Guard
    store or activity lookup failure yields no suggestions (fail closed)."""
    try:
        hits = MEILI_PROVIDER.get_suggestion_hits(query, limit=limit)
    except Exception:
        logger.exception("Öneri araması başarısız")
        hits = []
    reasons: dict[str, int] = {}

    def exclude(reason: str) -> None:
        reasons[reason] = reasons.get(reason, 0) + 1

    try:
        policy = RoutingGuardPolicy.load(db)
    except Exception:
        logger.exception("Routing guard deposu okunamadı; öneriler bloke edildi")
        policy = None
    candidates = []
    seen_ids = set()
    for hit in hits:
        qna_id = normalized_record_id(hit.get("qna_id"))
        title = str(hit.get("question") or "").strip()
        if qna_id is None:
            exclude(ExclusionReason.MISSING_QNA_ID.value)
            continue
        if not title:
            exclude(ExclusionReason.MISSING_QUESTION.value)
            continue
        if qna_id in seen_ids:
            continue
        seen_ids.add(qna_id)
        if policy is None:
            exclude("guard_store_error")
            continue
        try:
            allowed = policy.decision(qna_id).fallback_allowed
        except Exception:
            exclude(ExclusionReason.GUARD_EVALUATION_ERROR.value)
            continue
        if not allowed:
            exclude("guard_fallback_blocked")
            continue
        candidates.append((qna_id, title))

    safe: list[str] = []
    safe_ids: list[int] = []
    if candidates:
        try:
            active = _active_qna_lookup(db)([qna_id for qna_id, _ in candidates])
        except Exception:
            logger.exception("QnA aktiflik kontrolü başarısız; öneriler bloke edildi")
            active = None
        for qna_id, title in candidates:
            if active is None:
                exclude(ExclusionReason.ACTIVITY_LOOKUP_FAILED.value)
            elif qna_id not in active:
                exclude(ExclusionReason.INACTIVE_OR_MISSING.value)
            elif title not in safe:
                safe.append(title)
                safe_ids.append(qna_id)
    if trace is not None:
        trace.record_suggestions(
            retrieved_count=len(hits),
            offered_qna_ids=safe_ids,
            exclusion_reasons=reasons,
        )
    return safe
