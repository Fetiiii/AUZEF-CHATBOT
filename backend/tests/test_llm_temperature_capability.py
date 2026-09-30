"""Capability-driven temperature: a model that does not accept ``temperature``
(registry supports_temperature=False) never receives the request field.

The production path is exercised end to end: registry ModelDefinition ->
CapabilityAssignment.effective_config() -> ManagedLLMProvider -> the
OpenAI-compatible adapter, with a fake SDK client that captures the kwargs.
"""
from types import SimpleNamespace

import pytest

from services.ai_registry import (
    AIConfigError,
    CapabilityAssignment,
    CapabilityParams,
    ModelDefinition,
    QualificationStatus,
    validate_assignment,
)
from services.candidate_eligibility import CandidateKind, SelectorCandidate
from services.llm_config import EffectiveLLMConfig, EffectiveLLMConfigSet, LLMCapability, ReasoningEffort
from services.llm_provider import ManagedLLMProvider, OpenRouterProvider

IA, SEL = LLMCapability.INTENT_ANALYZER, LLMCapability.SELECTOR


def _model(identifier, *, supports_temperature=True, reasoning=False, caps=("intent_analyzer", "selector")):
    return ModelDefinition(
        id=1, display_name=identifier, provider="openrouter", model_identifier=identifier, enabled=True,
        allowed_capabilities=tuple(caps), supports_structured_output=True,
        supports_reasoning_effort=reasoning, allowed_reasoning_efforts=("none",) if reasoning else (),
        qualification_status=QualificationStatus.QUALIFIED, supports_temperature=supports_temperature,
    )


LUNA = _model("openai/gpt-6-luna", supports_temperature=False, reasoning=True)
MINI = _model("openai/gpt-4o-mini")


def _assign(cap, model, **params):
    base = {"temperature": 0.0, "max_tokens": 32 if cap is SEL else 600,
            "reasoning_effort": "none" if model.supports_reasoning_effort else None}
    base.update(params)
    return CapabilityAssignment(cap, model, CapabilityParams(**base))


def test_existing_config_fingerprints_are_byte_identical():
    """The new registry flag defaults to True; no existing identity moves."""
    selector_mini = EffectiveLLMConfig(SEL, "openrouter", "openai/gpt-4o-mini", None, 0.0, 32)
    analyzer_mini_600 = EffectiveLLMConfig(IA, "openrouter", "openai/gpt-4o-mini", None, 0.0, 600)
    analyzer_luna_am5 = EffectiveLLMConfig(IA, "openrouter", "openai/gpt-6-luna", ReasoningEffort.NONE, 0.0, 600)
    assert selector_mini.fingerprint == "af9eb2d0767d37cd632799cbae39e7938585b243ceb4c7a1527b8028cd489a6e"
    assert analyzer_mini_600.fingerprint == "512fe08df368e629a4960ea8f55bfa19874f49cf12f12b58ed8a681b00ab5ee3"
    assert analyzer_luna_am5.fingerprint == "0181aa63cac4912085dff1934c09674c59d80815cdfdefc8a2f5c2d4a279e835"
    assert _assign(SEL, MINI, reasoning_effort=None).effective_config().fingerprint == selector_mini.fingerprint


def test_model_without_temperature_support_gets_none_in_effective_config():
    selector = _assign(SEL, LUNA).effective_config()
    analyzer = _assign(IA, LUNA).effective_config()
    assert selector.temperature is None and analyzer.temperature is None
    # The v3 + Luna selector identity measured on the development ablation / holdout-v2 harness.
    assert selector.fingerprint == "f6fcb61426cf400fb7f679190480e97e3f2d0b8ea58aecfca94a3440ecb89346"
    assert analyzer.fingerprint != "0181aa63cac4912085dff1934c09674c59d80815cdfdefc8a2f5c2d4a279e835"


def test_nonzero_temperature_is_rejected_for_a_model_without_support():
    validate_assignment(SEL, LUNA, CapabilityParams(temperature=0.0, max_tokens=32, reasoning_effort="none"))
    with pytest.raises(AIConfigError) as exc:
        validate_assignment(SEL, LUNA, CapabilityParams(temperature=0.3, max_tokens=32, reasoning_effort="none"))
    assert exc.value.code == "temperature_unsupported"
    validate_assignment(SEL, MINI, CapabilityParams(temperature=0.3, max_tokens=32))


def test_model_definition_row_defaults_to_supported():
    row = SimpleNamespace(id=5, display_name="x", provider="openrouter", model_identifier="m", enabled=1,
                          allowed_capabilities='["selector"]', supports_structured_output=1,
                          supports_reasoning_effort=0, allowed_reasoning_efforts="[]",
                          qualification_status="QUALIFIED", qualified_at=None, qualified_by=None,
                          qualification_reference="r", supports_temperature=None)
    assert ModelDefinition.from_row(row).supports_temperature is True
    row.supports_temperature = 0
    assert ModelDefinition.from_row(row).supports_temperature is False
    assert ModelDefinition.from_row(row).to_dict()["supports_temperature"] is False


class CaptureClient:
    """Fake OpenAI SDK client (no streaming interface): records every request's kwargs."""

    max_retries = 0

    def __init__(self, content):
        self.content, self.requests = content, []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def with_options(self, **_):
        return self

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return SimpleNamespace(id="g", model=kwargs["model"], model_extra={}, usage=None,
                               choices=[SimpleNamespace(message=SimpleNamespace(content=self.content),
                                                        finish_reason="stop")])


def _managed(analyzer_model, selector_model):
    configs = EffectiveLLMConfigSet(
        intent_analyzer=_assign(IA, analyzer_model).effective_config(),
        selector=_assign(SEL, selector_model).effective_config(),
    )
    clients = {IA: OpenRouterProvider(api_key="test-key"), SEL: OpenRouterProvider(api_key="test-key")}
    provider = ManagedLLMProvider(configs, clients)
    capture = {IA: CaptureClient('{"mode":"SINGLE","intents":[]}'),
               SEL: CaptureClient('{"decision":"NONE"}')}
    for cap, client in capture.items():
        provider._clients[cap].client = client
    return provider, capture


def _call_both(provider):
    cand = SelectorCandidate(candidate_ref="qna:1", kind=CandidateKind.QNA, canonical_text="Soru?",
                             answer_text="Cevap.", qna_id=1)
    provider.ask_with_result("Soru?", [cand])
    provider.analyze_intents_with_result("Soru?")


def test_luna_requests_carry_no_temperature_for_selector_and_analyzer():
    provider, capture = _managed(LUNA, LUNA)
    _call_both(provider)
    for cap in (IA, SEL):
        assert capture[cap].requests, cap
        for kwargs in capture[cap].requests:
            assert kwargs["model"] == "openai/gpt-6-luna"
            assert "temperature" not in kwargs
            assert kwargs["extra_body"] == {"reasoning": {"effort": "none"}}
    assert capture[SEL].requests[0]["max_tokens"] == 32
    assert capture[IA].requests[0]["max_tokens"] == 600


def test_models_with_temperature_support_still_send_it():
    provider, capture = _managed(MINI, MINI)
    _call_both(provider)
    for cap in (IA, SEL):
        assert all(kwargs["temperature"] == 0.0 for kwargs in capture[cap].requests)
        assert all("extra_body" not in kwargs for kwargs in capture[cap].requests)
