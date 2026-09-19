"""Experimental Selector system-prompt variants (Phase 7B postmortem).

NOT production. ``services.selector.SELECTOR_SYSTEM_PROMPT`` is unchanged.
The texts are versioned in ``prompts/variant_a_v1.md`` and
``prompts/variant_b_v1.md`` (see ``prompt_contract``); this module keeps the
postmortem API.

- variant_a_v1 — practical need + bidirectional qualifier rule: relaxes the
  NONE threshold, selects the candidate matching a qualifier the user did
  state, keeps "never assume an unstated qualifier".
- variant_b_v1 — NONE-threshold ablation: production verification rules
  unchanged plus only short/informal-message tolerance and an explicit NONE
  criterion.

Both drop the production prompt's concrete example (benchmark wording) and
contain general principles only.
"""
from __future__ import annotations

from benchmarks.selector_v2.contract import fingerprint, sha256_text
from benchmarks.selector_v2.prompt_contract import load_prompt
from services.llm_types import SelectorDecision

VARIANT_A = load_prompt("variant_a_v1").text
VARIANT_B = load_prompt("variant_b_v1").text

VARIANTS = {"variant_a_practical_qualifier": VARIANT_A,
            "variant_b_none_threshold_ablation": VARIANT_B}


def variant_contract_fingerprint(system_prompt: str) -> str:
    """Postmortem-era combined fingerprint (kept for artifact continuity)."""
    return fingerprint({
        "system_prompt_sha256": sha256_text(system_prompt),
        "output_schema": SelectorDecision.model_json_schema(),
        "user_payload": "production build_selector_prompt (unchanged)",
    })
