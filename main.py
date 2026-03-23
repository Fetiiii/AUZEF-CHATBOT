from fastapi import FastAPI, Query, Depends
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from database import SessionLocal, SystemConfig
from providers import MeiliSearchProvider, QdrantProvider
from llm_provider import LLMFactory, OpenAIProvider
import os
import logging

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

import time

# Circuit Breaker Durumu
MEILI_STATUS = {"healthy": True, "last_check": 0}
CIRCUIT_BREAKER_TIME = int(os.getenv("CIRCUIT_BREAKER_TIME"))

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
        try:
            qdrant_hits = QDRANT_PROVIDER.search(q, limit=3)
            # Yüksek güvenli (0.75+) sonuç bulursa LLM'e gitme, direkt cevapla
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
