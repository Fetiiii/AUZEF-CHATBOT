"""TEST-ONLY analyzer override: inert by default, analyzer-only when gated."""
from services.llm_config import LLMCapability, ReasoningEffort, resolve_llm_config_set
from services.test_overrides import apply_test_analyzer_override, openrouter_base_url


def _configs():
    return resolve_llm_config_set("openrouter", model_override="openai/gpt-4o-mini")


def test_no_env_is_identity():
    configs = _configs()
    assert apply_test_analyzer_override(configs, {}) is configs


def test_override_without_gate_is_identity():
    configs = _configs()
    env = {"AUZEF_TEST_ANALYZER_MODEL": "openai/gpt-6-luna"}
    assert apply_test_analyzer_override(configs, env) is configs
    env["AUZEF_LOAD_METRICS"] = "0"
    assert apply_test_analyzer_override(configs, env) is configs


def test_gated_override_changes_only_the_analyzer():
    configs = _configs()
    env = {"AUZEF_TEST_ANALYZER_MODEL": "openai/gpt-6-luna",
           "AUZEF_TEST_ANALYZER_REASONING": "none", "AUZEF_LOAD_METRICS": "1"}
    out = apply_test_analyzer_override(configs, env)
    analyzer = out.for_capability(LLMCapability.INTENT_ANALYZER)
    assert analyzer.model == "openai/gpt-6-luna"
    assert analyzer.reasoning_effort is ReasoningEffort.NONE
    assert out.for_capability(LLMCapability.SELECTOR) == configs.for_capability(LLMCapability.SELECTOR)
    assert analyzer.max_tokens == configs.intent_analyzer.max_tokens
    assert out.fingerprint != configs.fingerprint


def test_non_openrouter_analyzer_is_untouched():
    configs = resolve_llm_config_set("openai")
    env = {"AUZEF_TEST_ANALYZER_MODEL": "openai/gpt-6-luna", "AUZEF_LOAD_METRICS": "1"}
    assert apply_test_analyzer_override(configs, env) is configs


DEFAULT_URL = "https://openrouter.ai/api/v1"


def test_base_url_override_is_inert_by_default_and_without_gate():
    assert openrouter_base_url(DEFAULT_URL, {}) == DEFAULT_URL
    env = {"AUZEF_TEST_OPENROUTER_BASE_URL": "http://127.0.0.1:18080/api/v1"}
    assert openrouter_base_url(DEFAULT_URL, env) == DEFAULT_URL


def test_base_url_override_accepts_only_loopback_proxy():
    env = {"AUZEF_TEST_OPENROUTER_BASE_URL": "http://127.0.0.1:18080/api/v1", "AUZEF_LOAD_METRICS": "1"}
    assert openrouter_base_url(DEFAULT_URL, env) == "http://127.0.0.1:18080/api/v1"
    env["AUZEF_TEST_OPENROUTER_BASE_URL"] = "https://evil.example/api/v1"
    assert openrouter_base_url(DEFAULT_URL, env) == DEFAULT_URL
