"""variant_a_v3_contract: catalog registration, pinned fingerprints, anti-overfit static checks."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from services.selector_prompt_catalog import (
    DEFAULT_PROMPT_VERSION,
    PRODUCTION_V2,
    PROMPT_VERSION_ENV,
    PROMPT_VERSIONS,
    VARIANT_A_V1,
    VARIANT_A_V3_CONTRACT,
    load_selector_prompt,
    resolve_selector_prompt,
)

V1_FP = "1aed568885db02f474534224695a45eb6f95835bc31f94e877af9659efe94a1e"
V3_CONTRACT_FP = "cdeea79518c6b20a26e65ac628af3a03e92a4bbd6d14802cc709e647643c46c7"
# Development benchmark (git-ignored outputs/); the exact-question scan runs when it is present.
BENCHMARK = (Path(__file__).resolve().parents[2] / "outputs" / "performance-readiness"
             / "kb-freeze-v1" / "benchmark-reclassification-freeze-v1.json")


def _text() -> str:
    return load_selector_prompt(VARIANT_A_V3_CONTRACT).text


def test_v1_fingerprint_and_default_are_unchanged():
    assert load_selector_prompt(VARIANT_A_V1).fingerprint == V1_FP
    assert DEFAULT_PROMPT_VERSION == PRODUCTION_V2
    assert resolve_selector_prompt({}).version == PRODUCTION_V2


def test_v3_contract_is_registered_and_loads_deterministically():
    assert VARIANT_A_V3_CONTRACT in PROMPT_VERSIONS
    first = resolve_selector_prompt({PROMPT_VERSION_ENV: VARIANT_A_V3_CONTRACT})
    second = load_selector_prompt(VARIANT_A_V3_CONTRACT)
    assert first.version == VARIANT_A_V3_CONTRACT
    assert first.fingerprint == second.fingerprint == V3_CONTRACT_FP
    assert first.source == "services/prompts/selector/variant_a_v3_contract.md"


def test_v3_contract_keeps_the_output_contract_of_v1():
    v1, v3 = load_selector_prompt(VARIANT_A_V1).text.splitlines(), _text().splitlines()
    assert v1[-3:] == v3[-3:]           # calendar rule, single choice / order, strict JSON


def test_v3_contract_has_no_case_ids_record_ids_or_candidate_refs():
    text = _text()
    assert not re.search(r"\b[A-G]\d{2}\b", text)             # benchmark case ids (A01 ... G16)
    assert not re.search(r"\b(qna|calendar):\d+", text)       # candidate refs
    assert not re.search(r"\b\d{3,}\b", text)                 # QnA ids / numeric anchors


def test_v3_contract_contains_no_full_benchmark_question():
    if not BENCHMARK.exists():
        pytest.skip("development benchmark artifact not present")
    norm = lambda s: " ".join(s.casefold().split())
    text = norm(_text())
    questions = [norm(c["query"]) for c in json.loads(BENCHMARK.read_text(encoding="utf-8"))["cases"]]
    assert questions and not [q for q in questions if q in text]
