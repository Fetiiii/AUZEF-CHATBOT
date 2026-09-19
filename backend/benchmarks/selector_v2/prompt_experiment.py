"""Phase 7B prompt experiment: frozen split, DEV evaluation, gates, plans.

The only varied factor is the selector system prompt (production /
variant_a_v1 / variant_b_v1). Model config, snapshot, candidate order,
serializer, schema, parser and Gold are fixed and fingerprinted.

HOLDOUT policy: HOLDOUT is scored at most once, for ONE prompt that a human
selected after the DEV comparison and that passed the DEV selection gate,
under its own explicitly approved plan. The gate never picks a winner.
"""
from __future__ import annotations

import difflib
import json
from pathlib import Path
from typing import Mapping, Optional, Sequence

from benchmarks.selector_v2.challenge import first_candidate_correct, selector_value
from benchmarks.selector_v2.contract import fingerprint
from benchmarks.selector_v2.plan import PRICE_REQUIRED, PlanApprovalError
from benchmarks.selector_v2.postmortem import SPLIT_VERSION, _case_key
from benchmarks.selector_v2.prompt_contract import PRODUCTION, PromptSpec
from benchmarks.selector_v2.schema import BenchmarkResult, CaseSnapshot
from benchmarks.selector_v2.tokens import estimate

SPLIT_FILE = Path(__file__).with_name("splits") / "prompt-experiment-split-v1.json"
DEV_PLAN_KIND = "selector-prompt-experiment-dev"
HOLDOUT_PLAN_KIND = "selector-prompt-experiment-holdout"
CRITICAL_CASES = ("471", "472")   # postmortem: the two clear selector errors
SLICE_OF_GROUP = {"A": "CLEAR_SELECTOR_ERROR", "B": "GOLD_ALIAS_QUESTIONABLE",
                  "C": "CONTRACT_MISMATCH", "U": "NEEDS_HUMAN_REVIEW"}
TAXONOMY_SLICES = (*SLICE_OF_GROUP.values(), "KB_OVERLAP_INTRINSIC")


# ── split ──────────────────────────────────────────────────────────────────

def split_fingerprint(dev: Sequence[str], holdout: Sequence[str]) -> str:
    return fingerprint({"version": SPLIT_VERSION, "dev": list(dev), "holdout": list(holdout)})


def load_split(path: Path = SPLIT_FILE, *, challenge_ids: Optional[set] = None,
               reference: Optional[dict] = None) -> dict:
    split = json.loads(Path(path).read_text(encoding="utf-8"))
    dev, holdout = split["dev"], split["holdout"]
    problems = []
    if split_fingerprint(dev, holdout) != split["split_fingerprint"]:
        problems.append("fingerprint does not match ids")
    if set(dev) & set(holdout):
        problems.append("DEV and HOLDOUT overlap")
    if sorted(dev, key=_case_key) != dev or sorted(holdout, key=_case_key) != holdout:
        problems.append("ids not in canonical order")
    if challenge_ids is not None and set(dev) | set(holdout) != set(challenge_ids):
        problems.append("DEV ∪ HOLDOUT differs from the frozen challenge set")
    if reference is not None and reference.get("split_fingerprint") != split["split_fingerprint"]:
        problems.append("differs from the postmortem split artifact")
    if problems:
        raise SystemExit(f"split refused: {'; '.join(problems)}")
    return split


# ── DEV evaluation ─────────────────────────────────────────────────────────

def taxonomy_slices(failure_cases: Sequence[dict]) -> dict[str, list[str]]:
    """Case ids per offline taxonomy slice (from the production Stage A postmortem)."""
    slices: dict[str, list[str]] = {name: [] for name in TAXONOMY_SLICES}
    for case in failure_cases:
        cls = case["classification"]
        slices[SLICE_OF_GROUP[cls["mismatch_group"]]].append(case["case_id"])
        if cls["primary"] == "KB_OVERLAP_INTRINSIC":
            slices["KB_OVERLAP_INTRINSIC"].append(case["case_id"])
    return {k: sorted(v, key=_case_key) for k, v in slices.items()}


