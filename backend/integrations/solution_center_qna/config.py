"""Environment-backed configuration for the inbound integration API."""

from __future__ import annotations

import os
from dataclasses import dataclass


INTEGRATION_API_KEY_ENV = "INTEGRATION_API_KEY"


@dataclass(frozen=True)
class IntegrationAPIConfig:
    api_key: str

    @classmethod
    def from_env(cls) -> IntegrationAPIConfig:
        return cls(api_key=os.getenv(INTEGRATION_API_KEY_ENV, ""))


def get_integration_config() -> IntegrationAPIConfig:
    """Resolve per request so secret rotation does not require module reload."""
    return IntegrationAPIConfig.from_env()
