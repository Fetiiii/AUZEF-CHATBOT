import copy
import os
import time
from services.load_metrics import Timer
from abc import ABC, abstractmethod
from dataclasses import replace
from typing import Optional, Sequence
from openai import OpenAI
from google import genai
from google.genai import types as genai_types
from dotenv import load_dotenv

from services.llm_config import (
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
        try:
            client = self.client
            if config.max_retries is not None:
                client = client.with_options(max_retries=config.max_retries)
            kwargs = {
                "model": config.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "max_tokens": config.max_tokens,
                "temperature": config.temperature,
            }
            # Omit unset optional values: passing None changes SDK defaults.
            if config.timeout_seconds is not None:
                kwargs["timeout"] = config.timeout_seconds
            kwargs.update(reasoning)
            with Timer("llm_provider"):
                response = client.chat.completions.create(**kwargs)
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
                    # SDK exposes retry policy, not the retries actually used.
                    retry_count=None,
                ),
            )
        except Exception as exc:
            return _error_result(exc, config.model, started)


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
    exc: Exception, requested_model: str, started: float
) -> LLMInvocationResult:
    return LLMInvocationResult(
        status=(
            LLMOutcomeStatus.TIMEOUT
            if _is_timeout_exception(exc)
            else LLMOutcomeStatus.MODEL_ERROR
        ),
        text=None,
        latency_ms=(time.perf_counter() - started) * 1000,
        metadata=LLMResponseMetadata(requested_model=requested_model),
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
