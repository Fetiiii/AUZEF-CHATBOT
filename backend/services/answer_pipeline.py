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
from typing import Optional

from sqlalchemy.orm import Session

from core.database import AcademicCalendar
from services.calendar_utils import format_calendar_answer, match_calendar_entry
from core.deps import get_llm_provider, is_llm_enabled, meili_search_safe, QDRANT_PROVIDER

logger = logging.getLogger("auzef")


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


def _build_candidate_pool(
    query: str, calendar_entries: list, conversation_context: tuple[dict, ...] = ()
) -> list:
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

    raw = []
    try:
        raw.extend(
            _decorate_hits(
                QDRANT_PROVIDER.search(query, limit=24), stage=0, provider_priority=0
            )
        )
    except Exception:
        pass
    raw.extend(
        _decorate_hits(meili_search_safe(query, limit=5), stage=0, provider_priority=1)
    )
    contextual_query = _contextual_retrieval_query(query, conversation_context)
    if contextual_query != query:
        try:
            raw.extend(
                _decorate_hits(
                    QDRANT_PROVIDER.search(contextual_query, limit=12),
                    stage=1,
                    provider_priority=0,
                )
            )
        except Exception:
            pass
        raw.extend(
            _decorate_hits(
                meili_search_safe(contextual_query, limit=3),
                stage=1,
                provider_priority=1,
            )
        )

    seen = set()
    for candidate in sorted(raw, key=_candidate_sort_key):
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
    return pool


def _select_from_pool(
    query: str,
    calendar_entries: list,
    prov,
    conversation_context: tuple[dict, ...] = (),
) -> tuple:
    """Bir (alt) soru için aday havuzunu kurup LLM'e birebir seçtirir.
    (answer_or_None, reached_llm) döner. reached_llm=False YALNIZCA havuz
    boşsa olur (retrieval çöktü ve takvim de yoksa) — bu durumda LLM hiç
    çağrılmamıştır. ``prov.ask`` hata yükseltebilir (çağıran yakalar)."""
    candidates = _build_candidate_pool(query, calendar_entries, conversation_context)
    if not candidates:
        return None, False
    return prov.ask(_selector_question(query, conversation_context), candidates), True


def _llm_answer(
    query: str, db: Session, conversation_context: tuple[dict, ...] = ()
) -> Optional[str]:
    """Ana yol: soruyu alt sorulara böler, her alt soru için birleşik aday
    havuzundan (QnA + takvim) birebir cevap seçtirir, cevapları birleştirir.

    Latency: 'soruyu böl' (split) ile 'tek soru olsaydı seç' (spekülatif seçim)
    AYNI ANDA (paralel) çalıştırılır. Soru tek çıkarsa spekülatif seçim sonucu
    kullanılır — iki LLM çağrısı ardışık değil yan yana olduğundan tek soru
    ~yarı sürede döner. Çoklu çıkarsa spekülatif sonuç atılır ve her alt soru
    için ayrı seçim yapılır (çoklu-soru doğruluğu korunur, split her zaman
    çalıştığı için noktalama olmayan çoklu sorular da yakalanır).

    Dönüş: birleşik cevap ya da 'uygun aday yok' için None. LLM'e HİÇ
    ulaşılamazsa (tüm ask'ler hata) RuntimeError yükseltir ki _answer_question
    tam eşik yedeğine (takvim kapısı dahil) düşsün."""
    calendar_entries = db.query(AcademicCalendar).all()
    prov = get_llm_provider(db)
    if prov is None:
        raise RuntimeError("LLM sağlayıcısı yok (anahtar DB'de/env'de bulunamadı)")

    ex = ThreadPoolExecutor(max_workers=2)
    try:
        split_future = ex.submit(prov.split_questions, query)
        single_future = ex.submit(
            _select_from_pool, query, calendar_entries, prov, conversation_context
        )

        sub_questions = split_future.result() or [query]

        if len(sub_questions) <= 1:
            # Tek soru: paralel yürüyen spekülatif seçimi kullan.
            try:
                ans, reached = single_future.result()
            except Exception:
                raise RuntimeError("LLM erişilemedi (tek soru seçimi)")
            if not reached:
                raise RuntimeError("aday havuzu boş (retrieval)")
            return ans  # None ise "uygun yok" (takvim kapısı açılmadan öneriye gider)
    finally:
        # Çoklu soruda spekülatif seçim boşa gider; arka planda bitmesine izin
        # ver, sonucunu bekleme (wait=False).
        ex.shutdown(wait=False)

    # Çoklu soru: her alt soru için ayrı seçim (spekülatif sonuç atıldı).
    answers = []
    any_success = False  # en az bir alt soruda LLM'e ULAŞILDI mı?
    for sub_q in sub_questions:
        try:
            ans, reached = _select_from_pool(
                sub_q, calendar_entries, prov, conversation_context
            )
            if reached:
                any_success = True
        except Exception:
            continue
        if ans and ans not in answers:
            answers.append(ans)

    if answers:
        return "\n\n".join(answers)
    if not any_success:
        raise RuntimeError("LLM tüm alt sorularda erişilemedi")
    return None


def _fallback_answer(query: str, db: Session, use_calendar: bool = True) -> tuple:
    """Eşik tabanlı yedek zincir. (answer, source) döner; bulunamazsa (None, "none").

    ``use_calendar``: kelime tabanlı takvim kapısını çalıştır. Yalnızca LLM
    tamamen erişilemezken (kapalı/hata) True olmalı. LLM çalışıp "uygun yok"
    dediyse takvim zaten aday havuzundaydı ve LLM onu reddetti; o durumda bu
    kapı yeniden AÇILMAMALI (yoksa "vize sınavına nasıl çalışmalıyım" gibi
    sorular tekrar yanlışlıkla bir tarihe düşer)."""
    if use_calendar and is_date_query(query):
        cal = search_calendar(query, db, use_llm=False)
        if cal:
            return cal, "academic_calendar"

    hits = meili_search_safe(query, limit=3)
    if hits and hits[0]["score"] >= MEILI_THERESHOLD:
        return hits[0]["answer"], "meilisearch"

    try:
        qhits = QDRANT_PROVIDER.search(query, limit=5)
        if qhits and qhits[0]["score"] > QDRANT_THERESHOLD:
            return qhits[0]["answer"], "qdrant_vector"
    except Exception:
        pass

    return None, "none"


def answer_question(
    query: str, db: Session, conversation_context: tuple[dict, ...] = ()
) -> tuple:
    """Bir soruya cevap üretir. (answer, source) döner; cevap yoksa (None, "none").

    - LLM açık ve seçim yaptı        → o cevap (source "llm").
    - LLM açık ama "uygun yok" dedi   → yüksek-güven eşik hit'ine bak, ama takvim
      kelime kapısını AÇMA (takvim zaten havuzdaydı, LLM reddetti).
    - LLM kapalı ya da hata verdi     → tam eski eşik davranışı (takvim kapısı dahil)."""
    if is_llm_enabled(db):
        try:
            ans = _llm_answer(query, db, conversation_context)
            if ans:
                return ans, "llm"
            # LLM çalıştı ama uygun aday yok → takvim kapısı olmadan eşik yedeği.
            return _fallback_answer(query, db, use_calendar=False)
        except Exception as e:
            logger.error(f"LLM ana yol hatası (yedeğe düşülüyor): {e}")

    # LLM kapalı ya da hata → tam eski davranış.
    return _fallback_answer(query, db, use_calendar=True)
