"""Paylaşılan altyapı: DB oturumu, arama/LLM sağlayıcıları, circuit breaker,
girdi sınırları, sorgu logu. Router'lar ve cevap pipeline'ı buradan beslenir.

Bu modül ALT SEVİYEDİR: yalnızca database/providers/llm_provider'a bağımlıdır,
hiçbir router'a ya da settings_api'ye bağımlı DEĞİLDİR (döngüsel import olmasın).
"""
import logging
import os
import time
from typing import Optional

from sqlalchemy.orm import Session

from database import SessionLocal, SystemConfig, QueryLog, utcnow
from providers import MeiliSearchProvider, QdrantProvider
from llm_provider import LLMFactory, OpenRouterProvider

logger = logging.getLogger("auzef")

# Ayarlar sayfasından girilen OpenRouter anahtarının system_config anahtarı.
# (Eskiden settings_api'deydi; get_llm_provider buna ihtiyaç duyduğu için
# alt seviyeye taşındı — settings_api ve main buradan import eder.)
OPENROUTER_KEY_CONFIG = "OPENROUTER_API_KEY"

# Girdi sınırları: sınırsız mesaj = embedding CPU'su + LLM token maliyeti (DoS yüzeyi).
MAX_MESSAGE_LEN = 1000
MAX_IMPORT_BYTES = 5 * 1024 * 1024  # CSV import dosya boyutu üst sınırı

# ── Arama sağlayıcıları (boot'ta bir kez kurulur) ────────────────────────────
MEILI_PROVIDER = MeiliSearchProvider(
    url=os.getenv("MEILI_URL"),
    master_key=os.getenv("MEILI_MASTER_KEY"),
    index_name="auzef_qna_index",
)

QDRANT_PROVIDER = QdrantProvider(
    host=os.getenv("QDRANT_HOST", "localhost"),
    port=int(os.getenv("QDRANT_PORT", "6333")),
    collection_name="auzef_qna_vectors",
    model_name="nezahatkorkmaz/turkce-embedding-bge-m3",
)

# ── Circuit Breaker (Meili) ──────────────────────────────────────────────────
MEILI_STATUS = {"healthy": True, "last_check": 0}
CIRCUIT_BREAKER_TIME = int(os.getenv("CIRCUIT_BREAKER_TIME", "30"))


def meili_search_safe(query: str, limit: int) -> list:
    """Circuit-breaker'lı Meili araması. Meili çökükse boş liste döner ve
    CIRCUIT_BREAKER_TIME saniye boyunca Meili'yi atlar (her istekte yavaş
    hataya düşmemek için)."""
    now = time.time()
    if not MEILI_STATUS["healthy"] and (now - MEILI_STATUS["last_check"] <= CIRCUIT_BREAKER_TIME):
        return []
    try:
        hits = MEILI_PROVIDER.search(query, limit=limit)
        MEILI_STATUS["healthy"] = True
        return hits
    except Exception:
        MEILI_STATUS["healthy"] = False
        MEILI_STATUS["last_check"] = now
        logger.error(f"⚠️ MeiliSearch hatası — {CIRCUIT_BREAKER_TIME} sn atlanacak.")
        return []


# ── LLM sağlayıcısı ──────────────────────────────────────────────────────────
def _create_llm_provider():
    """LLM sağlayıcısını kurar; kurulamazsa None döner (uygulama ÇÖKMEZ).

    Env eksik/yanlışsa yalnızca LLM yolu kapanır; sistem eşik yedeğiyle
    (Meili/Qdrant/takvim) çalışmaya devam eder."""
    name = (os.getenv("LLM_PROVIDER") or "").strip().lower()
    if not name:
        logger.warning("LLM_PROVIDER tanımlı değil — LLM yolu devre dışı, eşik yedeğiyle çalışılacak.")
        return None
    try:
        return LLMFactory.create_provider(name)
    except Exception as e:
        logger.error(f"LLM sağlayıcı kurulamadı ({name!r}): {e} — LLM yolu devre dışı, eşik yedeğiyle devam.")
        return None


LLM_PROVIDER = _create_llm_provider()

# Ayarlar sayfasından girilen OpenRouter anahtarı için dinamik sağlayıcı durumu.
# Her worker kendi kopyasını tutar; anahtar HER İSTEKTE DB'den okunduğu için
# panelden yapılan değişiklik tüm worker'lara bir sonraki istekte yansır.
_dyn_llm = {"provider": None, "key": None}


def get_llm_provider(db: Session):
    """Etkin LLM sağlayıcısını döner (yoksa None).

    LLM_PROVIDER=openrouter iken anahtar önceliği: DB (ayarlar sayfası) → .env.
    Anahtar değiştiyse istemci yeniden kurulur (ucuz). Diğer sağlayıcılarda
    (openai/gemini) boot'ta kurulan statik sağlayıcı kullanılır."""
    name = (os.getenv("LLM_PROVIDER") or "").strip().lower()
    if name != "openrouter":
        return LLM_PROVIDER

    row = db.query(SystemConfig).filter(SystemConfig.key == OPENROUTER_KEY_CONFIG).first()
    key = (row.value if row else None) or os.getenv("OPENROUTER_API_KEY") or None
    if not key:
        return None
    if _dyn_llm["provider"] is None or _dyn_llm["key"] != key:
        try:
            _dyn_llm["provider"] = OpenRouterProvider(api_key=key)
            _dyn_llm["key"] = key
        except Exception as e:
            logger.error(f"OpenRouter sağlayıcısı kurulamadı: {e}")
            _dyn_llm["provider"] = None
            _dyn_llm["key"] = None
    return _dyn_llm["provider"]


def is_llm_enabled(db: Session) -> bool:
    # Kullanılabilir sağlayıcı yoksa (anahtar ne DB'de ne .env'de) LLM yolu
    # denenmez bile; DB'deki LLM_ENABLED açık olsa dahi eşik yedeğiyle devam.
    if get_llm_provider(db) is None:
        return False
    config = db.query(SystemConfig).filter(SystemConfig.key == "LLM_ENABLED").first()
    return config.value.lower() == "true" if config else False


# ── DB oturumu (FastAPI dependency) ──────────────────────────────────────────
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def actor_email(me) -> Optional[str]:
    """Denetim izi için: istek sahibinin e-postası (oturum yoksa None)."""
    return me.email if me is not None else None


# ── Query Logging (arka planda, yanıt yolunu etkilemez) ──────────────────────
def log_query(source: str, status: str, ip: Optional[str]):
    db = SessionLocal()
    try:
        db.add(QueryLog(source=source, status=status, ip_address=ip))
        db.commit()
    except Exception as e:
        logger.error(f"Query log yazılamadı: {e}")
    finally:
        db.close()
