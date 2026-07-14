"""QnA CRUD + CSV import/export + arama saglayici senkronizasyonu."""
import csv
import io
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session, selectinload

from database import QnA
from deps import get_db, actor_email as _actor_email, MAX_IMPORT_BYTES, MEILI_PROVIDER, QDRANT_PROVIDER
from csv_utils import stream_csv as _stream_csv
from auth import current_user

logger = logging.getLogger("auzef")
router = APIRouter()


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


@router.get("/api/qna")
def list_qna(
    skip: int = 0,
    limit: int = 500,
    db: Session = Depends(get_db)
):
    """Tüm QnA kayıtlarını döner (AG Grid için)."""
    rows = db.query(QnA).order_by(QnA.id).offset(skip).limit(limit).all()
    return [_qna_dict(r) for r in rows]


@router.post("/api/qna", status_code=201)
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


@router.put("/api/qna/bulk-update")
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


@router.put("/api/qna/{qna_id}")
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


@router.delete("/api/qna/{qna_id}", status_code=204)
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


@router.post("/api/qna/import")
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


@router.get("/api/qna/export")
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
