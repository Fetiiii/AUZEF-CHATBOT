"""Cevap üretim pipeline'ı: Intent Analyzer + LLM selector + V1 fallback.

Tasarım: Takvim artık bir "ön kapı" değil, aday havuzundaki bir kayıttır.
LLM açıkken current turn 1 veya en fazla 2 resolved intent'e dönüştürülür;
her intent QnA + takvim adaylarından oluşan tek havuzdan birebir seçtirilir.
LLM asla son kullanıcı cevabı üretmez.
LLM kapalı/hatalı/seçim yoksa eşik tabanlı yola (takvim kelime eşleşmesi →
Meili ≥0.90 → Qdrant >0.75) düşülür.
"""
import math
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
from core.deps import get_llm_provider, is_llm_enabled, meili_search_safe, QDRANT_PROVIDER
from services.routing_guards import RoutingGuardPolicy
from services.decision_trace import DecisionTrace
from services.llm_config import LLMCapability
from services.llm_types import (
    LLMOutcomeStatus,
    LLMParseStatus,
    SelectorResult,
)

logger = logging.getLogger("auzef")


@dataclass(frozen=True)
class CandidatePoolBuild:
    candidates: list
    trace_snapshot: dict


@dataclass(frozen=True)
class PoolSelection:
    result: Optional[SelectorResult]
    reached_llm: bool
    candidate_count: int
    candidate_qna_ids: list


@dataclass(frozen=True)
class LLMAnswerResult:
    answer: Optional[str]
    status: LLMOutcomeStatus
    selected_qna_ids: list
    answer_count: int = 0


class LLMPipelineError(RuntimeError):
    def __init__(self, message: str, status: LLMOutcomeStatus):
        super().__init__(message)
        self.status = status


def _previous_user_turns(conversation_context: tuple[dict, ...]) -> tuple[str, ...]:
    """Return only the last two previous user turns for Intent Analyzer V2."""
    return tuple([
        item["content"].strip()
        for item in conversation_context
        if item.get("role") == "user" and item.get("content", "").strip()
    ][-2:])


# Birbirine çok yakın retrieval skorları sağlayıcı/float ayrıntıları yüzünden
# son basamaklarda oynayabilir. Bu skorları aynı kovaya alıp QnA kimliğiyle
# bağlamak, aynı aday kümesinin prompt'ta aynı sırayı almasını sağlar.
RETRIEVAL_SCORE_DECIMALS = 5


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


def _score_bucket(value) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(score):
        return 0.0
    return round(score, RETRIEVAL_SCORE_DECIMALS)


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


