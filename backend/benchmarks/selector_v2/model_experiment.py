"""Phase 7B model-capability experiment: variant_c_v1 × openai/gpt-5.6-luna.

The only intended variable is the model. Declared deviations (from the
capability validation, approved by the user): ``temperature`` is omitted for
a model that does not support it, and ``reasoning`` is sent explicitly as
``none``. Prompt, candidates, candidate order, serializer, parser, Semantic
Gold V1.1, qualifier heuristic and scorer are those of the Variant C run.

Two stages, both gated by pre-frozen definitions:

* M1 — the 36 frozen DEV qualifier-slice cases + diagnostics 471/472 (38).
* M2 — only if the M1 gate passed: the remaining 59 DEV cases (97 in total).
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Mapping, Optional, Sequence

from benchmarks.selector_v2 import prompt_experiment as px
from benchmarks.selector_v2 import qualifier_postmortem as qp
from benchmarks.selector_v2 import semantic_gold as sg
from benchmarks.selector_v2 import variant_c as vc
from benchmarks.selector_v2.challenge import first_candidate_correct, paired_bool
from benchmarks.selector_v2.contract import fingerprint
from benchmarks.selector_v2.plan import PlanApprovalError
from benchmarks.selector_v2.postmortem import _case_key
from benchmarks.selector_v2.schema import BenchmarkResult, CaseSnapshot
from benchmarks.selector_v2.snapshot import describe
from services.llm_types import LLMOutcomeStatus

MODEL = "openai/gpt-5.6-luna"
PROVIDER = "openrouter"
REASONING = "none"
PLAN_KIND = "selector-model-capability-experiment"
EXPLORATORY_PLAN_KIND = "selector-model-exploratory-full-dev"
BASELINE_VERSION = "selector-model-experiment-baseline-v1"
M1_CALLS, M2_CALLS, MAX_CALLS = 38, 59, 97
RUN_NAMES = ("production", "variant_a_v1", "variant_b_v1", "variant_c_v1_4o_mini")
NEW = "variant_c_v1_5_6_luna"

M1_GATE = {
    "version": "selector-model-m1-gate-v1",
    "operational": "38 complete (36 slice + 471 + 472); invalid_output, model_error, timeout all 0",
    "qualifier": "new-model UNSTATED_QUALIFIER_ASSUMED on the frozen 36-case slice <= 2",
    "known_regressions": "471 correct AND 472 correct (Semantic Gold V1.1)",
    "explicit_specific": "0 STATED-slice cases where A or C/4o-mini is correct and the new model is wrong",
    "qualifier_max": 2,
    "on_fail": "MODEL_CAPABILITY_SCREEN = FAIL; FULL_DEV = NOT_RUN; FINAL_VALIDATION = BLOCKED",
}
FULL_GATE = {
    "version": "selector-model-full-dev-gate-v1",
    "operational": "95 DEV + 2 diagnostics complete; invalid_output, model_error, timeout all 0",
    "qualifier": "frozen-slice UNSTATED_QUALIFIER_ASSUMED <= 2",
    "known_regressions": "471 and 472 correct",
    "exact": "exact >= 64/77 (>= 0.83 if the denominator differs)",
    "false_none": "false NONE <= 3",
    "general_specific": "0 production-correct -> new-wrong on the DEV general/specific slices",
    "vs_a": "new exact >= Variant A exact",
    "exact_threshold": {"numerator": 64, "denominator": 77, "ratio": 0.83},
    "false_none_max": 3, "qualifier_max": 2,
    "pass_label": "MODEL_CAPABILITY_DEV = PASS; FINAL_VALIDATION = READY_FOR_PREPARATION",
    "fail_label": "MODEL_CAPABILITY_DEV = FAIL; FINAL_VALIDATION = BLOCKED",
}


# ── baseline freeze ────────────────────────────────────────────────────────

def stage_ids(baseline: Mapping, stage: str, scope: str) -> list[str]:
    slice_ids = list(baseline["qualifier_slice_ids"])
    if scope == "diagnostic":
        if stage != "m1":
            raise SystemExit("diagnostics run only in M1")
        return list(vc.KNOWN_REGRESSIONS)
    if scope != "dev":
        raise SystemExit(f"scope {scope!r} refused (old HOLDOUT full run forbidden)")
    if stage == "m1":
        return slice_ids
    if stage == "m2":
        return [i for i in baseline["dev_case_ids"] if i not in set(slice_ids)]
    raise SystemExit(f"unknown stage {stage!r}")


def build_baseline(*, snapshots, sem, split, runs: Mapping[str, Mapping[str, BenchmarkResult]],
                   c_baseline: Mapping, identity: dict) -> dict:
    sem_by = {s.case.case_id: s for s in sem}
    parent_by = {s.case.case_id: s for s in snapshots}
    ids = vc.dev_ids(sem_by, split)
    qslice = vc.qualifier_slice(snapshots, sem_by, ids)
    if qslice != c_baseline["qualifier_slice_ids"] or ids != c_baseline["dev_evaluable_ids"]:
        raise SystemExit("qualifier slice / DEV ids differ from the Variant C freeze")
    inv = {r["case_id"]: r for r in qp.inventory([parent_by[i] for i in qslice], sem_by, {})}
    stated = [i for i in qslice if inv[i]["mode"] == "STATED"]
    gs = vc.gs_slices(sem_by, parent_by, snapshots, ids)
    rescored = {n: sg.rescore_run(sem, r) for n, r in runs.items()}
    blocks = {n: vc.run_block(n, ids, r, sem_by, qslice, gs) for n, r in rescored.items()}
    known = {cid: {"semantic_gold_v11": sem_by[cid].case.acceptable_candidate_refs,
                   **{n: ({"selected": runs[n][cid].selected_candidate_ref, "decision": runs[n][cid].decision}
                          if cid in runs[n] else None) for n in runs}} for cid in vc.KNOWN_REGRESSIONS}
    fc = {i: first_candidate_correct(sem_by[i]) for i in ids}
    body = {"version": BASELINE_VERSION, "identity": identity,
            "dev_case_ids": list(split["dev"]), "dev_evaluable_ids": ids,
            "qualifier_slice_ids": qslice, "stated_slice_ids": stated, "gs_slices": gs,
            "stage_case_ids": {"m1_dev": qslice, "m1_diagnostic": list(vc.KNOWN_REGRESSIONS),
                               "m2_dev": [i for i in split["dev"] if i not in set(qslice)]},
            "runs": blocks, "known_regressions_saved": known,
            "first_candidate": {"exact": sum(fc.values()), "denominator": len(ids)},
            "heuristic": vc.heuristic_fingerprint(), "m1_gate": M1_GATE, "full_gate": FULL_GATE,
            "new_model_outputs_read": False}
    if len(body["stage_case_ids"]["m1_dev"]) + 2 != M1_CALLS or len(body["stage_case_ids"]["m2_dev"]) != M2_CALLS:
        raise SystemExit("stage sizes differ from 38 / 59")
    return {**body, "baseline_fingerprint": fingerprint(body)}


def verify_baseline(path: Path) -> dict:
    baseline = json.loads(Path(path).read_text(encoding="utf-8"))
    body = {k: v for k, v in baseline.items() if k not in ("baseline_fingerprint", "frozen_at")}
    if fingerprint(body) != baseline.get("baseline_fingerprint"):
        raise SystemExit("model-experiment baseline changed after freeze")
    if baseline["heuristic"] != vc.heuristic_fingerprint():
        raise SystemExit("qualifier heuristic changed after freeze")
    return baseline


# ── plan / guard ───────────────────────────────────────────────────────────

def build_plan(*, config, omitted: Sequence[str], prompt, split, baseline, snapshot_fp, serializer_fp,
               gold_fp, capability_sha256: str) -> dict:
    plan = {
        "plan_kind": PLAN_KIND, "live": True, "provider": config.provider, "model": config.model,
        "reasoning": config.reasoning_effort.value if config.reasoning_effort else None,
        "reasoning_sent_explicitly": True, "max_tokens": config.max_tokens,
        "temperature_config": config.temperature, "omitted_request_params": sorted(omitted),
        "config_fingerprint": config.fingerprint,
        "prompt_id": prompt.prompt_id, "prompt_fingerprint": prompt.fingerprint,
        "candidate_order": "production (original retrieval order)",
        "stages": {"m1": {"dev": baseline["stage_case_ids"]["m1_dev"],
                          "diagnostic": baseline["stage_case_ids"]["m1_diagnostic"], "calls": M1_CALLS},
                   "m2": {"dev": baseline["stage_case_ids"]["m2_dev"], "calls": M2_CALLS,
                          "requires": "m1-gate.json passed"}},
        "max_logical_calls": MAX_CALLS,
        "other_calls": {"production": 0, "variant_a_v1": 0, "variant_b_v1": 0, "variant_c_v1_4o_mini": 0,
                        "old_holdout_full": 0, "order_experiment": 0, "final_validation": 0},
        "retries": "none; fail-fast after the first operational error",
        "snapshot_fingerprint": snapshot_fp, "split_fingerprint": split["split_fingerprint"],
        "serializer_contract_fingerprint": serializer_fp, "semantic_gold_fingerprint": gold_fp,
        "prelive_baseline_fingerprint": baseline["baseline_fingerprint"],
        "capability_validation_sha256": capability_sha256,
        "m1_gate_version": M1_GATE["version"], "full_gate_version": FULL_GATE["version"],
        "provider_policy": "OpenRouter only; no fallback",
    }
    plan["plan_fingerprint"] = px.plan_fingerprint(plan)
    return plan


def validate_plan(plan, approved_fp, *, config, omitted, prompt, split, baseline, snapshot_fp,
                  serializer_fp, gold_fp) -> None:
    if not approved_fp:
        raise PlanApprovalError("model experiment requires --approve-plan-fingerprint")
    checks = {
        "wrong plan kind": plan.get("plan_kind") != PLAN_KIND,
        "plan file modified": px.plan_fingerprint(plan) != plan.get("plan_fingerprint"),
        "approved fingerprint differs": approved_fp != plan.get("plan_fingerprint"),
        "provider is not openrouter": config.provider != PROVIDER or plan.get("provider") != PROVIDER,
        "model differs": config.model != MODEL or plan.get("model") != MODEL,
        "reasoning is not explicit none": config.reasoning_effort is None
        or config.reasoning_effort.value != REASONING,
        "config differs": plan.get("config_fingerprint") != config.fingerprint,
        "omissions differ": sorted(omitted) != plan.get("omitted_request_params"),
        "prompt is not variant_c_v1": prompt.prompt_id != vc.PROMPT_ID
        or plan.get("prompt_fingerprint") != prompt.fingerprint,
        "split differs": plan.get("split_fingerprint") != split["split_fingerprint"],
        "budget differs": plan.get("max_logical_calls") != MAX_CALLS,
        "other calls planned": any(plan.get("other_calls", {}).values()),
        "snapshot differs": plan.get("snapshot_fingerprint") != snapshot_fp,
        "serializer differs": plan.get("serializer_contract_fingerprint") != serializer_fp,
        "gold differs": plan.get("semantic_gold_fingerprint") != gold_fp,
        "baseline differs": plan.get("prelive_baseline_fingerprint") != baseline["baseline_fingerprint"],
    }
    failed = [k for k, bad in checks.items() if bad]
    if failed:
        raise PlanApprovalError(f"model experiment plan refused: {', '.join(failed)}")


class PacedBackend:
    """Paces provider calls (upstream rate limits) without touching scoring."""

    def __init__(self, inner, interval_ms: int = 0):
        self.inner = inner
        self.interval_ms = interval_ms
        self._lock = threading.Lock()

    def select(self, snapshot: CaseSnapshot, candidates):
        if self.interval_ms:
            with self._lock:
                time.sleep(self.interval_ms / 1000)
        return self.inner.select(snapshot, candidates)


class FailFastSkipped(RuntimeError):
    """Marker: the provider was NOT called (fail-fast). Never a provider attempt."""


class FailFastBackend:
    """Stops calling the provider after N consecutive operational errors.

    One transient provider error should not burn a run, but a systemic one
    must not spend the whole budget. Skipped cases raise ``FailFastSkipped``
    and are excluded from the spent-call accounting."""

    OK = (LLMOutcomeStatus.SUCCESS, LLMOutcomeStatus.SEMANTIC_NONE)

    def __init__(self, inner, max_consecutive_errors: int = 3):
        self.inner = inner
        self.max_consecutive_errors = max_consecutive_errors
        self.tripped: Optional[str] = None
        self.attempts = 0
        self.errors: list[str] = []
        self._streak = 0
        self._lock = threading.Lock()

    def select(self, snapshot: CaseSnapshot, candidates):
        with self._lock:
            if self.tripped:
                raise FailFastSkipped(f"provider not called: fail-fast after {self.tripped}")
            self.attempts += 1
        result = self.inner.select(snapshot, candidates)
        with self._lock:
            if result.status in self.OK:
                self._streak = 0
            else:
                self._streak += 1
                self.errors.append(snapshot.case.case_id)
                if self._streak >= self.max_consecutive_errors:
                    self.tripped = f"{self._streak} consecutive errors (last {snapshot.case.case_id})"
        return result


SKIPPED_ERROR_TYPES = ("FailFastSkipped", "RuntimeError")   # RuntimeError: the first fail-fast version


def spent_attempts(results: Mapping[str, BenchmarkResult]) -> int:
    """Provider attempts actually made (fail-fast skips never reached the API)."""
    return sum(1 for r in results.values()
               if not (r.outcome.value == "MODEL_ERROR" and r.error_type in SKIPPED_ERROR_TYPES))


# ── evaluation ─────────────────────────────────────────────────────────────

def _ops(results: Sequence[BenchmarkResult]) -> dict:
    return {k: sum(r.outcome.value == k for r in results) for k in ("INVALID_OUTPUT", "MODEL_ERROR", "TIMEOUT")}


def _known(sem_by, baseline, diagnostics):
    out = {}
    for cid in vc.KNOWN_REGRESSIONS:
        r = diagnostics.get(cid)
        scored = sg.rescore_run([sem_by[cid]], {cid: r}).get(cid) if r else None
        comp = qp.compliance(sem_by[cid], scored) if scored else None
        texts = {c.candidate_ref: c.canonical_text for c in sem_by[cid].candidates}
        out[cid] = {"label": "KNOWN DEVELOPMENT REGRESSION (consumed HOLDOUT; not validation)",
                    "user_text": sem_by[cid].case.intent_text,
                    "semantic_gold_v11": sem_by[cid].case.acceptable_candidate_refs,
                    "decision": r.decision if r else None, "selected": r.selected_candidate_ref if r else None,
                    "selected_text": texts.get(r.selected_candidate_ref) if r else None,
                    "correct": bool(scored and scored.correct),
                    "unstated_qualifier_assumed": bool(comp and comp["label"] == "UNSTATED_QUALIFIER_ASSUMED"),
                    "saved": baseline["known_regressions_saved"][cid]}
    return out


def evaluate_m1(*, baseline, sem, dev: Mapping[str, BenchmarkResult],
                diagnostics: Mapping[str, BenchmarkResult]) -> dict:
    sem_by = {s.case.case_id: s for s in sem}
    qslice = baseline["qualifier_slice_ids"]
    sem_dev = sg.rescore_run([sem_by[i] for i in qslice], {i: dev[i] for i in qslice if i in dev})
    comp = qp.compliance_table(sem_by, [i for i in qslice if i in sem_dev], sem_dev)
    ok = {i: sem_dev[i].correct for i in sem_dev}
    ref_ok = {n: baseline["runs"][n]["correct_by_case"] for n in ("variant_a_v1", "variant_c_v1_4o_mini")}
    spec_regr = {n: sorted((i for i in baseline["stated_slice_ids"]
                            if ref_ok[n].get(i) and ok.get(i) is False), key=_case_key) for n in ref_ok}
    known = _known(sem_by, baseline, diagnostics)
    results = [dev[i] for i in qslice if i in dev] + list(diagnostics.values())
    ops = _ops(results)
    complete = sorted(i for i in dev if i in set(qslice)) == sorted(qslice) and \
        sorted(diagnostics) == sorted(vc.KNOWN_REGRESSIONS)
    checks = {
        "operational": complete and not any(ops.values()),
        "qualifier": comp["counts"]["UNSTATED_QUALIFIER_ASSUMED"] <= M1_GATE["qualifier_max"],
        "known_regressions": all(k["correct"] for k in known.values()),
        "explicit_specific": not any(spec_regr.values()),
    }
    return {"stage": "M1", "slice_size": len(qslice), "completed": len(results),
            "unstated_qualifier_assumed": comp["counts"]["UNSTATED_QUALIFIER_ASSUMED"],
            "unstated_ids": comp["unstated_qualifier_assumed_ids"], "compliance": comp["counts"],
            "slice_exact": sum(ok.values()),
            "reference": {n: {"slice_unstated": baseline["runs"][n]["unstated_qualifier_assumed_slice"],
                              "slice_exact": sum(1 for i in qslice if baseline["runs"][n]["correct_by_case"].get(i))}
                          for n in baseline["runs"]},
            "stated_slice_ids": baseline["stated_slice_ids"], "specific_regressions": spec_regr,
            "known_regressions": known, "operational": {**ops, "complete": complete},
            "gate": {"version": M1_GATE["version"], "checks": checks, "passed": all(checks.values())},
            "usage": usage(results)}


def usage(results: Sequence[BenchmarkResult]) -> dict:
    def values(attr):
        return [getattr(r, attr) for r in results if getattr(r, attr) is not None]

    return {"calls": len(results), "input_tokens": describe(values("input_tokens")),
            "output_tokens": describe(values("output_tokens")), "latency_ms": describe(values("latency_ms")),
            "finish_reasons": sorted({r.finish_reason for r in results if r.finish_reason}),
            "actual_models": sorted({r.actual_model for r in results if r.actual_model})}


def evaluate_full(*, baseline, sem, dev, diagnostics) -> dict:
    sem_by = {s.case.case_id: s for s in sem}
    ids, qslice, gs = baseline["dev_evaluable_ids"], baseline["qualifier_slice_ids"], baseline["gs_slices"]
    new = vc.run_block(NEW, ids, sg.rescore_run(sem, dev), sem_by, qslice, gs)
    blocks = {**baseline["runs"], NEW: new}
    ok = {n: b["correct_by_case"] for n, b in blocks.items()}

    def pair(a):
        p = paired_bool(ok[a], ok[NEW], ids)
        return {"both_correct": p["both_correct"], f"only_{a}": p["only_a_correct"], f"only_{NEW}": p["only_b_correct"],
                "both_wrong": p["both_wrong"], "paired_net": p["only_b_correct"] - p["only_a_correct"],
                "mcnemar_exact_p": p["mcnemar_exact_p_two_sided"]}

    def regressions(a):
        return sorted((i for i in ids if ok[a].get(i) and ok[NEW].get(i) is False), key=_case_key)

    gs_ids = set(gs["phase7a_general"] + gs["phase7a_specific"] + gs["inventory_general"] + gs["inventory_specific"])
    prod_gs = [i for i in regressions("production") if i in gs_ids]
    known = _known(sem_by, baseline, diagnostics)
    all_results = [dev[i] for i in baseline["dev_case_ids"] if i in dev] + list(diagnostics.values())
    ops = _ops(all_results)
    thr = FULL_GATE["exact_threshold"]
    checks = {
        "operational": sorted(dev) == sorted(baseline["dev_case_ids"])
        and sorted(diagnostics) == sorted(vc.KNOWN_REGRESSIONS) and not any(ops.values()),
        "qualifier": new["unstated_qualifier_assumed_slice"] <= FULL_GATE["qualifier_max"],
        "known_regressions": all(k["correct"] for k in known.values()),
        "exact": (new["exact"] >= thr["numerator"] if len(ids) == thr["denominator"]
                  else new["accuracy"] >= thr["ratio"]),
        "false_none": new["false_none"] <= FULL_GATE["false_none_max"],
        "general_specific": not prod_gs,
        "vs_a": new["exact"] >= blocks["variant_a_v1"]["exact"],
    }
    return {"stage": "FULL_DEV", "dev_evaluable": len(ids),
            "runs": {n: {k: v for k, v in b.items() if k != "correct_by_case"} for n, b in blocks.items()},
            "first_candidate": baseline["first_candidate"],
            "paired": {f"{a} vs {NEW}": pair(a) for a in RUN_NAMES},
            "regressions": {f"{a}_correct_new_wrong": regressions(a) for a in RUN_NAMES}
            | {"production_correct_new_wrong_general_specific": prod_gs},
            "known_regressions": known, "operational": ops,
            "gate": {"version": FULL_GATE["version"], "checks": checks, "passed": all(checks.values())},
            "usage": usage(all_results),
            "scored_rows": [{"case_id": i, "semantic_evaluable": i in set(ids), "decision": dev[i].decision,
                             "selected": dev[i].selected_candidate_ref} for i in baseline["dev_case_ids"] if i in dev]}


# ── exploratory full DEV (no gate; the M1 history is never rewritten) ───────

EXPLORATORY = {
    "purpose": "Full frozen-DEV behavioural comparison of variant_c_v1 on the new model against the "
               "saved gpt-4o-mini runs. NOT a promotion gate.",
    "m1_history": "MODEL_CAPABILITY_SCREEN = FAIL (unchanged; m1-gate.json and m1-metrics.json are "
                  "never rewritten)",
    "internal_pilot_baseline": "variant_a_v1 / openai/gpt-4o-mini / OpenRouter (unchanged by this run)",
    "parity": {"strict_transport_parity": "NO (temperature omitted: the model does not support it)",
               "behavioral_experiment_parity": "YES (prompt, candidates, order, schema, parser, scorer, "
                                               "Gold, max_tokens, reasoning=none identical)"},
    "gold_provenance": "blind model adjudication by GPT-5.6 Sol; not independent human annotation",
}


def build_m2_plan(*, config, omitted, prompt, split, baseline, snapshot_fp, serializer_fp, gold_fp,
                  capability_sha256: str, m1_gate: Mapping) -> dict:
    """Exploratory plan for the remaining DEV cases; explicitly not gated."""
    remaining = baseline["stage_case_ids"]["m2_dev"]
    plan = {
        "plan_kind": EXPLORATORY_PLAN_KIND, "live": True, "gated": False,
        "purpose": EXPLORATORY["purpose"], "m1_history": EXPLORATORY["m1_history"],
        "m1_gate_passed": bool(m1_gate["gate"]["passed"]),
        "provider": config.provider, "model": config.model,
        "reasoning": config.reasoning_effort.value if config.reasoning_effort else None,
        "max_tokens": config.max_tokens, "omitted_request_params": sorted(omitted),
        "config_fingerprint": config.fingerprint,
        "prompt_id": prompt.prompt_id, "prompt_fingerprint": prompt.fingerprint,
        "candidate_order": "production (original retrieval order)",
        "dev_case_ids_remaining": remaining, "calls": len(remaining),
        "max_logical_calls": len(remaining),
        "reused_without_new_calls": {"luna_dev_m1": baseline["stage_case_ids"]["m1_dev"],
                                     "luna_diagnostics": baseline["stage_case_ids"]["m1_diagnostic"]},
        "other_calls": {"production": 0, "variant_a_v1": 0, "variant_b_v1": 0, "variant_c_v1_4o_mini": 0,
                        "diagnostics_471_472": 0, "old_holdout": 0, "order_experiment": 0,
                        "final_validation": 0},
        "retries": "none; fail-fast after the first operational error",
        "snapshot_fingerprint": snapshot_fp, "split_fingerprint": split["split_fingerprint"],
        "serializer_contract_fingerprint": serializer_fp, "semantic_gold_fingerprint": gold_fp,
        "prelive_baseline_fingerprint": baseline["baseline_fingerprint"],
        "capability_validation_sha256": capability_sha256,
        "provider_policy": "OpenRouter only; no fallback",
    }
    if len(remaining) != M2_CALLS:
        raise SystemExit(f"{len(remaining)} remaining DEV cases, expected {M2_CALLS}")
    plan["plan_fingerprint"] = px.plan_fingerprint(plan)
    return plan


def validate_m2_plan(plan, approved_fp, *, config, omitted, prompt, split, baseline, snapshot_fp,
                     serializer_fp, gold_fp) -> None:
    if not approved_fp:
        raise PlanApprovalError("exploratory run requires --approve-plan-fingerprint")
    checks = {
        "wrong plan kind": plan.get("plan_kind") != EXPLORATORY_PLAN_KIND,
        "plan file modified": px.plan_fingerprint(plan) != plan.get("plan_fingerprint"),
        "approved fingerprint differs": approved_fp != plan.get("plan_fingerprint"),
        "provider is not openrouter": config.provider != PROVIDER or plan.get("provider") != PROVIDER,
        "model differs": config.model != MODEL or plan.get("model") != MODEL,
        "reasoning is not explicit none": config.reasoning_effort is None
        or config.reasoning_effort.value != REASONING,
        "config differs": plan.get("config_fingerprint") != config.fingerprint,
        "omissions differ": sorted(omitted) != plan.get("omitted_request_params"),
        "prompt is not variant_c_v1": prompt.prompt_id != vc.PROMPT_ID
        or plan.get("prompt_fingerprint") != prompt.fingerprint,
        "split differs": plan.get("split_fingerprint") != split["split_fingerprint"],
        "case ids differ": plan.get("dev_case_ids_remaining") != baseline["stage_case_ids"]["m2_dev"],
        "budget differs": plan.get("max_logical_calls") != M2_CALLS or plan.get("calls") != M2_CALLS,
        "other calls planned": any(plan.get("other_calls", {}).values()),
        "snapshot differs": plan.get("snapshot_fingerprint") != snapshot_fp,
        "serializer differs": plan.get("serializer_contract_fingerprint") != serializer_fp,
        "gold differs": plan.get("semantic_gold_fingerprint") != gold_fp,
        "baseline differs": plan.get("prelive_baseline_fingerprint") != baseline["baseline_fingerprint"],
    }
    failed = [k for k, bad in checks.items() if bad]
    if failed:
        raise PlanApprovalError(f"exploratory plan refused: {', '.join(failed)}")


def evaluate_exploratory(*, baseline, sem, dev, diagnostics, membership: Mapping[str, list],
                         c4o_dev: Mapping[str, BenchmarkResult]) -> dict:
    """Full-DEV comparison. No gates, no pass/fail."""
    sem_by = {s.case.case_id: s for s in sem}
    ids, qslice, gs = baseline["dev_evaluable_ids"], baseline["qualifier_slice_ids"], baseline["gs_slices"]
    missing = [i for i in baseline["dev_case_ids"] if i not in dev]
    extra = sorted(set(dev) - set(baseline["dev_case_ids"]), key=_case_key)
    new = vc.run_block(NEW, ids, sg.rescore_run(sem, dev), sem_by, qslice, gs)
    blocks = {**baseline["runs"], NEW: new}
    ok = {n: b["correct_by_case"] for n, b in blocks.items()}

    def pair(a, b):
        p = paired_bool(ok[a], ok[b], ids)
        return {"both_correct": p["both_correct"], f"only_{a}": p["only_a_correct"],
                f"only_{b}": p["only_b_correct"], "both_wrong": p["both_wrong"],
                "paired_net": p["only_b_correct"] - p["only_a_correct"],
                "mcnemar_exact_p": p["mcnemar_exact_p_two_sided"],
                "note": "descriptive; no gate"}

    def diff(a, b):
        return sorted((i for i in ids if ok[a].get(i) and ok[b].get(i) is False), key=_case_key)

    tags = {i: set(sem_by[i].all_tags) for i in ids}
    inv_ids = {r["case_id"] for r in qp.inventory([sem_by[i] for i in ids], sem_by, {})}
    slices = {"near_qna": [i for i in ids if "near_qna" in tags[i]],
              "general_specific": [i for i in ids if "general_specific" in tags[i]],
              "multi_acceptable": [i for i in ids if sem_by[i].case.multi_acceptable],
              "kb_overlap": [i for i in ids if "kb_overlap_flagged" in tags[i]],
              "easy_control": [i for i in ids if "easy_control" in membership.get(i, [])],
              "qualifier_sensitive": sorted(inv_ids, key=_case_key),
              "frozen_qualifier_slice": qslice,
              "phase7a_general": gs["phase7a_general"], "phase7a_specific": gs["phase7a_specific"]}
    dev_results = [dev[i] for i in baseline["dev_case_ids"] if i in dev]
    return {
        "mode": "EXPLORATORY_FULL_DEV_COMPARISON", "gates": None, **EXPLORATORY,
        "dev_total": len(dev), "missing": missing, "duplicates": extra,
        "dev_evaluable": len(ids),
        "runs": {n: {k: v for k, v in b.items() if k != "correct_by_case"} for n, b in blocks.items()},
        "first_candidate": baseline["first_candidate"],
        "paired": {"variant_c_v1_4o_mini vs variant_c_v1_5_6_luna": pair("variant_c_v1_4o_mini", NEW),
                   "variant_a_v1 vs variant_c_v1_5_6_luna": pair("variant_a_v1", NEW),
                   "production vs variant_c_v1_5_6_luna": pair("production", NEW),
                   "variant_b_v1 vs variant_c_v1_5_6_luna": pair("variant_b_v1", NEW)},
        "case_diffs": {"a_correct_luna_wrong": diff("variant_a_v1", NEW),
                       "c4o_correct_luna_wrong": diff("variant_c_v1_4o_mini", NEW),
                       "luna_correct_a_wrong": diff(NEW, "variant_a_v1"),
                       "luna_correct_c4o_wrong": diff(NEW, "variant_c_v1_4o_mini"),
                       "production_correct_luna_wrong": diff("production", NEW)},
        "slices": {name: {"cases": len(members),
                          **{n: sum(1 for i in members if ok[n].get(i)) for n in blocks}}
                   for name, members in slices.items()},
        "known_diagnostics": _known(sem_by, baseline, diagnostics),
        "operational": {**_ops(dev_results), "dev_complete": not missing and not extra},
        "usage_luna_dev": usage(dev_results),
        "latency_c4o_dev": describe([c4o_dev[i].latency_ms for i in baseline["dev_case_ids"]
                                     if i in c4o_dev and c4o_dev[i].latency_ms is not None]),
        "scored_rows": [{"case_id": i, "semantic_evaluable": i in set(ids),
                         "decision": dev[i].decision, "selected": dev[i].selected_candidate_ref,
                         "correct": ok[NEW].get(i)} for i in baseline["dev_case_ids"] if i in dev],
    }


def logical_cases(results: Mapping[str, BenchmarkResult]) -> set:
    """Distinct cases with any stored attempt: one logical selector invocation
    each, regardless of transport retries after an upstream rate limit."""
    return set(results)
