"""Çözüm Merkezi entegrasyonu — dış API.

Chatbot yalnızca SolutionCenterService ile konuşur. Bağımlılıklar (client,
cache, config) FastAPI Depends ile enjekte edilir; böylece testte mock
transport'lu bir client kolayca override edilebilir.
"""
from fastapi import Depends
from sqlalchemy.orm import Session

from core.deps import get_db

from .base_client import SolutionCenterConfig
from .category_cache import CategoryCache
from .client import SolutionCenterClient
from .service import SolutionCenterService

# ── Boot'ta bir kez kurulan paylaşılan durum ─────────────────────────────────
# Config env'den okunur (service token yalnızca backend'de kalır). Client tek
# bir httpx.AsyncClient'ı paylaşır (connection pooling). Cache worker başına.
_CONFIG = SolutionCenterConfig.from_env()
_CLIENT = SolutionCenterClient(_CONFIG)
_CACHE = CategoryCache(_CONFIG.category_cache_ttl_seconds)


# ── FastAPI bağımlılıkları (testte override edilebilir) ──────────────────────

def get_config() -> SolutionCenterConfig:
    return _CONFIG


def get_sc_client() -> SolutionCenterClient:
    return _CLIENT


def get_category_cache() -> CategoryCache:
    return _CACHE


def get_sc_service(
    db: Session = Depends(get_db),
    client: SolutionCenterClient = Depends(get_sc_client),
    cache: CategoryCache = Depends(get_category_cache),
    config: SolutionCenterConfig = Depends(get_config),
) -> SolutionCenterService:
    return SolutionCenterService(db=db, client=client, cache=cache, config=config)


__all__ = [
    "SolutionCenterService",
    "SolutionCenterConfig",
    "SolutionCenterClient",
    "CategoryCache",
    "get_config",
    "get_sc_client",
    "get_category_cache",
    "get_sc_service",
]
