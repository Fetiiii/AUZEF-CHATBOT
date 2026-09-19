"""Pre-run token and (optional) cost estimate over the frozen snapshot.

No model-compatible tokenizer ships with this repo, so unless ``tiktoken``
happens to be importable the estimate is an explicit APPROXIMATION
(characters / 3.0, with a chars/4.0 .. chars/2.5 sensitivity band). No
provider price is hardcoded: a dollar figure is produced only from
user-supplied ``input_price_per_1m`` / ``output_price_per_1m``.
"""
from __future__ import annotations

import json
import math
from typing import Optional, Sequence

from benchmarks.selector_v2.contract import build_model_input
from benchmarks.selector_v2.schema import CaseSnapshot
from benchmarks.selector_v2.snapshot import describe

APPROX_CHARS_PER_TOKEN = 3.0
APPROX_BAND = (4.0, 2.5)  # chars/token: optimistic .. pessimistic
# OpenAI chat format framing: ~4 tokens per message + 3 reply priming.
CHAT_FRAMING_TOKENS = 2 * 4 + 3


def _tiktoken_encoder():
    try:
        import tiktoken  # noqa: F401 - optional

        return tiktoken.get_encoding("o200k_base")
    except Exception:
        return None


def token_counter():
    encoder = _tiktoken_encoder()
    if encoder is not None:
        return "tiktoken:o200k_base", (lambda text: len(encoder.encode(text))), True
    return (
        f"APPROXIMATE chars/{APPROX_CHARS_PER_TOKEN}",
        lambda text: math.ceil(len(text) / APPROX_CHARS_PER_TOKEN),
        False,
    )


def _cost(tokens: int, price_per_1m: Optional[float]) -> Optional[float]:
    return None if price_per_1m is None else round(tokens / 1_000_000 * price_per_1m, 6)


def estimate(
    snapshots: Sequence[CaseSnapshot],
    *,
    max_tokens: int,
    input_price_per_1m: Optional[float] = None,
    output_price_per_1m: Optional[float] = None,
    configs_planned: int = 1,
    primary_only: bool = False,
    system_prompt: Optional[str] = None,
) -> dict:
    method, count, exact = token_counter()
    cases = [s for s in snapshots if s.selector_evaluable and (s.case.primary or not primary_only)]
    inputs, chars, outputs = [], [], []
    for snapshot in cases:
        system, user = build_model_input(snapshot.case.intent_text, snapshot.selector_candidates())
        if system_prompt is not None:  # benchmark prompt variant; user payload unchanged
            system = system_prompt
        chars.append(len(system) + len(user))
        inputs.append(count(system) + count(user) + CHAT_FRAMING_TOKENS)
        if snapshot.case.expected_decision == "SELECT":
            ref = snapshot.case.acceptable_candidate_refs[0]
            reply = json.dumps({"decision": "SELECT", "candidate_ref": ref})
        else:
            reply = json.dumps({"decision": "NONE"})
        outputs.append(min(max_tokens, count(reply)))
    total_in, total_out = sum(inputs), sum(outputs)
    band = {
        f"chars/{ratio}": math.ceil(sum(chars) / ratio) + CHAT_FRAMING_TOKENS * len(cases)
        for ratio in APPROX_BAND
    }
    prices_given = input_price_per_1m is not None and output_price_per_1m is not None
    per_run_cost = (
        (_cost(total_in, input_price_per_1m) or 0) + (_cost(total_out, output_price_per_1m) or 0)
        if prices_given else None
    )
    return {
        "tokenizer": method,
        "approximate": not exact,
        "cases": len(cases),
        "calls_per_config": len(cases),
        "configs_planned": configs_planned,
        "planned_live_calls": len(cases) * configs_planned,
        "input_tokens": {**describe(inputs), "total": total_in},
        "input_chars_total": sum(chars),
        "input_tokens_total_sensitivity_band": band,
        "output_tokens_estimated": {**describe(outputs), "total": total_out},
        "output_tokens_upper_bound_total": max_tokens * len(cases),
        "max_tokens": max_tokens,
        "price_inputs": {
            "input_price_per_1m": input_price_per_1m,
            "output_price_per_1m": output_price_per_1m,
            "source": "user-supplied" if prices_given else None,
        },
        "estimated_cost_per_config": per_run_cost,
        "estimated_cost_all_configs": (
            round(per_run_cost * configs_planned, 6) if per_run_cost is not None else None
        ),
        "cost_note": (
            "computed from user-supplied prices" if prices_given
            else "not computed: no explicit price input (no pricing config exists in the repo)"
        ),
    }