def _acc(ids: Sequence[str], correct: Mapping[str, bool]) -> dict:
    done = [i for i in ids if i in correct]
    ok = sum(correct[i] for i in done)
    return {"cases": len(ids), "completed": len(done), "correct": ok,
            "accuracy": round(ok / len(done), 6) if done else None}


def dev_report(snapshots: Sequence[CaseSnapshot], ids: Sequence[str],
               results: Mapping[str, BenchmarkResult], membership: Mapping[str, list],
               slices: Mapping[str, list], *, label: str,
               split_side: Mapping[str, str]) -> dict:
    """Prompt-selection metrics on one split (not a global accuracy)."""
    by_id = {s.case.case_id: s for s in snapshots}
    rows = [by_id[i] for i in ids]
    correct = {i: results[i].correct for i in ids if i in results}
    tags = {i: set(by_id[i].all_tags) for i in ids}
    general = [i for i in ids if "gs_expected:general" in tags[i]]
    specific = [i for i in ids if "gs_expected:specific" in tags[i]]
    near = [i for i in ids if "near_qna" in tags[i]]
    easy = [i for i in ids if "easy_control" in membership.get(i, [])]
    idset = set(ids)
    return {
        "label": label,
        "cases": len(ids),
        "completed": len(correct),
        "exact": _acc(ids, correct),
        "selector_value": selector_value(rows, correct),
        "false_none": sum(1 for i in ids if i in results and results[i].outcome.value == "FALSE_NONE"),
        "invalid_or_error": sum(1 for i in ids if i in results and results[i].outcome.value in
                                ("INVALID_OUTPUT", "MODEL_ERROR", "TIMEOUT")),
        "general_expected": _acc(general, correct),
        "specific_expected": _acc(specific, correct),
        "near_qna": _acc(near, correct),
        "easy_control": {**_acc(easy, correct),
                         "corruption": selector_value([by_id[i] for i in easy], correct)["corruption_count"]},
        "taxonomy_slices": {name: _acc([i for i in members if i in idset], correct)
                            for name, members in slices.items()},
        "critical_cases": {cid: {"split": split_side.get(cid),
                                 "in_this_set": cid in idset,
                                 "outcome": results[cid].outcome.value
                                 if cid in idset and cid in results else None}
                           for cid in CRITICAL_CASES},
        "gs_case_correctness": {i: correct.get(i) for i in general + specific},
        "notes": [
            "GOLD_ALIAS_QUESTIONABLE / CONTRACT_MISMATCH gains mean more agreement with the "
            "alias-derived Gold, not necessarily a production semantic improvement",
            "benchmark score is unchanged by taxonomy; slices are diagnostic only",
        ],
    }


def first_candidate_results(snapshots: Sequence[CaseSnapshot], ids: Sequence[str]) -> dict:
    by_id = {s.case.case_id: s for s in snapshots}
    return {i: first_candidate_correct(by_id[i]) for i in ids}


def selection_gate(variant: dict, production: dict) -> dict:
    """Minimum safety conditions for carrying a prompt to HOLDOUT. Not a winner."""
    pv, bv = variant["selector_value"], production["selector_value"]
    gs_regressions = sorted(
        (cid for cid, ok in production["gs_case_correctness"].items()
         if ok and variant["gs_case_correctness"].get(cid) is False), key=_case_key)
    checks = {
        "complete": variant["completed"] == variant["cases"],
        "net_corrections >= 0": pv["net_corrections"] >= 0,
        "corruption < production": pv["corruption_count"] < bv["corruption_count"],
        "false_none < production": variant["false_none"] < production["false_none"],
        "no general/specific regression vs production": not gs_regressions,
    }
    return {"checks": checks, "passed": all(checks.values()),
            "general_specific_regressions": gs_regressions, "winner": None,
            "note": "experiment gate only; several passing variants → human review; "
                    "none passing → no HOLDOUT run"}


# ── plans and gates ────────────────────────────────────────────────────────

def plan_fingerprint(plan: dict) -> str:
    return fingerprint({k: v for k, v in plan.items() if k not in ("plan_fingerprint", "created_at")})


