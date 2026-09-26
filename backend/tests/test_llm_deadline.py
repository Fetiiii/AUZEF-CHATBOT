"""Application-level provider deadline (OpenAI-compatible adapter).

Invariant: one logical invocation (attempts + backoff) never outlives
LOGICAL_DEADLINE_SECONDS for its capability, so the backend produces a
controlled result well before nginx's 120 s proxy_read_timeout.
A fake clock drives time; a hanging attempt consumes its whole per-attempt
timeout and raises APITimeoutError, exactly as httpx/the SDK would.
"""
from dataclasses import replace
from types import SimpleNamespace

import httpx
import openai
import pytest

import services.llm_provider as llm_provider
from services.answer_pipeline import _availability_kind
from services.circuit_breaker import CallOutcomeKind
from services.llm_config import (
    DEFAULT_ATTEMPT_TIMEOUT_SECONDS,
    LOGICAL_DEADLINE_SECONDS,
    LLMCapability,
)
from services.llm_provider import OpenAIProvider
from services.llm_types import LLMOutcomeStatus

NGINX_PROXY_READ_TIMEOUT = 120.0
REQUEST = httpx.Request("POST", "https://example.invalid/v1/chat/completions")


class Clock:
    def __init__(self):
        self.t = 1000.0

    def perf_counter(self):
        return self.t


def ok():
    return SimpleNamespace(id="g", model="m", model_extra={}, usage=None,
                           choices=[SimpleNamespace(message=SimpleNamespace(content="{}"), finish_reason="stop")])


class TimedClient:
    """Script items: ("ok", secs) | ("hang",) | ("body", status, secs) | ("http", status, secs)."""

    def __init__(self, clock, script, max_retries=2):
        self.clock, self.script, self.max_retries = clock, list(script), max_retries
        self.attempt_timeouts = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def with_options(self, **_):
        return self

    def create(self, **kwargs):
        timeout = kwargs["timeout"]
        self.attempt_timeouts.append(timeout)
        kind, *rest = self.script.pop(0)
        secs = rest[-1] if rest else float("inf")
        if kind == "hang" or secs > timeout:
            self.clock.t += timeout
            raise openai.APITimeoutError(request=REQUEST)
        self.clock.t += secs
        if kind == "ok":
            return ok()
        if kind == "body":
            return SimpleNamespace(choices=None, model_extra={"error": {"code": rest[0]}})
        raise openai.APIStatusError("x", response=httpx.Response(rest[0], request=REQUEST), body=None)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    clock = Clock()
    monkeypatch.setattr(llm_provider.time, "perf_counter", clock.perf_counter)
    delays = []

    def sleep(seconds):
        delays.append(seconds)
        clock.t += seconds

    monkeypatch.setattr(llm_provider, "_sleep", sleep)
    monkeypatch.setattr(llm_provider, "_jitter", lambda: 0.5)
    return SimpleNamespace(provider=OpenAIProvider(), clock=clock, delays=delays)


def run(env, capability, script, **config_changes):
    env.provider.client = TimedClient(env.clock, script)
    config = replace(env.provider.effective_config(capability), **config_changes)
    start = env.clock.t
    result = env.provider._invoke("s", "u", config)
    elapsed = env.clock.t - start
    # Invariants for every scenario.
    assert elapsed <= LOGICAL_DEADLINE_SECONDS[capability] + 1e-9
    assert elapsed < NGINX_PROXY_READ_TIMEOUT
    assert result.latency_ms == pytest.approx(elapsed * 1000)
    return result, elapsed


SEL, IA = LLMCapability.SELECTOR, LLMCapability.INTENT_ANALYZER


def test_budget_invariant_worst_request_fits_under_nginx():
    worst = LOGICAL_DEADLINE_SECONDS[IA] + 2 * LOGICAL_DEADLINE_SECONDS[SEL]
    assert worst < NGINX_PROXY_READ_TIMEOUT
    for cap in (IA, SEL):
        assert DEFAULT_ATTEMPT_TIMEOUT_SECONDS[cap] < LOGICAL_DEADLINE_SECONDS[cap]


def test_normal_success(env):
    r, t = run(env, SEL, [("ok", 1.4)])
    assert r.status is LLMOutcomeStatus.SUCCESS and r.metadata.retry_count == 0 and t == pytest.approx(1.4)
    assert env.provider.client.attempt_timeouts == [DEFAULT_ATTEMPT_TIMEOUT_SECONDS[SEL]]


