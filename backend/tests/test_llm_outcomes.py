"""Typed LLM outcomes retain V1 public behavior while preserving causes."""
from types import SimpleNamespace

from services.candidate_eligibility import CandidateKind, SelectorCandidate
from services.llm_config import resolve_llm_config_set
import services.llm_provider as llm_provider
from services.llm_provider import BaseLLMProvider, GeminiProvider, OpenAIProvider
from services.llm_types import LLMOutcomeStatus, LLMParseStatus


class ScriptedProvider(BaseLLMProvider):
    provider_name = "openai"

    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.model = "gpt-4o-mini"
        self._configs = resolve_llm_config_set("openai", environ={})

    def _complete(self, _system, _user, max_tokens=5):
        del max_tokens
        if self.error is not None:
            raise self.error
        return self.result


def _candidates():
    return [
        SelectorCandidate("qna:10", CandidateKind.QNA, "q1", "a1", qna_id=10),
        SelectorCandidate("qna:20", CandidateKind.QNA, "q2", "a2", qna_id=20),
    ]


def test_selector_success_returns_selected_ref_qna_and_curated_answer():
    provider = ScriptedProvider('{"decision":"SELECT","candidate_ref":"qna:20"}')
    result = provider.ask_with_result("soru", _candidates())
    assert result.status is LLMOutcomeStatus.SUCCESS
    assert result.parse_status is LLMParseStatus.SUCCESS
    assert result.decision == "SELECT"
    assert result.selected_candidate_ref == "qna:20"
    assert result.selected_qna_id == 20
    assert result.answer == "a2"
    assert provider.ask("soru", _candidates()) == "a2"


def test_semantic_none_and_invalid_output_are_distinct():
    semantic = ScriptedProvider('{"decision":"NONE"}')
    invalid = ScriptedProvider("cevap yok")
    legacy_numeric = ScriptedProvider("2")
    out_of_set = ScriptedProvider('{"decision":"SELECT","candidate_ref":"qna:7"}')
    assert semantic.ask_with_result("s", _candidates()).status is LLMOutcomeStatus.SEMANTIC_NONE
    assert invalid.ask_with_result("s", _candidates()).status is LLMOutcomeStatus.INVALID_OUTPUT
    assert legacy_numeric.ask_with_result("s", _candidates()).status is LLMOutcomeStatus.INVALID_OUTPUT
    assert out_of_set.ask_with_result("s", _candidates()).status is LLMOutcomeStatus.INVALID_OUTPUT
    assert semantic.ask("s", _candidates()) is None
    assert invalid.ask("s", _candidates()) is None


def test_provider_exception_and_timeout_are_distinct():
    failed = ScriptedProvider(error=ValueError("secret provider detail"))
    timed_out = ScriptedProvider(error=TimeoutError("late"))
    failed_result = failed.ask_with_result("s", _candidates())
    timeout_result = timed_out.ask_with_result("s", _candidates())
    assert failed_result.status is LLMOutcomeStatus.MODEL_ERROR
    assert timeout_result.status is LLMOutcomeStatus.TIMEOUT
    assert failed_result.invocation.error_type == "ValueError"
    assert "secret provider detail" not in str(failed_result.invocation)


def test_intent_analyzer_error_falls_back_to_lossless_single_and_keeps_status():
    provider = ScriptedProvider(error=RuntimeError("down"))
    current = "vize ne zaman? final ne zaman?"
    result = provider.analyze_intents_with_result(current)
    assert result.analysis.intent_count == 1
    assert result.analysis.intents[0].source_text == current
    assert result.analysis.intents[0].resolved_text == current
    assert result.analysis.intents[0].context_used is False
    assert result.analysis.intents[0].calendar_relevant is False
    assert result.status is LLMOutcomeStatus.MODEL_ERROR
    assert result.parse_status is LLMParseStatus.FALLBACK
    assert result.fallback_to_single is True


def test_openai_adapter_preserves_metadata_transmits_reasoning_and_omits_structured_output(
        monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("LLM_SELECTOR_REASONING_EFFORT", "high")
    monkeypatch.setenv("LLM_SELECTOR_STRUCTURED_OUTPUT_ENABLED", "true")
    provider = OpenAIProvider()
    captured = {}
    response = SimpleNamespace(
        id="provider-response-id",
        model="resolved-model",
        usage=SimpleNamespace(prompt_tokens=12, completion_tokens=1),
        choices=[SimpleNamespace(
            message=SimpleNamespace(content='{"decision":"NONE"}'), finish_reason="stop"
        )],
    )

    def create(**kwargs):
        captured.update(kwargs)
        return response

    provider.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    result = provider.ask_with_result("soru", _candidates())
    meta = result.invocation.metadata
    assert result.status is LLMOutcomeStatus.SEMANTIC_NONE
    assert meta.requested_model == "gpt-4o-mini"
    assert meta.actual_model == "resolved-model"
    assert meta.provider_response_id == "provider-response-id"
    assert meta.input_tokens == 12 and meta.output_tokens == 1
    assert meta.finish_reason == "stop"
    assert "timeout" not in captured
    # Phase 7B-Prep: a configured reasoning level is transmitted (never dropped).
    assert captured["reasoning_effort"] == "high"
    # Phase 4 uses strict JSON + Pydantic; native response_format is not sent.
    assert "response_format" not in captured
    assert captured["max_tokens"] == 32 and captured["temperature"] == 0


def test_openai_explicit_timeout_and_retry_are_applied(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("LLM_SELECTOR_TIMEOUT_SECONDS", "8")
    monkeypatch.setenv("LLM_SELECTOR_MAX_RETRIES", "4")
    provider = OpenAIProvider()
    captured = {}

    class Client:
        def __init__(self):
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=self.create)
            )

        def with_options(self, **kwargs):
            captured["client_options"] = kwargs
            return self

        def create(self, **kwargs):
            captured["request"] = kwargs
            return SimpleNamespace(
                id="id", model="model", usage=None,
                choices=[SimpleNamespace(
                    message=SimpleNamespace(
                        content='{"decision":"SELECT","candidate_ref":"qna:10"}'
                    ),
                    finish_reason="stop",
                )],
            )

    provider.client = Client()
    assert provider.ask_with_result("s", _candidates()).status is LLMOutcomeStatus.SUCCESS
    assert captured["client_options"] == {"max_retries": 4}
    assert captured["request"]["timeout"] == 8


def test_gemini_explicit_timeout_and_retry_use_client_http_options(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("LLM_SELECTOR_TIMEOUT_SECONDS", "8")
    monkeypatch.setenv("LLM_SELECTOR_MAX_RETRIES", "4")
    calls = []

    def client_factory(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr(llm_provider.genai, "Client", client_factory)
    provider = GeminiProvider()
    config = provider.effective_config(llm_provider.LLMCapability.SELECTOR)
    first = provider._client_for_config(config)
    second = provider._client_for_config(config)
    assert first is second
    assert len(calls) == 2  # default client + one configured cached client
    options = calls[1]["http_options"]
    assert options.timeout == 8000
    assert options.retry_options.attempts == 5