def build_dev_plan(*, snapshot_manifest: dict, challenge_manifest: dict, split: dict,
                   serializer_fp: str, config, prompts: Sequence[PromptSpec],
                   production_prompt: PromptSpec, dev_snapshots: Sequence[CaseSnapshot],
                   baseline_run_id: str, calibration: Optional[dict] = None) -> dict:
    configs, total_in, total_out = [], 0, 0
    for prompt in prompts:
        est = estimate(dev_snapshots, max_tokens=config.max_tokens, primary_only=True,
                       system_prompt=prompt.text)
        calibrated = (round(est["input_tokens"]["total"] * calibration["factor"])
                      if calibration else None)
        configs.append({
            "prompt_id": prompt.prompt_id,
            "prompt_fingerprint": prompt.fingerprint,
            "calls": est["cases"],
            "estimated_input_tokens": est["input_tokens"],
            "estimated_input_tokens_stage_a_calibrated": calibrated,
            "estimated_output_tokens": est["output_tokens_estimated"],
            "output_tokens_upper_bound": est["output_tokens_upper_bound_total"],
            "cost": PRICE_REQUIRED,
        })
        total_in += est["input_tokens"]["total"]
        total_out += est["output_tokens_estimated"]["total"]
    plan = {
        "plan_kind": DEV_PLAN_KIND,
        "live": False,
        "approved": False,
        "provider": config.provider,
        "model": config.model,
        "reasoning": None if config.reasoning_effort is None else config.reasoning_effort.value,
        "temperature": config.temperature,
        "max_tokens": config.max_tokens,
        "config_fingerprint": config.fingerprint,
        "dataset": "DEV",
        "case_count": len(split["dev"]),
        "dev_case_ids": split["dev"],
        "snapshot_fingerprint": snapshot_manifest["snapshot_fingerprint"],
        "challenge_fingerprint": challenge_manifest["challenge_fingerprint"],
        "split_fingerprint": split["split_fingerprint"],
        "serializer_contract_fingerprint": serializer_fp,
        "production_prompt_fingerprint": production_prompt.fingerprint,
        "configs": configs,
        "calls": {**{c["prompt_id"]: c["calls"] for c in configs},
                  "total": sum(c["calls"] for c in configs)},
        "production_baseline_calls": 0,
        "production_baseline_source": f"Stage A run {baseline_run_id} (reused, not rerun)",
        "holdout_calls": 0,
        "estimated_tokens_total": {"input": total_in, "output": total_out,
                                   "approximate": True, "calibration": calibration},
        "cost": PRICE_REQUIRED,
        "provider_policy": "OpenRouter only",
    }
    plan["plan_fingerprint"] = plan_fingerprint(plan)
    return plan


def validate_dev_plan(path: Path, approved_fp: Optional[str], *, snapshot_fp: str, split_fp: str,
                      serializer_fp: str, prompt: PromptSpec, config) -> dict:
    if not path or not approved_fp:
        raise PlanApprovalError("prompt DEV live run requires --live-plan and --approve-plan-fingerprint")
    plan = json.loads(Path(path).read_text(encoding="utf-8"))
    actual = plan_fingerprint(plan)
    checks = {
        "not a DEV prompt plan": plan.get("plan_kind") != DEV_PLAN_KIND,
        "plan file modified": actual != plan.get("plan_fingerprint"),
        "approved fingerprint differs": approved_fp != actual,
        "snapshot differs": plan.get("snapshot_fingerprint") != snapshot_fp,
        "split differs": plan.get("split_fingerprint") != split_fp,
        "serializer contract differs": plan.get("serializer_contract_fingerprint") != serializer_fp,
        "config differs": plan.get("config_fingerprint") != config.fingerprint,
        "prompt not in plan": prompt.fingerprint not in {
            c["prompt_fingerprint"] for c in plan.get("configs", [])},
    }
    failed = [name for name, bad in checks.items() if bad]
    if failed:
        raise PlanApprovalError(f"prompt DEV plan refused: {', '.join(failed)}")
    return plan


