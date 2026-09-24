"""Opt-in, content-free measurements for controlled load tests.

Disabled unless AUZEF_LOAD_METRICS=1. No request body, query, answer, token,
client IP or credential is emitted. This module never changes routing/results.
"""

from __future__ import annotations

from contextvars import ContextVar
import json
import logging
import os
import threading
import time
import uuid


ENABLED = os.getenv("AUZEF_LOAD_METRICS") == "1"
_request_id: ContextVar[str | None] = ContextVar("load_request_id", default=None)
_lock = threading.Lock()
_counts = {"chat": 0, "embedding": 0, "meili": 0, "qdrant": 0}
_logger = logging.getLogger("auzef.load_metrics")


def _emit(event: str, **fields) -> None:
    if not ENABLED:
        return
    payload = {
        "event": event,
        "time_unix_ns": time.time_ns(),
        "pid": os.getpid(),
        "request_id": _request_id.get(),
        **fields,
    }
    try:
        _logger.info("AUZEF_LOAD_METRIC %s", json.dumps(payload, separators=(",", ":")))
    except Exception:
        # Measurement must never alter the answer path.
        pass


def event(name: str, **fields) -> None:
    _emit(name, **fields)


class Timer:
    def __init__(self, phase: str):
        self.phase = phase
        self.started = 0

    def __enter__(self):
        if ENABLED:
            with _lock:
                _counts[self.phase] = _counts.get(self.phase, 0) + 1
                active = _counts[self.phase]
            self.started = time.perf_counter_ns()
            _emit("phase_start", phase=self.phase, active=active)
        return self

    def __exit__(self, exc_type, exc, tb):
        if ENABLED:
            duration = (time.perf_counter_ns() - self.started) / 1_000_000
            with _lock:
                _counts[self.phase] -= 1
                active = _counts[self.phase]
            _emit("phase_end", phase=self.phase, duration_ms=round(duration, 3), active=active,
                  error_type=exc_type.__name__ if exc_type else None)
        return False


def chat_started() -> None:
    if ENABLED:
        with _lock:
            _counts["chat"] += 1
            active = _counts["chat"]
        _emit("handler_start", phase="chat", active=active)
        pool_snapshot()


def pool_snapshot() -> None:
    """Instantaneous process-local pool occupancy; checkout wait is not inferred."""
    if not ENABLED:
        return
    try:
        from core.database import admin_engine, chat_engine
        for name, engine in (("admin", admin_engine), ("chat", chat_engine)):
            pool = engine.pool
            _emit("db_pool", pool=name, size=pool.size(), checked_out=pool.checkedout(),
                  overflow=pool.overflow())
    except Exception:
        _emit("db_pool_unavailable")


def threadpool_snapshot(point: str) -> None:
    """Sample AnyIO's current worker token use from the ASGI event loop."""
    if not ENABLED:
        return
    try:
        from anyio.to_thread import current_default_thread_limiter
        limiter = current_default_thread_limiter()
        _emit("threadpool", point=point, borrowed_tokens=limiter.borrowed_tokens,
              total_tokens=limiter.total_tokens)
    except Exception:
        _emit("threadpool_unavailable", point=point)


class LoadMetricsMiddleware:
    """ASGI wrapper; first response header marks handler completion."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if not ENABLED or scope.get("type") != "http" or scope.get("path") != "/widget-chat":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter_ns()
        headers = dict(scope.get("headers", []))
        candidate = headers.get(b"x-auzef-load-request-id", b"").decode("ascii", "ignore")
        request_id = candidate if len(candidate) <= 64 and candidate.isascii() and candidate.replace("-", "").isalnum() else uuid.uuid4().hex
        token = _request_id.set(request_id)
        _emit("request_arrival")
        threadpool_snapshot("arrival")
        response_started = False

        async def measured_send(message):
            nonlocal response_started
            if message.get("type") == "http.response.start" and not response_started:
                response_started = True
                with _lock:
                    if scope.get("state", {}).get("load_handler_started"):
                        _counts["chat"] -= 1
                    active = _counts["chat"]
                _emit("handler_finish", status=message.get("status"),
                      arrival_to_response_ms=round((time.perf_counter_ns() - started) / 1_000_000, 3),
                      active=active)
                threadpool_snapshot("response_start")
                pool_snapshot()
            await send(message)

        try:
            scope.setdefault("state", {})["load_arrival_ns"] = started
            await self.app(scope, receive, measured_send)
        finally:
            if not response_started and scope.get("state", {}).get("load_handler_started"):
                with _lock:
                    _counts["chat"] -= 1
            _request_id.reset(token)
