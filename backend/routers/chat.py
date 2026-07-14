"""Widget/arama uclari (public) + konusma kaliciligi yardimcilari."""
import hmac
import logging
import secrets
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import Conversation, ConversationMessage, utcnow
from deps import get_db, log_query as _log_query, MEILI_PROVIDER, MAX_MESSAGE_LEN
from answer_pipeline import answer_question as _answer_question

logger = logging.getLogger("auzef")
router = APIRouter()


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


@router.post("/widget-chat")
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


@router.post("/api/messages/{message_id}/rating")
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


@router.post("/api/conversations/{conversation_id}/talep")
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


@router.get("/api/search")
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