def _candidate_identity(candidate: dict) -> tuple:
    qna_id = _normalized_qna_id(candidate.get("qna_id"))
    if qna_id is not None:
        return ("qna", qna_id)
    # Eski/eksik indeks sonuçlarında farklı QnA'ları cevap metnine göre
    # birleştirmeyiz. Sağlayıcı + teknik id + metin yalnızca güvenli bir
    # deterministik yedektir; normal sözleşmede qna_id her zaman bulunur.
    return (
        "legacy",
        str(candidate.get("source") or ""),
        str(candidate.get("id") or ""),
        str(candidate.get("question") or ""),
        str(candidate.get("answer") or ""),
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


def _build_candidate_pool_result(
    query: str,
    calendar_entries: list,
    routing_policy: RoutingGuardPolicy | None = None,
) -> CandidatePoolBuild:
    """Bir (alt) soru için LLM seçiciye verilecek aday havuzunu kurar:
    Qdrant (semantik) + Meili (anahtar kelime) QnA adayları + Calendar V2'nin
    önceden filtrelediği küçük ve bounded takvim adaylarını birleştirir.

    QnA'lar cevap metnine göre değil gerçek ``qna_id`` ile tekilleştirilir:
    aynı kaydın kanonik/alias Qdrant noktaları tek adaya inerken aynı cevabı
    paylaşan farklı QnA'lar korunur. Sıra retrieval aşaması, sağlayıcı, beş
    ondalığa yuvarlanmış skor ve QnA kimliğiyle tamamen belirlenir."""
    pool = []
    candidate_order = []

    # Takvim adaylarını havuzun BAŞINA koy. "Final ne zaman" gibi bir tarih
    # sorusunda, aynı konudaki genel/yönlendirici bir QnA ("sınav tarihleri
    # akademik takvimden yayınlanır") çoğu zaman aramada üst sırada çıkıyor;
    # somut takvim tarihi listenin sonunda kalırsa LLM konum yanlılığıyla genel
    # cevabı seçebiliyor. Takvimi öne almak (prompt'taki "somut tarihi tercih et"
    # kuralıyla birlikte) somut tarihin seçilmesini sağlar.
    sorted_calendar = sorted(
        calendar_entries,
        key=lambda entry: (
            str(entry.period or "").casefold(),
            str(entry.event or "").casefold(),
            str(entry.start_date or ""),
            str(entry.end_date or ""),
            _stable_id_key(getattr(entry, "id", None)),
        ),
    )
    seen_calendar = set()
    for e in sorted_calendar:
        # period ("Güz"/"Bahar") sorunun hangi döneme ait olduğunu LLM'in ayırt
        # edebilmesi için soru metnine dahil edilir.
        calendar_key = (
            str(e.period or ""),
            str(e.event or ""),
            str(e.start_date or ""),
            str(e.end_date or ""),
        )
        if calendar_key in seen_calendar:
            continue
        seen_calendar.add(calendar_key)
        pool.append({
            "question": f"{e.period} {e.event}".strip() + " ne zaman?",
            "answer": format_calendar_answer(e.period, e.event, e.start_date, e.end_date),
        })
        candidate_order.append({
            "order": len(pool),
            "candidate_type": "academic_calendar",
            "calendar_id": _normalized_qna_id(getattr(e, "id", None)),
            "qna_id": None,
            "source": "academic_calendar",
            "retrieval_stage": "calendar",
            "score": None,
        })

    raw = []
    qdrant_hits = []
    meili_hits = []
    try:
        qdrant_hits = QDRANT_PROVIDER.search(query, limit=24)
        raw.extend(
            _decorate_hits(qdrant_hits, stage=0, provider_priority=0)
        )
    except Exception:
        pass
    meili_hits = meili_search_safe(query, limit=5)
    raw.extend(_decorate_hits(meili_hits, stage=0, provider_priority=1))

    seen = set()
    eligible_after_guard_count = 0
    guard_rejections = {}
    for candidate in sorted(raw, key=_candidate_sort_key):
        if routing_policy is not None:
            decision = routing_policy.decision(candidate.get("qna_id"))
            if not decision.selector_allowed:
                reason = decision.reason or "guard_rejected"
                guard_rejections[reason] = guard_rejections.get(reason, 0) + 1
                continue
        eligible_after_guard_count += 1
        identity = _candidate_identity(candidate)
        if identity in seen or not candidate.get("answer"):
            continue
        seen.add(identity)
        pool.append(
            {
                "qna_id": _normalized_qna_id(candidate.get("qna_id")),
                "question": candidate.get("question"),
                "answer": candidate.get("answer"),
            }
        )
        candidate_order.append({
            "order": len(pool),
            "candidate_type": "qna",
            "qna_id": _normalized_qna_id(candidate.get("qna_id")),
            "source": candidate.get("source"),
            "retrieval_stage": (
                "context" if candidate.get("_stage") == 1 else "current"
            ),
            "score": _score_bucket(candidate.get("score")),
        })
    qna_ids = [item["qna_id"] for item in candidate_order if item.get("qna_id") is not None]
    return CandidatePoolBuild(
        candidates=pool,
        trace_snapshot={
            "calendar_candidate_count": len(seen_calendar),
            "qdrant_candidate_count": len(qdrant_hits),
            "meili_candidate_count": len(meili_hits),
            "context_qdrant_candidate_count": 0,
            "context_meili_candidate_count": 0,
            "deduped_candidate_count": len(pool),
            "eligible_after_guard_count": eligible_after_guard_count + len(seen_calendar),
            "guard_rejections": guard_rejections,
            "candidate_qna_ids": qna_ids,
            "candidate_order": candidate_order,
        },
    )


def _build_candidate_pool(
    query: str,
    calendar_entries: list,
    routing_policy: RoutingGuardPolicy | None = None,
) -> list:
    """Compatibility wrapper retaining the Phase 0 candidate-list contract."""
    return _build_candidate_pool_result(
        query, calendar_entries, routing_policy
    ).candidates


def _select_from_pool(
    query: str,
    calendar_entries: list,
    prov,
    routing_policy: RoutingGuardPolicy | None = None,
    trace: DecisionTrace | None = None,
    trace_purpose: str = "selector",
) -> PoolSelection:
    """Bir (alt) soru için aday havuzunu kurup LLM'e birebir seçtirir.
    Typed sonucu, aday sayısını ve LLM'e ulaşılıp ulaşılmadığını döner."""
    build = _build_candidate_pool_result(
        query,
        calendar_entries,
        routing_policy,
    )
    candidates = build.candidates
    if trace is not None:
        snapshot = {**build.trace_snapshot, "purpose": trace_purpose}
        trace.record_retrieval(snapshot)
    if not candidates:
        return PoolSelection(None, False, 0, [])
    if hasattr(prov, "ask_with_result"):
        result = prov.ask_with_result(query, candidates)
    else:
        # Compatibility for repository-local/custom V1 provider doubles.
        answer = prov.ask(query, candidates)
        selected_index = next(
            (i for i, item in enumerate(candidates) if item.get("answer") == answer),
            None,
        )
        selected = candidates[selected_index] if selected_index is not None else {}
        result = SelectorResult(
            status=(
                LLMOutcomeStatus.SUCCESS if answer else LLMOutcomeStatus.SEMANTIC_NONE
            ),
            parse_status=(
                LLMParseStatus.SUCCESS if answer else LLMParseStatus.SEMANTIC_NONE
            ),
            answer=answer,
            selected_index=selected_index,
            selected_qna_id=selected.get("qna_id"),
        )
    if result.selected_index is not None:
        descriptor = build.trace_snapshot["candidate_order"][result.selected_index]
        result = replace(
            result,
            selected_candidate_source=descriptor.get("source"),
        )
    return PoolSelection(
        result=result,
        reached_llm=True,
        candidate_count=len(candidates),
        candidate_qna_ids=build.trace_snapshot["candidate_qna_ids"],
    )


def _llm_answer(
    query: str,
    db: Session,
    conversation_context: tuple[dict, ...] = (),
    routing_policy: RoutingGuardPolicy | None = None,
    trace: DecisionTrace | None = None,
) -> LLMAnswerResult:
    """Analyze the current turn, then select once for each resolved intent.

    Context is consumed only by the analyzer. Retrieval and selector receive the
    resolved intent, never the full conversation. Existing candidate, selector,
    composition, and fallback semantics remain unchanged."""
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

    answers = []
    selected_qna_ids = []
    any_success = False  # en az bir alt soruda LLM'e ULAŞILDI mı?
    observed_non_success = []
    observed_errors = []
    for position, intent in enumerate(analysis_result.analysis.intents, start=1):
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
            trace.record_calendar_route(
                calendar_result.trace_snapshot,
                purpose=f"intent_{position}",
            )
        try:
            selection = _select_from_pool(
                intent.resolved_text,
                list(calendar_result.candidates),
                prov,
                routing_policy,
                trace,
                f"intent_{position}",
            )
        except Exception:
            continue
        if not selection.reached_llm or selection.result is None:
            continue
        result = selection.result
        if trace is not None:
            trace.record_selector(
                result,
                config=prov.effective_config(LLMCapability.SELECTOR).to_dict(),
                candidate_count=selection.candidate_count,
                candidate_qna_ids=selection.candidate_qna_ids,
                purpose=f"intent_{position}",
                used_in_final=result.answer is not None,
            )
        if result.status in (LLMOutcomeStatus.MODEL_ERROR, LLMOutcomeStatus.TIMEOUT):
            observed_errors.append(result.status)
            continue
        any_success = True
        observed_non_success.append(result.status)
        if result.answer and result.answer not in answers:
            answers.append(result.answer)
            if result.selected_qna_id is not None:
                selected_qna_ids.append(result.selected_qna_id)

    if answers:
        return LLMAnswerResult(
            "\n\n".join(answers), LLMOutcomeStatus.SUCCESS, selected_qna_ids,
            answer_count=len(answers),
        )
    if not any_success:
        status = (
            LLMOutcomeStatus.TIMEOUT
            if LLMOutcomeStatus.TIMEOUT in observed_errors
            else LLMOutcomeStatus.MODEL_ERROR
        )
        raise LLMPipelineError("LLM tüm intent'lerde erişilemedi", status)
    aggregate_status = (
        LLMOutcomeStatus.SEMANTIC_NONE
        if LLMOutcomeStatus.SEMANTIC_NONE in observed_non_success
        else LLMOutcomeStatus.INVALID_OUTPUT
    )
    return LLMAnswerResult(None, aggregate_status, [], answer_count=0)


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

    - LLM açık ve seçim yaptı        → o cevap (source "llm").
    - LLM açık ama "uygun yok" dedi   → yüksek-güven eşik hit'ine bak, ama takvim
      kelime kapısını AÇMA (takvim zaten havuzdaydı, LLM reddetti).
    - LLM kapalı ya da hata verdi     → tam eski eşik davranışı (takvim kapısı dahil)."""
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
    if llm_enabled:
        try:
            llm_result = _llm_answer(
                query, db, conversation_context, routing_policy, trace
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
            # LLM çalıştı ama uygun aday yok → takvim kapısı olmadan eşik yedeği.
            return _fallback_answer(
                query,
                db,
                use_calendar=False,
                routing_policy=routing_policy,
                trace=trace,
                fallback_reason=(
                    "selector_semantic_none"
                    if llm_result.status is LLMOutcomeStatus.SEMANTIC_NONE
                    else "selector_invalid_output"
                ),
            )
        except Exception as e:
            logger.error(f"LLM ana yol hatası (yedeğe düşülüyor): {e}")
            failure_status = (
                e.status if isinstance(e, LLMPipelineError)
                else LLMOutcomeStatus.MODEL_ERROR
            )

    # LLM kapalı ya da hata → tam eski davranış.
    return _fallback_answer(
        query,
        db,
        use_calendar=True,
        routing_policy=routing_policy,
        trace=trace,
        fallback_reason=(
            (
                "llm_timeout"
                if failure_status is LLMOutcomeStatus.TIMEOUT
                else "llm_model_error"
            )
            if llm_enabled
            else "llm_disabled_or_unavailable"
        ),
    )
