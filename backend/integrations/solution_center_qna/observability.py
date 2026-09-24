"""Secret-safe request logging for the integration namespace."""

from __future__ import annotations

import logging
import time

from fastapi import Request


logger = logging.getLogger("auzef")
PATH_PREFIX = "/api/integrations/v1"


async def integration_request_logging(request: Request, call_next):
    if not request.url.path.startswith(PATH_PREFIX):
        return await call_next(request)
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        duration_ms = (time.perf_counter() - started) * 1000
        logger.exception(
            "Integration request endpoint=%s status_code=500 duration_ms=%.2f "
            "client=cozum_merkezi",
            request.url.path,
            duration_ms,
        )
        raise
    duration_ms = (time.perf_counter() - started) * 1000
    logger.info(
        "Integration request endpoint=%s status_code=%d duration_ms=%.2f "
        "client=cozum_merkezi",
        request.url.path,
        response.status_code,
        duration_ms,
    )
    return response
