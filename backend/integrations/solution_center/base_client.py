"""Çözüm Merkezi HTTP taban istemcisi.

Sorumluluk (SADECE transport):
- httpx.AsyncClient yaşam döngüsü + connection pooling
- Authorization header (service token) — ASLA loglanmaz
- Timeout + basit retry (ağ/5xx)
- SSL doğrulaması AÇIK (SPEC güvenlik kuralı)
- Gövde/response ASLA loglanmaz (TC/OTP/token sızmasın)

İş mantığı YOK. Endpoint'e özel metodlar client.py'dedir.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from .constants import (
    AUTH_HEADER,
    DEFAULT_AUTH_SCHEME,
    DEFAULT_CATEGORY_CACHE_TTL_SECONDS,
    DEFAULT_CHANNEL_SHORTCODE,
    DEFAULT_MAX_RETRIES,
    DEFAULT_OTP_MAX_ATTEMPTS,
    DEFAULT_SMS_CONVERSATION_WINDOW_MIN,
    DEFAULT_SMS_IP_WINDOW_MIN,
    DEFAULT_SMS_PER_CONVERSATION,
    DEFAULT_SMS_PER_IP,
    DEFAULT_SMS_PER_TC,
    DEFAULT_SMS_TC_WINDOW_MIN,
    DEFAULT_TIMEOUT_SECONDS,
    DEFAULT_VERIFICATION_TTL_MIN,
    ENV_AUTH_SCHEME,
    ENV_BASE_URL,
    ENV_CATEGORY_CACHE_TTL,
    ENV_CHANNEL_SHORTCODE,
    ENV_HASH_SECRET,
    ENV_MAX_RETRIES,
    ENV_OTP_MAX_ATTEMPTS,
    ENV_SERVICE_TOKEN,
    ENV_SMS_CONVERSATION_WINDOW_MIN,
    ENV_SMS_IP_WINDOW_MIN,
    ENV_SMS_PER_CONVERSATION,
    ENV_SMS_PER_IP,
    ENV_SMS_PER_TC,
    ENV_SMS_TC_WINDOW_MIN,
    ENV_TIMEOUT,
    ENV_VERIFICATION_TTL_MIN,
)
from .exceptions import ApiConnectionException, UnauthorizedException

logger = logging.getLogger("auzef.sc")

# GÜVENLİK: httpx/httpcore DEBUG logu istek gövdesini (TC/OTP/token) dökebilir.
# Modül yüklenirken bu logger'ları WARNING'e sabitle — "loglanmayacak" kuralının
# pratikte en çok ihlal edildiği yer burasıdır.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, "").strip() or default)
    except ValueError:
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, "").strip() or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class SolutionCenterConfig:
    """Env'den okunan yapılandırma. Service token yalnızca backend'de kalır."""
    base_url: Optional[str]
    service_token: Optional[str]
    channel_short_code: str
    timeout_seconds: float
    max_retries: int
    verification_ttl_min: int
    category_cache_ttl_seconds: int
    # Authorization header şeması ("Api-Key" / "Bearer" ...). Token bunun ardına
    # eklenir → "Authorization: <auth_scheme> <service_token>".
    auth_scheme: str = DEFAULT_AUTH_SCHEME

    # ── SMS hız sınırı (P0-1) ────────────────────────────────────────────────
    # Üç kapsam ayrı ayrı bütçelenir; ayrıntı için rate_limit.py.
    sms_per_conversation: int = DEFAULT_SMS_PER_CONVERSATION
    sms_conversation_window_min: int = DEFAULT_SMS_CONVERSATION_WINDOW_MIN
    sms_per_tc: int = DEFAULT_SMS_PER_TC
    sms_tc_window_min: int = DEFAULT_SMS_TC_WINDOW_MIN
    sms_per_ip: int = DEFAULT_SMS_PER_IP
    sms_ip_window_min: int = DEFAULT_SMS_IP_WINDOW_MIN
    # TC hash'lemede kullanılan secret; None ise service_token'dan türetilir.
    hash_secret: Optional[str] = None

    # ── OTP deneme sayacı (P0-2) ─────────────────────────────────────────────
    # Kaç yanlış koddan sonra doğrulama oturumu iptal edilir. SMS limitiyle
    # çarpım etkisi için bkz. constants.DEFAULT_OTP_MAX_ATTEMPTS notu.
    otp_max_attempts: int = DEFAULT_OTP_MAX_ATTEMPTS

    @property
    def enabled(self) -> bool:
        """base_url + service_token yoksa entegrasyon KAPALI (uçlar nazik 503 döner)."""
        return bool(self.base_url) and bool(self.service_token)

    @classmethod
    def from_env(cls) -> "SolutionCenterConfig":
        return cls(
            base_url=(os.getenv(ENV_BASE_URL) or "").strip() or None,
            service_token=(os.getenv(ENV_SERVICE_TOKEN) or "").strip() or None,
            channel_short_code=(os.getenv(ENV_CHANNEL_SHORTCODE) or "").strip() or DEFAULT_CHANNEL_SHORTCODE,
            timeout_seconds=_env_float(ENV_TIMEOUT, DEFAULT_TIMEOUT_SECONDS),
            max_retries=_env_int(ENV_MAX_RETRIES, DEFAULT_MAX_RETRIES),
            verification_ttl_min=_env_int(ENV_VERIFICATION_TTL_MIN, DEFAULT_VERIFICATION_TTL_MIN),
            category_cache_ttl_seconds=_env_int(ENV_CATEGORY_CACHE_TTL, DEFAULT_CATEGORY_CACHE_TTL_SECONDS),
            auth_scheme=(os.getenv(ENV_AUTH_SCHEME) or "").strip() or DEFAULT_AUTH_SCHEME,
            sms_per_conversation=_env_int(ENV_SMS_PER_CONVERSATION, DEFAULT_SMS_PER_CONVERSATION),
            sms_conversation_window_min=_env_int(
                ENV_SMS_CONVERSATION_WINDOW_MIN, DEFAULT_SMS_CONVERSATION_WINDOW_MIN),
            sms_per_tc=_env_int(ENV_SMS_PER_TC, DEFAULT_SMS_PER_TC),
            sms_tc_window_min=_env_int(ENV_SMS_TC_WINDOW_MIN, DEFAULT_SMS_TC_WINDOW_MIN),
            sms_per_ip=_env_int(ENV_SMS_PER_IP, DEFAULT_SMS_PER_IP),
            sms_ip_window_min=_env_int(ENV_SMS_IP_WINDOW_MIN, DEFAULT_SMS_IP_WINDOW_MIN),
            hash_secret=(os.getenv(ENV_HASH_SECRET) or "").strip() or None,
            otp_max_attempts=_env_int(ENV_OTP_MAX_ATTEMPTS, DEFAULT_OTP_MAX_ATTEMPTS),
        )


# Retry'lanabilir HTTP durumları (geçici sunucu/ağ hataları).
_RETRYABLE_STATUS = {502, 503, 504}


class BaseClient:
    """httpx.AsyncClient sarmalayıcısı: auth, timeout, retry, güvenli loglama."""

    def __init__(
        self,
        config: SolutionCenterConfig,
        *,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        # transport: testlerde httpx.MockTransport enjekte edilir (DI, ağ yok).
        self._config = config
        headers = {}
        if config.service_token:
            headers[AUTH_HEADER] = f"{config.auth_scheme} {config.service_token}"
        self._client = httpx.AsyncClient(
            base_url=config.base_url or "",
            headers=headers,
            timeout=config.timeout_seconds,
            verify=True,  # SSL doğrulaması açık
            transport=transport,
        )

    @property
    def config(self) -> SolutionCenterConfig:
        return self._config

    async def aclose(self) -> None:
        await self._client.aclose()

    async def post(self, path: str, json_body: dict) -> Any:
        """POST atar, çözümlenmiş JSON gövdesini (dict ya da list) döner.

        - Ağ/timeout → ApiConnectionException
        - 401/403     → UnauthorizedException (service token sorunu)
        - 5xx         → ApiConnectionException (retry sonrası)
        - isSuccess=false yorumlaması ÇAĞIRAN'a (service) aittir; burada ham
          gövde döner (iş mantığı client katmanında yok — SPEC kuralı).
        GÖVDE/RESPONSE ASLA LOGLANMAZ.
        """
        started = time.monotonic()
        last_exc: Optional[Exception] = None
        # İlk deneme + max_retries yeniden deneme.
        for attempt in range(self._config.max_retries + 1):
            try:
                resp = await self._client.post(path, json=json_body)
            except httpx.TimeoutException as exc:
                last_exc = exc
                logger.warning("SC timeout endpoint=%s deneme=%d", path, attempt + 1)
            except httpx.TransportError as exc:
                last_exc = exc
                logger.warning("SC bağlantı hatası endpoint=%s deneme=%d", path, attempt + 1)
            else:
                elapsed_ms = int((time.monotonic() - started) * 1000)
                if resp.status_code in (401, 403):
                    logger.error("SC yetki hatası endpoint=%s status=%d sure=%dms",
                                 path, resp.status_code, elapsed_ms)
                    raise UnauthorizedException(f"CM auth reddetti: {resp.status_code}")
                if resp.status_code in _RETRYABLE_STATUS and attempt < self._config.max_retries:
                    logger.warning("SC geçici hata endpoint=%s status=%d — yeniden denenecek",
                                   path, resp.status_code)
                    last_exc = ApiConnectionException(f"CM geçici hata: {resp.status_code}")
                    await asyncio.sleep(0.2 * (attempt + 1))
                    continue
                # Endpoint + status + süre loglanır; gövde ASLA.
                logger.info("SC istek endpoint=%s status=%d sure=%dms",
                            path, resp.status_code, elapsed_ms)
                if resp.status_code >= 500:
                    raise ApiConnectionException(f"CM sunucu hatası: {resp.status_code}")
                try:
                    return resp.json()
                except ValueError as exc:
                    raise ApiConnectionException("CM yanıtı çözümlenemedi") from exc

            # Ağ hatası → kısa bekleyip yeniden dene (retry bütçesi varsa).
            if attempt < self._config.max_retries:
                await asyncio.sleep(0.2 * (attempt + 1))

        raise ApiConnectionException("CM'ye ulaşılamadı") from last_exc
