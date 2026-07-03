from fastapi import FastAPI, Query, Depends, HTTPException, Request, BackgroundTasks, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
import csv
import io
from sqlalchemy.orm import Session
from sqlalchemy import text
from database import SessionLocal, SystemConfig, QnA, QueryLog, AcademicCalendar
from providers import MeiliSearchProvider, QdrantProvider
from llm_provider import LLMFactory
from calendar_utils import format_calendar_answer, match_calendar_entry
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
    host=os.getenv("QDRANT_HOST", "localhost"),
    port=int(os.getenv("QDRANT_PORT", "6333")),
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

def _llm_select_answer(q: str, qdrant_hits: list):
    """Selector mantığını korur: tek soruda mevcut davranış, çoklu soruda
    her alt soru için ayrı retrieval + ayrı seçim yapıp cevapları birleştirir.

    Tek bir birleşik sorgu için yapılan embedding/retrieval, ikinci soruyu
    aday havuzundan dışarıda bırakabildiği için her alt soru kendi
    retrieval'ı ile seçtirilir. Sorular noktalama olmadan yazılmış olsa bile
    LLM ile ayrılır. Cevaplar yine birebir (verbatim) seçilir, asla üretilmez.
    """
    sub_questions = LLM_PROVIDER.split_questions(q)
    if len(sub_questions) <= 1:
        return LLM_PROVIDER.ask(q, qdrant_hits)

    answers = []
    for sub_q in sub_questions:
        # Bir alt sorudaki hata (retrieval ya da seçim), diğer alt soruların
        # başarılı cevaplarını kaybettirmemeli.
        try:
            hits = QDRANT_PROVIDER.search(sub_q, limit=5)
            if not hits:
                continue
            ans = LLM_PROVIDER.ask(sub_q, hits)
        except Exception:
            continue
        if ans and ans not in answers:
            answers.append(ans)

    if not answers:
        return None
    return "\n\n".join(answers)

@app.on_event("startup")
async def startup_event():
    try:
        QDRANT_PROVIDER.ensure_collection()
        logger.info("Qdrant koleksiyonu hazır.")
    except Exception as e:
        logger.error(f"Qdrant collection init hatası: {e}")


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

class WidgetChatRequest(BaseModel):
    message: str

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

    if use_llm:
        candidates = [
            {
                "question": f"{e.event} ne zaman?",
                "answer": format_calendar_answer(e.period, e.event, e.start_date, e.end_date),
            }
            for e in entries
        ]
        try:
            answer = LLM_PROVIDER.ask(query, candidates)
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
#  Query Logging (arka planda, yanıt yolunu etkilemez)
# ─────────────────────────────────────────────

def _log_query(source: str, status: str, ip: Optional[str]):
    db = SessionLocal()
    try:
        db.add(QueryLog(source=source, status=status, ip_address=ip))
        db.commit()
    except Exception as e:
        logger.error(f"Query log yazılamadı: {e}")
    finally:
        db.close()


# ─────────────────────────────────────────────
#  Widget Chat Adapter
# ─────────────────────────────────────────────

