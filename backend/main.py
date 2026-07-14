"""FastAPI uygulama montaji: middleware, lifespan, router'lar, healthcheck.

Is mantigi modullere ayrildi:
- deps: DB/saglayicilar/LLM/circuit breaker/limitler
- answer_pipeline: cevap uretim zinciri
- csv_utils: CSV yardimcilari
- routers/*: uc noktalar (chat, conversations, qna, stats, calendar)
- auth / settings_api: oturum + ayarlar
"""
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.orm import Session

from dotenv import load_dotenv
load_dotenv()

from core.deps import get_db, QDRANT_PROVIDER
from admin.auth import router as auth_router, AdminAuthMiddleware
from admin.settings_api import router as settings_router
from routers.chat import router as chat_router
from routers.conversations import router as conversations_router
from routers.qna import router as qna_router
from routers.stats import router as stats_router
from routers.calendar import router as calendar_router

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("auzef")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Baslangic: Qdrant koleksiyonunu hazirla (on_event yerine lifespan).
    try:
        QDRANT_PROVIDER.ensure_collection()
        logger.info("Qdrant koleksiyonu hazir.")
    except Exception as e:
        logger.error(f"Qdrant collection init hatasi: {e}")
    yield


app = FastAPI(title="AUZEF Akilli Asistan API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Router'lar prefix'siz; tam yollari kendi dekoratorlerinde tasir. Middleware
# (ADMIN_AUTH_ENFORCED=true iken) yol->rol kuraliyla yonetim uclarini korur.
app.include_router(auth_router)
app.include_router(settings_router)
app.include_router(chat_router)
app.include_router(conversations_router)
app.include_router(qna_router)
app.include_router(stats_router)
app.include_router(calendar_router)
app.add_middleware(AdminAuthMiddleware)


@app.get("/health")
def health(db: Session = Depends(get_db)):
    """Container healthcheck ucu: public ve ucuz, DB baglantisini dogrular."""
    db.execute(text("SELECT 1"))
    return {"ok": True}
