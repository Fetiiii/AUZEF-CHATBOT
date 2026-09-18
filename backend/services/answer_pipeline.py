"""Cevap üretim pipeline'ı: LLM seçici ana yol + eşik tabanlı yedek zincir.

Tasarım: Takvim artık bir "ön kapı" değil, aday havuzundaki bir kayıttır.
LLM açıkken her soru (alt sorulara bölünüp) QnA + takvim adaylarından oluşan
TEK bir havuzdan birebir (verbatim) seçtirilir; LLM asla cevap üretmez.
LLM kapalı/hatalı/seçim yoksa eşik tabanlı yola (takvim kelime eşleşmesi →
Meili ≥0.90 → Qdrant >0.75) düşülür.
"""
import math
import os
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from typing import Optional

from sqlalchemy.orm import Session

from core.database import AcademicCalendar
from services.calendar_utils import format_calendar_answer, match_calendar_entry
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


def _contextual_retrieval_query(query: str, conversation_context: tuple[dict, ...]) -> str:
    """Son iki kullanıcı mesajını güncel retrieval sorgusuna ekler."""
    previous_user_messages = [
        item["content"].strip()
        for item in conversation_context
        if item.get("role") == "user" and item.get("content", "").strip()
    ][-2:]
    if not previous_user_messages:
        return query
    return "\n".join([*previous_user_messages, query])


def _selector_question(query: str, conversation_context: tuple[dict, ...]) -> str:
    """Geçmiş ile güncel soruyu seçici için açıkça ayırır."""
    if not conversation_context:
        return query
    role_labels = {"user": "Öğrenci", "bot": "Asistan"}
    history = "\n".join(
        f"{role_labels.get(item.get('role'), 'Mesaj')}: {item.get('content', '').strip()}"
        for item in conversation_context
        if item.get("content", "").strip()
    )
    if not history:
        return query
    return (
        "Önceki konuşma (yalnız güncel mesajı anlamlandırmak için):\n"
        f"{history}\n\nGüncel öğrenci mesajı (kararın ana girdisi):\n{query}"
    )


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
    """Kullanıcı sorusunun tarih/takvim ile ilgili olup olmadığını belirler."""
    q = query.lower()
    date_patterns = ["ne zaman", "hangi tarih", "hangi gün", "tarihi ne", "tarihleri ne",
                     "kaçında", "kaçınca", "ayın kaçı", "ne vakit"]
    if any(p in q for p in date_patterns):
        return True
    event_keywords = ["büt", "bütünleme", "vize", "final", "ara sınav", "bitirme sınavı",
                      "telafi", "kayıt yenileme", "ders seçim", "ders ekle", "ekle-sil",
                      "muafiyet", "mezuniyet", "üç ders sınavı", "ikinci üniversite kayıt",
                      "eğitim öğretim başlangıcı", "akademik takvim"]
    if any(ew in q for ew in event_keywords):
        return True
    return False


def search_calendar(query: str, db: Session, use_llm: bool) -> Optional[str]:
    """Akademik takvim tablosundan tarih sorusuna cevap arar."""
    entries = db.query(AcademicCalendar).all()
    if not entries:
        return None

    prov = get_llm_provider(db) if use_llm else None
    if prov is not None:
        candidates = [
            {
                "question": f"{e.event} ne zaman?",
                "answer": format_calendar_answer(e.period, e.event, e.start_date, e.end_date),
            }
            for e in entries
        ]
        try:
            answer = prov.ask(query, candidates)
            if answer:
                return answer
        except Exception as e:
            logger.error(f"Takvim LLM hatası: {e}")

    # Yedek: kelime örtüşmesine göre en uygun kaydı seç (LLM'siz de doğru çalışır)
    best = match_calendar_entry(query, entries)
    if best:
        return format_calendar_answer(best.period, best.event, best.start_date, best.end_date)

    return None


