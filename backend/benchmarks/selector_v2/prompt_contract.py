"""Benchmark-only selector system-prompt override (Phase 7B prompt experiment).

Production is untouched. The production prompt is read at runtime from
``services.selector.SELECTOR_SYSTEM_PROMPT`` — there is deliberately no copy
of it in ``prompts/``. Variants live in versioned ``prompts/*.md`` files.

The override changes ONLY the system prompt: the user payload comes from the
production ``build_selector_prompt`` (same candidate serializer), invocation
goes through the production adapter ``_invoke`` (same model config and
transport) and the response is parsed by production ``parse_selector_output``
(same strict schema and candidate-membership check). For ``production`` the
unmodified ``BaseLLMProvider.ask_with_result`` is used.

Fingerprints are split:
- ``prompt_fingerprint``: sha256 of the normalized system prompt text;
- ``serializer_contract_fingerprint``: output schema + candidate prompt-view
  fields + serialized probe payload — no system prompt.

Normalization policy: CRLF→LF, trailing whitespace stripped per line,
leading/trailing blank lines removed; nothing else (inner text, blank lines
between paragraphs and characters are preserved). The normalized text is
exactly what is sent to the model.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from benchmarks.selector_v2.contract import _PROBE_CANDIDATE, fingerprint, sha256_text
from services.candidate_eligibility import SelectorCandidate
from services.llm_config import LLMCapability
from services.llm_types import LLMOutcomeStatus, LLMParseStatus, SelectorDecision, SelectorResult
from services.selector import build_selector_prompt, parse_selector_output

PROMPT_DIR = Path(__file__).with_name("prompts")
PRODUCTION = "production"
PROMPT_FILES = {"variant_a_v1": "variant_a_v1.md", "variant_b_v1": "variant_b_v1.md",
                "variant_c_v1": "variant_c_v1.md"}
PROMPT_IDS = (PRODUCTION, *PROMPT_FILES)
NORMALIZATION = ("CRLF->LF; rstrip each line; strip leading/trailing blank lines; "
                 "no other change; normalized text is what the model receives")


def normalize_prompt(text: str) -> str:
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return "\n".join(lines).strip("\n")


@dataclass(frozen=True)
class PromptSpec:
    prompt_id: str
    text: str
    source: str
    benchmark_only: bool

    @property
    def fingerprint(self) -> str:
        return sha256_text(self.text)

    def to_dict(self) -> dict:
        return {"prompt_id": self.prompt_id, "source": self.source,
                "benchmark_only": self.benchmark_only, "prompt_fingerprint": self.fingerprint,
                "chars": len(self.text)}


def load_prompt(prompt_id: str = PRODUCTION) -> PromptSpec:
    if prompt_id == PRODUCTION:
        from services import selector

        return PromptSpec(PRODUCTION, normalize_prompt(selector.SELECTOR_SYSTEM_PROMPT),
                          "services.selector.SELECTOR_SYSTEM_PROMPT (runtime)", False)
    if prompt_id not in PROMPT_FILES:
        raise SystemExit(f"unknown prompt {prompt_id!r}; known: {', '.join(PROMPT_IDS)}")
    path = PROMPT_DIR / PROMPT_FILES[prompt_id]
    return PromptSpec(prompt_id, normalize_prompt(path.read_text(encoding="utf-8")),
                      f"benchmarks/selector_v2/prompts/{path.name}", True)


def serializer_contract() -> dict:
    """Everything the selector contract fixes except the system prompt."""
    _system, user = build_selector_prompt("probe intent", [_PROBE_CANDIDATE])
    return {
        "output_schema": SelectorDecision.model_json_schema(),
        "prompt_view_fields": sorted(_PROBE_CANDIDATE.prompt_view().keys()),
        "serializer_probe_sha256": sha256_text(user),
        "parser": "services.selector.parse_selector_output",
    }


def serializer_contract_fingerprint() -> str:
    return fingerprint(serializer_contract())


def build_request(prompt: PromptSpec, intent_text: str,
                  candidates: Sequence[SelectorCandidate]) -> tuple[str, str]:
    """(system, user): variant system prompt + production user payload."""
    _production_system, user = build_selector_prompt(intent_text, candidates)
    return prompt.text, user


def select_with_prompt(provider, prompt: PromptSpec, intent_text: str,
                       candidates: Sequence[SelectorCandidate]) -> SelectorResult:
    if prompt.prompt_id == PRODUCTION:
        return provider.ask_with_result(intent_text, candidates)
    if not candidates:
        raise ValueError("selector requires at least one eligible candidate")
    # Mirrors BaseLLMProvider.ask_with_result; only the system prompt differs.
    config = provider.effective_config(LLMCapability.SELECTOR)
    system, user = build_request(prompt, intent_text, candidates)
    invocation = provider._invoke(system, user, config)
    if invocation.status is not LLMOutcomeStatus.SUCCESS:
        return SelectorResult(status=invocation.status, parse_status=LLMParseStatus.NOT_APPLICABLE,
                              answer=None, invocation=invocation)
    return parse_selector_output(invocation.text, candidates, invocation)


def prompt_manifest() -> dict:
    return {
        "normalization": NORMALIZATION,
        "serializer_contract_fingerprint": serializer_contract_fingerprint(),
        "prompts": {pid: load_prompt(pid).to_dict() for pid in PROMPT_IDS},
    }


def load_committed_manifest() -> dict:
    return json.loads((PROMPT_DIR / "manifest.json").read_text(encoding="utf-8"))
