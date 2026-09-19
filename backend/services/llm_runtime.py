"""Runtime resolution of the managed AI config with a bounded in-process cache.

Resolution precedence:

1. Active DB config version (validated against the current registry) → ``DB``
   on load, ``CACHE`` while fresh.
2. No config version at all (never bootstrapped) → ``NOT_CONFIGURED``; the
   caller keeps the pre-Phase-6 env/default path (``ENV_BOOTSTRAP``).
3. Persisted config present but invalid → ``CONFIG_INVALID``: no LLM call,
   no env fallback (an operator error must not be hidden).
4. DB unreachable → last valid snapshot while younger than ``max_stale``
   (``STALE_CACHE``), else ``CONFIG_UNAVAILABLE`` (safe degraded mode).

Multi-node propagation: every node re-checks the active version id (one
primary-key ``max()`` query) at most every ``ttl`` seconds, so a change made on
one node is visible on every node within ``ttl`` (default 5 s). The writing
node invalidates its own cache immediately.

The cache never holds secrets: only validated effective configs, the version
id and registry provenance.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Mapping, Optional

from sqlalchemy.orm import Session

from services.circuit_breaker import LLM_CIRCUIT_BREAKER, breaker_key
from services.llm_config import EffectiveLLMConfigSet, LLMCapability

logger = logging.getLogger("auzef")

CACHE_TTL_ENV = "AI_CONFIG_CACHE_TTL_SECONDS"
MAX_STALE_ENV = "AI_CONFIG_MAX_STALE_SECONDS"
DEFAULT_CACHE_TTL_SECONDS = 5.0
DEFAULT_MAX_STALE_SECONDS = 300.0


class ConfigStatus(str, Enum):
    OK = "OK"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    CONFIG_INVALID = "CONFIG_INVALID"
    CONFIG_UNAVAILABLE = "CONFIG_UNAVAILABLE"


class ConfigSource(str, Enum):
    DB = "DB"
    CACHE = "CACHE"
    STALE_CACHE = "STALE_CACHE"
    ENV_BOOTSTRAP = "ENV_BOOTSTRAP"
    NONE = "NONE"


@dataclass(frozen=True)
class RuntimeAIConfig:
    status: ConfigStatus
    source: ConfigSource
    version_id: Optional[int] = None
    configs: Optional[EffectiveLLMConfigSet] = None
    capabilities: Mapping[str, dict] = field(default_factory=dict)
    error_code: Optional[str] = None

    def with_source(self, source: ConfigSource) -> "RuntimeAIConfig":
        return RuntimeAIConfig(self.status, source, self.version_id, self.configs,
                               self.capabilities, self.error_code)

    def breaker_keys(self) -> set:
        if self.configs is None:
            return set()
        return {
            breaker_key(cap.value, config.provider, config.model, config.fingerprint)
            for cap in (LLMCapability.INTENT_ANALYZER, LLMCapability.SELECTOR)
            for config in (self.configs.for_capability(cap),)
        }

    def trace_dict(self) -> dict:
        return {
            "ai_config_status": self.status.value,
            "ai_config_source": self.source.value,
            "ai_config_version": self.version_id,
            "ai_config_error": self.error_code,
            "ai_capabilities": {key: dict(value) for key, value in self.capabilities.items()},
        }


def _positive_float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise RuntimeError(f"{name} pozitif sayı olmalı, alınan: {raw!r}") from None
    if not value > 0:
        raise RuntimeError(f"{name} pozitif sayı olmalı")
    return value


def load_runtime_config(db: Session) -> RuntimeAIConfig:
    """Load and validate the active DB config (no caching)."""
    from services.ai_registry import AIConfigError, load_active_config

    try:
        active = load_active_config(db)
    except AIConfigError as exc:
        return RuntimeAIConfig(ConfigStatus.CONFIG_INVALID, ConfigSource.DB,
                               error_code=exc.code)
    if active is None:
        return RuntimeAIConfig(ConfigStatus.NOT_CONFIGURED, ConfigSource.ENV_BOOTSTRAP)
    configs = active.config_set()
    return RuntimeAIConfig(
        status=ConfigStatus.OK,
        source=ConfigSource.DB,
        version_id=active.version_id,
        configs=configs,
        capabilities={
            cap.value: {
                "registry_model_id": assignment.model.id,
                "provider": assignment.model.provider,
                "model": assignment.model.model_identifier,
                "qualification_status": assignment.model.qualification_status.value,
                "config_fingerprint": configs.for_capability(cap).fingerprint,
            }
            for cap, assignment in active.assignments.items()
        },
    )


def _probe_version(db: Session) -> Optional[int]:
    from services.ai_registry import current_version_id

    return current_version_id(db)


class AIConfigCache:
    def __init__(
        self,
        *,
        ttl_seconds: Optional[float] = None,
        max_stale_seconds: Optional[float] = None,
        clock: Callable[[], float] = time.monotonic,
        loader: Callable[[Session], RuntimeAIConfig] = load_runtime_config,
        prober: Callable[[Session], Optional[int]] = _probe_version,
        breaker=None,
        environ: Optional[Mapping[str, str]] = None,
    ):
        env = os.environ if environ is None else environ
        self.ttl = ttl_seconds if ttl_seconds is not None else _positive_float(
            env, CACHE_TTL_ENV, DEFAULT_CACHE_TTL_SECONDS)
        self.max_stale = max_stale_seconds if max_stale_seconds is not None else _positive_float(
            env, MAX_STALE_ENV, DEFAULT_MAX_STALE_SECONDS)
        self.clock = clock
        self.loader = loader
        self.prober = prober
        self.breaker = breaker
        self._lock = threading.Lock()
        self._current: Optional[RuntimeAIConfig] = None
        self._checked_at: Optional[float] = None
        self._last_valid: Optional[RuntimeAIConfig] = None
        self._last_valid_at: Optional[float] = None

    def invalidate(self) -> None:
        with self._lock:
            self._checked_at = None

    def reset(self) -> None:
        with self._lock:
            self._current = None
            self._checked_at = None
            self._last_valid = None
            self._last_valid_at = None

    def _activate(self, new: RuntimeAIConfig) -> None:
        """New valid version on this node: fresh breakers for its keys."""
        breaker = self.breaker or LLM_CIRCUIT_BREAKER
        old_keys = self._last_valid.breaker_keys() if self._last_valid else set()
        breaker.on_config_activated(new.breaker_keys(), old_keys)

    def get(self, db: Session) -> RuntimeAIConfig:
        now = self.clock()
        with self._lock:
            if (
                self._current is not None and self._checked_at is not None
                and now - self._checked_at < self.ttl
            ):
                return self._current.with_source(
                    ConfigSource.CACHE if self._current.status is ConfigStatus.OK
                    else self._current.source
                )
            cached = self._current
        try:
            version = self.prober(db)
            if (
                cached is not None and cached.status is ConfigStatus.OK
                and version is not None and version == cached.version_id
            ):
                result = cached.with_source(ConfigSource.CACHE)
            else:
                result = self.loader(db)
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
            logger.exception("AI config yüklenemedi")
            with self._lock:
                if (
                    self._last_valid is not None and self._last_valid_at is not None
                    and now - self._last_valid_at <= self.max_stale
                ):
                    return self._last_valid.with_source(ConfigSource.STALE_CACHE)
            return RuntimeAIConfig(ConfigStatus.CONFIG_UNAVAILABLE, ConfigSource.NONE,
                                   error_code="config_unavailable")
        with self._lock:
            if result.status is ConfigStatus.OK:
                if self._last_valid is None or self._last_valid.version_id != result.version_id:
                    self._activate(result)
                self._last_valid = result
                self._last_valid_at = now
            elif result.status is ConfigStatus.CONFIG_INVALID:
                logger.error("AI config geçersiz (%s); LLM yolu kapalı", result.error_code)
            self._current = result
            self._checked_at = now
        return result


AI_CONFIG_CACHE = AIConfigCache()
