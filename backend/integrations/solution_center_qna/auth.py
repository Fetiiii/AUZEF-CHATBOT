"""API-key authentication independent from the admin session system."""

from __future__ import annotations

import hmac

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader

from .config import IntegrationAPIConfig, get_integration_config


_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def verify_integration_api_key(
    provided_key: str | None = Security(_api_key_header),
    config: IntegrationAPIConfig = Depends(get_integration_config),
) -> None:
    """Reject missing/wrong keys without coupling to JWT or admin sessions."""
    if not config.api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Integration API is not configured.",
        )
    if provided_key is None or not hmac.compare_digest(provided_key, config.api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key.",
            headers={"WWW-Authenticate": "ApiKey"},
        )
