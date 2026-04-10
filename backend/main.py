from fastapi import FastAPI, Query, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import text
from database import SessionLocal, SystemConfig, QnA
from providers import MeiliSearchProvider, QdrantProvider
from llm_provider import LLMFactory
from pydantic import BaseModel
from typing import Optional, List
import os
import logging
import time

from dotenv import load_dotenv
load_dotenv()

# Loglama ayarları
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="AUZEF Akıllı Asistan API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

MEILI_PROVIDER = MeiliSearchProvider(
    url=os.getenv("MEILI_URL"),
    master_key=os.getenv("MEILI_MASTER_KEY"),
    index_name='auzef_qna_index'
)

QDRANT_PROVIDER = QdrantProvider(
    host="localhost",
    port=int(os.getenv("QDRANT_PORT")),
    collection_name="auzef_qna_vectors",
    model_name="nezahatkorkmaz/turkce-embedding-bge-m3"
)

LLM_PROVIDER = LLMFactory.create_provider(os.getenv("LLM_PROVIDER"))

# DB Dependency
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def is_llm_enabled(db: Session):
    config = db.query(SystemConfig).filter(SystemConfig.key == "LLM_ENABLED").first()
    return config.value.lower() == "true" if config else False

# Circuit Breaker Durumu
MEILI_STATUS = {"healthy": True, "last_check": 0}
CIRCUIT_BREAKER_TIME = int(os.getenv("CIRCUIT_BREAKER_TIME", "30"))


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

class LLMConfigRequest(BaseModel):
    enabled: bool


# ─────────────────────────────────────────────
#  SEARCH (Orijinal endpoint korundu)
# ─────────────────────────────────────────────

@app.get("/api/search")
async def search(q: str = Query(..., min_length=2), db: Session = Depends(get_db)):
    current_time = time.time()
    meili_hits = []

    try:
        # --- ADIM 1: MEILISEARCH (KEYWORD SEARCH) ---
        if MEILI_STATUS["healthy"] or (current_time - MEILI_STATUS["last_check"] > CIRCUIT_BREAKER_TIME):
            try:
                meili_hits = MEILI_PROVIDER.search(q, limit=3)
                MEILI_STATUS["healthy"] = True

                if meili_hits and meili_hits[0]['score'] >= 0.85:
                    return {
                        "source": "meilisearch",
                        "status": "success",
                        "answer": meili_hits[0]['answer'],
                        "question": meili_hits[0]['question'],
                        "score": meili_hits[0]['score']
                    }
            except Exception as e:
                MEILI_STATUS["healthy"] = False
                MEILI_STATUS["last_check"] = current_time
                logger.error(f"⚠️ MeiliSearch çöktü! {CIRCUIT_BREAKER_TIME} saniye boyunca Qdrant kullanılacak.")
        else:
            logger.info("⚡ Circuit Breaker aktif: MeiliSearch atlanıyor...")

        # --- ADIM 2: QDRANT (SEMANTIC SEARCH) ---
        try:
            qdrant_hits = QDRANT_PROVIDER.search(q, limit=3)
            if qdrant_hits and qdrant_hits[0]['score'] > 0.75:
                return {
                    "source": "qdrant_vector",
                    "status": "success",
                    "answer": qdrant_hits[0]['answer'],
                    "question": qdrant_hits[0]['question'],
                    "score": qdrant_hits[0]['score']
                }
        except Exception as e:
            logger.error(f"Qdrant hatası: {str(e)}")
            qdrant_hits = []

        # --- ADIM 3: LLM FALLBACK (RAG) ---
        if is_llm_enabled(db) and qdrant_hits:
            try:
                answer = LLM_PROVIDER.ask(q, qdrant_hits)
                return {
                    "source": "llm",
                    "status": "success",
                    "answer": answer,
                    "question": q,
                    "context_used": [h['question'] for h in qdrant_hits]
                }
            except Exception as e:
                logger.error(f"LLM Hatası: {str(e)}")

        # --- ADIM 4: SUGGESTIONS ---
        suggestions = []
        try:
            suggestions = MEILI_PROVIDER.get_suggestions(q)
        except:
            pass

        return {
            "source": "none",
            "status": "suggest",
            "message": "Doğrudan bir cevap bulamadım. Bunları mı demek istediniz?",
            "suggestions": suggestions
        }

    except Exception as e:
        return {"status": "error", "message": f"Sistem genel hatası: {str(e)}"}


