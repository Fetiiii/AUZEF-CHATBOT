"""FastAPI uygulama montaji: middleware, router'lar ve healthcheck.

Is mantigi modullere ayrildi:
- deps: DB/saglayicilar/LLM/circuit breaker/limitler
- answer_pipeline: cevap uretim zinciri
- csv_utils: CSV yardimcilari
- routers/*: uc noktalar (chat, conversations, qna, stats, calendar)
- auth / settings_api: oturum + ayarlar
"""
import logging

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from dotenv import load_dotenv
load_dotenv()

from core.database import admin_engine, chat_engine, execute_admin_sql
from core.deps import get_db, MEILI_PROVIDER, QDRANT_PROVIDER
from admin.auth import router as auth_router, AdminAuthMiddleware
from admin.settings_api import router as settings_router
from routers.chat import router as chat_router
from routers.conversations import router as conversations_router
from routers.qna import router as qna_router
from routers.stats import router as stats_router
from routers.calendar import router as calendar_router
from routers.solution_center import router as solution_center_router
from integrations.solution_center.exceptions import SolutionCenterException

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("auzef")


app = FastAPI(title="AUZEF Akilli Asistan API")

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
app.include_router(solution_center_router)
app.add_middleware(AdminAuthMiddleware)


# Çözüm Merkezi hataları → kullanıcıya nazik mesaj (API detayları SIZDIRILMAZ).
# Teknik ayrıntı yalnızca sunucu logunda; yanıtta yalnızca user_message döner.
@app.exception_handler(SolutionCenterException)
async def _solution_center_exception_handler(request, exc: SolutionCenterException):
    from fastapi.responses import JSONResponse
    logger.warning("Çözüm Merkezi hatası: %s", exc.message)
    return JSONResponse(status_code=exc.http_status, content={"detail": exc.user_message})


@app.get("/health")
def health(db: Session = Depends(get_db)):
    """Container healthcheck ucu: public ve ucuz, DB baglantisini dogrular."""
    execute_admin_sql(db, text("SELECT 1"))
    return {"ok": True}


def _probe_database(engine) -> None:
    """Routed Session kullanmadan verilen engine'i doğrudan kontrol et."""
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))


def _dependency_status(name: str, probe) -> str:
    """Probe hatasını iç yanıt ayrıntılarına taşımadan ok/error'a indirger."""
    try:
        probe()
        return "ok"
    except Exception:
        logger.exception("Readiness dependency probe failed: %s", name)
        return "error"


@app.get("/health/live")
def health_live():
    """Yalnız FastAPI request işleme kabiliyetini bildirir."""
    return {"status": "ok"}


@app.get("/health/ready")
def health_ready():
    """LB admission için kritik DB'leri ve degrade search durumunu bildirir."""
    dependencies = {
        "db_admin": _dependency_status(
            "db_admin", lambda: _probe_database(admin_engine)
        ),
        "db_chat": _dependency_status(
            "db_chat", lambda: _probe_database(chat_engine)
        ),
        "meilisearch": _dependency_status(
            "meilisearch", MEILI_PROVIDER.healthcheck
        ),
        "qdrant": _dependency_status("qdrant", QDRANT_PROVIDER.healthcheck),
    }

    if dependencies["db_admin"] == "error" or dependencies["db_chat"] == "error":
        content = {"status": "unready", "dependencies": dependencies}
        return JSONResponse(status_code=503, content=content)

    status = (
        "degraded"
        if dependencies["meilisearch"] == "error"
        or dependencies["qdrant"] == "error"
        else "ready"
    )
    return {"status": status, "dependencies": dependencies}