def _build_candidate_pool_result(
    query: str,
    calendar_entries: list,
    conversation_context: tuple[dict, ...] = (),
    routing_policy: RoutingGuardPolicy | None = None,
) -> CandidatePoolBuild:
    """Bir (alt) soru için LLM seçiciye verilecek aday havuzunu kurar:
    Qdrant (semantik) + Meili (anahtar kelime) QnA adayları + TÜM takvim
    kayıtları. Takvim kayıtları, kelime örtüşmesinin kaçırdığı ("güz dönemi
    başlangıcı" gibi) soruları LLM semantik olarak yakalayabilsin diye
    tümüyle eklenir (yalnızca 19 kayıt).

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
    context_qdrant_hits = []
    context_meili_hits = []
    try:
        qdrant_hits = QDRANT_PROVIDER.search(query, limit=24)
        raw.extend(
            _decorate_hits(qdrant_hits, stage=0, provider_priority=0)
        )
    except Exception:
        pass
    meili_hits = meili_search_safe(query, limit=5)
    raw.extend(_decorate_hits(meili_hits, stage=0, provider_priority=1))
    contextual_query = _contextual_retrieval_query(query, conversation_context)
    if contextual_query != query:
        try:
            context_qdrant_hits = QDRANT_PROVIDER.search(contextual_query, limit=12)
            raw.extend(
                _decorate_hits(
                    context_qdrant_hits,
                    stage=1,
                    provider_priority=0,
                )
            )
        except Exception:
            pass
        context_meili_hits = meili_search_safe(contextual_query, limit=3)
        raw.extend(
            _decorate_hits(
                context_meili_hits,
                stage=1,
                provider_priority=1,
            )
        )

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
            "context_qdrant_candidate_count": len(context_qdrant_hits),
            "context_meili_candidate_count": len(context_meili_hits),
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
    conversation_context: tuple[dict, ...] = (),
    routing_policy: RoutingGuardPolicy | None = None,
) -> list:
    """Compatibility wrapper retaining the Phase 0 candidate-list contract."""
    return _build_candidate_pool_result(
        query, calendar_entries, conversation_context, routing_policy
    ).candidates


def _select_from_pool(
    query: str,
    calendar_entries: list,
    prov,
    conversation_context: tuple[dict, ...] = (),
    routing_policy: RoutingGuardPolicy | None = None,
    trace: DecisionTrace | None = None,
    trace_purpose: str = "selector",
) -> PoolSelection:
    """Bir (alt) soru için aday havuzunu kurup LLM'e birebir seçtirir.
    Typed sonucu, aday sayısını ve LLM'e ulaşılıp ulaşılmadığını döner."""
    build = _build_candidate_pool_result(
        query,
        calendar_entries,
        conversation_context,
        routing_policy,
    )
    candidates = build.candidates
    if trace is not None:
        snapshot = {**build.trace_snapshot, "purpose": trace_purpose}
        trace.record_retrieval(snapshot)
    if not candidates:
        return PoolSelection(None, False, 0, [])
    if hasattr(prov, "ask_with_result"):
        result = prov.ask_with_result(
            _selector_question(query, conversation_context), candidates
        )
    else:
        # Compatibility for repository-local/custom V1 provider doubles.
        answer = prov.ask(_selector_question(query, conversation_context), candidates)
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
    """Ana yol: soruyu alt sorulara böler, her alt soru için birleşik aday
    havuzundan (QnA + takvim) birebir cevap seçtirir, cevapları birleştirir.

    Latency: 'soruyu böl' (split) ile 'tek soru olsaydı seç' (spekülatif seçim)
    AYNI ANDA (paralel) çalıştırılır. Soru tek çıkarsa spekülatif seçim sonucu
    kullanılır — iki LLM çağrısı ardışık değil yan yana olduğundan tek soru
    ~yarı sürede döner. Çoklu çıkarsa spekülatif sonuç atılır ve her alt soru
    için ayrı seçim yapılır (çoklu-soru doğruluğu korunur, split her zaman
    çalıştığı için noktalama olmayan çoklu sorular da yakalanır).

    Dönüş typed cevap + outcome + seçilen QnA kimlikleridir. LLM'e HİÇ
    ulaşılamazsa typed pipeline hatası yükseltir; dış fallback davranışı
    Phase 0 ile aynı kalır."""
    calendar_entries = db.query(AcademicCalendar).all()
    prov = get_llm_provider(db)
    if prov is None:
        raise RuntimeError("LLM sağlayıcısı yok (anahtar DB'de/env'de bulunamadı)")
    if trace is not None:
        trace.set_llm(enabled=True, configs=getattr(prov, "configs", None))

    ex = ThreadPoolExecutor(max_workers=2)
    try:
        split_future = ex.submit(
            prov.split_questions_with_result
            if hasattr(prov, "split_questions_with_result")
            else prov.split_questions,
            query,
        )
        single_future = ex.submit(
            _select_from_pool,
            query,
            calendar_entries,
            prov,
            conversation_context,
            routing_policy,
            trace,
            "speculative_full_query",
        )

        split_result = split_future.result()
        if hasattr(split_result, "subquestions"):
            sub_questions = split_result.subquestions or [query]
            if trace is not None:
                config = prov.effective_config(LLMCapability.INTENT_ANALYZER).to_dict()
                trace.record_splitter(split_result, config, input_length=len(query))
        else:
            sub_questions = split_result or [query]

        if len(sub_questions) <= 1:
            # Tek soru: paralel yürüyen spekülatif seçimi kullan.
            try:
                selection = single_future.result()
            except Exception:
                raise RuntimeError("LLM erişilemedi (tek soru seçimi)")
            if not selection.reached_llm:
                raise RuntimeError("aday havuzu boş (retrieval)")
            result = selection.result
            if result is None:
                raise RuntimeError("LLM seçim sonucu yok")
            if trace is not None:
                trace.record_selector(
                    result,
                    config=prov.effective_config(LLMCapability.SELECTOR).to_dict(),
                    candidate_count=selection.candidate_count,
                    candidate_qna_ids=selection.candidate_qna_ids,
                    purpose="speculative_full_query",
                    used_in_final=True,
                )
            if result.status in (LLMOutcomeStatus.MODEL_ERROR, LLMOutcomeStatus.TIMEOUT):
                raise LLMPipelineError(
                    "LLM erişilemedi (tek soru seçimi)", result.status
                )
            return LLMAnswerResult(
                answer=result.answer,
                status=result.status,
                selected_qna_ids=(
                    [result.selected_qna_id]
                    if result.selected_qna_id is not None else []
                ),
                answer_count=1 if result.answer else 0,
            )
    finally:
        # Çoklu soruda spekülatif seçim boşa gider; arka planda bitmesine izin
        # ver, sonucunu bekleme (wait=False).
        ex.shutdown(wait=False)

    # Çoklu soru: her alt soru için ayrı seçim (spekülatif sonuç atıldı).
    answers = []
    selected_qna_ids = []
    any_success = False  # en az bir alt soruda LLM'e ULAŞILDI mı?
    observed_non_success = []
    observed_errors = []
    for sub_q in sub_questions:
        try:
            selection = _select_from_pool(
                sub_q,
                calendar_entries,
                prov,
                conversation_context,
                routing_policy,
                trace,
                "subquestion",
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
                purpose="subquestion",
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
        raise LLMPipelineError("LLM tüm alt sorularda erişilemedi", status)
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
        cal = search_calendar(query, db, use_llm=False)
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
