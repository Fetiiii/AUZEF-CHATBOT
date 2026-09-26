"""Capability-based LLM configuration with V1-compatible defaults."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from enum import Enum
from hashlib import new as new_hash
from typing import Mapping, Optional


class LLMCapability(str, Enum):
    INTENT_ANALYZER = "intent_analyzer"
    SELECTOR = "selector"


class ReasoningEffort(str, Enum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ReasoningTransportError(ValueError):
    """A reasoning_effort was configured that the provider adapter cannot send.

    Raised before any outbound request: a labeled-but-untransmitted
    reasoning level must never be silently ignored.
    """


# Adapter-level transport only: which provider request protocols can carry a
# reasoning level. Whether a *model* accepts it is registry data
# (supports_reasoning_effort / allowed_reasoning_efforts), never guessed here.
# OpenAI Chat Completions: top-level ``reasoning_effort``; OpenRouter: unified
# ``reasoning.effort`` request field. Gemini (thinking_budget/thinking_level)
# has no 1:1 low/medium/high mapping, so it is intentionally unsupported.
REASONING_TRANSPORT = {
    "openai": frozenset({"low", "medium", "high"}),
    # "none" is sent explicitly as reasoning.effort="none" (OpenRouter
    # unified reasoning); unset (None) still sends no reasoning field.
    "openrouter": frozenset({"none", "low", "medium", "high"}),
}


def reasoning_transport_supported(provider: str, effort) -> bool:
    if effort is None:
        return True
    value = getattr(effort, "value", effort)
    return value in REASONING_TRANSPORT.get(provider, frozenset())


def reasoning_request_fields(provider: str, effort) -> dict:
    """Provider request fields for a reasoning level ({} when unset)."""
    if effort is None:
        return {}
    value = getattr(effort, "value", effort)
    if not reasoning_transport_supported(provider, value):
        raise ReasoningTransportError(
            f"reasoning_effort={value!r} cannot be transmitted by the {provider!r} adapter"
        )
    if provider == "openai":
        return {"reasoning_effort": value}
    return {"extra_body": {"reasoning": {"effort": value}}}


_DEFAULT_MODELS = {
    "openai": "gpt-4o-mini",
    "openrouter": "openai/gpt-4o-mini",
    "gemini": "gemini-2.5-flash-lite",
}
_DEFAULT_MAX_TOKENS = {
    LLMCapability.INTENT_ANALYZER: 300,
    # Selector V2 returns strict JSON such as
    # {"decision":"SELECT","candidate_ref":"calendar:12345"} (~15-20 tokens);
    # the V1 numeric contract fit in 5. 32 is the minimum with safe headroom.
    LLMCapability.SELECTOR: 32,
}

# Provider-call time budget (application level). The upstream proxy cuts the
# widget request at 120 s (nginx proxy_read_timeout); an unset timeout used to
# fall back to the SDK default (600 s read, retried), which let one hung
# selector call outlive the request. Invariant enforced by the adapter:
#   logical invocation deadline < request budget < nginx 120 s
# Worst request = analyzer deadline + 2 selector intents * selector deadline
# = 30 + 2*15 = 60 s, leaving room for retrieval, degraded path and DB writes.
# Values come from observed latencies (2026-09 load artifacts): analyzer p99
# ~6 s / max 17.6 s (long inputs), selector p99 ~2.6 s / max 8.3 s.
# A managed/env ``timeout_seconds`` still sets the per-attempt timeout; the
# logical deadline always caps the whole invocation (attempts + backoff).
DEFAULT_ATTEMPT_TIMEOUT_SECONDS = {
    LLMCapability.INTENT_ANALYZER: 20.0,
    LLMCapability.SELECTOR: 10.0,
}
LOGICAL_DEADLINE_SECONDS = {
    LLMCapability.INTENT_ANALYZER: 30.0,
    LLMCapability.SELECTOR: 15.0,
}


def default_model(provider: str) -> str:
    try:
        return _DEFAULT_MODELS[provider]
    except KeyError:
        raise ValueError(f"Unsupported provider: {provider}") from None


@dataclass(frozen=True)
class EffectiveLLMConfig:
    capability: LLMCapability
    provider: str
    model: str
    reasoning_effort: Optional[ReasoningEffort] = None
    temperature: float = 0.0
    max_tokens: int = 5
    timeout_seconds: Optional[float] = None
    max_retries: Optional[int] = None
    structured_output_enabled: bool = False

    def to_dict(self) -> dict:
        result = asdict(self)
        result["capability"] = self.capability.value
        result["reasoning_effort"] = (
            self.reasoning_effort.value if self.reasoning_effort is not None else None
        )
        return result

    @property
    def fingerprint(self) -> str:
        serialized = json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        return new_hash("sha256", serialized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EffectiveLLMConfigSet:
    intent_analyzer: EffectiveLLMConfig
    selector: EffectiveLLMConfig

    def for_capability(self, capability: LLMCapability) -> EffectiveLLMConfig:
        if capability is LLMCapability.INTENT_ANALYZER:
            return self.intent_analyzer
        if capability is LLMCapability.SELECTOR:
            return self.selector
        raise ValueError(f"Unsupported LLM capability: {capability!r}")

    def to_dict(self) -> dict:
        return {
            "intent_analyzer": self.intent_analyzer.to_dict(),
            "selector": self.selector.to_dict(),
        }

    @property
    def fingerprint(self) -> str:
        serialized = json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        return new_hash("sha256", serialized.encode("utf-8")).hexdigest()


def _optional_float(value: Optional[str], name: str) -> Optional[float]:
    if value is None or not value.strip():
        return None
    try:
        parsed = float(value)
    except ValueError:
        raise RuntimeError(f"{name} sayı olmalı, alınan: {value!r}") from None
    if parsed <= 0:
        raise RuntimeError(f"{name} pozitif olmalı, alınan: {parsed}")
    return parsed


def _optional_nonnegative_int(value: Optional[str], name: str) -> Optional[int]:
    if value is None or not value.strip():
        return None
    try:
        parsed = int(value)
    except ValueError:
        raise RuntimeError(f"{name} negatif olmayan tam sayı olmalı, alınan: {value!r}") from None
    if parsed < 0:
        raise RuntimeError(f"{name} negatif olmayan tam sayı olmalı, alınan: {parsed}")
    return parsed


def _bool(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _reasoning_effort(value: Optional[str], name: str) -> Optional[ReasoningEffort]:
    if value is None or not value.strip():
        return None
    try:
        return ReasoningEffort(value.strip().lower())
    except ValueError:
        allowed = ", ".join(item.value for item in ReasoningEffort)
        raise RuntimeError(f"{name} değeri şunlardan biri olmalı: {allowed}") from None


def resolve_capability_config(
    capability: LLMCapability,
    *,
    provider: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
    model_override: Optional[str] = None,
) -> EffectiveLLMConfig:
    """Resolve one effective config without changing Phase 0 defaults."""
    env = os.environ if environ is None else environ
    prefix = f"LLM_{capability.value.upper()}"
    resolved_provider = (
        provider or env.get(f"{prefix}_PROVIDER") or env.get("LLM_PROVIDER") or ""
    ).strip().lower()
    if not resolved_provider:
        raise ValueError("LLM provider is not configured")
    if resolved_provider not in _DEFAULT_MODELS:
        raise ValueError(f"Unsupported provider: {resolved_provider}")
    model = (
        env.get(f"{prefix}_MODEL") or model_override or default_model(resolved_provider)
    ).strip()
    temperature_name = f"{prefix}_TEMPERATURE"
    max_tokens_name = f"{prefix}_MAX_TOKENS"
    try:
        temperature = float(env.get(temperature_name, "0"))
        max_tokens = int(env.get(max_tokens_name, str(_DEFAULT_MAX_TOKENS[capability])))
    except ValueError as exc:
        raise RuntimeError(f"Geçersiz LLM capability config: {exc}") from None
    if max_tokens < 1:
        raise RuntimeError(f"{max_tokens_name} pozitif tam sayı olmalı")
    return EffectiveLLMConfig(
        capability=capability,
        provider=resolved_provider,
        model=model,
        reasoning_effort=_reasoning_effort(
            env.get(f"{prefix}_REASONING_EFFORT"), f"{prefix}_REASONING_EFFORT"
        ),
        temperature=temperature,
        max_tokens=max_tokens,
        timeout_seconds=_optional_float(
            env.get(f"{prefix}_TIMEOUT_SECONDS"), f"{prefix}_TIMEOUT_SECONDS"
        ),
        max_retries=_optional_nonnegative_int(
            env.get(f"{prefix}_MAX_RETRIES"), f"{prefix}_MAX_RETRIES"
        ),
        structured_output_enabled=_bool(
            env.get(f"{prefix}_STRUCTURED_OUTPUT_ENABLED")
        ),
    )


def resolve_llm_config_set(
    provider: str,
    *,
    environ: Optional[Mapping[str, str]] = None,
    model_override: Optional[str] = None,
) -> EffectiveLLMConfigSet:
    return EffectiveLLMConfigSet(
        intent_analyzer=resolve_capability_config(
            LLMCapability.INTENT_ANALYZER,
            provider=provider,
            environ=environ,
            model_override=model_override,
        ),
        selector=resolve_capability_config(
            LLMCapability.SELECTOR,
            provider=provider,
            environ=environ,
            model_override=model_override,
        ),
    )
