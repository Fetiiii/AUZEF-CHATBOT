import copy
import json
import os
import random
import time
from services import load_metrics
from services.load_metrics import Timer
from abc import ABC, abstractmethod
from dataclasses import replace
from types import SimpleNamespace
from typing import Optional, Sequence
import openai
from openai import OpenAI
from google import genai
from google.genai import types as genai_types
from dotenv import load_dotenv

from services.llm_config import (
    DEFAULT_ATTEMPT_TIMEOUT_SECONDS,
    LOGICAL_DEADLINE_SECONDS,
    EffectiveLLMConfig,
    EffectiveLLMConfigSet,
    LLMCapability,
    default_model,
    reasoning_request_fields,
    resolve_llm_config_set,
)
from services.candidate_eligibility import SelectorCandidate
from services.selector import build_selector_prompt, parse_selector_output
from services.intent_analyzer import (
    build_intent_analyzer_prompt,
    parse_intent_analysis,
    safe_single_intent,
)
from services.llm_types import (
    IntentAnalysis,
    IntentAnalyzerResult,
    LLMInvocationResult,
    LLMOutcomeStatus,
    LLMParseStatus,
    LLMResponseMetadata,
    SelectorResult,
)

load_dotenv()

# Body-level provider errors: some OpenAI-compatible gateways (OpenRouter)
# answer HTTP 200 with a top-level {"error": {"code": 429, ...}} and no
# choices. The SDK only retries on HTTP status, so such a response reached
# ``choices[0]`` as a TypeError (MODEL_ERROR/UNKNOWN, never retried). These
# values mirror the OpenAI SDK's own HTTP retry policy (openai._constants and
# BaseClient._should_retry) so a body-level status behaves like the HTTP one.
_RETRY_INITIAL_DELAY_SECONDS = 0.5
_RETRY_MAX_DELAY_SECONDS = 8.0
_SDK_DEFAULT_MAX_RETRIES = 2
_RETRYABLE_BODY_STATUSES = frozenset({408, 409, 429})
# Indirection points for tests; production uses the real clock/jitter.
_sleep = time.sleep
_jitter = random.random


class ProviderBodyError(Exception):
    """Provider error carried in a 2xx response body instead of the status line.

    ``status_code`` lets the existing ``_failure_category`` classify it exactly
    like an HTTP error (429 → RATE_LIMIT, 5xx → PROVIDER_5XX). The provider's
    message is deliberately not stored: it is never logged or traced.
    """

    def __init__(self, status_code: Optional[int]):
        super().__init__(f"provider body error (status={status_code})")
        self.status_code = status_code


