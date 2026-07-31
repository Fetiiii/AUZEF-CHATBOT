"""Kategori bellek cache'i (TTL).

SPEC: kategori endpoint'i her kullanıcı için tekrar çağrılmaz; ilk istek
API'den okur, sonrası bellek üzerinden gelir (TTL 15 dk).

Concurrency: async endpoint'ler tek event loop'ta koştuğundan asyncio.Lock ile
"cache stampede" (aynı anda N fetch) engellenir. Her uvicorn worker'ı kendi
cache'ini tutar — bu bir CACHE'tir, state değil; worker başına kopya sorun
değildir (SPEC "thread-safe" gereğini lock karşılar).
"""
from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable, List, Optional

from .models import Category


class CategoryCache:
    def __init__(self, ttl_seconds: int) -> None:
        self._ttl = ttl_seconds
        self._value: Optional[List[Category]] = None
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()

    def _fresh(self) -> bool:
        return self._value is not None and time.monotonic() < self._expires_at

    async def get_or_fetch(
        self, fetch: Callable[[], Awaitable[List[Category]]]
    ) -> List[Category]:
        """Taze cache varsa döner; yoksa (tek seferde) fetch edip saklar."""
        if self._fresh():
            return self._value  # type: ignore[return-value]
        async with self._lock:
            # Kilit beklerken başka bir çağrı doldurmuş olabilir (double-check).
            if self._fresh():
                return self._value  # type: ignore[return-value]
            value = await fetch()
            self._value = value
            self._expires_at = time.monotonic() + self._ttl
            return value

    def invalidate(self) -> None:
        self._value = None
        self._expires_at = 0.0
