"""Konusma yonetimi uclari (admin): liste, transkript, export, id listesi."""
import logging
from datetime import datetime, timedelta
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.orm import Session

from database import Conversation, ConversationMessage
from deps import get_db
from csv_utils import csv_safe as _csv_safe, stream_csv as _stream_csv

logger = logging.getLogger("auzef")
router = APIRouter()


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


@router.get("/api/conversations")
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


@router.get("/api/conversations/export")
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


@router.post("/api/conversations/export")
def export_conversations_post(body: ConversationExportRequest, db: Session = Depends(get_db)):
    """Seçili konuşmaları transkript CSV olarak dışa aktarır. 'Tümünü seç'
    binlerce id üretebildiği için (URL sınırına takılmamak adına) POST kullanılır."""
    if not body.ids:
        raise HTTPException(status_code=400, detail="ids gerekli.")
    q = db.query(Conversation).filter(Conversation.id.in_(body.ids))
    return _conversations_csv_response(db, q)


@router.get("/api/conversations/ids")
def list_conversation_ids(search: str = "", db: Session = Depends(get_db)):
    """Filtreye uyan TÜM konuşma id'lerini döner (sayfalama yok) — 'tümünü seç' için."""
    q = _apply_conversation_search(db.query(Conversation.id), db, search)
    rows = q.order_by(Conversation.started_at.desc()).all()
    return {"ids": [r[0] for r in rows]}


@router.get("/api/conversations/{conversation_id}")
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