@app.post("/widget-chat")
async def widget_chat(body: WidgetChatRequest, request: Request, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Embed widget için basit adapter: {message} → {answer}"""
    q = body.message.strip()
    if not q:
        return {"answer": "Lütfen bir soru yazın."}

    ip = request.client.host if request.client else None
    current_time = time.time()
    meili_hits = []

    # --- ADIM 0: AKADEMİK TAKVİM KONTROLÜ ---
    if is_date_query(q):
        cal_answer = search_calendar(q, db, is_llm_enabled(db))
        if cal_answer:
            background_tasks.add_task(_log_query, "academic_calendar", "success", ip)
            return {"answer": cal_answer}

    # MeiliSearch
    try:
        if MEILI_STATUS["healthy"] or (current_time - MEILI_STATUS["last_check"] > CIRCUIT_BREAKER_TIME):
            meili_hits = MEILI_PROVIDER.search(q, limit=3)
            MEILI_STATUS["healthy"] = True
            if meili_hits and meili_hits[0]["score"] >= 0.90:
                background_tasks.add_task(_log_query, "meilisearch", "success", ip)
                return {"answer": meili_hits[0]["answer"]}
    except Exception:
        MEILI_STATUS["healthy"] = False
        MEILI_STATUS["last_check"] = current_time
        meili_hits = []

    # Qdrant
    qdrant_hits = []
    try:
        qdrant_hits = QDRANT_PROVIDER.search(q, limit=5)
        if qdrant_hits and qdrant_hits[0]["score"] > 0.75:
            background_tasks.add_task(_log_query, "qdrant_vector", "success", ip)
            return {"answer": qdrant_hits[0]["answer"]}
    except Exception:
        qdrant_hits = []

    # LLM (RAG fallback) — selector mode: picks verbatim answer or None
    if is_llm_enabled(db) and qdrant_hits:
        try:
            answer = _llm_select_answer(q, qdrant_hits)
            if answer:
                background_tasks.add_task(_log_query, "llm", "success", ip)
                return {"answer": answer}
        except Exception:
            pass

    # Öneriler
    try:
        suggestions = MEILI_PROVIDER.get_suggestions(q, limit=20)
        if suggestions:
            background_tasks.add_task(_log_query, "none", "suggest", ip)
            return {
                "answer": "Bu konuda net bir bilgim yok. Şunları sormak istemiş olabilirsiniz:",
                "suggestions": suggestions
            }
    except Exception:
        pass

    background_tasks.add_task(_log_query, "none", "suggest", ip)
    return {"answer": "Bu konuda bilgim bulunmuyor."}


# ─────────────────────────────────────────────
#  SEARCH (Orijinal endpoint korundu)
# ─────────────────────────────────────────────

@app.get("/api/search")
async def search(request: Request, background_tasks: BackgroundTasks, q: str = Query(..., min_length=2), db: Session = Depends(get_db)):
    ip = request.client.host if request.client else None
    current_time = time.time()
    meili_hits = []

    try:
        # --- ADIM 0: AKADEMİK TAKVİM KONTROLÜ ---
        if is_date_query(q):
            cal_answer = search_calendar(q, db, is_llm_enabled(db))
            if cal_answer:
                background_tasks.add_task(_log_query, "academic_calendar", "success", ip)
                return {
                    "source": "academic_calendar",
                    "status": "success",
                    "answer": cal_answer,
                    "question": q
                }

        # --- ADIM 1: MEILISEARCH (KEYWORD SEARCH) ---
        if MEILI_STATUS["healthy"] or (current_time - MEILI_STATUS["last_check"] > CIRCUIT_BREAKER_TIME):
            try:
                meili_hits = MEILI_PROVIDER.search(q, limit=3)
                MEILI_STATUS["healthy"] = True

                if meili_hits and meili_hits[0]['score'] >= 0.90:
                    background_tasks.add_task(_log_query, "meilisearch", "success", ip)
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
            qdrant_hits = QDRANT_PROVIDER.search(q, limit=5)
            if qdrant_hits and qdrant_hits[0]['score'] > 0.75:
                background_tasks.add_task(_log_query, "qdrant_vector", "success", ip)
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

        # --- ADIM 3: LLM FALLBACK (RAG) — selector mode: picks verbatim answer or None ---
        if is_llm_enabled(db) and qdrant_hits:
            try:
                answer = _llm_select_answer(q, qdrant_hits)
                if answer:
                    background_tasks.add_task(_log_query, "llm", "success", ip)
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
            suggestions = MEILI_PROVIDER.get_suggestions(q, limit=20)
        except:
            pass

        background_tasks.add_task(_log_query, "none", "suggest", ip)
        return {
            "source": "none",
            "status": "suggest",
            "message": "Doğrudan bir cevap bulamadım. Bunları mı demek istediniz?",
            "suggestions": suggestions
        }

    except Exception as e:
        background_tasks.add_task(_log_query, "none", "error", ip)
        return {"status": "error", "message": f"Sistem genel hatası: {str(e)}"}


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
    if not doc:
        return
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
    
    # Sync with MeiliSearch & Qdrant
    sync_providers(db, row.id)
    
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
    
    # Sync all updated records
    for qna_id in updated:
        sync_providers(db, qna_id)

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

    # Sync with MeiliSearch & Qdrant
    sync_providers(db, row.id)

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

    # Remove from MeiliSearch & Qdrant
    remove_from_providers(qna_id)
    
    return None


# ─────────────────────────────────────────────
#  CSV Import
# ─────────────────────────────────────────────

@app.post("/api/qna/import")
async def import_csv(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """CSV dosyasından toplu QnA içe aktarır. Format: question;answer;tags;query_1;...;query_20"""
    content = await file.read()
    try:
        text_content = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text_content = content.decode("latin-1")

    reader = csv.DictReader(io.StringIO(text_content), delimiter=';')
    inserted_ids = []

    for row in reader:
        question = row.get('question', '').strip()
        answer = row.get('answer', '').strip()
        if not question or not answer:
            continue

        result = db.execute(
            text("INSERT INTO qna (question_text, answer_text) VALUES (:q, :a) RETURNING id"),
            {"q": question, "a": answer}
        ).fetchone()
        qna_id = result[0]
        inserted_ids.append(qna_id)

        tags_val = row.get('tags', '')
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
            query_val = row.get(f'query_{i}', '').strip()
            if query_val:
                db.execute(
                    text("INSERT INTO qna_queries (qna_id, query_text) VALUES (:q_id, :qt)"),
                    {"q_id": qna_id, "qt": query_val}
                )

    db.commit()

    for qna_id in inserted_ids:
        sync_providers(db, qna_id)

    logger.info(f"CSV import tamamlandı: {len(inserted_ids)} kayıt eklendi.")
    return {"imported": len(inserted_ids)}


@app.get("/api/qna/export")
def export_qna(db: Session = Depends(get_db)):
    """Tüm QnA verisini içe aktarma (import) formatında CSV olarak dışa aktarır.

    Sütunlar import ile birebir aynıdır (question;answer;tags;query_1;...;query_20),
    böylece dışa aktarılan dosya tekrar içe aktarılabilir (round-trip).
    """
    rows = db.query(QnA).order_by(QnA.id).all()

    buf = io.StringIO()
    fieldnames = ["question", "answer", "tags"] + [f"query_{i}" for i in range(1, 21)]
    writer = csv.DictWriter(buf, fieldnames=fieldnames, delimiter=";", extrasaction="ignore")
    writer.writeheader()

    for r in rows:
        record = {
            "question": r.question_text,
            "answer": r.answer_text,
            "tags": ", ".join(t.name for t in r.tags),
        }
        for i, query in enumerate(r.queries[:20], start=1):
            record[f"query_{i}"] = query.query_text
        writer.writerow(record)

    # utf-8-sig (BOM) → Excel Türkçe karakterleri doğru gösterir; import BOM'u atlıyor.
    data = buf.getvalue().encode("utf-8-sig")
    logger.info(f"QnA export: {len(rows)} kayıt dışa aktarıldı.")
    return Response(
        content=data,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=qna_export.csv"},
    )


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


# ─────────────────────────────────────────────
#  İzleme İstatistikleri
# ─────────────────────────────────────────────

@app.get("/api/stats")
def get_stats(db: Session = Depends(get_db)):
    from datetime import datetime, timedelta

    # Turkey is UTC+3 with no DST (since 2016)
    ISTANBUL_OFFSET = timedelta(hours=3)
    now_utc = datetime.utcnow()
    now_istanbul = now_utc + ISTANBUL_OFFSET

    # Midnight in Istanbul time, converted back to UTC for DB comparison
    today_start_utc = (now_istanbul.replace(hour=0, minute=0, second=0, microsecond=0)) - ISTANBUL_OFFSET
    five_min_ago = now_utc - timedelta(minutes=5)

    active_users = db.execute(
        text("SELECT COUNT(*)::int FROM query_logs WHERE created_at >= :since"),
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

    sources = {"meilisearch": 0, "qdrant_vector": 0, "llm": 0, "none": 0}
    for row in source_rows:
        if row.source in sources:
            sources[row.source] = row.cnt

    return {
        "active_users": int(active_users),
        "total_queries_today": int(total_today),
        "hourly": [{"label": row.label, "count": row.count} for row in hourly_rows],
        "sources": sources
    }


# ─────────────────────────────────────────────
#  Akademik Takvim CRUD
# ─────────────────────────────────────────────

@app.get("/api/academic-calendar")
def list_calendar(skip: int = 0, limit: int = 500, db: Session = Depends(get_db)):
    """Tüm akademik takvim kayıtlarını döner."""
    rows = db.query(AcademicCalendar).order_by(AcademicCalendar.id).offset(skip).limit(limit).all()
    return [
        {
            "id": r.id,
            "period": r.period,
            "event": r.event,
            "start_date": r.start_date,
            "end_date": r.end_date,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        }
        for r in rows
    ]


@app.post("/api/academic-calendar", status_code=201)
def create_calendar(body: AcademicCalendarCreateRequest, db: Session = Depends(get_db)):
    """Yeni takvim kaydı oluşturur."""
    row = AcademicCalendar(
        period=body.period,
        event=body.event,
        start_date=body.start_date,
        end_date=body.end_date,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {
        "id": row.id,
        "period": row.period,
        "event": row.event,
        "start_date": row.start_date,
        "end_date": row.end_date,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


@app.put("/api/academic-calendar/bulk-update")
def bulk_update_calendar(items: List[AcademicCalendarBulkUpdateItem], db: Session = Depends(get_db)):
    """Birden fazla takvim kaydını tek seferde günceller."""
    updated = []
    for item in items:
        row = db.query(AcademicCalendar).filter(AcademicCalendar.id == item.id).first()
        if not row:
            continue
        if item.period is not None:
            row.period = item.period
        if item.event is not None:
            row.event = item.event
        if item.start_date is not None:
            row.start_date = item.start_date
        if item.end_date is not None:
            row.end_date = item.end_date
        updated.append(row.id)
    db.commit()
    return {"updated_ids": updated, "count": len(updated)}


@app.put("/api/academic-calendar/{cal_id}")
def update_calendar(cal_id: int, body: AcademicCalendarUpdateRequest, db: Session = Depends(get_db)):
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
    db.commit()
    db.refresh(row)
    return {
        "id": row.id,
        "period": row.period,
        "event": row.event,
        "start_date": row.start_date,
        "end_date": row.end_date,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


@app.delete("/api/academic-calendar/{cal_id}", status_code=204)
def delete_calendar(cal_id: int, db: Session = Depends(get_db)):
    """Takvim kaydını siler."""
    row = db.query(AcademicCalendar).filter(AcademicCalendar.id == cal_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Takvim kaydı bulunamadı.")
    db.delete(row)
    db.commit()
    return None


@app.post("/api/academic-calendar/import")
async def import_calendar_csv(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """CSV dosyasından toplu takvim verisi içe aktarır.
    Kabul edilen formatlar (virgül veya noktalı virgül):
      Donem,Etkinlik,Baslangic_Tarihi,Bitis_Tarihi
      period;event;start_date;end_date
    """
    content = await file.read()
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
        ))
        inserted += 1

    db.commit()
    logger.info(f"Takvim CSV import tamamlandı: {inserted} kayıt eklendi.")
    return {"imported": inserted}


@app.get("/api/academic-calendar/export")
def export_calendar(db: Session = Depends(get_db)):
    """Takvim verisini içe aktarma (import) formatında CSV olarak dışa aktarır.

    Sütunlar import ile birebir aynıdır (Donem,Etkinlik,Baslangic_Tarihi,Bitis_Tarihi),
    böylece dışa aktarılan dosya tekrar içe aktarılabilir (round-trip).
    """
    rows = db.query(AcademicCalendar).order_by(AcademicCalendar.id).all()

    buf = io.StringIO()
    fieldnames = ["Donem", "Etkinlik", "Baslangic_Tarihi", "Bitis_Tarihi"]
    writer = csv.DictWriter(buf, fieldnames=fieldnames, delimiter=",")
    writer.writeheader()

    for r in rows:
        writer.writerow({
            "Donem": r.period,
            "Etkinlik": r.event,
            "Baslangic_Tarihi": r.start_date,
            "Bitis_Tarihi": r.end_date,
        })

    # utf-8-sig (BOM) → Excel Türkçe karakterleri doğru gösterir; import BOM'u atlıyor.
    data = buf.getvalue().encode("utf-8-sig")
    logger.info(f"Takvim export: {len(rows)} kayıt dışa aktarıldı.")
    return Response(
        content=data,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=akademik_takvim_export.csv"},
    )
