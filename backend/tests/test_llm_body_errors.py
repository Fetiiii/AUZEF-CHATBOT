"""Body-level provider errors (HTTP 200 + {"error": {...}}, choices=null).

Regression for the OpenRouter upstream rate limit observed in the Luna
qualification: the adapter must classify it like an HTTP error, retry it with
the SDK's own budget/backoff, and hand the circuit breaker exactly one logical
outcome per invocation.
"""
from dataclasses import replace
from types import SimpleNamespace

import httpx
import openai
import pytest

import services.llm_provider as llm_provider
from services.answer_pipeline import _availability_kind
from services.circuit_breaker import BreakerConfig, CircuitBreaker, CircuitState
from services.llm_config import LLMCapability
from services.llm_provider import OpenAIProvider, ProviderBodyError, _failure_category
from services.llm_types import LLMOutcomeStatus

OK = "ok"


def body_error(code, message="upstream rate-limited, secret-ish detail"):
    return SimpleNamespace(choices=None, model_extra={"error": {"code": code, "message": message}},
                           usage=None, id=None, model=None)


def ok_response():
    return SimpleNamespace(
        id="gen-1", model="openai/gpt-6-luna", model_extra={},
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=3),
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"x":1}'), finish_reason="stop")],
    )


class ScriptedClient:
    """Returns / raises the scripted items in order; counts physical attempts."""

    def __init__(self, script, max_retries=2):
        self.script = list(script)
        self.max_retries = max_retries
        self.calls = 0
        self.options = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def with_options(self, **kwargs):
        # Like the SDK: options apply to the returned client, never mutate
        # the original's retry budget.
        self.options.append(kwargs)
        return self

    def create(self, **kwargs):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        if item == OK:
            return ok_response()
        return body_error(item) if isinstance(item, int) else item


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    delays = []
    monkeypatch.setattr(llm_provider, "_sleep", delays.append)
    monkeypatch.setattr(llm_provider, "_jitter", lambda: 0.5)
    p = OpenAIProvider()
    p.delays = delays
    return p


def invoke(p, script, max_retries=None, client_max_retries=2):
    p.client = ScriptedClient(script, max_retries=client_max_retries)
    config = replace(p.effective_config(LLMCapability.INTENT_ANALYZER), max_retries=max_retries)
    return p._invoke("s", "u", config)


# --- classification + retry ------------------------------------------------

def test_normal_success_has_zero_retries(provider):
    r = invoke(provider, [OK])
    assert r.status is LLMOutcomeStatus.SUCCESS
    assert r.metadata.retry_count == 0 and provider.client.calls == 1
    assert r.metadata.attempt_latency_ms is not None and provider.delays == []


def test_body429_then_success_is_one_logical_success(provider):
    r = invoke(provider, [429, OK])
    assert r.status is LLMOutcomeStatus.SUCCESS and r.text == '{"x":1}'
    assert r.metadata.retry_count == 1 and provider.client.calls == 2
    # SDK formula min(0.5*2**n, 8) * (1 - 0.25*U) with U = 0.5
    assert provider.delays == [pytest.approx(0.4375)]


def test_body429_twice_then_success(provider):
    r = invoke(provider, [429, 429, OK])
    assert r.status is LLMOutcomeStatus.SUCCESS
    assert r.metadata.retry_count == 2 and provider.client.calls == 3
    assert provider.delays == [pytest.approx(0.4375), pytest.approx(0.875)]


def test_body429_exhausted_is_rate_limit_after_budget(provider):
    r = invoke(provider, [429, 429, 429])
    assert r.status is LLMOutcomeStatus.MODEL_ERROR
    assert r.failure_category == "RATE_LIMIT"
    assert r.error_type == "ProviderBodyError"
    assert r.metadata.retry_count == 2 and provider.client.calls == 3


def test_budget_comes_from_config_max_retries(provider):
    r = invoke(provider, [429, 429, 429, 429, OK], max_retries=4)
    assert r.status is LLMOutcomeStatus.SUCCESS and r.metadata.retry_count == 4
    # SDK retries are disabled; the adapter owns the budget.
    assert provider.client.options == [{"max_retries": 0}]
    assert provider.delays == [pytest.approx(x) for x in (0.4375, 0.875, 1.75, 3.5)]


def test_zero_budget_means_no_body_retry(provider):
    r = invoke(provider, [429, OK], max_retries=0)
    assert r.failure_category == "RATE_LIMIT" and provider.client.calls == 1


def test_unset_config_uses_client_sdk_budget(provider):
    r = invoke(provider, [429, 429, 429, 429, OK], client_max_retries=3)
    assert r.failure_category == "RATE_LIMIT" and r.metadata.retry_count == 3


def test_backoff_is_capped_at_sdk_max_delay(provider):
    invoke(provider, [429] * 7 + [OK], max_retries=6)
    assert max(provider.delays) == pytest.approx(8.0 * 0.875)


def test_body5xx_is_retried_and_classified_provider_5xx(provider):
    assert invoke(provider, [503, OK]).status is LLMOutcomeStatus.SUCCESS
    r = invoke(provider, [502, 502, 502])
    assert r.failure_category == "PROVIDER_5XX" and r.metadata.retry_count == 2


def test_body_non_retryable_error_is_not_retried(provider):
    r = invoke(provider, [400, OK])
    assert r.status is LLMOutcomeStatus.MODEL_ERROR and provider.client.calls == 1
    assert r.failure_category == "UNKNOWN"
    r = invoke(provider, [401, OK])
    assert r.failure_category == "AUTH" and provider.client.calls == 1


def test_unknown_provider_error_code_is_not_retried(provider):
    r = invoke(provider, [SimpleNamespace(choices=None, model_extra={"error": {"code": "provider_overloaded"}}), OK])
    assert r.status is LLMOutcomeStatus.MODEL_ERROR and r.failure_category == "UNKNOWN"
    assert r.metadata.retry_count == 0 and provider.delays == []