# ─────────────────────────────────────────────
#  QnA CRUD Endpoint'leri
# ─────────────────────────────────────────────

@app.get("/api/qna")
def list_qna(
    skip: int = 0,
    limit: int = 500,
    db: Session = Depends(get_db)
):
    """Tüm QnA kayıtlarını döner (AG Grid için)."""
    rows = db.query(QnA).order_by(QnA.id).offset(skip).limit(limit).all()
    return [
        {
            "id": r.id,
            "question_text": r.question_text,
            "answer_text": r.answer_text,
            "status": r.status,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        }
        for r in rows
    ]


@app.post("/api/qna", status_code=201)
def create_qna(body: QnACreateRequest, db: Session = Depends(get_db)):
    """Yeni QnA kaydı oluşturur."""
    row = QnA(
        question_text=body.question_text,
        answer_text=body.answer_text,
        status=body.status if body.status is not None else 1
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {
        "id": row.id,
        "question_text": row.question_text,
        "answer_text": row.answer_text,
        "status": row.status,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }

@app.put("/api/qna/bulk-update")
def bulk_update_qna(items: List[QnABulkUpdateItem], db: Session = Depends(get_db)):
    """Birden fazla QnA kaydını tek seferde günceller (AG Grid toplu kaydetme)."""
    updated = []
    for item in items:
        row = db.query(QnA).filter(QnA.id == item.id).first()
        if not row:
            continue
        if item.question_text is not None:
            row.question_text = item.question_text
        if item.answer_text is not None:
            row.answer_text = item.answer_text
        if item.status is not None:
            row.status = item.status
        updated.append(row.id)
    db.commit()
    return {"updated_ids": updated, "count": len(updated)}

@app.put("/api/qna/{qna_id}")
def update_qna(qna_id: int, body: QnAUpdateRequest, db: Session = Depends(get_db)):
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

    db.commit()
    db.refresh(row)
    return {
        "id": row.id,
        "question_text": row.question_text,
        "answer_text": row.answer_text,
        "status": row.status,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


@app.delete("/api/qna/{qna_id}", status_code=204)
def delete_qna(qna_id: int, db: Session = Depends(get_db)):
    """QnA kaydını siler (ilişkili queries ve tags cascade ile silinir)."""
    row = db.query(QnA).filter(QnA.id == qna_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="QnA kaydı bulunamadı.")
    db.delete(row)
    db.commit()
    return None





# ─────────────────────────────────────────────
#  LLM Konfigürasyonu
# ─────────────────────────────────────────────

@app.get("/api/config/llm")
def get_llm_config(db: Session = Depends(get_db)):
    """LLM_ENABLED değerini döner."""
    config = db.query(SystemConfig).filter(SystemConfig.key == "LLM_ENABLED").first()
    enabled = config.value.lower() == "true" if config else False
    return {"enabled": enabled}


@app.put("/api/config/llm")
def set_llm_config(body: LLMConfigRequest, db: Session = Depends(get_db)):
    """LLM_ENABLED değerini günceller."""
    config = db.query(SystemConfig).filter(SystemConfig.key == "LLM_ENABLED").first()
    new_value = "true" if body.enabled else "false"

    if config:
        config.value = new_value
    else:
        config = SystemConfig(key="LLM_ENABLED", value=new_value)
        db.add(config)

    db.commit()
    logger.info(f"LLM durumu güncellendi: {new_value}")
    return {"enabled": body.enabled, "message": f"LLM {'etkinleştirildi' if body.enabled else 'devre dışı bırakıldı'}."}
