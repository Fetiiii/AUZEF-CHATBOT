"""Phase 7B model-capability experiment: offline config-parity validation.

Before any live call with a new selector model, the frozen selector config
(temperature 0, max_tokens 32, reasoning none) must be expressible for that
model on OpenRouter through the existing adapter. The check reads only
OpenRouter CATALOG metadata (``/models`` and ``/models/<id>/endpoints``,
no inference) and the adapter's reasoning transport table. If any frozen
parameter cannot be transmitted as-is, the verdict is STOP_BEFORE_LIVE;
no alternative config is chosen.
"""
from __future__ import annotations

from typing import Mapping, Sequence

from services.llm_config import REASONING_TRANSPORT

STOP = "STOP_BEFORE_LIVE"
READY = "PARITY_OK"

ADJUDICATION_PROVENANCE = {
    "method": "blind model adjudication",
    "adjudicator_type": "model",
    "adjudicator_model": "GPT-5.6 Sol",
    "recorded_reviewer_field": "ChatGPT GPT-5.6 Sol",
    "applies_to": ["Semantic Gold V1 (106 locked rows)", "Semantic Gold V1.1 (471/472 lock)"],
    "note": "Semantic Gold is based on blinded adjudication by GPT-5.6 Sol, not independent "
            "human annotation.",
    "final_validation_requirement": "independent human review of the new production-like set",
}


def parity_check(*, model_meta: Mapping, endpoints: Sequence[Mapping], frozen: Mapping,
                 provider: str = "openrouter") -> dict:
    """Can the frozen selector config be sent unchanged to this model?"""
    model_params = set(model_meta.get("supported_parameters") or [])
    endpoint_params = [set(e.get("supported_parameters") or []) for e in endpoints]
    everywhere = set.intersection(*endpoint_params) if endpoint_params else set()
    anywhere = set.union(*endpoint_params) if endpoint_params else set()
    transport = sorted(REASONING_TRANSPORT.get(provider, frozenset()))
    reasons_capable = "reasoning" in model_params or "reasoning_effort" in model_params
    checks = {}
    checks["temperature"] = {
        "frozen": frozen["temperature"],
        "model_lists_temperature": "temperature" in model_params,
        "endpoints_listing_temperature": sum("temperature" in p for p in endpoint_params),
        "ok": "temperature" in model_params and "temperature" in everywhere,
        "adapter_behaviour": "the OpenRouter adapter always sends temperature",
    }
    want = frozen["reasoning"]
    if want in (None, "none"):
        expressible = not reasons_capable or "none" in transport
        checks["reasoning"] = {
            "frozen": "none",
            "model_is_reasoning_capable": reasons_capable,
            "adapter_transport_values": transport,
            "ok": expressible,
            "adapter_behaviour": ("reasoning_effort=None sends no reasoning field; a reasoning-capable "
                                  "model then uses its provider default, which is NOT 'none'"),
        }
    else:
        checks["reasoning"] = {"frozen": want, "adapter_transport_values": transport,
                               "ok": want in transport and reasons_capable}
    token_fields = {"max_tokens" in p for p in endpoint_params}
    checks["max_tokens"] = {
        "frozen": frozen["max_tokens"],
        "endpoints_listing_max_tokens": sum("max_tokens" in p for p in endpoint_params),
        "endpoints_listing_only_max_completion_tokens": sum(
            "max_completion_tokens" in p and "max_tokens" not in p for p in endpoint_params),
        "ok": token_fields == {True} and (not reasons_capable or checks["reasoning"]["ok"]),
        "risk": ("with provider-default reasoning, reasoning tokens count against a 32-token cap and "
                 "may leave no visible JSON" if reasons_capable else None),
    }
    blocked = [k for k, v in checks.items() if not v["ok"]]
    return {
        "model": model_meta.get("id"), "provider": provider,
        "verdict": STOP if blocked else READY, "blocked_parameters": blocked,
        "checks": checks,
        "model_supported_parameters": sorted(model_params),
        "parameters_on_every_endpoint": sorted(everywhere),
        "parameters_on_some_endpoint": sorted(anywhere),
        "endpoints": [{"tag": e.get("tag"), "provider": e.get("provider_name"), "status": e.get("status"),
                       "supported_parameters": sorted(e.get("supported_parameters") or [])}
                      for e in endpoints],
        "policy": "no alternative config is tried; a parity gap stops the experiment before any call",
    }
