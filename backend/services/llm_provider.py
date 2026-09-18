import os
import re
import time
from abc import ABC, abstractmethod
from dataclasses import replace
from typing import Optional
from openai import OpenAI
from google import genai
from google.genai import types as genai_types
from dotenv import load_dotenv

from services.llm_config import (
    EffectiveLLMConfig,
    EffectiveLLMConfigSet,
    LLMCapability,
    default_model,
    resolve_llm_config_set,
)
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

    def _build_prompt(self, question: str, context_list: list) -> tuple:
        candidates = "\n".join([
            f"[{i+1}] Soru: {item['question']}\n    Cevap: {item['answer']}"
            for i, item in enumerate(context_list)
        ])
        system = (
            "Sen bir soru-cevap seçici asistansın. "
            "Sana verilen aday cevaplar arasından kullanıcının sorusuna en uygun olanı seçersin. "
            "Önceki konuşma verilmişse onu yalnız güncel mesajı anlamlandırmak için kullanırsın; "
            "güncel mesaj yeni ve açık bir konuysa eski konuyu yok sayarsın. "
            "Kendi cevabını asla üretmezsin, yalnızca bir sayı yazarsın."
        )
        user = (
            f"Kullanıcı sorusu: {question}\n\n"
            f"Aday cevaplar:\n{candidates}\n\n"
            f"Yalnızca en uygun adayın numarasını yaz (1-{len(context_list)}). "
            f"Hiçbiri uygun değilse 0 yaz. Başka hiçbir şey yazma."
        )
        return system, user

    def _parse_selection(self, raw: str, context_list: list) -> Optional[str]:
        # İlk sayıyı regex ile çek: model "3." veya "[3]" gibi yazarsa da
        # seçim kaybolmasın. (Eskiden int() ValueError → geçerli seçim çöpe
        # gidiyor, sistem gereksiz yere eşik yedeğine düşüyordu.)
        m = re.search(r"\d+", raw or "")
        if m:
            idx = int(m.group())
            if 1 <= idx <= len(context_list):
                return context_list[idx - 1]['answer']
        return None

    def _parse_selection_result(
        self,
        raw: str,
        context_list: list,
        invocation: LLMInvocationResult,
    ) -> SelectorResult:
        match = re.search(r"\d+", raw or "")
        if not match:
            return SelectorResult(
                status=LLMOutcomeStatus.INVALID_OUTPUT,
                parse_status=LLMParseStatus.INVALID_OUTPUT,
                answer=None,
                selected_index=None,
                invocation=invocation,
            )
        value = int(match.group())
        if value == 0:
            return SelectorResult(
                status=LLMOutcomeStatus.SEMANTIC_NONE,
                parse_status=LLMParseStatus.SEMANTIC_NONE,
                answer=None,
                selected_index=None,
                raw_numeric_value=value,
                invocation=invocation,
            )
        if not 1 <= value <= len(context_list):
            return SelectorResult(
                status=LLMOutcomeStatus.INVALID_OUTPUT,
                parse_status=LLMParseStatus.INVALID_OUTPUT,
                answer=None,
                selected_index=None,
                raw_numeric_value=value,
                invocation=invocation,
            )
        candidate = context_list[value - 1]
        return SelectorResult(
            status=LLMOutcomeStatus.SUCCESS,
            parse_status=LLMParseStatus.SUCCESS,
            answer=candidate["answer"],
            selected_index=value - 1,
            selected_qna_id=candidate.get("qna_id"),
            raw_numeric_value=value,
            invocation=invocation,
        )

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

    def ask(self, question: str, context_list: list) -> Optional[str]:
        """Aday havuzundan birebir seçim. Tüm sağlayıcılarda aynı: prompt kur,
        tamamla, dönen numarayı çözümle. Sağlayıcıya özgü olan tek şey
        ``_complete``tir."""
        result = self.ask_with_result(question, context_list)
        if result.status is LLMOutcomeStatus.TIMEOUT:
            raise TimeoutError("LLM request timed out")
        if result.status is LLMOutcomeStatus.MODEL_ERROR:
            raise RuntimeError("LLM request failed")
        return result.answer

    def ask_with_result(self, question: str, context_list: list) -> SelectorResult:
        config = self.effective_config(LLMCapability.SELECTOR)
        system, user = self._build_prompt(question, context_list)
        invocation = self._invoke(system, user, config)
        if invocation.status is not LLMOutcomeStatus.SUCCESS:
            return SelectorResult(
                status=invocation.status,
                parse_status=LLMParseStatus.NOT_APPLICABLE,
                answer=None,
                selected_index=None,
                invocation=invocation,
            )
        return self._parse_selection_result(invocation.text or "", context_list, invocation)

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
        # Gemini uzun/serbest cevap verip sayı-seçim parse'ını bozabiliyor.
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
        started = time.perf_counter()
        try:
            generate_config_kwargs = {
                "max_output_tokens": config.max_tokens,
                "temperature": config.temperature,
            }
            client = self._client_for_config(config)
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
    )


def _enum_value(value) -> Optional[str]:
    if value is None:
        return None
    return str(getattr(value, "value", value))


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