def holdout_guard(*, prompt: PromptSpec, selected_winner: Optional[str],
                  gate_report_path: Optional[Path], split_fp: str, live: bool,
                  plan_path: Optional[Path], approved_fp: Optional[str]) -> None:
    """HOLDOUT runs only for the human-selected, gate-passing prompt, and
    (live) only under its own explicitly approved HOLDOUT plan."""
    if prompt.prompt_id == PRODUCTION:
        raise SystemExit("HOLDOUT refused: production HOLDOUT results already exist (Stage A)")
    if not selected_winner:
        raise SystemExit("HOLDOUT refused: --selected-dev-winner is required")
    if selected_winner != prompt.prompt_id:
        raise SystemExit("HOLDOUT refused: --selected-dev-winner differs from --prompt")
    if not gate_report_path or not Path(gate_report_path).exists():
        raise SystemExit("HOLDOUT refused: --dev-gate-report (DEV evaluation of this prompt) required")
    report = json.loads(Path(gate_report_path).read_text(encoding="utf-8"))
    if report.get("prompt_fingerprint") != prompt.fingerprint or \
            report.get("split_fingerprint") != split_fp:
        raise SystemExit("HOLDOUT refused: gate report belongs to another prompt/split")
    if not (report.get("gate") or {}).get("passed"):
        raise SystemExit("HOLDOUT refused: this prompt did not pass the DEV selection gate")
    if live:
        if not plan_path or not approved_fp:
            raise SystemExit("HOLDOUT refused: live HOLDOUT needs its own approved plan")
        plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
        if plan.get("plan_kind") != HOLDOUT_PLAN_KIND or plan_fingerprint(plan) != approved_fp \
                or plan.get("prompt_fingerprint") != prompt.fingerprint:
            raise SystemExit("HOLDOUT refused: HOLDOUT plan/approval mismatch")


# ── human-review prompt diff ───────────────────────────────────────────────

SEMANTIC_DIFF = {
    "production -> variant_a_v1": [
        "Role: 'verify, not find most similar' → 'select the candidate meeting the user's need'",
        "ADDED: short/informal/misspelled messages are normal; no word-by-word or detail-by-detail match required",
        "REPLACED rules 1-6 (all must hold) by 4 selection rules: same topic; stated qualifier → "
        "matching candidate (never contradict it); unstated qualifier → prefer general, never assume; "
        "several fit → most direct one",
        "CHANGED NONE: only when no candidate addresses the topic / every candidate answers a clearly "
        "different need / message states no topic",
        "REMOVED: concrete 'yatay geçiş / merkezi' example (benchmark wording)",
        "UNCHANGED: Calendar rule, candidate order meaningless, single choice, output contract",
    ],
    "production -> variant_b_v1": [
        "UNCHANGED: verifier role, rules 1-6, general/specific rule (abstract form)",
        "ADDED: short/informal/misspelled messages are normal; no word-by-word or detail-by-detail match required",
        "ADDED: explicit, narrower NONE criterion (same text as variant A)",
        "REMOVED: concrete 'yatay geçiş / merkezi' example (benchmark wording)",
        "UNCHANGED: Calendar rule, candidate order, single choice, output contract",
    ],
    "variant_b_v1 -> variant_a_v1": [
        "A drops the strict all-conditions verifier checklist (answer must cover the real need / centre)",
        "A adds the explicit 'user-stated qualifier → matching candidate' direction",
        "A frames the task as need-satisfaction instead of verification",
        "Same: short-message tolerance, NONE criterion, unstated-qualifier protection, contract",
    ],
}


def prompt_diff_markdown(prompts: Mapping[str, PromptSpec]) -> str:
    lines = ["# Selector prompt diff (benchmark-only variants)", ""]
    for key, items in SEMANTIC_DIFF.items():
        lines += [f"## {key} — semantic", ""] + [f"- {item}" for item in items] + [""]
    for a, b in (("production", "variant_a_v1"), ("production", "variant_b_v1"),
                 ("variant_b_v1", "variant_a_v1")):
        diff = difflib.unified_diff(prompts[a].text.splitlines(), prompts[b].text.splitlines(),
                                    fromfile=a, tofile=b, lineterm="")
        lines += [f"## {a} -> {b} — raw", "", "```diff", *diff, "```", ""]
    return "\n".join(lines)
