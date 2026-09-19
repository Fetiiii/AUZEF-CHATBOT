"""Process-local, capability/config-scoped LLM circuit breaker.

Only infrastructure/availability failures (``MODEL_ERROR``, ``TIMEOUT``) are
counted. ``SEMANTIC_NONE``, ``NO_ELIGIBLE_CANDIDATES`` and ``INVALID_OUTPUT``
are never availability failures. One logical invocation (after any SDK-internal
retries) is recorded once.

The breaker is deliberately node-local: no Redis/DB coordination. Each app
process learns provider health from its own requests.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Mapping, Optional

logger = logging.getLogger("auzef")

FAILURE_THRESHOLD_ENV = "LLM_CIRCUIT_BREAKER_FAILURE_THRESHOLD"
COOLDOWN_SECONDS_ENV = "LLM_CIRCUIT_BREAKER_COOLDOWN_SECONDS"
DEFAULT_FAILURE_THRESHOLD = 3
DEFAULT_COOLDOWN_SECONDS = 60.0


class CircuitState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CallOutcomeKind(str, Enum):
    """How one logical LLM invocation affects availability accounting."""

    SUCCESS = "success"          # schema-valid answer (SELECT/NONE/analysis)
    FAILURE = "failure"          # MODEL_ERROR / TIMEOUT
    NEUTRAL = "neutral"          # INVALID_OUTPUT: reachable, contract failed


@dataclass(frozen=True)
class BreakerConfig:
    failure_threshold: int = DEFAULT_FAILURE_THRESHOLD
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS

    @classmethod
    def from_env(cls, environ: Optional[Mapping[str, str]] = None) -> "BreakerConfig":
        env = os.environ if environ is None else environ
        raw_threshold = (env.get(FAILURE_THRESHOLD_ENV) or "").strip()
        raw_cooldown = (env.get(COOLDOWN_SECONDS_ENV) or "").strip()
        threshold = DEFAULT_FAILURE_THRESHOLD
        cooldown = DEFAULT_COOLDOWN_SECONDS
        if raw_threshold:
            try:
                threshold = int(raw_threshold)
            except ValueError:
                raise RuntimeError(
                    f"{FAILURE_THRESHOLD_ENV} pozitif tam sayı olmalı, alınan: {raw_threshold!r}"
                ) from None
            if threshold < 1:
                raise RuntimeError(f"{FAILURE_THRESHOLD_ENV} pozitif tam sayı olmalı")
        if raw_cooldown:
            try:
                cooldown = float(raw_cooldown)
            except ValueError:
                raise RuntimeError(
                    f"{COOLDOWN_SECONDS_ENV} pozitif sayı olmalı, alınan: {raw_cooldown!r}"
                ) from None
            if not cooldown > 0:
                raise RuntimeError(f"{COOLDOWN_SECONDS_ENV} pozitif sayı olmalı")
        return cls(failure_threshold=threshold, cooldown_seconds=cooldown)


@dataclass
class _KeyState:
    state: CircuitState = CircuitState.CLOSED
    consecutive_failures: int = 0
    opened_at: Optional[float] = None
    probe_started_at: Optional[float] = None
    open_count: int = 0
    recovery_count: int = 0


@dataclass(frozen=True)
class BreakerPermit:
    key: str
    allowed: bool
    probe: bool
    state_before: CircuitState
    failures_before: int


@dataclass(frozen=True)
class BreakerRecord:
    state_after: CircuitState
    failures_after: int
    transition: Optional[str] = None   # "opened" | "recovered" | "reopened"


def breaker_key(capability: str, provider: str, model: str, fingerprint: str) -> str:
    """Capability + effective config: a new model/config starts CLOSED."""
    return f"{capability}:{provider}:{model}:{fingerprint[:16]}"


@dataclass
class CircuitBreaker:
    config: BreakerConfig = field(default_factory=BreakerConfig.from_env)
    clock: Callable[[], float] = time.monotonic
    _states: dict = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _state(self, key: str) -> _KeyState:
        state = self._states.get(key)
        if state is None:
            state = self._states[key] = _KeyState()
        return state

    def acquire(self, key: str) -> BreakerPermit:
        """Decide atomically whether this request may call the capability.

        OPEN within cooldown → skip. OPEN after cooldown → exactly one caller
        becomes the HALF_OPEN probe; concurrent callers are skipped. A probe
        that never reports back is re-leased after another cooldown.
        """
        with self._lock:
            now = self.clock()
            st = self._state(key)
            before = (st.state, st.consecutive_failures)
            if st.state is CircuitState.CLOSED:
                return BreakerPermit(key, True, False, *before)
            cooldown = self.config.cooldown_seconds
            if st.state is CircuitState.OPEN:
                if st.opened_at is not None and now - st.opened_at >= cooldown:
                    st.state = CircuitState.HALF_OPEN
                    st.probe_started_at = now
                    return BreakerPermit(key, True, True, *before)
                return BreakerPermit(key, False, False, *before)
            # HALF_OPEN: one probe in flight.
            if st.probe_started_at is not None and now - st.probe_started_at >= cooldown:
                st.probe_started_at = now
                return BreakerPermit(key, True, True, *before)
            return BreakerPermit(key, False, False, *before)

    def record(self, permit: BreakerPermit, kind: CallOutcomeKind) -> BreakerRecord:
        if not permit.allowed:
            raise ValueError("a skipped call cannot be recorded")
        with self._lock:
            now = self.clock()
            st = self._state(permit.key)
            transition = None
            if kind is CallOutcomeKind.FAILURE:
                st.consecutive_failures += 1
                if permit.probe or st.state is CircuitState.HALF_OPEN:
                    st.state = CircuitState.OPEN
                    st.opened_at = now
                    st.probe_started_at = None
                    transition = "reopened"
                elif (
                    st.state is CircuitState.CLOSED
                    and st.consecutive_failures >= self.config.failure_threshold
                ):
                    st.state = CircuitState.OPEN
                    st.opened_at = now
                    st.open_count += 1
                    transition = "opened"
            elif kind is CallOutcomeKind.SUCCESS or permit.probe:
                # A schema-valid answer resets the count. A probe that reached
                # the provider (even with INVALID_OUTPUT) proves availability.
                recovered = st.state is not CircuitState.CLOSED
                st.state = CircuitState.CLOSED
                st.consecutive_failures = 0
                st.opened_at = None
                st.probe_started_at = None
                if recovered:
                    st.recovery_count += 1
                    transition = "recovered"
            # NEUTRAL outside a probe: no availability change at all.
            record = BreakerRecord(st.state, st.consecutive_failures, transition)
        if transition is not None:
            logger.warning(
                "llm_circuit_transition=%s",
                json.dumps(
                    {"key": permit.key, "transition": transition,
                     "state": record.state_after.value},
                    sort_keys=True,
                ),
            )
        return record

    def snapshot(self, key: str) -> tuple[CircuitState, int]:
        with self._lock:
            st = self._states.get(key)
            if st is None:
                return CircuitState.CLOSED, 0
            return st.state, st.consecutive_failures

    def reset_all(self) -> None:
        with self._lock:
            self._states.clear()


LLM_CIRCUIT_BREAKER = CircuitBreaker()


class _AdminModeTracker:
    """Detects the effective LLM OFF → ON transition in this process."""

    def __init__(self):
        self._lock = threading.Lock()
        self._last: Optional[bool] = None

    def observe(self, enabled: bool, breaker: CircuitBreaker) -> bool:
        with self._lock:
            reenabled = self._last is False and enabled
            self._last = enabled
        if reenabled:
            # Manual re-enable is an explicit operator action: start clean.
            breaker.reset_all()
        return reenabled

    def reset(self) -> None:
        with self._lock:
            self._last = None


LLM_ADMIN_MODE_TRACKER = _AdminModeTracker()