@pytest.mark.parametrize("extra", [None, {}, {"error": None}, {"error": "text"}, {"error": {"code": True}}])
def test_malformed_or_missing_error_body(provider, extra):
    r = invoke(provider, [SimpleNamespace(choices=None, model_extra=extra), OK])
    assert r.status is LLMOutcomeStatus.MODEL_ERROR
    assert r.failure_category == "UNKNOWN" and r.error_type == "ProviderBodyError"
    assert provider.delays == [] and provider.client.calls == 1


def test_http_429_is_retried_by_the_adapter_like_the_sdk(provider):
    request = httpx.Request("POST", "https://example.invalid/v1/chat/completions")
    error = lambda: openai.RateLimitError("rate limited", response=httpx.Response(429, request=request), body=None)
    r = invoke(provider, [error(), OK])
    assert r.status is LLMOutcomeStatus.SUCCESS and r.metadata.retry_count == 1
    r = invoke(provider, [error(), error(), error()])
    assert r.failure_category == "RATE_LIMIT" and r.error_type == "RateLimitError"
    assert provider.client.calls == 3


def test_http_retry_after_header_is_honoured(provider):
    request = httpx.Request("POST", "https://example.invalid/v1/chat/completions")
    response = httpx.Response(429, request=request, headers={"retry-after": "2"})
    r = invoke(provider, [openai.RateLimitError("rl", response=response, body=None), OK])
    assert r.status is LLMOutcomeStatus.SUCCESS and provider.delays == [pytest.approx(2.0)]


def test_x_should_retry_false_is_not_retried(provider):
    request = httpx.Request("POST", "https://example.invalid/v1/chat/completions")
    response = httpx.Response(503, request=request, headers={"x-should-retry": "false"})
    r = invoke(provider, [openai.InternalServerError("x", response=response, body=None), OK])
    assert r.failure_category == "PROVIDER_5XX" and provider.client.calls == 1


def test_provider_message_is_never_retained(provider):
    r = invoke(provider, [429, 429, 429])
    assert "secret-ish" not in repr(r)
    assert _failure_category(ProviderBodyError(None)) == "UNKNOWN"


# --- logical invocation x circuit breaker ----------------------------------

def run_logical_calls(provider, script, n_calls, step_seconds=1.0):
    """Real breaker + real availability mapping, one record per logical call."""
    clock = {"t": 0.0}
    breaker = CircuitBreaker(config=BreakerConfig(failure_threshold=3, cooldown_seconds=60), clock=lambda: clock["t"])
    provider.client = ScriptedClient(script)
    config = provider.effective_config(LLMCapability.INTENT_ANALYZER)
    rows = []
    for _ in range(n_calls):
        permit = breaker.acquire("k")
        if not permit.allowed:
            rows.append(("skipped", breaker.snapshot("k")[0], None))
        else:
            r = provider._invoke("s", "u", config)
            record = breaker.record(permit, _availability_kind(r.status))
            rows.append((r.status.value, record.state_after, record.transition))
        clock["t"] += step_seconds
    return rows, breaker


def test_circuit_sees_one_success_for_429_429_success(provider):
    rows, breaker = run_logical_calls(provider, [429, 429, OK], 1)
    assert rows == [("success", CircuitState.CLOSED, None)]
    assert breaker.snapshot("k") == (CircuitState.CLOSED, 0)


def test_circuit_sees_one_success_for_429_success(provider):
    rows, breaker = run_logical_calls(provider, [429, OK], 1)
    assert rows[0][0] == "success" and breaker.snapshot("k") == (CircuitState.CLOSED, 0)


def test_continuous_429_opens_after_three_logical_failures(provider):
    rows, breaker = run_logical_calls(provider, [429] * 9 + [OK] * 3, 4)
    assert [r[0] for r in rows] == ["model_error", "model_error", "model_error", "skipped"]
    assert rows[2][2] == "opened"
    assert provider.client.calls == 9          # 3 logical calls x (1 + 2 retries)


def test_success_resets_consecutive_logical_failures(provider):
    rows, breaker = run_logical_calls(provider, [429] * 6 + [OK] + [429] * 6, 5)
    assert [r[0] for r in rows] == ["model_error", "model_error", "success", "model_error", "model_error"]
    assert all(state is CircuitState.CLOSED for _, state, _ in rows)
    assert breaker.snapshot("k") == (CircuitState.CLOSED, 2)


def test_open_then_half_open_probe_success_recovers(provider):
    rows, breaker = run_logical_calls(provider, [429] * 9 + [OK] * 3, 7, step_seconds=25.0)
    assert [r[0] for r in rows] == ["model_error"] * 3 + ["skipped", "skipped", "success", "success"]
    assert rows[5][2] == "recovered" and breaker.snapshot("k") == (CircuitState.CLOSED, 0)


def test_analyzer_body429_then_success_keeps_real_analysis(provider):
    analysis = ('{"intent_count":1,"intents":[{"source_text":"Harç ne kadar?",'
                '"normalized_text":"Harç ne kadar?","resolved_text":"Harç ne kadar?",'
                '"context_used":false,"calendar_relevant":false}]}')
    response = SimpleNamespace(
        id="g", model="m", model_extra={}, usage=None,
        choices=[SimpleNamespace(message=SimpleNamespace(content=analysis), finish_reason="stop")])
    provider.client = ScriptedClient([429, response])
    result = provider.analyze_intents_with_result("Harç ne kadar?")
    assert result.status is LLMOutcomeStatus.SUCCESS and not result.fallback_to_single
    assert result.invocation.metadata.retry_count == 1
