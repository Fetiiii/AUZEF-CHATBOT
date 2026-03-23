from fastapi import FastAPI, Query, Depends
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from database import SessionLocal, SystemConfig
from providers import MeiliSearchProvider, QdrantProvider
import logging

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
    url='http://localhost:7700', 
    master_key='masterKey123', 
    index_name='auzef_qna_index'
)

QDRANT_PROVIDER = QdrantProvider(
    host="localhost", 
    port=6333, 
    collection_name="auzef_qna_vectors",
    model_name="nezahatkorkmaz/turkce-embedding-bge-m3"
)

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

import time

# Circuit Breaker Durumu
MEILI_STATUS = {"healthy": True, "last_check": 0}
CIRCUIT_BREAKER_TIME = 30  # 30 saniye boyunca deneme

@app.get("/search")
async def search(q: str = Query(..., min_length=2), db: Session = Depends(get_db)):
    current_time = time.time()
    meili_hits = []

    try:
        # --- ADIM 1: MEILISEARCH (KEYWORD SEARCH) ---
        # Eğer Meili sağlıklıysa VEYA devre kesici süresi dolduysa dene
        if MEILI_STATUS["healthy"] or (current_time - MEILI_STATUS["last_check"] > CIRCUIT_BREAKER_TIME):
            try:
                meili_hits = MEILI_PROVIDER.search(q, limit=3)
                MEILI_STATUS["healthy"] = True # Başarılıysa durumu düzelt
                
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
            # Devre kesici aktif, Meili'yi pas geç
            logger.info("⚡ Circuit Breaker aktif: MeiliSearch atlanıyor...")

        # --- ADIM 2: QDRANT (SEMANTIC SEARCH) ---
        # MeiliSearch zayıf kaldıysa VEYA devre kesici devreye girdiyse buraya gelir.
        try:
            qdrant_hits = QDRANT_PROVIDER.search(q, limit=3)
            if qdrant_hits and qdrant_hits[0]['score'] > 0.50:
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

        # --- ADIM 3: LLM FALLBACK (OPSİYONEL) ---
        if is_llm_enabled(db):
            # Buraya mimarideki LLM Prompt + Context Retrieval mantığı gelecek.
            # Şimdilik placeholder dönüyoruz.
            return {
                "source": "llm",
                "status": "processing",
                "message": "Cevap üretiliyor (LLM yakında aktif edilecek)...",
                "context_used": [h['question'] for h in (meili_hits + qdrant_hits)[:3]]
            }

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
