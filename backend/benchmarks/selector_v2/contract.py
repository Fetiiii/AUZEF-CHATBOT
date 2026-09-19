"""Selector contract reuse and fingerprints.

The benchmark never owns a prompt: the model input is produced by the
production ``build_selector_prompt`` over production ``SelectorCandidate``
objects, and output is parsed by production ``parse_selector_output``. The
contract fingerprint hashes what those functions produce, so any prompt,
schema or serializer change yields a new fingerprint and a new result
namespace automatically.
"""
from __future__ import annotations

import hashlib
import json
import random
from typing import Sequence

from services.candidate_eligibility import CandidateKind, SelectorCandidate
from services.llm_types import SelectorDecision
from services.selector import SELECTOR_SYSTEM_PROMPT, build_selector_prompt

PRODUCTION_ORDER = "production"
# Deterministic hash-neutral order (Phase 7B order experiment; never production).
NEUTRAL_ORDER = "neutral"
NEUTRAL_ORDER_SALT = "selector-neutral-order-v1"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(value) -> str:
    return sha256_text(canonical_json(value))


# The probe deliberately carries provenance: if a production change ever made
# prompt_view() serialize score/source/alias, the serialized probe (and thus
# the fingerprint) would change.
_PROBE_CANDIDATE = SelectorCandidate(
    candidate_ref="qna:1",
    kind=CandidateKind.QNA,
    canonical_text="probe question",
    answer_text="probe answer",
    qna_id=1,
    source="qdrant",
    retrieval_stage="current",
    score=0.5,
    alias_match=True,
)


def selector_contract() -> dict:
    system, user = build_selector_prompt("probe intent", [_PROBE_CANDIDATE])
    return {
        "system_prompt_sha256": sha256_text(SELECTOR_SYSTEM_PROMPT),
        "built_system_prompt_sha256": sha256_text(system),
        "output_schema": SelectorDecision.model_json_schema(),
        "prompt_view_fields": sorted(_PROBE_CANDIDATE.prompt_view().keys()),
        "serializer_probe_sha256": sha256_text(user),
    }


def selector_contract_fingerprint() -> str:
    return fingerprint(selector_contract())


def order_candidates(
    candidates: Sequence[SelectorCandidate], *, case_id: str, order: str = PRODUCTION_ORDER
) -> list[SelectorCandidate]:
    """Frozen production order, or a deterministic permutation (position-bias
    extension point; not used by default and never by production)."""
    items = list(candidates)
    if order == PRODUCTION_ORDER:
        return items
    if order == NEUTRAL_ORDER:
        return sorted(items, key=lambda c: sha256_text(f"{NEUTRAL_ORDER_SALT}|{case_id}|{c.candidate_ref}"))
    if order.startswith("permute:") and order.split(":", 1)[1]:
        rng = random.Random(f"{order}|{case_id}")
        rng.shuffle(items)
        return items
    raise ValueError(f"unknown candidate order {order!r} (production | neutral | permute:<seed>)")


def build_model_input(intent_text: str, candidates: Sequence[SelectorCandidate]) -> tuple[str, str]:
    """Exactly the production selector request (system, user)."""
    return build_selector_prompt(intent_text, candidates)
