"""Phase 7B: human-selected Variant A on HOLDOUT, scored on Semantic Gold V1.

Order is enforced:

1. ``load_semantic_gold`` re-verifies the child Gold (artifact hashes and a
   recomputed fingerprint).
2. ``build_prelive_baseline`` freezes — BEFORE any live call — the production
   and first-candidate semantic HOLDOUT scores (from the saved Stage A run),
   the semantic-evaluable HOLDOUT ids and the promotion gate definition.
3. ``build_holdout_plan`` / ``validate_holdout_plan`` bind the live run to
   OpenRouter, variant_a_v1, HOLDOUT, 42 logical calls and the frozen baseline;
   ``BudgetedBackend`` refuses a call beyond the budget.
4. ``evaluate_holdout`` re-verifies the frozen baseline, re-scores the saved
   Variant A outputs on the Semantic Gold and applies the frozen gate.

All 42 HOLDOUT cases are CALLED (parent snapshot: every one is evaluable);
Semantic Gold exclusions only leave the scoring denominator.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Mapping, Optional, Sequence

from benchmarks.selector_v2 import prompt_experiment as px
from benchmarks.selector_v2 import semantic_gold as sg
from benchmarks.selector_v2.challenge import first_candidate_correct, first_candidate_ref, paired_bool
from benchmarks.selector_v2.contract import fingerprint, sha256_text
from benchmarks.selector_v2.plan import PRICE_REQUIRED, PlanApprovalError
from benchmarks.selector_v2.postmortem import _case_key
from benchmarks.selector_v2.schema import BenchmarkCase, BenchmarkResult, CaseSnapshot
from benchmarks.selector_v2.snapshot import describe

SELECTED_PROMPT_ID = "variant_a_v1"
SELECTED_PROMPT_FINGERPRINT = "1aed568885db02f474534224695a45eb6f95835bc31f94e877af9659efe94a1e"
HOLDOUT_SIZE = 42
MAX_LOGICAL_CALLS = 42
LIVE_PROVIDER = "openrouter"
BASELINE_VERSION = "selector-semantic-holdout-baseline-v1"
PLAN_KIND = px.HOLDOUT_PLAN_KIND
BASELINE_FILE = "prelive-baseline.json"
PLAN_FILE = "live-plan.json"
SEMANTIC_GATE_FILE = "dev-gate-report-semantic.json"
ERROR_OUTCOMES = ("INVALID_OUTPUT", "MODEL_ERROR", "TIMEOUT")

# Frozen before the live call (part of the baseline fingerprint).
PROMOTION_GATE = {
    "version": "selector-holdout-promotion-gate-v1",
    "comparator": "production selector (Stage A saved outputs), NOT first-candidate",
    "denominator": "semantic-evaluable HOLDOUT cases (Semantic Gold V1), same ids for both",
    "hard_checks": [
        "run_complete == true",
        "invalid_output == 0",
        "model_error == 0",
        "timeout == 0",
        "variant_a_exact > production_exact",
        "paired_net_vs_production > 0 (only_variant_a_correct - only_production_correct)",
        "no regression on production-correct general/specific HOLDOUT cases",
        "critical cases 471 and 472 correct under Semantic Gold",
    ],
    "not_gating": [
        "McNemar p-value (descriptive; small HOLDOUT)",
        "first-candidate rescue/corruption (historical diagnostic)",
        "historical DEV selection gate (reported, not sufficient)",
        "NONE recall (not measurable without expected-NONE cases)",
    ],
    "critical_cases": list(px.CRITICAL_CASES),
    "pass_label": "VARIANT_A_HOLDOUT = PASS; SELECTOR_PROMPT_CANDIDATE = variant_a_v1 (no deploy)",
    "fail_label": "VARIANT_A_HOLDOUT = FAIL (prompt not changed)",
}


# ── Semantic Gold V1 (re-verified) ─────────────────────────────────────────

def load_semantic_gold(gold_dir: Path, expected_fp: Optional[str] = None) -> tuple[dict, list[BenchmarkCase]]:
    gold_dir = Path(gold_dir)
    manifest = json.loads((gold_dir / "manifest.json").read_text(encoding="utf-8"))
    for name, digest in manifest["artifact_sha256"].items():
        if sha256_text(sg._read_exact(gold_dir / name)) != digest:
            raise SystemExit(f"semantic gold artifact {name} changed")
    body = sg._read_exact(gold_dir / "semantic-gold.jsonl")
    cases = [BenchmarkCase.model_validate_json(line) for line in body.splitlines() if line.strip()]
    recomputed = fingerprint({"version": sg.SEMANTIC_GOLD_VERSION,
                              "lock": manifest["review_lock_fingerprint"],
                              "parent": manifest["parent_snapshot_fingerprint"],
                              "cases": [c.model_dump(mode="json") for c in cases]})
    if recomputed != manifest["semantic_gold_fingerprint"]:
        raise SystemExit("semantic gold fingerprint does not match its cases")
    if expected_fp and recomputed != expected_fp:
        raise SystemExit(f"semantic gold fingerprint {recomputed} != expected {expected_fp}")
    return manifest, cases


def load_changes(gold_dir: Path) -> dict[str, dict]:
    text = sg._read_exact(Path(gold_dir) / "changes.jsonl")
    return {r["case_id"]: r for r in (json.loads(line) for line in text.splitlines() if line.strip())}


# ── scoring helpers ────────────────────────────────────────────────────────

def _outcomes(ids: Sequence[str], results: Mapping[str, BenchmarkResult]) -> dict[str, int]:
    out: dict[str, int] = {}
    for i in ids:
        if i in results:
            key = results[i].outcome.value
            out[key] = out.get(key, 0) + 1
    return out


def decision_block(ids: Sequence[str], results: Mapping[str, BenchmarkResult],
                   sem_by: Mapping[str, CaseSnapshot]) -> dict:
    """Exact + NONE/SELECT breakdown on the semantic-evaluable ids."""
    outcomes = _outcomes(ids, results)
    done = [i for i in ids if i in results]
    correct = sum(results[i].correct for i in done)
    expected_none = [i for i in ids if sem_by[i].case.expected_decision == "NONE"]
    return {
        "denominator": len(ids),
        "completed": len(done),
        "exact": correct,
        "accuracy": round(correct / len(ids), 6) if ids else None,
        "correct_select": outcomes.get("CORRECT_SELECT", 0),
        "wrong_select": outcomes.get("WRONG_SELECT", 0),
        "false_select_on_expected_none": outcomes.get("FALSE_SELECT", 0),
        "false_none": outcomes.get("FALSE_NONE", 0),
        "correct_none": outcomes.get("CORRECT_NONE", 0),
        "none_count": outcomes.get("FALSE_NONE", 0) + outcomes.get("CORRECT_NONE", 0),
        "expected_none_count": len(expected_none),
        "none_recall": (round(outcomes.get("CORRECT_NONE", 0) / len(expected_none), 6)
                        if expected_none else "NOT_MEASURABLE"),
        "invalid_output": outcomes.get("INVALID_OUTPUT", 0),
        "model_error": outcomes.get("MODEL_ERROR", 0),
        "timeout": outcomes.get("TIMEOUT", 0),
        "outcomes": outcomes,
    }


def case_rows(ids: Sequence[str], results: Mapping[str, BenchmarkResult]) -> dict[str, dict]:
    return {i: {"decision": results[i].decision, "selected": results[i].selected_candidate_ref,
                "outcome": results[i].outcome.value, "correct": results[i].correct}
            for i in ids if i in results}


def slice_ids(sem_by: Mapping[str, CaseSnapshot], ids: Sequence[str],
              membership: Mapping[str, list]) -> dict[str, list[str]]:
    tags = {i: set(sem_by[i].all_tags) for i in ids}
    return {
        "general_expected": [i for i in ids if "gs_expected:general" in tags[i]],
        "specific_expected": [i for i in ids if "gs_expected:specific" in tags[i]],
        "near_qna": [i for i in ids if "near_qna" in tags[i]],
        "kb_overlap": [i for i in ids if "kb_overlap_flagged" in tags[i]],
        "multi_acceptable": [i for i in ids if sem_by[i].case.multi_acceptable],
        "easy_control": [i for i in ids if "easy_control" in membership.get(i, [])],
    }


def _slice_acc(ids: Sequence[str], correct: Mapping[str, bool]) -> dict:
    ok = sum(1 for i in ids if correct.get(i))
    return {"cases": len(ids), "correct": ok, "accuracy": round(ok / len(ids), 6) if ids else None}


# ── 1. pre-live baseline freeze ────────────────────────────────────────────

def build_prelive_baseline(*, snapshots: Sequence[CaseSnapshot], sem: Sequence[CaseSnapshot],
                           split: dict, production: Mapping[str, BenchmarkResult],
                           membership: Mapping[str, list], identity: dict) -> dict:
    """Production + first-candidate semantic HOLDOUT baseline (no variant output read)."""
    holdout = list(split["holdout"])
    if len(holdout) != HOLDOUT_SIZE:
        raise SystemExit(f"HOLDOUT has {len(holdout)} cases, expected {HOLDOUT_SIZE}")
    parent_by = {s.case.case_id: s for s in snapshots}
    sem_by = {s.case.case_id: s for s in sem}
    not_callable = [i for i in holdout if not parent_by[i].selector_evaluable]
    if not_callable:
        raise SystemExit(f"HOLDOUT cases not evaluable in the parent snapshot: {not_callable}")
    evaluable = [i for i in holdout if sem_by[i].selector_evaluable]
    excluded = {i: sem_by[i].case.status_reason or sem_by[i].pool_status.value
                for i in holdout if i not in evaluable}
    prod_sem = sg.rescore_run(sem, production)
    missing = [i for i in evaluable if i not in prod_sem]
    if missing:
        raise SystemExit(f"production Stage A outputs missing for {missing}")
    fc = {i: first_candidate_correct(sem_by[i]) for i in evaluable}
    slices = slice_ids(sem_by, evaluable, membership)
    body = {
        "version": BASELINE_VERSION,
        "identity": identity,
        "holdout_case_ids": holdout,
        "holdout_total": len(holdout),
        "semantic_evaluable_ids": evaluable,
        "semantic_evaluable_count": len(evaluable),
        "excluded": excluded,
        "excluded_count": len(excluded),
        "production": decision_block(evaluable, prod_sem, sem_by),
        "production_cases": case_rows(evaluable, prod_sem),
        "first_candidate": {"exact": sum(fc.values()), "denominator": len(evaluable),
                            "accuracy": round(sum(fc.values()) / len(evaluable), 6) if evaluable else None,
                            "cases": {i: {"ref": first_candidate_ref(sem_by[i]), "correct": fc[i]}
                                      for i in evaluable}},
        "slice_membership": slices,
        "production_slices": {k: _slice_acc(v, {i: prod_sem[i].correct for i in v})
                              for k, v in slices.items()},
        "critical_cases_production": {cid: case_rows([cid], prod_sem).get(cid)
                                      for cid in px.CRITICAL_CASES},
        "promotion_gate": PROMOTION_GATE,
        "historical_dev_gate_checks": list(px.selection_gate(
            _gate_view(evaluable, prod_sem, sem_by, slices),
            _gate_view(evaluable, prod_sem, sem_by, slices))["checks"]),
        "variant_outputs_read": False,
    }
    return {**body, "baseline_fingerprint": fingerprint(body)}


def verify_baseline(path: Path) -> dict:
    baseline = json.loads(Path(path).read_text(encoding="utf-8"))
    body = {k: v for k, v in baseline.items() if k not in ("baseline_fingerprint", "frozen_at")}
    if fingerprint(body) != baseline.get("baseline_fingerprint"):
        raise SystemExit("pre-live baseline changed after freeze")
    return baseline


def _gate_view(ids, results, sem_by, slices) -> dict:
    """The fields px.selection_gate reads (historical DEV gate, reported only)."""
    correct = {i: results[i].correct for i in ids if i in results}
    from benchmarks.selector_v2.challenge import selector_value

    return {"cases": len(ids), "completed": len(correct),
            "selector_value": selector_value([sem_by[i] for i in ids], correct),
            "false_none": sum(1 for i in ids if i in results and results[i].outcome.value == "FALSE_NONE"),
            "gs_case_correctness": {i: correct.get(i) for i in
                                    slices["general_expected"] + slices["specific_expected"]}}


# ── 2. plan, approval and call budget ──────────────────────────────────────

def semantic_gate_report(rescore: dict, prompt, split_fp: str, gold_fp: str) -> dict:
    """DEV selection gate recomputed on Semantic Gold, in the shape holdout_guard reads."""
    if rescore.get("semantic_gold_fingerprint") != gold_fp:
        raise SystemExit("semantic rescore belongs to another Semantic Gold")
    gate = rescore["gates"].get(prompt.prompt_id)
    if gate is None:
        raise SystemExit(f"no semantic DEV gate for {prompt.prompt_id}")
    return {"prompt_id": prompt.prompt_id, "prompt_fingerprint": prompt.fingerprint,
            "split_fingerprint": split_fp, "semantic_gold_fingerprint": gold_fp,
            "gate": gate, "source": "semantic-rescore-v1/semantic-rescore.json (DEV, 77 cases)",
            "note": "the old-Gold DEV gate report (gate.passed=false) is kept unchanged"}


def build_holdout_plan(*, config, prompt, split: dict, baseline: dict, snapshot_fp: str,
                       challenge_fp: str, serializer_fp: str, gold_fp: str,
                       estimate_block: dict, human_selection: str) -> dict:
    plan = {
        "plan_kind": PLAN_KIND,
        "live": True,
        "provider": config.provider,
        "model": config.model,
        "reasoning": None if config.reasoning_effort is None else config.reasoning_effort.value,
        "temperature": config.temperature,
        "max_tokens": config.max_tokens,
        "config_fingerprint": config.fingerprint,
        "prompt_id": prompt.prompt_id,
        "prompt_fingerprint": prompt.fingerprint,
        "human_selection": human_selection,
        "dataset": "HOLDOUT",
        "holdout_case_ids": list(split["holdout"]),
        "case_count": len(split["holdout"]),
        "logical_calls": len(split["holdout"]),
        "max_logical_calls": MAX_LOGICAL_CALLS,
        "retries": "none (errors are reported, never retried)",
        "calls_by_config": {prompt.prompt_id: len(split["holdout"]), "production": 0,
                            "variant_b_v1": 0, "stage_b": 0},
        "snapshot_fingerprint": snapshot_fp,
        "challenge_fingerprint": challenge_fp,
        "split_fingerprint": split["split_fingerprint"],
        "serializer_contract_fingerprint": serializer_fp,
        "semantic_gold_fingerprint": gold_fp,
        "prelive_baseline_fingerprint": baseline["baseline_fingerprint"],
        "promotion_gate_version": PROMOTION_GATE["version"],
        "estimated_tokens": estimate_block,
        "cost": PRICE_REQUIRED,
        "provider_policy": "OpenRouter only",
    }
    plan["plan_fingerprint"] = px.plan_fingerprint(plan)
    return plan


def validate_holdout_plan(plan: dict, approved_fp: Optional[str], *, config, prompt, split: dict,
                          baseline: dict, snapshot_fp: str, serializer_fp: str, gold_fp: str) -> None:
    if not approved_fp:
        raise PlanApprovalError("HOLDOUT live run requires --approve-plan-fingerprint")
    checks = {
        "not a HOLDOUT plan": plan.get("plan_kind") != PLAN_KIND,
        "plan file modified": px.plan_fingerprint(plan) != plan.get("plan_fingerprint"),
        "approved fingerprint differs": approved_fp != plan.get("plan_fingerprint"),
        "provider is not openrouter": config.provider != LIVE_PROVIDER or plan.get("provider") != LIVE_PROVIDER,
        "config differs": plan.get("config_fingerprint") != config.fingerprint,
        "prompt is not variant_a_v1": prompt.prompt_id != SELECTED_PROMPT_ID
        or plan.get("prompt_id") != SELECTED_PROMPT_ID,
        "prompt fingerprint differs": prompt.fingerprint != SELECTED_PROMPT_FINGERPRINT
        or plan.get("prompt_fingerprint") != SELECTED_PROMPT_FINGERPRINT,
        "not the HOLDOUT split": plan.get("holdout_case_ids") != list(split["holdout"])
        or plan.get("split_fingerprint") != split["split_fingerprint"],
        "HOLDOUT size differs": len(split["holdout"]) != HOLDOUT_SIZE,
        "call budget differs": plan.get("logical_calls") != HOLDOUT_SIZE
        or plan.get("max_logical_calls") != MAX_LOGICAL_CALLS,
        "other configs planned": any(v for k, v in plan.get("calls_by_config", {}).items()
                                     if k != SELECTED_PROMPT_ID),
        "snapshot differs": plan.get("snapshot_fingerprint") != snapshot_fp,
        "serializer contract differs": plan.get("serializer_contract_fingerprint") != serializer_fp,
        "semantic gold differs": plan.get("semantic_gold_fingerprint") != gold_fp,
        "baseline not frozen / differs": plan.get("prelive_baseline_fingerprint")
        != baseline.get("baseline_fingerprint"),
    }
    failed = [name for name, bad in checks.items() if bad]
    if failed:
        raise PlanApprovalError(f"HOLDOUT plan refused: {', '.join(failed)}")


class CallBudgetExceeded(RuntimeError):
    """A logical selector call beyond the approved budget."""


class BudgetedBackend:
    """Counts logical selector invocations and refuses any beyond the budget."""

    def __init__(self, backend, max_calls: int, allowed_case_ids: set):
        self.backend = backend
        self.max_calls = max_calls
        self.allowed = set(allowed_case_ids)
        self.calls = 0
        self.refused = 0
        self._lock = threading.Lock()

    def select(self, snapshot: CaseSnapshot, candidates):
        with self._lock:
            if snapshot.case.case_id not in self.allowed:
                self.refused += 1
                raise CallBudgetExceeded(f"case {snapshot.case.case_id} is not in the approved HOLDOUT")
            if self.calls >= self.max_calls:
                self.refused += 1
                raise CallBudgetExceeded(f"call budget {self.max_calls} exhausted")
            self.calls += 1
        return self.backend.select(snapshot, candidates)


# ── 3. evaluation ──────────────────────────────────────────────────────────

def _qualifier(text: Optional[str], word: str = "merkez") -> bool:
    return bool(text) and word in text.replace("İ", "i").replace("I", "ı").casefold()


def critical_case(cid: str, sem_by, parent_by, changes, prod_sem, var_sem) -> dict:
    snap = sem_by[cid]
    texts = {c.candidate_ref: c.canonical_text for c in snap.candidates}
    user = snap.case.intent_text

    def side(results):
        r = results.get(cid)
        if r is None:
            return None
        ref = r.selected_candidate_ref
        return {"decision": r.decision, "selected": ref, "selected_text": texts.get(ref),
                "outcome": r.outcome.value, "correct": r.correct,
                "selected_mentions_merkez": _qualifier(texts.get(ref))}

    return {
        "case_id": cid,
        "user_text": user,
        "user_mentions_merkez": _qualifier(user),
        "semantic_acceptable_refs": {r: texts.get(r) for r in snap.case.acceptable_candidate_refs},
        "gold_provenance": (f"adjudicated in the semantic review ({changes[cid]['derived_outcome']})"
                            if cid in changes else
                            "inherited unchanged from the parent reviewed Gold (not re-adjudicated)"),
        "candidates": [{"ref": c.candidate_ref, "order": c.order, "text": c.canonical_text,
                        "mentions_merkez": _qualifier(c.canonical_text)}
                       for c in sorted(snap.candidates, key=lambda c: c.order)],
        "production": side(prod_sem),
        "variant_a": side(var_sem),
    }


def evaluate_holdout(*, baseline: dict, snapshots: Sequence[CaseSnapshot], sem: Sequence[CaseSnapshot],
                     production: Mapping[str, BenchmarkResult], variant: Mapping[str, BenchmarkResult],
                     membership: Mapping[str, list], changes: Mapping[str, dict]) -> dict:
    holdout = baseline["holdout_case_ids"]
    evaluable = baseline["semantic_evaluable_ids"]
    sem_by = {s.case.case_id: s for s in sem}
    parent_by = {s.case.case_id: s for s in snapshots}
    prod_sem = sg.rescore_run(sem, production)
    var_sem = sg.rescore_run(sem, variant)
    # The frozen baseline must be reproduced exactly by today's scorer.
    if decision_block(evaluable, prod_sem, sem_by) != baseline["production"] or \
            case_rows(evaluable, prod_sem) != baseline["production_cases"]:
        raise SystemExit("production baseline no longer reproduces the frozen pre-live values")
    completed = [i for i in holdout if i in variant]
    extra = sorted(set(variant) - set(holdout), key=_case_key)
    variant_block = decision_block(evaluable, var_sem, sem_by)
    all_validity = decision_block(holdout, {i: variant[i] for i in completed},
                                  {i: parent_by[i] for i in holdout})
    prod_ok = {i: prod_sem[i].correct for i in evaluable}
    var_ok = {i: var_sem[i].correct for i in evaluable if i in var_sem}
    paired = paired_bool(prod_ok, var_ok, evaluable)
    paired_net = paired["only_b_correct"] - paired["only_a_correct"]
    slices = baseline["slice_membership"]
    gs_ids = slices["general_expected"] + slices["specific_expected"]
    gs_regressions = sorted((i for i in gs_ids if prod_ok.get(i) and var_ok.get(i) is False), key=_case_key)
    prod_to_wrong = sorted((i for i in evaluable if prod_ok.get(i) and var_ok.get(i) is False), key=_case_key)
    critical = {cid: critical_case(cid, sem_by, parent_by, changes, prod_sem, var_sem)
                for cid in px.CRITICAL_CASES}
    critical_ok = {cid: bool(c["variant_a"] and c["variant_a"]["correct"]) for cid, c in critical.items()}
    run_complete = len(completed) == len(holdout) and not extra
    checks = {
        "run_complete": run_complete,
        "invalid_output == 0": all_validity["invalid_output"] == 0,
        "model_error == 0": all_validity["model_error"] == 0,
        "timeout == 0": all_validity["timeout"] == 0,
        "variant_a_exact > production_exact": variant_block["exact"] > baseline["production"]["exact"],
        "paired_net_vs_production > 0": paired_net > 0,
        "no general/specific regression": not gs_regressions,
        "critical 471/472 correct": all(critical_ok.values()),
    }
    from benchmarks.selector_v2.challenge import selector_value

    fc_value = {name: selector_value([sem_by[i] for i in evaluable], ok)
                for name, ok in (("production", prod_ok), ("variant_a", var_ok))}
    historical = px.selection_gate(_gate_view(evaluable, var_sem, sem_by, slices),
                                   _gate_view(evaluable, prod_sem, sem_by, slices))
    usage_rows = [variant[i] for i in completed]

    def values(attr):
        return [getattr(r, attr) for r in usage_rows if getattr(r, attr) is not None]

    return {
        "holdout_total": len(holdout),
        "semantic_evaluable": len(evaluable),
        "excluded": baseline["excluded"],
        "variant_calls_completed": len(completed),
        "unexpected_case_ids": extra,
        "production": baseline["production"],
        "variant_a": variant_block,
        "validity_all_42": {k: all_validity[k] for k in
                            ("completed", "invalid_output", "model_error", "timeout", "outcomes")},
        "first_candidate": {k: baseline["first_candidate"][k] for k in ("exact", "denominator", "accuracy")},
        "accuracy_delta_vs_production": round(
            (variant_block["exact"] - baseline["production"]["exact"]) / len(evaluable), 6),
        "exact_delta_vs_production": variant_block["exact"] - baseline["production"]["exact"],
        "paired_vs_production": {"both_correct": paired["both_correct"],
                                 "only_production_correct": paired["only_a_correct"],
                                 "only_variant_a_correct": paired["only_b_correct"],
                                 "both_wrong": paired["both_wrong"],
                                 "paired_net": paired_net,
                                 "mcnemar_exact_p": paired["mcnemar_exact_p_two_sided"],
                                 "note": "p is descriptive; the gate uses paired_net"},
        "first_candidate_value": fc_value,
        "slices": {k: {"cases": len(v), "production": _slice_acc(v, prod_ok),
                       "variant_a": _slice_acc(v, var_ok), "ids": v} for k, v in slices.items()},
        "general_specific_regressions": gs_regressions,
        "production_correct_variant_wrong": prod_to_wrong,
        "production_wrong_variant_correct": sorted(
            (i for i in evaluable if prod_ok.get(i) is False and var_ok.get(i)), key=_case_key),
        "critical_cases": critical,
        "historical_dev_gate_on_holdout": historical,
        "promotion_gate": {"definition_version": PROMOTION_GATE["version"], "checks": checks,
                           "passed": all(checks.values()),
                           "general_specific_counts": {"general": len(slices["general_expected"]),
                                                       "specific": len(slices["specific_expected"])}},
        "usage": {"input_tokens": describe(values("input_tokens")),
                  "output_tokens": describe(values("output_tokens")),
                  "latency_ms": describe(values("latency_ms")),
                  "usage_coverage": f"{len(values('input_tokens'))}/{len(usage_rows)}",
                  "cost": PRICE_REQUIRED},
        "scored_rows": [{"case_id": i, "split": "HOLDOUT", "semantic_evaluable": i in evaluable,
                         "exclusion": baseline["excluded"].get(i),
                         "variant_a": case_rows([i], var_sem).get(i)
                         or {"decision": variant[i].decision, "selected": variant[i].selected_candidate_ref,
                             "outcome_vs_parent_gold": variant[i].outcome.value,
                             "correct": None},
                         "production": baseline["production_cases"].get(i),
                         "first_candidate": baseline["first_candidate"]["cases"].get(i)}
                        for i in completed],
    }