def test_selector_hang_is_cut_at_the_logical_deadline(env):
    r, t = run(env, SEL, [("hang",), ("hang",), ("hang",)])
    assert r.status is LLMOutcomeStatus.TIMEOUT and r.failure_category == "TIMEOUT"
    assert t == pytest.approx(LOGICAL_DEADLINE_SECONDS[SEL])
    assert r.metadata.retry_count == 1        # 10 s + backoff + remaining ~4.6 s
    assert _availability_kind(r.status) is CallOutcomeKind.FAILURE


def test_timeout_then_retry_success(env):
    r, t = run(env, SEL, [("hang",), ("ok", 1.5)])
    assert r.status is LLMOutcomeStatus.SUCCESS and r.metadata.retry_count == 1
    assert t == pytest.approx(10 + 0.4375 + 1.5)


def test_analyzer_timeout_retry_timeout(env):
    r, t = run(env, IA, [("hang",), ("hang",), ("hang",)])
    assert r.status is LLMOutcomeStatus.TIMEOUT
    assert t == pytest.approx(LOGICAL_DEADLINE_SECONDS[IA])
    timeouts = env.provider.client.attempt_timeouts
    assert timeouts[0] == 20 and timeouts[1] == pytest.approx(30 - 20 - 0.4375)


def test_body429_backoff_success_within_deadline(env):
    r, t = run(env, SEL, [("body", 429, 0.3), ("ok", 1.5)])
    assert r.status is LLMOutcomeStatus.SUCCESS and r.metadata.retry_count == 1
    assert t < LOGICAL_DEADLINE_SECONDS[SEL]


def test_body429_backoff_deadline_exhausted(env):
    # Slow 429s: the second backoff would not leave room for another attempt.
    r, t = run(env, SEL, [("body", 429, 7.0), ("body", 429, 7.0), ("ok", 1.0)])
    assert r.status is LLMOutcomeStatus.MODEL_ERROR and r.failure_category == "RATE_LIMIT"
    assert r.metadata.retry_count == 1 and t < LOGICAL_DEADLINE_SECONDS[SEL]


def test_5xx_retry_success(env):
    r, _ = run(env, SEL, [("http", 503, 0.2), ("ok", 1.0)])
    assert r.status is LLMOutcomeStatus.SUCCESS and r.metadata.retry_count == 1


def test_slow_success_just_below_attempt_timeout(env):
    r, t = run(env, SEL, [("ok", 9.9)])
    assert r.status is LLMOutcomeStatus.SUCCESS and t == pytest.approx(9.9)


def test_slow_success_just_above_attempt_timeout(env):
    r, t = run(env, SEL, [("ok", 10.5), ("ok", 10.5)])
    assert r.status is LLMOutcomeStatus.TIMEOUT
    assert t == pytest.approx(LOGICAL_DEADLINE_SECONDS[SEL])


def test_combined_429_backoff_then_hang_respects_deadline(env):
    r, t = run(env, IA, [("body", 429, 0.4), ("hang",), ("hang",)])
    assert r.status is LLMOutcomeStatus.TIMEOUT
    assert t == pytest.approx(LOGICAL_DEADLINE_SECONDS[IA])
    # 429 (0.4 s) + backoff, then each hanging attempt only gets what is left.
    timeouts = env.provider.client.attempt_timeouts
    assert timeouts[0] == 20 and timeouts[1] == 20
    assert timeouts[2] == pytest.approx(30 - 0.4 - 0.4375 - 20 - 0.875)


def test_explicit_timeout_seconds_is_per_attempt_and_capped_by_deadline(env):
    r, t = run(env, SEL, [("hang",), ("hang",)], timeout_seconds=40.0)
    assert env.provider.client.attempt_timeouts == [LOGICAL_DEADLINE_SECONDS[SEL]]
    assert r.status is LLMOutcomeStatus.TIMEOUT and t == pytest.approx(LOGICAL_DEADLINE_SECONDS[SEL])


def test_non_retryable_http_error_returns_immediately(env):
    r, t = run(env, SEL, [("http", 400, 0.2), ("ok", 1.0)])
    assert r.status is LLMOutcomeStatus.MODEL_ERROR and r.metadata.retry_count == 0 and t == pytest.approx(0.2)
