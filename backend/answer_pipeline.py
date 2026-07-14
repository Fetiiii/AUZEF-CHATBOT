"""Cevap üretim pipeline'ı: LLM seçici ana yol + eşik tabanlı yedek zincir.

Tasarım: Takvim artık bir "ön kapı" değil, aday havuzundaki bir kayıttır.
LLM açıkken her soru (alt sorulara bölünüp) QnA + takvim adaylarından oluşan
TEK bir havuzdan birebir (verbatim) seçtirilir; LLM asla cevap üretmez.
LLM kapalı/hatalı/seçim yoksa eşik tabanlı yola (takvim kelime eşleşmesi →
Meili ≥0.90 → Qdrant >0.75) düşülür.
"""
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from sqlalchemy.orm import Session

from database import AcademicCalendar
from calendar_utils import format_calendar_answer, match_calendar_entry
from deps import get_llm_provider, is_llm_enabled, meili_search_safe, QDRANT_PROVIDER

logger = logging.getLogger("auzef")


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


def _build_candidate_pool(query: str, calendar_entries: list) -> list:
    """Bir (alt) soru için LLM seçiciye verilecek aday havuzunu kurar:
    Qdrant (semantik) + Meili (anahtar kelime) QnA adayları + TÜM takvim
    kayıtları. Takvim kayıtları, kelime örtüşmesinin kaçırdığı ("güz dönemi
    başlangıcı" gibi) soruları LLM semantik olarak yakalayabilsin diye
    tümüyle eklenir (yalnızca 19 kayıt). Cevaba göre tekilleştirilir."""
    pool = []

    # Takvim adaylarını havuzun BAŞINA koy. "Final ne zaman" gibi bir tarih
    # sorusunda, aynı konudaki genel/yönlendirici bir QnA ("sınav tarihleri
    # akademik takvimden yayınlanır") çoğu zaman aramada üst sırada çıkıyor;
    # somut takvim tarihi listenin sonunda kalırsa LLM konum yanlılığıyla genel
    # cevabı seçebiliyor. Takvimi öne almak (prompt'taki "somut tarihi tercih et"
    # kuralıyla birlikte) somut tarihin seçilmesini sağlar.
    for e in calendar_entries:
        # period ("Güz"/"Bahar") sorunun hangi döneme ait olduğunu LLM'in ayırt
        # edebilmesi için soru metnine dahil edilir.
        pool.append({
            "question": f"{e.period} {e.event}".strip() + " ne zaman?",
            "answer": format_calendar_answer(e.period, e.event, e.start_date, e.end_date),
        })

    raw = []
    try:
        raw.extend(QDRANT_PROVIDER.search(query, limit=8))
    except Exception:
        pass
    raw.extend(meili_search_safe(query, limit=5))
    pool.extend({"question": c.get("question"), "answer": c.get("answer")} for c in raw)

    seen = set()
    unique = []
    for c in pool:
        a = c["answer"]
        if a and a not in seen:
            seen.add(a)
            unique.append(c)
    return unique


def _select_from_pool(query: str, calendar_entries: list, prov) -> tuple:
    """Bir (alt) soru için aday havuzunu kurup LLM'e birebir seçtirir.
    (answer_or_None, reached_llm) döner. reached_llm=False YALNIZCA havuz
    boşsa olur (retrieval çöktü ve takvim de yoksa) — bu durumda LLM hiç
    çağrılmamıştır. ``prov.ask`` hata yükseltebilir (çağıran yakalar)."""
    candidates = _build_candidate_pool(query, calendar_entries)
    if not candidates:
        return None, False
    return prov.ask(query, candidates), True


def _llm_answer(query: str, db: Session) -> Optional[str]:
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
        single_future = ex.submit(_select_from_pool, query, calendar_entries, prov)

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
            ans, reached = _select_from_pool(sub_q, calendar_entries, prov)
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
    if hits and hits[0]["score"] >= 0.90:
        return hits[0]["answer"], "meilisearch"

    try:
        qhits = QDRANT_PROVIDER.search(query, limit=5)
        if qhits and qhits[0]["score"] > 0.75:
            return qhits[0]["answer"], "qdrant_vector"
    except Exception:
        pass

    return None, "none"


def answer_question(query: str, db: Session) -> tuple:
    """Bir soruya cevap üretir. (answer, source) döner; cevap yoksa (None, "none").

    - LLM açık ve seçim yaptı        → o cevap (source "llm").
    - LLM açık ama "uygun yok" dedi   → yüksek-güven eşik hit'ine bak, ama takvim
      kelime kapısını AÇMA (takvim zaten havuzdaydı, LLM reddetti).
    - LLM kapalı ya da hata verdi     → tam eski eşik davranışı (takvim kapısı dahil)."""
    if is_llm_enabled(db):
        try:
            ans = _llm_answer(query, db)
            if ans:
                return ans, "llm"
            # LLM çalıştı ama uygun aday yok → takvim kapısı olmadan eşik yedeği.
            return _fallback_answer(query, db, use_calendar=False)
        except Exception as e:
            logger.error(f"LLM ana yol hatası (yedeğe düşülüyor): {e}")

    # LLM kapalı ya da hata → tam eski davranış.
    return _fallback_answer(query, db, use_calendar=True)