def _body_error_status(response) -> Optional[int]:
    """HTTP-style status from a top-level ``error`` object, if well-formed."""
    extra = getattr(response, "model_extra", None)
    error = extra.get("error") if isinstance(extra, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    if isinstance(code, bool):
        return None
    try:
        status = int(code)
    except (TypeError, ValueError):
        return None
    return status if 100 <= status <= 599 else None


def _is_retryable_body_status(status: Optional[int]) -> bool:
    return status is not None and (status in _RETRYABLE_BODY_STATUSES or status >= 500)


def _retry_delay_seconds(retry_index: int) -> float:
    """SDK formula: min(0.5 * 2**n, 8) * (1 - 0.25 * U)."""
    base = min(_RETRY_INITIAL_DELAY_SECONDS * 2 ** retry_index, _RETRY_MAX_DELAY_SECONDS)
    return base * (1 - 0.25 * _jitter())


# A retry is not started unless at least this much of the logical deadline
# remains for the next attempt.
_MIN_ATTEMPT_SECONDS = 0.5
_MAX_RETRY_AFTER_SECONDS = 60.0     # same ceiling as the SDK


class LogicalDeadlineExceeded(TimeoutError):
    """The whole logical invocation ran out of time (classified TIMEOUT)."""


class AttemptWallClockTimeout(openai.APITimeoutError):
    """One attempt exceeded its wall-clock budget while the body kept arriving.

    httpx's ``timeout`` is an inactivity timeout (reset by every received
    chunk), so an upstream that trickles bytes would never time out. Being an
    APITimeoutError it is classified/retried exactly like an SDK timeout.
    """


def _create_with_wall_clock(client, kwargs: dict, attempt_deadline: float):
    """chat.completions.create with a hard wall-clock bound on reading the body.

    Reads through the SDK's streaming-response interface (same request, same
    error handling for non-2xx) and aborts once ``attempt_deadline`` passes.
    Clients without that interface (test doubles) use the plain call.
    """
    completions = client.chat.completions
    streaming = getattr(completions, "with_streaming_response", None)
    if streaming is None:
        return completions.create(**kwargs)
    with streaming.create(**kwargs) as raw:
        buffer = bytearray()
        for chunk in raw.iter_bytes():
            buffer.extend(chunk)
            if time.perf_counter() > attempt_deadline:
                raise AttemptWallClockTimeout(request=raw.http_request)
    try:
        body = json.loads(bytes(buffer) or b"{}")
    except ValueError:
        raise ProviderBodyError(None) from None
    return _completion_view(body if isinstance(body, dict) else {})


def _completion_view(body: dict):
    """Minimal attribute view of a chat completion body (fields the adapter uses)."""
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else None
    choices = [
        SimpleNamespace(
            message=SimpleNamespace(content=(c.get("message") or {}).get("content")),
            finish_reason=c.get("finish_reason"),
        )
        for c in (body.get("choices") or []) if isinstance(c, dict)
    ]
    return SimpleNamespace(
        id=body.get("id"), model=body.get("model"), choices=choices,
        usage=SimpleNamespace(prompt_tokens=usage.get("prompt_tokens"),
                              completion_tokens=usage.get("completion_tokens")) if usage else None,
        # Same shape the SDK exposes for unknown top-level fields.
        model_extra={k: v for k, v in body.items() if k not in ("id", "model", "choices", "usage")},
    )


def _logical_deadline(config: EffectiveLLMConfig) -> float:
    return LOGICAL_DEADLINE_SECONDS.get(config.capability, max(LOGICAL_DEADLINE_SECONDS.values()))


def _default_attempt_timeout(config: EffectiveLLMConfig) -> float:
    return DEFAULT_ATTEMPT_TIMEOUT_SECONDS.get(
        config.capability, max(DEFAULT_ATTEMPT_TIMEOUT_SECONDS.values())
    )


def _retry_after_seconds(exc: Exception) -> Optional[float]:
    """Retry-After(-ms) from an HTTP error response, SDK-compatible bounds."""
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if not headers:
        return None
    for name, scale in (("retry-after-ms", 0.001), ("retry-after", 1.0)):
        raw = headers.get(name)
        if raw is None:
            continue
        try:
            value = float(raw) * scale
        except (TypeError, ValueError):
            return None
        return value if 0 < value <= _MAX_RETRY_AFTER_SECONDS else None
    return None


def _retry_delay_for(exc: Exception, retry_index: int) -> Optional[float]:
    """Delay before the next attempt, or None when the SDK would not retry.

    Mirrors openai BaseClient._should_retry: timeouts and connection errors,
    x-should-retry, 408/409/429 and 5xx. Body-level errors use their status.
    """
    if isinstance(exc, ProviderBodyError):
        return _retry_delay_seconds(retry_index) if _is_retryable_body_status(exc.status_code) else None
    if isinstance(exc, (openai.APITimeoutError, openai.APIConnectionError)):
        return _retry_delay_seconds(retry_index)
    if isinstance(exc, openai.APIStatusError):
        headers = getattr(exc.response, "headers", None) or {}
        should = headers.get("x-should-retry")
        if should == "false":
            return None
        if should == "true" or _is_retryable_body_status(exc.status_code):
            return _retry_after_seconds(exc) or _retry_delay_seconds(retry_index)
    return None


class BaseLLMProvider(ABC):

    provider_name = "openai"

    @property
    def configs(self) -> EffectiveLLMConfigSet:
        configs = getattr(self, "_configs", None)
        if configs is None:
            model = getattr(self, "model", None)
            configs = resolve_llm_config_set(self.provider_name, model_override=model)
            self._configs = configs
        return configs

    def effective_config(self, capability: LLMCapability) -> EffectiveLLMConfig:
        return self.configs.for_capability(capability)

    def analyze_intents(
        self, message: str, previous_user_turns: list[str] | tuple[str, ...] = ()
    ) -> IntentAnalysis:
        return self.analyze_intents_with_result(message, previous_user_turns).analysis

    def analyze_intents_with_result(
        self, message: str, previous_user_turns: list[str] | tuple[str, ...] = ()
    ) -> IntentAnalyzerResult:
        """Analyze one current turn using strict JSON and a lossless SINGLE fallback."""
        message = message.strip()
        fallback = safe_single_intent(message)
        config = self.effective_config(LLMCapability.INTENT_ANALYZER)
        previous = [text.strip() for text in previous_user_turns if text.strip()][-2:]
        system, user = build_intent_analyzer_prompt(message, previous)
        invocation = self._invoke(system, user, config)
        if invocation.status is not LLMOutcomeStatus.SUCCESS:
            return IntentAnalyzerResult(
                analysis=fallback,
                status=invocation.status,
                parse_status=LLMParseStatus.FALLBACK,
                fallback_to_single=True,
                invocation=invocation,
            )
        try:
            analysis = parse_intent_analysis(
                invocation.text or "",
                current_user_turn=message,
                previous_user_turns=previous,
            )
        except ValueError:
            return IntentAnalyzerResult(
                analysis=fallback,
                status=LLMOutcomeStatus.INVALID_OUTPUT,
                parse_status=LLMParseStatus.INVALID_OUTPUT,
                fallback_to_single=True,
                invocation=invocation,
            )
        return IntentAnalyzerResult(analysis=analysis, invocation=invocation)

    def ask(self, question: str, candidates: Sequence[SelectorCandidate]) -> Optional[str]:
        """Compatibility wrapper: curated answer text of the SELECTed candidate."""
        result = self.ask_with_result(question, candidates)
        if result.status is LLMOutcomeStatus.TIMEOUT:
            raise TimeoutError("LLM request timed out")
        if result.status is LLMOutcomeStatus.MODEL_ERROR:
            raise RuntimeError("LLM request failed")
        return result.answer

    def ask_with_result(
        self, question: str, candidates: Sequence[SelectorCandidate]
    ) -> SelectorResult:
        """Selector V2: resolved intent + eligible candidates -> SELECT/NONE.

        Strict JSON is validated with Pydantic and candidate-set membership;
        there is no numeric/first-digit parsing."""
        if not candidates:
            raise ValueError("selector requires at least one eligible candidate")
        config = self.effective_config(LLMCapability.SELECTOR)
        system, user = build_selector_prompt(question, candidates)
        invocation = self._invoke(system, user, config)
        if invocation.status is not LLMOutcomeStatus.SUCCESS:
            return SelectorResult(
                status=invocation.status,
                parse_status=LLMParseStatus.NOT_APPLICABLE,
                answer=None,
                invocation=invocation,
            )
        return parse_selector_output(invocation.text, candidates, invocation)

    def _invoke(
        self, system: str, user: str, config: EffectiveLLMConfig
    ) -> LLMInvocationResult:
        """Compatibility invocation for test/custom providers implementing _complete."""
        started = time.perf_counter()
        try:
            text = self._complete(system, user, max_tokens=config.max_tokens)
            return LLMInvocationResult(
                status=LLMOutcomeStatus.SUCCESS,
                text=text,
                latency_ms=(time.perf_counter() - started) * 1000,
                metadata=LLMResponseMetadata(requested_model=config.model),
            )
        except Exception as exc:
            return _error_result(exc, config.model, started)

    @abstractmethod
    def _complete(self, system: str, user: str, max_tokens: int = 5) -> str:
        """Sağlayıcıya özgü ham metin tamamlama çağrısı."""
        pass


class _OpenAICompatibleProvider(BaseLLMProvider):
    """OpenAI Chat Completions uyumlu sağlayıcılar için ortak taban.
    OpenAI ve OpenRouter YALNIZCA base_url + varsayılan model ile ayrışır;
    istek gövdesi birebir aynıdır."""
    def __init__(
        self,
        model: str,
        api_key: Optional[str],
        base_url: Optional[str] = None,
        provider_name: str = "openai",
    ):
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = model
        self.provider_name = provider_name
        self._configs = resolve_llm_config_set(provider_name, model_override=model)

    def _complete(self, system: str, user: str, max_tokens: int = 5) -> str:
        config = replace(
            self.effective_config(LLMCapability.SELECTOR), max_tokens=max_tokens
        )
        result = self._invoke(system, user, config)
        if result.status is LLMOutcomeStatus.TIMEOUT:
            raise TimeoutError("LLM request timed out")
        if result.status is LLMOutcomeStatus.MODEL_ERROR:
            raise RuntimeError("LLM request failed")
        return result.text or ""

    def _invoke(
        self, system: str, user: str, config: EffectiveLLMConfig
    ) -> LLMInvocationResult:
        # Validated before the request: raises instead of silently dropping.
        reasoning = reasoning_request_fields(self.provider_name, config.reasoning_effort)
        started = time.perf_counter()
        # One logical invocation with ONE deadline. The adapter owns the retry
        # loop (SDK retries disabled) so attempts + backoff can never outlive
        # LOGICAL_DEADLINE_SECONDS; the circuit breaker (recorded once by the
        # caller) sees only the final outcome. Retry decisions and backoff
        # mirror the SDK's own HTTP policy, and body-level errors (HTTP 200 +
        # {"error": ...}) are treated like their HTTP status.
        deadline = started + _logical_deadline(config)
        attempt_timeout = (
            config.timeout_seconds if config.timeout_seconds is not None
            else _default_attempt_timeout(config)
        )
        retries = 0
        try:
            # Same budget the SDK would have applied on this client.
            budget = (
                config.max_retries if config.max_retries is not None
                else getattr(self.client, "max_retries", _SDK_DEFAULT_MAX_RETRIES)
            )
            if not isinstance(budget, int) or isinstance(budget, bool):
                budget = _SDK_DEFAULT_MAX_RETRIES
            # SDK-internal retries off: the loop below owns them (deadline).
            with_options = getattr(self.client, "with_options", None)
            client = with_options(max_retries=0) if callable(with_options) else self.client
            kwargs = {
                "model": config.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "max_tokens": config.max_tokens,
                "temperature": config.temperature,
            }
            kwargs.update(reasoning)
            while True:
                remaining = deadline - time.perf_counter()
                if remaining < _MIN_ATTEMPT_SECONDS:
                    raise LogicalDeadlineExceeded()
                kwargs["timeout"] = min(attempt_timeout, remaining)
                attempt_started = time.perf_counter()
                try:
                    with Timer("llm_provider"):
                        response = _create_with_wall_clock(
                            client, kwargs, attempt_started + kwargs["timeout"]
                        )
                    if not getattr(response, "choices", None):
                        raise ProviderBodyError(_body_error_status(response))
                except Exception as exc:
                    delay = _retry_delay_for(exc, retries) if retries < budget else None
                    if delay is None or time.perf_counter() + delay + _MIN_ATTEMPT_SECONDS > deadline:
                        raise
                    retries += 1
                    load_metrics.event(
                        "llm_retry", kind=_failure_category(exc),
                        status=getattr(exc, "status_code", None), retry=retries,
                        delay_ms=round(delay * 1000, 1), model=config.model,
                    )
                    _sleep(delay)
                    continue
                attempt_ms = (time.perf_counter() - attempt_started) * 1000
                break
            usage = getattr(response, "usage", None)
            choice = response.choices[0]
            return LLMInvocationResult(
                status=LLMOutcomeStatus.SUCCESS,
                text=choice.message.content,
                latency_ms=(time.perf_counter() - started) * 1000,
                metadata=LLMResponseMetadata(
                    requested_model=config.model,
                    actual_model=getattr(response, "model", None),
                    provider_response_id=getattr(response, "id", None),
                    input_tokens=getattr(usage, "prompt_tokens", None),
                    output_tokens=getattr(usage, "completion_tokens", None),
                    finish_reason=_enum_value(getattr(choice, "finish_reason", None)),
                    retry_count=retries,
                    attempt_latency_ms=attempt_ms,
                ),
            )
        except Exception as exc:
            return _error_result(exc, config.model, started, retry_count=retries)


class OpenAIProvider(_OpenAICompatibleProvider):
    def __init__(self, model: Optional[str] = None):
        model = model or default_model("openai")
        super().__init__(
            model=model, api_key=os.getenv("OPENAI_API_KEY"), provider_name="openai"
        )


class OpenRouterProvider(_OpenAICompatibleProvider):
    def __init__(self, model: Optional[str] = None, api_key: Optional[str] = None):
        model = model or default_model("openrouter")
        # api_key parametresi ayarlar sayfasından (DB) gelen anahtar için;
        # verilmezse eski davranış (.env) korunur.
        super().__init__(
            model=model,
            api_key=api_key or os.getenv("OPENROUTER_API_KEY"),
            base_url="https://openrouter.ai/api/v1",
            provider_name="openrouter",
        )


class GeminiProvider(BaseLLMProvider):
    def __init__(self, model: Optional[str] = None):
        model = model or default_model("gemini")
        self.api_key = os.getenv("GEMINI_API_KEY")
        self.client = genai.Client(api_key=self.api_key)
        self._configured_clients = {}
        self.model = model
        self.provider_name = "gemini"
        self._configs = resolve_llm_config_set("gemini", model_override=model)

    def _complete(self, system: str, user: str, max_tokens: int = 5) -> str:
        # max_tokens/temperature diğer sağlayıcılarla tutarlı geçilir; yoksa
        # Gemini uzun/serbest cevap verip strict JSON seçim parse'ını bozabiliyor.
        config = replace(
            self.effective_config(LLMCapability.SELECTOR), max_tokens=max_tokens
        )
        result = self._invoke(system, user, config)
        if result.status is LLMOutcomeStatus.TIMEOUT:
            raise TimeoutError("LLM request timed out")
        if result.status is LLMOutcomeStatus.MODEL_ERROR:
            raise RuntimeError("LLM request failed")
        return result.text or ""

    def _invoke(
        self, system: str, user: str, config: EffectiveLLMConfig
    ) -> LLMInvocationResult:
        # No reasoning transport for Gemini: fail before the request.
        reasoning_request_fields(self.provider_name, config.reasoning_effort)
        started = time.perf_counter()
        try:
            generate_config_kwargs = {
                "max_output_tokens": config.max_tokens,
                "temperature": config.temperature,
            }
            client = self._client_for_config(config)
            with Timer("llm_provider"):
                response = client.models.generate_content(
                    model=config.model,
                    contents=f"{system}\n\n{user}",
                    config=genai_types.GenerateContentConfig(**generate_config_kwargs),
                )
            usage = getattr(response, "usage_metadata", None)
            candidates = getattr(response, "candidates", None) or []
            finish_reason = (
                _enum_value(getattr(candidates[0], "finish_reason", None))
                if candidates else None
            )
            return LLMInvocationResult(
                status=LLMOutcomeStatus.SUCCESS,
                text=response.text,
                latency_ms=(time.perf_counter() - started) * 1000,
                metadata=LLMResponseMetadata(
                    requested_model=config.model,
                    actual_model=getattr(response, "model_version", None),
                    provider_response_id=getattr(response, "response_id", None),
                    input_tokens=getattr(usage, "prompt_token_count", None),
                    output_tokens=getattr(usage, "candidates_token_count", None),
                    finish_reason=finish_reason,
                    retry_count=None,
                ),
            )
        except Exception as exc:
            return _error_result(exc, config.model, started)

    def _client_for_config(self, config: EffectiveLLMConfig):
        if config.timeout_seconds is None and config.max_retries is None:
            return self.client
        fingerprint = config.fingerprint
        configured = self._configured_clients.get(fingerprint)
        if configured is not None:
            return configured
        http_options = {}
        if config.timeout_seconds is not None:
            # google-genai HttpOptions expects milliseconds.
            http_options["timeout"] = int(config.timeout_seconds * 1000)
        if config.max_retries is not None:
            # Google counts the original attempt; our config counts retries.
            http_options["retry_options"] = genai_types.HttpRetryOptions(
                attempts=config.max_retries + 1
            )
        configured = genai.Client(
            api_key=self.api_key,
            http_options=genai_types.HttpOptions(**http_options),
        )
        self._configured_clients[fingerprint] = configured
        return configured


def _is_timeout_exception(exc: Exception) -> bool:
    return isinstance(exc, TimeoutError) or "timeout" in exc.__class__.__name__.lower()


def _failure_category(exc: Exception) -> str:
    """Coarse, secret-free category from exception class/status only."""
    if _is_timeout_exception(exc):
        return "TIMEOUT"
    name = exc.__class__.__name__.lower()
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None
    if "ratelimit" in name or status == 429:
        return "RATE_LIMIT"
    if "authentication" in name or "permission" in name or status in (401, 403):
        return "AUTH"
    if "connection" in name or "network" in name or isinstance(exc, ConnectionError):
        return "NETWORK"
    if (status is not None and status >= 500) or "internalserver" in name or "unavailable" in name:
        return "PROVIDER_5XX"
    module = exc.__class__.__module__ or ""
    if module.startswith(("openai", "google", "httpx")):
        return "SDK_ERROR"
    return "UNKNOWN"


def _error_result(
    exc: Exception, requested_model: str, started: float,
    retry_count: Optional[int] = None,
) -> LLMInvocationResult:
    return LLMInvocationResult(
        status=(
            LLMOutcomeStatus.TIMEOUT
            if _is_timeout_exception(exc)
            else LLMOutcomeStatus.MODEL_ERROR
        ),
        text=None,
        latency_ms=(time.perf_counter() - started) * 1000,
        metadata=LLMResponseMetadata(
            requested_model=requested_model, retry_count=retry_count
        ),
        error_type=exc.__class__.__name__,
        failure_category=_failure_category(exc),
    )


def _enum_value(value) -> Optional[str]:
    if value is None:
        return None
    return str(getattr(value, "value", value))


class ManagedLLMProvider:
    """Registry-managed LLM runtime (Phase 6).

    Each capability is routed to its own provider client (Intent Analyzer and
    Selector may use different providers/models) and every call uses the
    DB-managed effective config, never the client's boot-time env config.
    ``runtime`` carries non-secret provenance (version, source, registry ids).
    """

    def __init__(self, configs: EffectiveLLMConfigSet, clients: dict, runtime=None):
        self.configs = configs
        self.runtime = runtime
        self._clients = {
            capability: _bind_configs(client, configs)
            for capability, client in clients.items()
        }

    def effective_config(self, capability: LLMCapability) -> EffectiveLLMConfig:
        return self.configs.for_capability(capability)

    def analyze_intents_with_result(
        self, message: str, previous_user_turns: list[str] | tuple[str, ...] = ()
    ) -> IntentAnalyzerResult:
        return self._clients[LLMCapability.INTENT_ANALYZER].analyze_intents_with_result(
            message, previous_user_turns
        )

    def ask_with_result(self, question: str, candidates) -> SelectorResult:
        return self._clients[LLMCapability.SELECTOR].ask_with_result(question, candidates)

    def ask(self, question: str, candidates) -> Optional[str]:
        return self._clients[LLMCapability.SELECTOR].ask(question, candidates)


def _bind_configs(client: BaseLLMProvider, configs: EffectiveLLMConfigSet) -> BaseLLMProvider:
    """Shallow per-request copy sharing the SDK client, with managed configs."""
    bound = copy.copy(client)
    bound._configs = configs
    return bound


class LLMFactory:
    @staticmethod
    def create_provider(provider_name: str):
        if provider_name == "openai":
            return OpenAIProvider()
        elif provider_name == "gemini":
            return GeminiProvider()
        elif provider_name == "openrouter":
            return OpenRouterProvider()
        else:
            raise ValueError(f"Unsupported provider: {provider_name}")
