"""Phase 1 capability-config foundation contracts."""
from dataclasses import replace

import pytest

from services.llm_config import (
    LLMCapability,
    ReasoningEffort,
    resolve_capability_config,
    resolve_llm_config_set,
)


@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("openai", "gpt-4o-mini"),
        ("openrouter", "openai/gpt-4o-mini"),
        ("gemini", "gemini-2.5-flash-lite"),
    ],
)
def test_phase0_provider_model_defaults_are_preserved(provider, model):
    configs = resolve_llm_config_set(provider, environ={})
    assert configs.intent_analyzer.model == model
    assert configs.selector.model == model
    assert configs.intent_analyzer.provider == provider
    assert configs.selector.provider == provider


def test_capability_generation_defaults_are_preserved():
    configs = resolve_llm_config_set("openrouter", environ={})
    assert configs.intent_analyzer.max_tokens == 300
    assert configs.selector.max_tokens == 5
    assert configs.intent_analyzer.temperature == 0
    assert configs.selector.temperature == 0
    assert configs.selector.reasoning_effort is None
    assert configs.selector.timeout_seconds is None
    assert configs.selector.max_retries is None
    assert configs.selector.structured_output_enabled is False


def test_capability_overrides_are_independent():
    configs = resolve_llm_config_set(
        "openrouter",
        environ={
            "LLM_SELECTOR_MODEL": "future-selector",
            "LLM_SELECTOR_MAX_TOKENS": "9",
            "LLM_SELECTOR_REASONING_EFFORT": "high",
            "LLM_SELECTOR_TIMEOUT_SECONDS": "12.5",
            "LLM_SELECTOR_MAX_RETRIES": "3",
            "LLM_SELECTOR_STRUCTURED_OUTPUT_ENABLED": "true",
        },
    )
    assert configs.selector.model == "future-selector"
    assert configs.selector.max_tokens == 9
    assert configs.selector.reasoning_effort is ReasoningEffort.HIGH
    assert configs.selector.timeout_seconds == 12.5
    assert configs.selector.max_retries == 3
    assert configs.selector.structured_output_enabled is True
    assert configs.intent_analyzer.model == "openai/gpt-4o-mini"
    assert configs.intent_analyzer.max_tokens == 300


def test_config_fingerprint_is_deterministic_and_sensitive():
    first = resolve_llm_config_set("openai", environ={})
    second = resolve_llm_config_set("openai", environ={})
    assert first.fingerprint == second.fingerprint
    changed = replace(first.selector, max_tokens=6)
    assert first.selector.fingerprint != changed.fingerprint


def test_fingerprint_snapshot_contains_no_api_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "do-not-leak-this-key")
    configs = resolve_llm_config_set("openai", environ={})
    assert "do-not-leak-this-key" not in str(configs.to_dict())
    assert "do-not-leak-this-key" not in configs.fingerprint


def test_legacy_provider_env_is_still_resolved():
    config = resolve_capability_config(
        LLMCapability.SELECTOR,
        environ={"LLM_PROVIDER": "gemini"},
    )
    assert config.provider == "gemini"
    assert config.model == "gemini-2.5-flash-lite"


def test_invalid_reasoning_effort_is_rejected_explicitly():
    with pytest.raises(RuntimeError, match="REASONING_EFFORT"):
        resolve_llm_config_set(
            "openai", environ={"LLM_SELECTOR_REASONING_EFFORT": "turbo"}
        )
