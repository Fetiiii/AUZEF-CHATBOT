"""Selector backends: deterministic fakes (dry run) and the gated live path.

Both go through the production ``BaseLLMProvider.ask_with_result`` so the
request is built by ``build_selector_prompt`` and the response is parsed by
``parse_selector_output`` — only the network completion differs. Fake
results are harness self-tests, never model accuracy.
"""
from __future__ import annotations

import json
import threading
from dataclasses import replace
from typing import Callable, Optional, Sequence

from benchmarks.selector_v2.prompt_contract import PRODUCTION, load_prompt, select_with_prompt
from benchmarks.selector_v2.schema import CaseSnapshot
from services.candidate_eligibility import SelectorCandidate
from services.llm_config import (
    EffectiveLLMConfig,
    EffectiveLLMConfigSet,
    LLMCapability,
    ReasoningEffort,
    ReasoningTransportError,
    reasoning_request_fields,
)
from services.llm_provider import BaseLLMProvider, _bind_configs
from services.llm_types import SelectorResult

FAKE_PROVIDER = "fake"
# Request parameters a benchmark run may drop for a model whose catalog entry
# does not support them (declared in the run identity; never production).
OMITTABLE_REQUEST_PARAMS = frozenset({"temperature"})


class ParamOmittingClient:
    """OpenAI-compatible client proxy that removes declared request params."""

    def __init__(self, inner, omit: Sequence[str]):
        self._inner = inner
        self.omit = tuple(omit)
        self.sent_param_keys: list[list[str]] = []
        self.chat = _Chat(self._create)

    def _create(self, **kwargs):
        for key in self.omit:
            kwargs.pop(key, None)
        self.sent_param_keys.append(sorted(kwargs))
        return self._inner.chat.completions.create(**kwargs)

    def with_options(self, **options):
        return ParamOmittingClient(self._inner.with_options(**options), self.omit)


class _Chat:
    def __init__(self, create):
        self.completions = _Completions(create)


class _Completions:
    def __init__(self, create):
        self.create = create

Policy = Callable[[CaseSnapshot, Sequence[SelectorCandidate]], str]


def _select(ref: str) -> str:
    return json.dumps({"decision": "SELECT", "candidate_ref": ref})


def _none() -> str:
    return json.dumps({"decision": "NONE"})


def _oracle(snapshot: CaseSnapshot, candidates: Sequence[SelectorCandidate]) -> str:
    if snapshot.case.expected_decision == "NONE":
        return _none()
    refs = {c.candidate_ref for c in candidates}
    for ref in snapshot.case.acceptable_candidate_refs:
        if ref in refs:
            return _select(ref)
    return _none()


def _raise_timeout(*_args) -> str:
    raise TimeoutError("fake timeout")


def _raise_model_error(*_args) -> str:
    raise RuntimeError("fake provider failure")


FAKE_POLICIES: dict[str, Policy] = {
    "oracle": _oracle,
    "always_none": lambda _s, _c: _none(),
    "first_candidate": lambda _s, c: _select(c[0].candidate_ref),
    "malformed": lambda _s, _c: "SELECT qna:1 because it looks right",
    "unknown_ref": lambda _s, _c: _select("qna:999999999"),
    "empty": lambda _s, _c: "",
    "timeout": _raise_timeout,
    "model_error": _raise_model_error,
}


def config_set_for(selector: EffectiveLLMConfig) -> EffectiveLLMConfigSet:
    """Only the selector capability is exercised; the analyzer slot mirrors it."""
    return EffectiveLLMConfigSet(
        intent_analyzer=replace(selector, capability=LLMCapability.INTENT_ANALYZER),
        selector=selector,
    )


def selector_config(
    *,
    provider: str,
    model: str,
    temperature: float = 0.0,
    max_tokens: int = 32,
    reasoning_effort: Optional[str] = None,
    timeout_seconds: Optional[float] = None,
    max_retries: Optional[int] = None,
) -> EffectiveLLMConfig:
    return EffectiveLLMConfig(
        capability=LLMCapability.SELECTOR,
        provider=provider,
        model=model,
        reasoning_effort=ReasoningEffort(reasoning_effort) if reasoning_effort else None,
        temperature=float(temperature),
        max_tokens=int(max_tokens),
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
        structured_output_enabled=False,
    )


class FakeSelectorProvider(BaseLLMProvider):
    """Deterministic provider double driven by a named policy."""

    provider_name = FAKE_PROVIDER

    def __init__(self, policy: str, config: EffectiveLLMConfig, prompt=None):
        if policy not in FAKE_POLICIES:
            raise ValueError(f"unknown fake policy {policy!r}: {sorted(FAKE_POLICIES)}")
        self.policy_name = policy
        self._policy = FAKE_POLICIES[policy]
        self._configs = config_set_for(config)
        self._local = threading.local()
        self.calls = 0
        self._calls_lock = threading.Lock()
        self.prompt = prompt or load_prompt(PRODUCTION)
        self.requests: list[tuple[str, str]] = []  # (system, user) as sent

    def _complete(self, system: str, user: str, max_tokens: int = 5) -> str:
        with self._calls_lock:
            self.calls += 1
            self.requests.append((system, user))
        snapshot, candidates = self._local.current
        return self._policy(snapshot, candidates)

    def select(self, snapshot: CaseSnapshot, candidates: Sequence[SelectorCandidate]) -> SelectorResult:
        self._local.current = (snapshot, candidates)
        try:
            return select_with_prompt(self, self.prompt, snapshot.case.intent_text, candidates)
        finally:
            self._local.current = None


class LiveSelectorBackend:
    """Production provider adapter bound to one explicit, ephemeral selector config.

    Constructed only behind the live gate (``--live`` +
    ``--confirm-live-provider-calls`` + an approved live plan). The config is
    never written to the registry; API keys come from the environment exactly
    as in production. It calls ``ask_with_result`` directly, so the production
    circuit breaker and DecisionTrace are never touched.
    """

    def __init__(self, config: EffectiveLLMConfig, client_factory=None, prompt=None,
                 omit_request_params: Sequence[str] = ()):
        try:
            # Preflight: an untransmittable reasoning level fails before any request.
            reasoning_request_fields(config.provider, config.reasoning_effort)
        except ReasoningTransportError as exc:
            raise SystemExit(f"live run refused: {exc}") from None
        from services.llm_provider import GeminiProvider, OpenAIProvider, OpenRouterProvider

        factories = {"openai": OpenAIProvider, "openrouter": OpenRouterProvider,
                     "gemini": GeminiProvider}
        if config.provider not in factories:
            raise SystemExit(f"unsupported live provider {config.provider!r}")
        client = (client_factory or factories[config.provider])(model=config.model)
        self.provider = _bind_configs(client, config_set_for(config))
        self.prompt = prompt or load_prompt(PRODUCTION)
        self.omit_request_params = tuple(sorted(omit_request_params))
        if self.omit_request_params:
            if set(self.omit_request_params) - OMITTABLE_REQUEST_PARAMS:
                raise SystemExit(f"live run refused: cannot omit {self.omit_request_params}")
            self.provider.client = ParamOmittingClient(self.provider.client, self.omit_request_params)

    def select(self, snapshot: CaseSnapshot, candidates: Sequence[SelectorCandidate]) -> SelectorResult:
        return select_with_prompt(self.provider, self.prompt, snapshot.case.intent_text, candidates)
