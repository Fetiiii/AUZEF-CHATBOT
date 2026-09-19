"""Registry discovery and the fingerprinted live-run plan (approval artifact).

Nothing here calls a provider. The plan lists proposed ephemeral selector
configs (never written to ``ai_capability_config``), call counts and token
estimates; dollar figures appear only for explicit, user-supplied prices.
A live run must name the plan file AND its fingerprint; a stale or different
plan, snapshot, challenge or config is refused.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping, Optional, Sequence

from benchmarks.selector_v2.contract import fingerprint
from benchmarks.selector_v2.providers import selector_config
from services.llm_config import REASONING_TRANSPORT

PLAN_VERSION = "selector-live-plan-1"
PRICE_REQUIRED = "PRICE_REQUIRED"
STAGE_B_MAX_CANDIDATE_CONFIGS = 2
ASSIGNABLE = ("QUALIFIED", "LEGACY_APPROVED")


class PlanApprovalError(SystemExit):
    pass


def discover_selector_models(models: Sequence[dict], key_present: Mapping[str, bool]) -> list[dict]:
    """Registry models (as ``ModelDefinition.to_dict()``) with selector runnability."""
    out = []
    for m in models:
        blockers = []
        if not m["enabled"]:
            blockers.append("disabled")
        if "selector" not in m["allowed_capabilities"]:
            blockers.append("selector_not_allowed")
        if m["qualification_status"] not in ASSIGNABLE:
            blockers.append(f"qualification_{m['qualification_status']}")
        if not m["supports_structured_output"]:
            blockers.append("no_strict_json_support")
        if not key_present.get(m["provider"], False):
            blockers.append("provider_key_missing")
        declared = list(m["allowed_reasoning_efforts"]) if m["supports_reasoning_effort"] else []
        transport = sorted(set(declared) & REASONING_TRANSPORT.get(m["provider"], frozenset()))
        out.append({
            "registry_id": m["id"],
            "provider": m["provider"],
            "model_identifier": m["model_identifier"],
            "enabled": m["enabled"],
            "qualification_status": m["qualification_status"],
            "selector_allowed": "selector" in m["allowed_capabilities"],
            "supports_reasoning_effort": m["supports_reasoning_effort"],
            "allowed_reasoning_efforts": declared,
            "reasoning_transport_efforts": transport,
            "provider_key_present": bool(key_present.get(m["provider"], False)),
            "runnable": not blockers,
            "blockers": blockers,
        })
    return out


def _price(prices: Mapping[str, tuple[float, float]], provider: str, model: str):
    return prices.get(f"{provider}/{model}") or prices.get("*")


def _stage_cost(estimate: dict, price) -> dict:
    if price is None:
        return {"status": PRICE_REQUIRED, "estimated_cost": None}
    cost = (estimate["input_tokens"]["total"] / 1_000_000 * price[0]
            + estimate["output_tokens_estimated"]["total"] / 1_000_000 * price[1])
    return {"status": "COMPUTED_FROM_EXPLICIT_PRICE", "input_price_per_1m": price[0],
            "output_price_per_1m": price[1], "estimated_cost": round(cost, 6)}


def build_live_plan(
    *,
    snapshot_manifest: dict,
    challenge_manifest: dict,
    contract_fingerprint: str,
    production_selector: dict,
    discovered: Sequence[dict],
    challenge_estimate: dict,
    full_estimate: dict,
    prior_models: Sequence[str] = (),
    prices: Optional[Mapping[str, tuple[float, float]]] = None,
) -> dict:
    prices = prices or {}
    params = {"temperature": production_selector["temperature"],
              "max_tokens": production_selector["max_tokens"]}
    configs, blocked = [], []
    for model in discovered:
        if not model["runnable"]:
            blocked.append({"provider": model["provider"], "model": model["model_identifier"],
                            "registry_id": model["registry_id"], "status": "BLOCKED",
                            "blockers": model["blockers"]})
            continue
        efforts = [None, *model["reasoning_transport_efforts"]]
        for effort in efforts:
            config = selector_config(provider=model["provider"], model=model["model_identifier"],
                                     reasoning_effort=effort, **params)
            is_baseline = config.fingerprint == production_selector["config_fingerprint"]
            price = _price(prices, model["provider"], model["model_identifier"])
            configs.append({
                "config_id": f"{model['provider']}/{model['model_identifier']}"
                             f"@{effort or 'none'}",
                "role": "production_baseline" if is_baseline else (
                    "reasoning" if effort else "alternative_model"),
                "registry_id": model["registry_id"],
                "config": config.to_dict(),
                "config_fingerprint": config.fingerprint,
                "stage_a": {"calls": challenge_estimate["cases"],
                            "estimated_input_tokens": challenge_estimate["input_tokens"],
                            "estimated_output_tokens": challenge_estimate["output_tokens_estimated"],
                            "cost": _stage_cost(challenge_estimate, price)},
                "stage_b_full": {"calls": full_estimate["cases"],
                                 "incremental_calls_after_stage_a":
                                     full_estimate["cases"] - challenge_estimate["cases"],
                                 "cost": _stage_cost(full_estimate, price)},
            })
    registered = {f"{m['provider']}/{m['model_identifier']}" for m in discovered}
    for prior in prior_models:
        if prior not in registered:
            blocked.append({"provider": prior.split("/", 1)[0], "model": prior.split("/", 1)[1],
                            "registry_id": None, "status": "NOT_REGISTERED",
                            "blockers": ["not in ai_model_registry; needs registration + "
                                         "qualification before any 7B-Live run"]})
    if not any(c["role"] == "production_baseline" for c in configs):
        raise SystemExit("production baseline config is not runnable; plan refused")
    stage_b_configs = 1 + STAGE_B_MAX_CANDIDATE_CONFIGS
    plan = {
        "plan_version": PLAN_VERSION,
        "stage": "A",
        "live": False,
        "approved": False,
        "snapshot_fingerprint": snapshot_manifest["snapshot_fingerprint"],
        "challenge_fingerprint": challenge_manifest["challenge_fingerprint"],
        "selector_contract_fingerprint": contract_fingerprint,
        "production_selector": production_selector,
        "proposed_configs": configs,
        "blocked_or_unregistered": blocked,
        "reasoning_experiment": (
            [c["config_id"] for c in configs if c["role"] == "reasoning"]
            or "BLOCKED: no runnable registry model declares reasoning support with an "
               "adapter transport (openai/openrouter low|medium|high)"),
        "stage_a": {
            "case_set": "challenge",
            "calls_per_config": challenge_estimate["cases"],
            "number_of_configs": len(configs),
            "total_calls": challenge_estimate["cases"] * len(configs),
            "tokens_are_approximate": challenge_estimate["approximate"],
        },
        "stage_b_policy": {
            "case_set": "full reference (primary)",
            "runs_automatically": False,
            "requires_new_plan_and_approval": True,
            "configs": f"production baseline + at most {STAGE_B_MAX_CANDIDATE_CONFIGS} "
                       "configs chosen by human review of Stage A",
            "calls_per_config": full_estimate["cases"],
            "provisional_max_total_calls": full_estimate["cases"] * stage_b_configs,
            "provisional_incremental_calls_reusing_stage_a":
                (full_estimate["cases"] - challenge_estimate["cases"]) * stage_b_configs,
            "note": "Stage A results are reused because run identity does not depend on "
                    "the case set (same config + snapshot + contract).",
        },
        "prices": {k: list(v) for k, v in prices.items()} or PRICE_REQUIRED,
        "winner": None,
    }
    plan["plan_fingerprint"] = plan_fingerprint(plan)
    return plan


def plan_fingerprint(plan: dict) -> str:
    return fingerprint({k: v for k, v in plan.items() if k not in ("plan_fingerprint", "created_at")})


def write_plan(path: Path, plan: dict) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                          encoding="utf-8")


def load_approved_plan(path: Path, approved_fingerprint: Optional[str], *,
                       snapshot_fingerprint: str, challenge_fingerprint: Optional[str],
                       contract_fingerprint: str, config_fingerprint: str) -> dict:
    """Validate an explicit approval; any mismatch refuses the live run."""
    if not path or not approved_fingerprint:
        raise PlanApprovalError("live run requires --live-plan and --approve-plan-fingerprint")
    plan = json.loads(Path(path).read_text(encoding="utf-8"))
    actual = plan_fingerprint(plan)
    checks = {
        "plan file modified": actual != plan.get("plan_fingerprint"),
        "approved fingerprint differs from plan": approved_fingerprint != actual,
        "snapshot differs from plan": plan.get("snapshot_fingerprint") != snapshot_fingerprint,
        "selector contract differs from plan":
            plan.get("selector_contract_fingerprint") != contract_fingerprint,
        "config not in plan": config_fingerprint not in {
            c["config_fingerprint"] for c in plan.get("proposed_configs", [])},
    }
    if plan.get("stage") == "A":
        checks["challenge differs from plan"] = plan.get("challenge_fingerprint") != challenge_fingerprint
    failed = [name for name, bad in checks.items() if bad]
    if failed:
        raise PlanApprovalError(f"live plan refused: {', '.join(failed)}")
    return plan
