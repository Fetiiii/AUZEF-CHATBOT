"""Cevap üretim pipeline'ı: Intent Analyzer + eligibility + Selector V2.

LLM açıkken current turn 1 veya en fazla 2 resolved intent'e dönüştürülür.
Her intent için QnA (Meili + Qdrant) ve gerekiyorsa filtreli Calendar adayları
tek tipli (typed) aday kümesine çevrilir, objektif eligibility uygulanır ve
Selector V2 strict ``SELECT``/``NONE`` kararı verir. LLM asla son kullanıcı
cevabı üretmez; seçilen curated cevap birebir döner.

Semantic ``NONE`` ve ``NO_ELIGIBLE_CANDIDATES`` finaldir: eşik fallback'i
çalışmaz. Selector ``INVALID_OUTPUT``/``MODEL_ERROR``/``TIMEOUT`` ise Phase 5
degraded-mode tasarımına kadar mevcut deterministik uyumluluk yedeğine
(takvim kelime eşleşmesi → Meili ≥0.90 → Qdrant >0.75) açık gerekçeyle düşer.
LLM kapalıyken aynı deterministik yol değişmeden kullanılır.
"""
import os
import logging
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
from services.llm_config import LLMCapability
from services.llm_types import (
    SELECTION_ERROR_OUTCOMES,
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
class LLMAnswerResult:
    answer: Optional[str]
    outcome: SelectionOutcome
    selected_qna_ids: list
    answer_count: int = 0
    intent_outcomes: tuple = ()


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
    try:
        result = retrieve_calendar_candidates(query, db, limit=1)
    except Exception:
        logger.exception("Calendar V2 retrieval hatası")
        result = failed_calendar_result()
    if trace is not None:
        trace.record_calendar_route(result.trace_snapshot, purpose="fallback")
    if not result.candidates:
        return None
    best = result.candidates[0]
    return format_calendar_answer(best.period, best.event, best.start_date, best.end_date)


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
    try:
        qdrant_hits = QDRANT_PROVIDER.search(query, limit=24)
        raw.extend(_decorate_hits(qdrant_hits, stage=0, provider_priority=0))
    except Exception:
        pass
    meili_hits = meili_search_safe(query, limit=5)
    raw.extend(_decorate_hits(meili_hits, stage=0, provider_priority=1))
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

    Sıfır eligible aday → selector çağrılmaz (``NO_ELIGIBLE_CANDIDATES``).
    Tek aday bile selector'dan geçer; otomatik bypass yoktur."""
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
    result = prov.ask_with_result(query, list(build.candidates))
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
    outcome = _OUTCOME_BY_STATUS.get(result.status, SelectionOutcome.INVALID_OUTPUT)
    return PoolSelection(outcome, result, build)


def _aggregate_outcome(outcomes: list[SelectionOutcome]) -> SelectionOutcome:
    """No-answer aggregation across intents.

    A semantic decision (NONE / no eligible candidate) in any intent is final
    and suppresses the error-compatibility fallback, which would otherwise
    re-answer the raw turn and could override that NONE. Only when every
    intent failed with a selector error does the compatibility path run.
    """
    if not outcomes:
        return SelectionOutcome.MODEL_ERROR
    if SelectionOutcome.SEMANTIC_NONE in outcomes:
        return SelectionOutcome.SEMANTIC_NONE
    if SelectionOutcome.NO_ELIGIBLE_CANDIDATES in outcomes:
        return SelectionOutcome.NO_ELIGIBLE_CANDIDATES
    if SelectionOutcome.INVALID_OUTPUT in outcomes:
        return SelectionOutcome.INVALID_OUTPUT
    if SelectionOutcome.TIMEOUT in outcomes:
        return SelectionOutcome.TIMEOUT
    return SelectionOutcome.MODEL_ERROR


def _llm_answer(
    query: str,
    db: Session,
    conversation_context: tuple[dict, ...] = (),
    routing_policy: RoutingGuardPolicy | None = None,
    trace: DecisionTrace | None = None,
) -> LLMAnswerResult:
    """Analyze the current turn, then run eligibility + Selector V2 per intent.

    Context is consumed only by the analyzer. Retrieval and selector receive
    the resolved intent, never the conversation. Each intent is independent:
    one intent's NONE never removes another intent's valid answer."""
    prov = get_llm_provider(db)
    if prov is None:
        raise RuntimeError("LLM sağlayıcısı yok (anahtar DB'de/env'de bulunamadı)")
    if trace is not None:
        trace.set_llm(enabled=True, configs=getattr(prov, "configs", None))

    previous_user_turns = _previous_user_turns(conversation_context)
    analysis_result = prov.analyze_intents_with_result(query, previous_user_turns)
    if trace is not None:
        analyzer_config = prov.effective_config(LLMCapability.INTENT_ANALYZER)
        trace.record_intent_analyzer(
            analysis_result,
            analyzer_config.to_dict(),
            config_fingerprint=analyzer_config.fingerprint,
            current_input_length=len(query),
            previous_user_context_count=len(previous_user_turns),
        )

    active_lookup = _active_qna_lookup(db)
    answers = []
    selected_qna_ids = []
    outcomes: list[SelectionOutcome] = []
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
        except Exception:
            logger.exception("Selector V2 intent hattı hatası")
            outcomes.append(SelectionOutcome.MODEL_ERROR)
            if trace is not None:
                trace.record_selector_pipeline_error(purpose=purpose)
            continue
        outcomes.append(selection.outcome)
        if trace is not None:
            selector_config = prov.effective_config(LLMCapability.SELECTOR)
            if selection.result is None:
                trace.record_selector_skipped(
                    purpose=purpose, outcome=selection.outcome.value
                )
            else:
                trace.record_selector(
                    selection.result,
                    outcome=selection.outcome.value,
                    config=selector_config.to_dict(),
                    config_fingerprint=selector_config.fingerprint,
                    candidate_refs=selection.build.trace_snapshot["selector_candidate_refs"],
                    candidate_kinds=selection.build.trace_snapshot["selector_candidate_kinds"],
                    candidate_qna_ids=selection.build.trace_snapshot["candidate_qna_ids"],
                    purpose=purpose,
                    used_in_final=selection.outcome is SelectionOutcome.SELECTED,
                )
        result = selection.result
        if (
            selection.outcome is SelectionOutcome.SELECTED
            and result.answer
            and result.answer not in answers
        ):
            answers.append(result.answer)
            if result.selected_qna_id is not None:
                selected_qna_ids.append(result.selected_qna_id)

    if answers:
        return LLMAnswerResult(
            "\n\n".join(answers), SelectionOutcome.SELECTED, selected_qna_ids,
            answer_count=len(answers), intent_outcomes=tuple(outcomes),
        )
    return LLMAnswerResult(
        None, _aggregate_outcome(outcomes), [], answer_count=0,
        intent_outcomes=tuple(outcomes),
    )


def _fallback_answer(
    query: str,
    db: Session,
    use_calendar: bool = True,
    routing_policy: RoutingGuardPolicy | None = None,
    trace: DecisionTrace | None = None,
    fallback_reason: str = "llm_disabled_or_unavailable",
) -> tuple:
    """Eşik tabanlı yedek zincir. (answer, source) döner; bulunamazsa (None, "none").

    ``use_calendar``: kelime tabanlı takvim kapısını çalıştır. Yalnızca LLM
    tamamen erişilemezken (kapalı/hata) True olmalı. LLM çalışıp "uygun yok"
    dediyse takvim zaten aday havuzundaydı ve LLM onu reddetti; o durumda bu
    kapı yeniden AÇILMAMALI (yoksa "vize sınavına nasıl çalışmalıyım" gibi
    sorular tekrar yanlışlıkla bir tarihe düşer)."""
    if use_calendar and is_date_query(query):
        cal = search_calendar(query, db, use_llm=False, trace=trace)
        if cal:
            if trace is not None:
                trace.record_fallback(
                    reason=fallback_reason, selected_source="academic_calendar"
                )
            return cal, "academic_calendar"

    policy = routing_policy or RoutingGuardPolicy.empty()
    hits = [
        hit for hit in meili_search_safe(query, limit=3)
        if policy.fallback_allows(hit)
    ]
    if hits and hits[0]["score"] >= MEILI_THERESHOLD:
        if trace is not None:
            trace.record_fallback(
                reason=fallback_reason,
                selected_source="meilisearch",
                selected_qna_id=_normalized_qna_id(hits[0].get("qna_id")),
            )
        return hits[0]["answer"], "meilisearch"

    try:
        qhits = [
            hit for hit in QDRANT_PROVIDER.search(query, limit=5)
            if policy.fallback_allows(hit)
        ]
        if qhits and qhits[0]["score"] > QDRANT_THERESHOLD:
            if trace is not None:
                trace.record_fallback(
                    reason=fallback_reason,
                    selected_source="qdrant_vector",
                    selected_qna_id=_normalized_qna_id(qhits[0].get("qna_id")),
                )
            return qhits[0]["answer"], "qdrant_vector"
    except Exception:
        pass

    if trace is not None:
        trace.record_fallback(reason=fallback_reason, selected_source="none")
    return None, "none"


def answer_question(
    query: str,
    db: Session,
    conversation_context: tuple[dict, ...] = (),
    trace: DecisionTrace | None = None,
) -> tuple:
    """Bir soruya cevap üretir. (answer, source) döner; cevap yoksa (None, "none").

    - Selector SELECT                   → seçilen curated cevap (source "llm").
    - Semantic NONE / eligible aday yok → final cevapsız; eşik/takvim yedeği YOK.
    - Selector INVALID_OUTPUT           → uyumluluk yedeği, takvim kapısı kapalı.
    - Selector MODEL_ERROR / TIMEOUT ya da hat hatası → uyumluluk yedeği
      (takvim kapısı dahil); Phase 5 degraded-mode ile yeniden tasarlanacak.
    - LLM kapalı                        → değişmemiş deterministik yol."""
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
    if trace is not None:
        trace.set_llm(enabled=llm_enabled, configs=None)
    if not llm_enabled:
        # LLM kapalı → Phase 0 deterministik yol değişmeden (Phase 5 konusu).
        return _fallback_answer(
            query,
            db,
            use_calendar=True,
            routing_policy=routing_policy,
            trace=trace,
            fallback_reason="llm_disabled_or_unavailable",
        )

    try:
        llm_result = _llm_answer(
            query, db, conversation_context, routing_policy, trace
        )
    except Exception as e:
        # Sağlayıcı yok / analyzer hattı çöktü: seçim hiç yapılamadı.
        logger.error(f"LLM ana yol hatası (yedeğe düşülüyor): {e}")
        if trace is not None:
            trace.record_selection_outcome(
                SelectionOutcome.MODEL_ERROR.value, fallback_allowed=True
            )
        return _fallback_answer(
            query,
            db,
            use_calendar=True,
            routing_policy=routing_policy,
            trace=trace,
            fallback_reason="llm_model_error",
        )

    outcome = llm_result.outcome
    if trace is not None:
        trace.record_selection_outcome(
            outcome.value,
            fallback_allowed=(
                llm_result.answer is None and outcome in SELECTION_ERROR_OUTCOMES
            ),
            intent_outcomes=[item.value for item in llm_result.intent_outcomes],
        )
    if llm_result.answer:
        if trace is not None:
            trace.finalize(
                outcome="answer",
                source="llm",
                qna_ids=llm_result.selected_qna_ids,
                answer_count=llm_result.answer_count,
            )
        return llm_result.answer, "llm"

    if outcome not in SELECTION_ERROR_OUTCOMES:
        # SEMANTIC_NONE / NO_ELIGIBLE_CANDIDATES: final no-answer. Meili,
        # Qdrant ve Calendar yedekleri bu karardan sonra ÇALIŞMAZ.
        return None, "none"

    # Selector sistem hatası: Phase 5'e kadar mevcut uyumluluk yedeği. Geçersiz
    # çıktı Phase 0'daki gibi takvim kapısını açmaz; model hatası/timeout açar.
    return _fallback_answer(
        query,
        db,
        use_calendar=outcome is not SelectionOutcome.INVALID_OUTPUT,
        routing_policy=routing_policy,
        trace=trace,
        fallback_reason={
            SelectionOutcome.INVALID_OUTPUT: "selector_invalid_output",
            SelectionOutcome.TIMEOUT: "selector_timeout",
            SelectionOutcome.MODEL_ERROR: "selector_model_error",
        }[outcome],
    )


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
