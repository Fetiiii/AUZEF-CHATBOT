from fastapi import FastAPI, Query, Depends, HTTPException, Request, BackgroundTasks, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
import csv
import io
from sqlalchemy.orm import Session, selectinload
from sqlalchemy import text, or_
from database import SessionLocal, SystemConfig, QnA, QueryLog, AcademicCalendar, Conversation, ConversationMessage, utcnow
from auth import router as auth_router, AdminAuthMiddleware, current_user
from settings_api import router as settings_router
from deps import (
    get_db, is_llm_enabled, get_llm_provider,
    log_query as _log_query, actor_email as _actor_email,
    meili_search_safe as _meili_search_safe,
    MEILI_PROVIDER, QDRANT_PROVIDER,
    MAX_MESSAGE_LEN, MAX_IMPORT_BYTES,
)
from calendar_utils import format_calendar_answer, match_calendar_entry
from csv_utils import csv_safe as _csv_safe, stream_csv as _stream_csv
from pydantic import BaseModel
from typing import Optional, List
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
import hmac
import os
import logging
import secrets
import time
from datetime import datetime, timedelta

from dotenv import load_dotenv
load_dotenv()

# Loglama ayarları
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Başlangıç: Qdrant koleksiyonunu hazırla (on_event yerine lifespan —
    # on_event Starlette'te deprecated). QDRANT_PROVIDER modül global'i,
    # lifespan çalıştığında (import sonrası) tanımlı olur.
    try:
        QDRANT_PROVIDER.ensure_collection()
        logger.info("Qdrant koleksiyonu hazır.")
    except Exception as e:
        logger.error(f"Qdrant collection init hatası: {e}")
    yield


app = FastAPI(title="AUZEF Akıllı Asistan API", lifespan=lifespan)

# allow_credentials=False: widget cookie kullanmaz; "*" + credentials birleşimi
# zaten spec'e aykırıdır (tarayıcı reddeder) ve ileride cookie tabanlı admin
# oturumu geldiğinde sessizce kırılırdı. Admin istekleri aynı origin'den
# geldiği için CORS'a hiç takılmaz.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Admin oturum sistemi: /api/auth/* uçları + (ADMIN_AUTH_ENFORCED=true iken)
# yönetim uçlarını oturum ve ROL koşuluna bağlayan middleware. Ayrıntı: auth.py
app.include_router(auth_router)
app.include_router(settings_router)  # yalnızca super_admin (middleware kilitler)
app.add_middleware(AdminAuthMiddleware)

@app.get("/health")
def health(db: Session = Depends(get_db)):
    """Container healthcheck ucu: public ve ucuz, DB bağlantısını doğrular.

    Eski healthcheck /api/config/llm kullanıyordu; o uç artık oturum koruması
    kapsamında olduğundan ADMIN_AUTH_ENFORCED=true iken 401 dönüp container'ı
    'unhealthy' yapardı (ve frontend hiç başlamazdı)."""
    db.execute(text("SELECT 1"))
    return {"ok": True}


# ─────────────────────────────────────────────
#  Pydantic Şemaları
# ─────────────────────────────────────────────

class QnACreateRequest(BaseModel):
    question_text: str
    answer_text: str
    status: Optional[int] = 1

class QnAUpdateRequest(BaseModel):
    question_text: Optional[str] = None
    answer_text: Optional[str] = None
    status: Optional[int] = None

class QnABulkUpdateItem(BaseModel):
    id: int
    question_text: Optional[str] = None
    answer_text: Optional[str] = None
    status: Optional[int] = None

class WidgetChatRequest(BaseModel):
    message: str
    conversation_id: Optional[int] = None
    # Sahiplik token'ı: konuşma ilk cevapla birlikte verilir, sonraki
    # mesajlarda geri gönderilir. Yanlış/eksik token = yeni konuşma açılır
    # (öğrenci deneyimi asla kırılmaz, hijack imkânsızlaşır).
    conversation_token: Optional[str] = None

class RatingRequest(BaseModel):
    rating: int
    conversation_token: Optional[str] = None

class TalepRequest(BaseModel):
    status: str
    conversation_token: Optional[str] = None

# ── Academic Calendar Schemas ──
class AcademicCalendarCreateRequest(BaseModel):
    period: str
    event: str
    start_date: str
    end_date: str

class AcademicCalendarUpdateRequest(BaseModel):
    period: Optional[str] = None
    event: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None

class AcademicCalendarBulkUpdateItem(BaseModel):
    id: int
    period: Optional[str] = None
    event: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None

# ─────────────────────────────────────────────
#  Akademik Takvim — Tarih Sorusu Algılama
# ─────────────────────────────────────────────

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


# ─────────────────────────────────────────────
#  Cevap Üretimi — LLM seçici ana yol + eşik yedeği
# ─────────────────────────────────────────────
#
#  Tasarım: Takvim artık bir "ön kapı" değil, aday havuzundaki bir kayıttır.
#  LLM açıkken her soru (alt sorulara bölünüp) QnA + takvim adaylarından
#  oluşan TEK bir havuzdan birebir (verbatim) seçtirilir; LLM asla cevap
#  üretmez. Böylece "vize sınavına nasıl çalışmalıyım" gibi sorular yanlışlıkla
#  bir tarihe düşmez; gerçek cevap yoksa dürüstçe "uygun yok" (None) döner.
#
#  LLM kapalı / hatalı / hiçbir aday seçmezse, bugünkü eşik tabanlı yola
#  (takvim kelime eşleşmesi → Meili ≥0.90 → Qdrant >0.75) düşülür; böylece
#  anahtar bozulsa bile sistem çökmez, en fazla eski davranışa geriler.


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
    raw.extend(_meili_search_safe(query, limit=5))
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

    hits = _meili_search_safe(query, limit=3)
    if hits and hits[0]["score"] >= 0.90:
        return hits[0]["answer"], "meilisearch"

    try:
        qhits = QDRANT_PROVIDER.search(query, limit=5)
        if qhits and qhits[0]["score"] > 0.75:
            return qhits[0]["answer"], "qdrant_vector"
    except Exception:
        pass

    return None, "none"


def _answer_question(query: str, db: Session) -> tuple:
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


# ─────────────────────────────────────────────
#  Conversation (sohbet) kalıcılığı
# ─────────────────────────────────────────────

def _token_matches(conv: Optional[Conversation], token: Optional[str]) -> bool:
    """Sahiplik kontrolü: token'sız (eski) konuşmalara yazma da reddedilir."""
    return (
        conv is not None
        and bool(conv.client_token)
        and bool(token)
        and hmac.compare_digest(conv.client_token, token)
    )


def _get_or_create_conversation(db: Session, conversation_id: Optional[int], token: Optional[str], ip: Optional[str]) -> Conversation:
    """Sahiplik token'ı doğrulanan conversation'ı getirir; doğrulanamazsa
    (ilk mesaj, yanlış token, eski kayıt) YENİ konuşma açar.

    id'ler ardışık olduğundan token'sız devam etmeye izin vermek, herkesin
    başkasının konuşmasına mesaj yazabilmesi demekti (S5)."""
    if conversation_id:
        conv = db.query(Conversation).filter(Conversation.id == conversation_id).first()
        if _token_matches(conv, token):
            return conv
    conv = Conversation(ip_address=ip, client_token=secrets.token_urlsafe(24))
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return conv


def _store_message(db: Session, conversation_id: int, role: str, content: str, source: Optional[str] = None) -> ConversationMessage:
    msg = ConversationMessage(conversation_id=conversation_id, role=role, content=content, source=source)
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return msg


def _widget_reply(db: Session, conv: Optional[Conversation], answer: str, source: str, suggestions: Optional[list] = None) -> dict:
    """Bot cevabını (mümkünse) kaydedip yanıtı döner.

    Kayıt best-effort'tur: bir hata olursa cevabın kullanıcıya dönmesini
    engellemez (yalnızca o mesaj için conversation_id/message_id dönmez).
    """
    resp = {"answer": answer}
    if conv is not None:
        try:
            msg = _store_message(db, conv.id, "bot", answer, source=source)
            resp["conversation_id"] = conv.id
            resp["conversation_token"] = conv.client_token
            resp["message_id"] = msg.id
        except Exception as e:
            logger.error(f"Bot mesajı kaydedilemedi (yanıt yolu etkilenmez): {e}")
            try:
                db.rollback()
            except Exception:
                pass
    if suggestions:
        resp["suggestions"] = suggestions
    return resp


# ─────────────────────────────────────────────
#  Widget Chat Adapter
# ─────────────────────────────────────────────

# DİKKAT: bu endpoint bilinçli olarak SYNC (def) tanımlı. İçindeki tüm işler
# (SQLAlchemy, Meili HTTP, model.encode, LLM çağrısı) bloklayıcıdır; "async def"
# olsaydı bir LLM çağrısı boyunca worker'ın event loop'u kilitlenir, diğer TÜM
# istekler dururdu. Sync def'i FastAPI threadpool'da çalıştırır (eşzamanlılık ~40).
@app.post("/widget-chat")
def widget_chat(body: WidgetChatRequest, request: Request, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Embed widget için basit adapter: {message} → {answer}"""
    q = body.message.strip()
    if not q:
        return {"answer": "Lütfen bir soru yazın."}
    if len(q) > MAX_MESSAGE_LEN:
        # Sınırsız girdi = embedding CPU'su + LLM token maliyeti. Nazikçe kes.
        return {"answer": f"Sorunuz çok uzun. Lütfen {MAX_MESSAGE_LEN} karakterden kısa şekilde yazın."}

    ip = request.client.host if request.client else None

    # İlk mesajda conversation oluştur, kullanıcı mesajını kaydet.
    # Best-effort: kayıt başarısız olursa (ör. tablo henüz yoksa) cevap yolu
    # etkilenmez; sadece bu sohbet loglanmaz.
    conv = None
    try:
        conv = _get_or_create_conversation(db, body.conversation_id, body.conversation_token, ip)
        _store_message(db, conv.id, "user", q)
    except Exception as e:
        logger.error(f"Conversation kaydı yapılamadı (yanıt yolu etkilenmez): {e}")
        try:
            db.rollback()
        except Exception:
            pass
        conv = None

    # Cevap üret: LLM seçici ana yol (birleşik QnA + takvim havuzu, çoklu soru)
    # + eşik yedeği. Takvim artık ön kapı değil, havuzdaki bir aday.
    answer, source = _answer_question(q, db)
    if answer:
        background_tasks.add_task(_log_query, source, "success", ip)
        return _widget_reply(db, conv, answer, source)

    # Öneriler
    try:
        suggestions = MEILI_PROVIDER.get_suggestions(q, limit=20)
        if suggestions:
            background_tasks.add_task(_log_query, "none", "suggest", ip)
            return _widget_reply(
                db, conv,
                "Bu konuda net bir bilgim yok. Şunları sormak istemiş olabilirsiniz:",
                "none", suggestions=suggestions
            )
    except Exception:
        pass

    background_tasks.add_task(_log_query, "none", "suggest", ip)
    return _widget_reply(db, conv, "Bu konuda bilgim bulunmuyor.", "none")


@app.post("/api/messages/{message_id}/rating")
def rate_message(message_id: int, body: RatingRequest, db: Session = Depends(get_db)):
    """Bir bot cevabına verilen yıldız puanını (1-5) kaydeder.

    Sahiplik: mesajın ait olduğu konuşmanın token'ı istenir — id'ler ardışık
    olduğu için token'sız herkes başkasının cevabını puanlayabilirdi (S5)."""
    if body.rating < 1 or body.rating > 5:
        raise HTTPException(status_code=400, detail="Puan 1-5 aralığında olmalı.")
    msg = db.query(ConversationMessage).filter(ConversationMessage.id == message_id).first()
    if not msg:
        raise HTTPException(status_code=404, detail="Mesaj bulunamadı.")
    conv = db.query(Conversation).filter(Conversation.id == msg.conversation_id).first()
    if not _token_matches(conv, body.conversation_token):
        raise HTTPException(status_code=403, detail="Bu konuşma için yetkiniz yok.")
    msg.rating = body.rating
    msg.rating_at = utcnow()
    db.commit()
    return {"ok": True}


@app.post("/api/conversations/{conversation_id}/talep")
def set_talep_status(conversation_id: int, body: TalepRequest, db: Session = Depends(get_db)):
    """Talep adımının sonucunu kaydeder: 'declined' (Hayır) veya 'redirected' (Evet).

    Sahiplik: konuşmanın token'ı istenir (bkz. rate_message notu)."""
    if body.status not in ("declined", "redirected"):
        raise HTTPException(status_code=400, detail="Geçersiz talep durumu.")
    conv = db.query(Conversation).filter(Conversation.id == conversation_id).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Konuşma bulunamadı.")
    if not _token_matches(conv, body.conversation_token):
        raise HTTPException(status_code=403, detail="Bu konuşma için yetkiniz yok.")
    conv.talep_status = body.status
    db.commit()
    return {"ok": True}


def _apply_conversation_search(q, db: Session, search: str):
    """Konuşma sorgusuna meta arama filtresi uygular (ilk soru / ip / #id)."""
    term = (search or "").strip()
    if not term:
        return q
    like = f"%{term}%"
    conds = [Conversation.ip_address.ilike(like)]
    if term.isdigit():
        conds.append(Conversation.id == int(term))
    sub = db.query(ConversationMessage.conversation_id).filter(
        ConversationMessage.role == "user",
        ConversationMessage.content.ilike(like),
    )
    conds.append(Conversation.id.in_(sub))
    return q.filter(or_(*conds))


def _conversation_rows(db: Session, conv_query):
    """Konuşmaları öbek öbek (500'lük) çekip her mesaj için bir satır üretir.
    Tüm transkripti aynı anda belleğe almaz; her öbekte o öbeğin mesajları
    tek sorguyla çekilir (N+1 yok)."""
    CHUNK = 500
    ordered = conv_query.order_by(Conversation.started_at, Conversation.id)
    offset = 0
    while True:
        convs = ordered.offset(offset).limit(CHUNK).all()
        if not convs:
            break
        ids = [c.id for c in convs]
        msgs_by_conv = {}
        for m in (
            db.query(ConversationMessage)
            .filter(ConversationMessage.conversation_id.in_(ids))
            .order_by(ConversationMessage.created_at, ConversationMessage.id)
            .all()
        ):
            msgs_by_conv.setdefault(m.conversation_id, []).append(m)
        for c in convs:
            conv_date = c.started_at.isoformat() if c.started_at else ""
            for m in msgs_by_conv.get(c.id, []):
                yield [
                    c.id, conv_date, c.ip_address or "", c.talep_status,
                    m.created_at.isoformat() if m.created_at else "",
                    m.role, _csv_safe(m.content), m.source or "",
                    m.rating if m.rating is not None else "",
                ]
        offset += CHUNK


def _conversations_csv_response(db: Session, conv_query) -> StreamingResponse:
    """Verilen konuşma SORGUSUNU transkript (her mesaj bir satır) CSV olarak
    streaming döner. NOT: Artık materialize edilmiş liste değil, filtrelenmiş
    bir Query bekler — böylece binlerce konuşma belleğe yüklenmez."""
    header = [
        "konusma_id", "konusma_tarihi", "ip", "talep_durumu",
        "mesaj_tarihi", "rol", "icerik", "kaynak", "puan",
    ]
    logger.info("Konuşma export (streaming) başladı.")
    return _stream_csv(header, _conversation_rows(db, conv_query), "konusmalar_export.csv")


@app.get("/api/conversations")
def list_conversations(skip: int = 0, limit: int = 25, search: str = "", db: Session = Depends(get_db)):
    """Sohbet listesini sayfalı ÖZET olarak döner (mesaj metinleri hariç).

    Performans: her istek en fazla `limit` konuşma + o sayfaya ait mesajların
    tek bir toplu sorgusunu çeker (N+1 yok). Tüm veriyi asla aynı anda çekmez.
    """
    base = _apply_conversation_search(db.query(Conversation), db, search)

    total = base.count()
    convs = base.order_by(Conversation.started_at.desc()).offset(skip).limit(limit).all()

    ids = [c.id for c in convs]
    stats = {}
    if ids:
        msgs = (
            db.query(ConversationMessage)
            .filter(ConversationMessage.conversation_id.in_(ids))
            .order_by(ConversationMessage.created_at, ConversationMessage.id)
            .all()
        )
        for m in msgs:
            s = stats.setdefault(m.conversation_id, {"count": 0, "first_q": None, "ratings": []})
            s["count"] += 1
            if m.role == "user" and s["first_q"] is None:
                s["first_q"] = m.content
            if m.rating is not None:
                s["ratings"].append(m.rating)

    items = []
    for c in convs:
        s = stats.get(c.id, {"count": 0, "first_q": None, "ratings": []})
        ratings = s["ratings"]
        items.append({
            "id": c.id,
            "started_at": c.started_at.isoformat() if c.started_at else None,
            "ip_address": c.ip_address,
            "talep_status": c.talep_status,
            "message_count": s["count"],
            "first_question": s["first_q"],
            "avg_rating": round(sum(ratings) / len(ratings), 1) if ratings else None,
            "rating_count": len(ratings),
        })

    return {"total": total, "items": items}


@app.get("/api/conversations/export")
def export_conversations(ids: str = "", start: str = "", end: str = "", db: Session = Depends(get_db)):
    """Seçili konuşmaları (?ids=1,2,3) veya bir tarih aralığındaki
    (?start=YYYY-MM-DD&end=YYYY-MM-DD) konuşmaları transkript olarak
    (her mesaj bir satır) CSV dışa aktarır.

    NOT: Bu rota, /{conversation_id} rotasından ÖNCE tanımlı olmalı; aksi
    halde "export" bir id sanılır.
    """
    ISTANBUL_OFFSET = timedelta(hours=3)
    q = db.query(Conversation)

    id_list = [int(x) for x in ids.split(",") if x.strip().isdigit()]
    if id_list:
        q = q.filter(Conversation.id.in_(id_list))
    elif start or end:
        if start:
            try:
                q = q.filter(Conversation.started_at >= datetime.strptime(start, "%Y-%m-%d") - ISTANBUL_OFFSET)
            except ValueError:
                raise HTTPException(status_code=400, detail="Geçersiz başlangıç tarihi (YYYY-MM-DD).")
        if end:
            try:
                # bitiş günü dahil olsun diye +1 gün
                q = q.filter(Conversation.started_at < datetime.strptime(end, "%Y-%m-%d") + timedelta(days=1) - ISTANBUL_OFFSET)
            except ValueError:
                raise HTTPException(status_code=400, detail="Geçersiz bitiş tarihi (YYYY-MM-DD).")
    else:
        raise HTTPException(status_code=400, detail="En az bir filtre gerekli: ids ya da tarih aralığı.")

    # Streaming: materialize etme, filtrelenmiş sorguyu ver (öbek öbek çekilir).
    return _conversations_csv_response(db, q)


class ConversationExportRequest(BaseModel):
    ids: List[int] = []


@app.post("/api/conversations/export")
def export_conversations_post(body: ConversationExportRequest, db: Session = Depends(get_db)):
    """Seçili konuşmaları transkript CSV olarak dışa aktarır. 'Tümünü seç'
    binlerce id üretebildiği için (URL sınırına takılmamak adına) POST kullanılır."""
    if not body.ids:
        raise HTTPException(status_code=400, detail="ids gerekli.")
    q = db.query(Conversation).filter(Conversation.id.in_(body.ids))
    return _conversations_csv_response(db, q)


@app.get("/api/conversations/ids")
def list_conversation_ids(search: str = "", db: Session = Depends(get_db)):
    """Filtreye uyan TÜM konuşma id'lerini döner (sayfalama yok) — 'tümünü seç' için."""
    q = _apply_conversation_search(db.query(Conversation.id), db, search)
    rows = q.order_by(Conversation.started_at.desc()).all()
    return {"ids": [r[0] for r in rows]}


@app.get("/api/conversations/{conversation_id}")
def get_conversation(conversation_id: int, db: Session = Depends(get_db)):
    """Tek bir konuşmanın tüm mesajlarını (transkript) döner — yalnızca tıklanınca çağrılır."""
    conv = db.query(Conversation).filter(Conversation.id == conversation_id).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Konuşma bulunamadı.")
    msgs = (
        db.query(ConversationMessage)
        .filter(ConversationMessage.conversation_id == conversation_id)
        .order_by(ConversationMessage.created_at, ConversationMessage.id)
        .all()
    )
    return {
        "id": conv.id,
        "ip_address": conv.ip_address,
        "talep_status": conv.talep_status,
        "started_at": conv.started_at.isoformat() if conv.started_at else None,
        "updated_at": conv.updated_at.isoformat() if conv.updated_at else None,
        "messages": [
            {
                "id": m.id,
                "role": m.role,
                "content": m.content,
                "source": m.source,
                "rating": m.rating,
                "rating_at": m.rating_at.isoformat() if m.rating_at else None,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in msgs
        ],
    }


# ─────────────────────────────────────────────
#  SEARCH (Orijinal endpoint korundu)
# ─────────────────────────────────────────────

# Sync def: bloklayıcı iş içerir (bkz. widget_chat üzerindeki not).
@app.get("/api/search")
def search(request: Request, background_tasks: BackgroundTasks, q: str = Query(..., min_length=2, max_length=1000), db: Session = Depends(get_db)):
    ip = request.client.host if request.client else None

    try:
        # Cevap üret: LLM seçici ana yol (birleşik QnA + takvim havuzu, çoklu
        # soru) + eşik yedeği. Takvim artık ön kapı değil, havuzdaki bir aday.
        answer, source = _answer_question(q, db)
        if answer:
            background_tasks.add_task(_log_query, source, "success", ip)
            return {
                "source": source,
                "status": "success",
                "answer": answer,
                "question": q,
            }

        # Cevap yok → öneriler
        suggestions = []
        try:
            suggestions = MEILI_PROVIDER.get_suggestions(q, limit=20)
        except Exception:
            pass

        background_tasks.add_task(_log_query, "none", "suggest", ip)
        return {
            "source": "none",
            "status": "suggest",
            "message": "Doğrudan bir cevap bulamadım. Bunları mı demek istediniz?",
            "suggestions": suggestions
        }

    except Exception:
        # Hata ayrıntısı istemciye SIZDIRILMAZ (bağlantı dizesi/host adı içerebilir);
        # tam traceback sunucu loguna yazılır.
        logger.exception("Arama sırasında beklenmeyen hata")
        background_tasks.add_task(_log_query, "none", "error", ip)
        return {"status": "error", "message": "Beklenmeyen bir sistem hatası oluştu. Lütfen tekrar deneyin."}


# ─────────────────────────────────────────────
#  QnA CRUD Endpoint'leri
# ─────────────────────────────────────────────

def get_qna_view_dict(db: Session, qna_id: int):
    view_data = db.execute(
        text("SELECT * FROM qna_search_view WHERE id = :id"),
        {"id": qna_id}
    ).mappings().first()
    return dict(view_data) if view_data else None

def sync_providers(db: Session, qna_id: int):
    doc = get_qna_view_dict(db, qna_id)
    if not doc or doc.get("status") != 1:
        # Kayıt silinmiş YA DA pasife alınmış (status != 1): arama indekslerinde
        # kalmasın. Eskiden pasif kayıtlar indekslerde sonsuza dek kalıyor ve
        # bot pasif cevapları vermeye devam ediyordu.
        remove_from_providers(qna_id)
        return
    doc.pop("status", None)  # arama dokümanına status taşımaya gerek yok
    try:
        MEILI_PROVIDER.add_documents([doc])
    except Exception as e:
        logger.error(f"MeiliSearch sync hatası: {e}")
    try:
        QDRANT_PROVIDER.upsert_point(qna_id, doc['question'], doc['answer'])
    except Exception as e:
        logger.error(f"Qdrant sync hatası: {e}")

def remove_from_providers(qna_id: int):
    try:
        MEILI_PROVIDER.delete_document(qna_id)
    except Exception as e:
        logger.error(f"MeiliSearch delete hatası: {e}")
    try:
        QDRANT_PROVIDER.delete_point(qna_id)
    except Exception as e:
        logger.error(f"Qdrant delete hatası: {e}")


def sync_providers_batch(db: Session, qna_ids: list):
    """Birden fazla QnA'yı TEK turda indeksler: tek view sorgusu + tek Meili
    add_documents + tek Qdrant batch upsert. CSV import / toplu güncellemede
    satır satır sync (N view sorgusu + N encode + N HTTP) yerine kullanılır.
    Pasif/silinmiş (status != 1) istenen id'ler indekslerden düşülür."""
    if not qna_ids:
        return
    rows = db.execute(
        text("SELECT * FROM qna_search_view WHERE id = ANY(:ids)"),
        {"ids": list(qna_ids)},
    ).mappings().all()

    active_docs = []
    active_ids = set()
    for row in rows:
        d = dict(row)
        if d.get("status") == 1:
            d.pop("status", None)
            active_docs.append(d)
            active_ids.add(d["id"])

    if active_docs:
        try:
            MEILI_PROVIDER.add_documents(active_docs)
        except Exception as e:
            logger.error(f"MeiliSearch batch sync hatası: {e}")
        try:
            QDRANT_PROVIDER.upsert_points(
                [(d["id"], d["question"], d["answer"]) for d in active_docs]
            )
        except Exception as e:
            logger.error(f"Qdrant batch sync hatası: {e}")

    # İstenen ama aktif olmayan (pasif/silinmiş) kayıtlar indeksten düşülür.
    for qna_id in qna_ids:
        if qna_id not in active_ids:
            remove_from_providers(qna_id)

def _qna_dict(r: QnA) -> dict:
    return {
        "id": r.id,
        "question_text": r.question_text,
        "answer_text": r.answer_text,
        "status": r.status,
        "updated_by": r.updated_by,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


@app.get("/api/qna")
def list_qna(
    skip: int = 0,
    limit: int = 500,
    db: Session = Depends(get_db)
):
    """Tüm QnA kayıtlarını döner (AG Grid için)."""
    rows = db.query(QnA).order_by(QnA.id).offset(skip).limit(limit).all()
    return [_qna_dict(r) for r in rows]


@app.post("/api/qna", status_code=201)
def create_qna(body: QnACreateRequest, db: Session = Depends(get_db), me=Depends(current_user)):
    """Yeni QnA kaydı oluşturur."""
    row = QnA(
        question_text=body.question_text,
        answer_text=body.answer_text,
        status=body.status if body.status is not None else 1,
        updated_by=_actor_email(me),
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    # Sync with MeiliSearch & Qdrant
    sync_providers(db, row.id)

    return _qna_dict(row)

@app.put("/api/qna/bulk-update")
def bulk_update_qna(items: List[QnABulkUpdateItem], db: Session = Depends(get_db), me=Depends(current_user)):
    """Birden fazla QnA kaydını tek seferde günceller (AG Grid toplu kaydetme)."""
    actor = _actor_email(me)
    updated = []
    for item in items:
        row = db.query(QnA).filter(QnA.id == item.id).first()
        if not row:
            continue
        changed = False
        if item.question_text is not None:
            row.question_text = item.question_text
            changed = True
        if item.answer_text is not None:
            row.answer_text = item.answer_text
            changed = True
        if item.status is not None:
            row.status = item.status
            changed = True
        if changed:
            row.updated_by = actor
        updated.append(row.id)
    db.commit()

    # Toplu sync: tek turda indeksle (satır satır değil).
    sync_providers_batch(db, updated)

    return {"updated_ids": updated, "count": len(updated)}

@app.put("/api/qna/{qna_id}")
def update_qna(qna_id: int, body: QnAUpdateRequest, db: Session = Depends(get_db), me=Depends(current_user)):
    """Tek bir QnA kaydını günceller."""
    row = db.query(QnA).filter(QnA.id == qna_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="QnA kaydı bulunamadı.")

    if body.question_text is not None:
        row.question_text = body.question_text
    if body.answer_text is not None:
        row.answer_text = body.answer_text
    if body.status is not None:
        row.status = body.status
    row.updated_by = _actor_email(me)

    db.commit()
    db.refresh(row)

    # Sync with MeiliSearch & Qdrant
    sync_providers(db, row.id)

    return _qna_dict(row)


@app.delete("/api/qna/{qna_id}", status_code=204)
def delete_qna(qna_id: int, db: Session = Depends(get_db)):
    """QnA kaydını siler (ilişkili queries ve tags cascade ile silinir)."""
    row = db.query(QnA).filter(QnA.id == qna_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="QnA kaydı bulunamadı.")
    db.delete(row)
    db.commit()

    # Remove from MeiliSearch & Qdrant
    remove_from_providers(qna_id)
    
    return None


# ─────────────────────────────────────────────
#  CSV Import
# ─────────────────────────────────────────────

# Sync def: satır satır DB insert + provider sync bloklayıcıdır (bkz. widget_chat notu).
@app.post("/api/qna/import")
def import_csv(file: UploadFile = File(...), db: Session = Depends(get_db), me=Depends(current_user)):
    """CSV dosyasından toplu QnA içe aktarır. Format: question;answer;tags;query_1;...;query_20"""
    actor = _actor_email(me)
    content = file.file.read(MAX_IMPORT_BYTES + 1)
    if len(content) > MAX_IMPORT_BYTES:
        raise HTTPException(status_code=413, detail="Dosya çok büyük (limit 5 MB).")
    try:
        text_content = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text_content = content.decode("latin-1")

    reader = csv.DictReader(io.StringIO(text_content), delimiter=';')
    inserted_ids = []

    for row in reader:
        # "or ''": DictReader eksik (kısa) satırlarda değeri None yapar; .strip()
        # patlamasın. (Sütun hiç yoksa .get zaten '' döndürür ama satır kısaysa
        # anahtar None değerle VAR olur.)
        question = (row.get('question') or '').strip()
        answer = (row.get('answer') or '').strip()
        if not question or not answer:
            continue

        result = db.execute(
            text("INSERT INTO qna (question_text, answer_text, status, updated_by) VALUES (:q, :a, 1, :by) RETURNING id"),
            {"q": question, "a": answer, "by": actor}
        ).fetchone()
        qna_id = result[0]
        inserted_ids.append(qna_id)

        tags_val = row.get('tags') or ''
        if tags_val and tags_val.strip():
            for tag_name in [t.strip() for t in tags_val.split(',') if t.strip()]:
                tag_res = db.execute(
                    text("INSERT INTO tags (name) VALUES (:n) ON CONFLICT (name) DO UPDATE SET name=EXCLUDED.name RETURNING id"),
                    {"n": tag_name}
                ).fetchone()
                db.execute(
                    text("INSERT INTO qna_tags (qna_id, tag_id) VALUES (:q_id, :t_id) ON CONFLICT DO NOTHING"),
                    {"q_id": qna_id, "t_id": tag_res[0]}
                )

        for i in range(1, 21):
            query_val = (row.get(f'query_{i}') or '').strip()
            if query_val:
                db.execute(
                    text("INSERT INTO qna_queries (qna_id, query_text) VALUES (:q_id, :qt)"),
                    {"q_id": qna_id, "qt": query_val}
                )

    db.commit()

    # Toplu sync: tek encode + tek Meili + tek Qdrant upsert (satır satır değil).
    sync_providers_batch(db, inserted_ids)

    logger.info(f"CSV import tamamlandı: {len(inserted_ids)} kayıt eklendi.")
    return {"imported": len(inserted_ids)}


def _qna_rows(db: Session):
    """QnA'ları öbek öbek (500'lük), tag/query'leri selectinload ile TEK
    sorguda çekerek satırlar üretir (lazy-load N+1'i önlenir)."""
    CHUNK = 500
    offset = 0
    while True:
        rows = (
            db.query(QnA)
            .options(selectinload(QnA.tags), selectinload(QnA.queries))
            .order_by(QnA.id)
            .offset(offset).limit(CHUNK).all()
        )
        if not rows:
            break
        for r in rows:
            queries = [q.query_text for q in r.queries[:20]]
            queries += [""] * (20 - len(queries))  # 20 sütuna sabitle
            yield [r.question_text, r.answer_text, ", ".join(t.name for t in r.tags)] + queries
        offset += CHUNK


@app.get("/api/qna/export")
def export_qna(db: Session = Depends(get_db)):
    """Tüm QnA verisini içe aktarma (import) formatında CSV olarak streaming
    dışa aktarır. Sütunlar import ile birebir aynıdır
    (question;answer;tags;query_1;...;query_20), böylece round-trip korunur."""
    header = ["question", "answer", "tags"] + [f"query_{i}" for i in range(1, 21)]
    logger.info("QnA export (streaming) başladı.")
    return _stream_csv(header, _qna_rows(db), "qna_export.csv")


# NOT: Eski /api/config/llm uçları kaldırıldı — LLM aç/kapa ve OpenRouter
# anahtarı artık ayarlar sayfasının API'sinde: /api/settings/llm
# (settings_api.py, yalnızca super_admin).


# ─────────────────────────────────────────────
#  İzleme İstatistikleri
# ─────────────────────────────────────────────

@app.get("/api/stats")
def get_stats(db: Session = Depends(get_db)):
    # Turkey is UTC+3 with no DST (since 2016)
    ISTANBUL_OFFSET = timedelta(hours=3)
    now_utc = utcnow()
    now_istanbul = now_utc + ISTANBUL_OFFSET

    # Midnight in Istanbul time, converted back to UTC for DB comparison
    today_start_utc = (now_istanbul.replace(hour=0, minute=0, second=0, microsecond=0)) - ISTANBUL_OFFSET
    five_min_ago = now_utc - timedelta(minutes=5)

    # "Aktif kullanıcı" = son 5 dakikada mesajı olan FARKLI sohbet (oturum) sayısı.
    # IP kullanılamaz: tüm trafik nginx proxy'sinin IP'siyle geldiği için (172.18.0.7)
    # her kullanıcı tek IP'ye çöker. conversation_id her oturuma özel olduğundan
    # proxy arkasında bile kullanıcıları doğru ayrıştırır.
    active_users = db.execute(
        text("SELECT COUNT(DISTINCT conversation_id)::int FROM conversation_messages WHERE created_at >= :since"),
        {"since": five_min_ago}
    ).scalar() or 0

    total_today = db.execute(
        text("SELECT COUNT(*)::int FROM query_logs WHERE created_at >= :today"),
        {"today": today_start_utc}
    ).scalar() or 0

    # generate_series and labels in Istanbul time; created_at stored as UTC so cast accordingly
    hourly_rows = db.execute(text("""
        SELECT
            to_char(gs.hour, 'HH24:00') AS label,
            COALESCE(COUNT(ql.id), 0)::int AS count
        FROM generate_series(
            date_trunc('hour', NOW() AT TIME ZONE 'Europe/Istanbul') - INTERVAL '23 hours',
            date_trunc('hour', NOW() AT TIME ZONE 'Europe/Istanbul'),
            INTERVAL '1 hour'
        ) AS gs(hour)
        LEFT JOIN query_logs ql
            ON date_trunc('hour', (ql.created_at AT TIME ZONE 'UTC') AT TIME ZONE 'Europe/Istanbul') = gs.hour
        GROUP BY gs.hour
        ORDER BY gs.hour
    """)).fetchall()

    source_rows = db.execute(
        text("""
            SELECT source, COUNT(*)::int AS cnt
            FROM query_logs
            WHERE created_at >= :today
            GROUP BY source
        """),
        {"today": today_start_utc}
    ).fetchall()

    sources = {"meilisearch": 0, "qdrant_vector": 0, "llm": 0, "academic_calendar": 0, "none": 0}
    for row in source_rows:
        if row.source in sources:
            sources[row.source] = row.cnt

    return {
        "active_users": int(active_users),
        "total_queries_today": int(total_today),
        "hourly": [{"label": row.label, "count": row.count} for row in hourly_rows],
        "sources": sources
    }


@app.get("/api/stats/conversations")
def get_conversation_stats(start: str = "", end: str = "", db: Session = Depends(get_db)):
    """Konuşma bazlı istatistikler (tarih filtreli, conversations.started_at üzerinden).

    - rating_distribution: her cevaba verilen puanların 1..5 dağılımı
    - outcome: sohbetin SON puanına göre olumlu(>=4)/olumsuz(<=3)/puansız
    - talep: redirected (yönlendirilen) / declined (oluşturmadan giden) / not_offered

    Tüm sayımlar SQL'de yapılır — Python'a hiç satır çekilmez. (Eski sürüm
    aralıktaki TÜM konuşma ve puanlı mesaj nesnelerini belleğe yüklüyordu;
    100k konuşmada yüzlerce MB ve saniyeler sürüyordu.)
    """
    ISTANBUL_OFFSET = timedelta(hours=3)
    params = {}
    conds = []
    if start:
        try:
            params["start"] = datetime.strptime(start, "%Y-%m-%d") - ISTANBUL_OFFSET
            conds.append("c.started_at >= :start")
        except ValueError:
            raise HTTPException(status_code=400, detail="Geçersiz başlangıç tarihi (YYYY-MM-DD).")
    if end:
        try:
            # bitiş günü dahil olsun diye +1 gün
            params["end"] = datetime.strptime(end, "%Y-%m-%d") + timedelta(days=1) - ISTANBUL_OFFSET
            conds.append("c.started_at < :end")
        except ValueError:
            raise HTTPException(status_code=400, detail="Geçersiz bitiş tarihi (YYYY-MM-DD).")
    # Tarih koşulları sabit metinlerdir (kullanıcı girdisi yalnızca bound
    # parametre olarak geçer) — f-string ile eklenmesi güvenlidir.
    where = (" AND " + " AND ".join(conds)) if conds else ""

    # 1) Toplam + talep dağılımı (tek sorgu)
    talep = {"redirected": 0, "declined": 0, "not_offered": 0}
    total = 0
    for row in db.execute(text(f"""
        SELECT c.talep_status, COUNT(*)::int AS cnt
        FROM conversations c
        WHERE TRUE{where}
        GROUP BY c.talep_status
    """), params):
        total += row.cnt
        if row.talep_status in talep:
            talep[row.talep_status] = row.cnt

    # 2) Puan dağılımı (her puanlı cevap sayılır)
    rating_distribution = {str(i): 0 for i in range(1, 6)}
    for row in db.execute(text(f"""
        SELECT cm.rating, COUNT(*)::int AS cnt
        FROM conversation_messages cm
        JOIN conversations c ON c.id = cm.conversation_id
        WHERE cm.rating BETWEEN 1 AND 5{where}
        GROUP BY cm.rating
    """), params):
        rating_distribution[str(row.rating)] = row.cnt

    # 3) Sonuç: her konuşmanın SON puanı (DISTINCT ON + DESC = en son yazılan)
    outcome_row = db.execute(text(f"""
        SELECT
            COUNT(*) FILTER (WHERE last_rating >= 4)::int AS olumlu,
            COUNT(*) FILTER (WHERE last_rating <= 3)::int AS olumsuz,
            COUNT(*)::int AS rated_convs
        FROM (
            SELECT DISTINCT ON (cm.conversation_id) cm.rating AS last_rating
            FROM conversation_messages cm
            JOIN conversations c ON c.id = cm.conversation_id
            WHERE cm.rating IS NOT NULL{where}
            ORDER BY cm.conversation_id, cm.created_at DESC, cm.id DESC
        ) t
    """), params).first()

    return {
        "total_conversations": total,
        "rating_distribution": rating_distribution,
        "outcome": {
            "olumlu": outcome_row.olumlu,
            "olumsuz": outcome_row.olumsuz,
            "puansiz": total - outcome_row.rated_convs,
        },
        "talep": talep,
    }


# ─────────────────────────────────────────────
#  Akademik Takvim CRUD
# ─────────────────────────────────────────────

def _calendar_dict(r: AcademicCalendar) -> dict:
    return {
        "id": r.id,
        "period": r.period,
        "event": r.event,
        "start_date": r.start_date,
        "end_date": r.end_date,
        "updated_by": r.updated_by,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


@app.get("/api/academic-calendar")
def list_calendar(skip: int = 0, limit: int = 500, db: Session = Depends(get_db)):
    """Tüm akademik takvim kayıtlarını döner."""
    rows = db.query(AcademicCalendar).order_by(AcademicCalendar.id).offset(skip).limit(limit).all()
    return [_calendar_dict(r) for r in rows]


@app.post("/api/academic-calendar", status_code=201)
def create_calendar(body: AcademicCalendarCreateRequest, db: Session = Depends(get_db), me=Depends(current_user)):
    """Yeni takvim kaydı oluşturur."""
    row = AcademicCalendar(
        period=body.period,
        event=body.event,
        start_date=body.start_date,
        end_date=body.end_date,
        updated_by=_actor_email(me),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _calendar_dict(row)


@app.put("/api/academic-calendar/bulk-update")
def bulk_update_calendar(items: List[AcademicCalendarBulkUpdateItem], db: Session = Depends(get_db), me=Depends(current_user)):
    """Birden fazla takvim kaydını tek seferde günceller."""
    actor = _actor_email(me)
    updated = []
    for item in items:
        row = db.query(AcademicCalendar).filter(AcademicCalendar.id == item.id).first()
        if not row:
            continue
        changed = False
        if item.period is not None:
            row.period = item.period
            changed = True
        if item.event is not None:
            row.event = item.event
            changed = True
        if item.start_date is not None:
            row.start_date = item.start_date
            changed = True
        if item.end_date is not None:
            row.end_date = item.end_date
            changed = True
        if changed:
            row.updated_by = actor
        updated.append(row.id)
    db.commit()
    return {"updated_ids": updated, "count": len(updated)}


@app.put("/api/academic-calendar/{cal_id}")
def update_calendar(cal_id: int, body: AcademicCalendarUpdateRequest, db: Session = Depends(get_db), me=Depends(current_user)):
    """Tek bir takvim kaydını günceller."""
    row = db.query(AcademicCalendar).filter(AcademicCalendar.id == cal_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Takvim kaydı bulunamadı.")
    if body.period is not None:
        row.period = body.period
    if body.event is not None:
        row.event = body.event
    if body.start_date is not None:
        row.start_date = body.start_date
    if body.end_date is not None:
        row.end_date = body.end_date
    row.updated_by = _actor_email(me)
    db.commit()
    db.refresh(row)
    return _calendar_dict(row)


@app.delete("/api/academic-calendar/{cal_id}", status_code=204)
def delete_calendar(cal_id: int, db: Session = Depends(get_db)):
    """Takvim kaydını siler."""
    row = db.query(AcademicCalendar).filter(AcademicCalendar.id == cal_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Takvim kaydı bulunamadı.")
    db.delete(row)
    db.commit()
    return None


# Sync def: bloklayıcı DB işleri içerir (bkz. widget_chat notu).
@app.post("/api/academic-calendar/import")
def import_calendar_csv(file: UploadFile = File(...), db: Session = Depends(get_db), me=Depends(current_user)):
    """CSV dosyasından toplu takvim verisi içe aktarır.
    Kabul edilen formatlar (virgül veya noktalı virgül):
      Donem,Etkinlik,Baslangic_Tarihi,Bitis_Tarihi
      period;event;start_date;end_date
    """
    actor = _actor_email(me)
    content = file.file.read(MAX_IMPORT_BYTES + 1)
    if len(content) > MAX_IMPORT_BYTES:
        raise HTTPException(status_code=413, detail="Dosya çok büyük (limit 5 MB).")
    try:
        text_content = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text_content = content.decode("latin-1")

    # Otomatik delimiter algılama
    first_line = text_content.split("\n")[0]
    delimiter = ";" if ";" in first_line else ","

    reader = csv.DictReader(io.StringIO(text_content), delimiter=delimiter)
    inserted = 0

    for row in reader:
        # Hem Türkçe hem İngilizce sütun isimlerini destekle
        period = (row.get("Donem") or row.get("period") or "").strip()
        event = (row.get("Etkinlik") or row.get("event") or "").strip()
        start = (row.get("Baslangic_Tarihi") or row.get("start_date") or "").strip()
        end = (row.get("Bitis_Tarihi") or row.get("end_date") or "").strip()

        if not event or not start:
            continue

        db.add(AcademicCalendar(
            period=period,
            event=event,
            start_date=start,
            end_date=end or start,
            updated_by=actor,
        ))
        inserted += 1

    db.commit()
    logger.info(f"Takvim CSV import tamamlandı: {inserted} kayıt eklendi.")
    return {"imported": inserted}


def _calendar_rows(db: Session):
    CHUNK = 1000
    offset = 0
    while True:
        rows = db.query(AcademicCalendar).order_by(AcademicCalendar.id).offset(offset).limit(CHUNK).all()
        if not rows:
            break
        for r in rows:
            yield [r.period, r.event, r.start_date, r.end_date]
        offset += CHUNK


@app.get("/api/academic-calendar/export")
def export_calendar(db: Session = Depends(get_db)):
    """Takvim verisini içe aktarma (import) formatında CSV olarak streaming
    dışa aktarır. Sütunlar import ile birebir aynıdır
    (Donem,Etkinlik,Baslangic_Tarihi,Bitis_Tarihi), round-trip korunur."""
    header = ["Donem", "Etkinlik", "Baslangic_Tarihi", "Bitis_Tarihi"]
    logger.info("Takvim export (streaming) başladı.")
    return _stream_csv(header, _calendar_rows(db), "akademik_takvim_export.csv", delimiter=",")
