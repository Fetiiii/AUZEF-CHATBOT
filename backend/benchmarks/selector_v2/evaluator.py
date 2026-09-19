"""Metrics over one run: denominators, primary/secondary metrics, slices,
NONE / near-QnA / general-specific matrices, errors and token/latency.

Primary metric — Exact Selector Accuracy: over selector-evaluable primary
cases (expected candidate present in the frozen pool), a SELECT case is
correct iff the selected ref is one of ``acceptable_candidate_refs``; a NONE
case is correct iff the output is a valid semantic NONE. Invalid output,
model error and timeout count as incorrect. Retrieval/eligibility/budget
misses are outside this denominator and reported separately.
"""
from __future__ import annotations

from collections import Counter
from typing import Mapping, Optional, Sequence

from benchmarks.selector_v2.schema import (
    BenchmarkResult,
    CaseSnapshot,
    EvaluationStatus,
    MISS_STATUSES,
    Outcome,
    PoolStatus,
)
from benchmarks.selector_v2.snapshot import describe

SELF_TEST_LABEL = "HARNESS SELF-TEST — fake provider output, NOT a model accuracy"
SLICES = (
    "general_specific", "near_qna", "multi_acceptable", "calendar", "qna_only",
    "single_intent", "context_independent", "kb_overlap_flagged",
    "multi_intent_derived", "reformulated_intent_text", "temporal", "routing_guarded",
    "first_turn", "follow_up_context_not_required",
)


def _rate(num: int, den: int) -> Optional[float]:
    return round(num / den, 6) if den else None


def metrics(snapshots: Sequence[CaseSnapshot], results: Mapping[str, BenchmarkResult]) -> dict:
    done = [(s, results[s.case.case_id]) for s in snapshots if s.case.case_id in results]
    outcomes = Counter(r.outcome for _, r in done)
    exp_select = [r for s, r in done if s.case.expected_decision == "SELECT"]
    exp_none = [r for s, r in done if s.case.expected_decision == "NONE"]
    predicted_none = outcomes[Outcome.CORRECT_NONE] + outcomes[Outcome.FALSE_NONE]
    correct = outcomes[Outcome.CORRECT_SELECT] + outcomes[Outcome.CORRECT_NONE]
    n = len(done)
    return {
        "cases": len(snapshots),
        "completed": n,
        "complete": n == len(snapshots),
        "correct": correct,
        "exact_selector_accuracy": _rate(correct, n),
        "select_accuracy": _rate(outcomes[Outcome.CORRECT_SELECT], len(exp_select)),
        "expected_select": len(exp_select),
        "expected_none": len(exp_none),
        "none_precision": _rate(outcomes[Outcome.CORRECT_NONE], predicted_none),
        "none_recall": _rate(outcomes[Outcome.CORRECT_NONE], len(exp_none)),
        "predicted_valid_none": predicted_none,
        "false_none": outcomes[Outcome.FALSE_NONE],
        "false_none_rate": _rate(outcomes[Outcome.FALSE_NONE], len(exp_select)),
        "false_select": outcomes[Outcome.FALSE_SELECT],
        "false_select_rate": _rate(outcomes[Outcome.FALSE_SELECT], len(exp_none)),
        "wrong_select": outcomes[Outcome.WRONG_SELECT],
        "invalid_output_rate": _rate(outcomes[Outcome.INVALID_OUTPUT], n),
        "model_error_rate": _rate(outcomes[Outcome.MODEL_ERROR], n),
        "timeout_rate": _rate(outcomes[Outcome.TIMEOUT], n),
        "outcomes": {o.value: outcomes[o] for o in Outcome},
    }


def _none_matrix(pairs) -> dict:
    columns = ("SELECT_correct_ref", "SELECT_other_ref", "NONE", "INVALID_OUTPUT",
               "MODEL_ERROR", "TIMEOUT")
    matrix = {"SELECT": dict.fromkeys(columns, 0), "NONE": dict.fromkeys(columns, 0)}
    for snapshot, result in pairs:
        row = matrix[snapshot.case.expected_decision]
        outcome = result.outcome
        if outcome is Outcome.CORRECT_SELECT:
            row["SELECT_correct_ref"] += 1
        elif outcome in (Outcome.WRONG_SELECT, Outcome.FALSE_SELECT):
            row["SELECT_other_ref"] += 1
        elif outcome in (Outcome.FALSE_NONE, Outcome.CORRECT_NONE):
            row["NONE"] += 1
        else:
            row[outcome.value] += 1
    return matrix


def _pair_bucket(snapshot: CaseSnapshot, result: BenchmarkResult, sibling_refs: set[str]) -> str:
    if result.outcome is Outcome.CORRECT_SELECT:
        return "correct"
    if result.outcome in (Outcome.WRONG_SELECT, Outcome.FALSE_SELECT):
        return "chose_sibling" if result.selected_candidate_ref in sibling_refs else "other_select"
    if result.outcome in (Outcome.FALSE_NONE, Outcome.CORRECT_NONE):
        return "none"
    return "error"


def _siblings(snapshot: CaseSnapshot, tag_prefix: str) -> dict[str, set[str]]:
    out = {}
    acceptable = set(snapshot.case.acceptable_candidate_refs)
    for tag in snapshot.derived_tags:
        if tag.startswith(tag_prefix):
            a, b = tag.split(":", 1)[1].split("-")
            refs = {f"qna:{a}", f"qna:{b}"}
            out[tag.split(":", 1)[1]] = refs - acceptable
    return out


def near_qna_matrix(pairs) -> dict:
    table: dict[str, Counter] = {}
    for snapshot, result in pairs:
        for key, siblings in _siblings(snapshot, "near_qna_pair:").items():
            table.setdefault(key, Counter())["cases"] += 1
            table[key][_pair_bucket(snapshot, result, siblings)] += 1
    return {key: dict(counter) for key, counter in sorted(table.items())}


def general_specific_matrix(pairs) -> dict:
    table: dict[str, Counter] = {}
    for snapshot, result in pairs:
        if "general_specific" not in snapshot.derived_tags:
            continue
        roles = [t.split(":", 1)[1] for t in snapshot.derived_tags if t.startswith("gs_expected:")]
        siblings = set().union(*_siblings(snapshot, "near_qna_pair:").values())
        for role in roles or ["unknown"]:
            bucket = _pair_bucket(snapshot, result, siblings)
            bucket = "chose_counterpart" if bucket == "chose_sibling" else bucket
            table.setdefault(f"expected_{role}", Counter())["cases"] += 1
            table[f"expected_{role}"][bucket] += 1
    return {key: dict(counter) for key, counter in sorted(table.items())}


def _usage(pairs, is_live: bool) -> dict:
    results = [r for _, r in pairs]

    def values(attr):
        return [getattr(r, attr) for r in results if getattr(r, attr) is not None]

    return {
        "real_provider_usage": is_live,
        "note": None if is_live else "fake provider: no real tokens/latency (not a cost metric)",
        "input_tokens": describe(values("input_tokens")),
        "output_tokens": describe(values("output_tokens")),
        "total_tokens": describe(values("total_tokens")),
        "latency_ms": describe(values("latency_ms")),
    }


def evaluate(
    snapshots: Sequence[CaseSnapshot],
    results: Mapping[str, BenchmarkResult],
    *,
    snapshot_manifest: Optional[dict] = None,
    run_manifest: Optional[dict] = None,
) -> dict:
    modes = {r.run_mode for r in results.values()}
    is_live = modes == {"LIVE"}
    self_test = any(m.startswith("DRY_RUN_FAKE") for m in modes)
    fps = {r.snapshot_fingerprint for r in results.values()}
    if snapshot_manifest and fps and fps != {snapshot_manifest.get("snapshot_fingerprint")}:
        raise ValueError("results were produced on a different candidate snapshot")

    status_counts = Counter(s.case.evaluation_status.value for s in snapshots)
    candidate_cases = [s for s in snapshots
                       if s.case.evaluation_status is EvaluationStatus.SELECTOR_EVALUABLE]
    evaluable = [s for s in snapshots if s.selector_evaluable]
    primary = [s for s in evaluable if s.case.primary]
    derived = [s for s in evaluable if not s.case.primary]
    pool = Counter(s.pool_status for s in candidate_cases)
    primary_candidates = [s for s in candidate_cases if s.case.primary]
    primary_misses = sum(1 for s in primary_candidates if s.pool_status in MISS_STATUSES)

    def pairs_of(subset):
        return [(s, results[s.case.case_id]) for s in subset if s.case.case_id in results]

    primary_metrics = metrics(primary, results)
    retrieval_inclusive = _rate(primary_metrics["correct"], len(primary) + primary_misses)
    by_slice = {}
    for tag in SLICES:
        subset = [s for s in evaluable if tag in s.all_tags]
        if subset:  # never invent a metric for an absent tag
            by_slice[tag] = metrics(subset, results)
    primary_pairs = pairs_of(primary)
    errors = [(s, r) for s, r in pairs_of(evaluable)
              if r.outcome in (Outcome.INVALID_OUTPUT, Outcome.MODEL_ERROR, Outcome.TIMEOUT)]
    return {
        "label": SELF_TEST_LABEL if self_test else ("LIVE MODEL RUN" if is_live else "UNKNOWN"),
        "self_test": self_test,
        "run": run_manifest,
        "snapshot_fingerprint": (snapshot_manifest or {}).get("snapshot_fingerprint"),
        "selector_contract_fingerprint": (snapshot_manifest or {}).get("selector_contract_fingerprint"),
        "denominators": {
            "total_cases": len(snapshots),
            "evaluation_status": dict(status_counts),
            "not_selector_evaluable": status_counts[EvaluationStatus.NOT_SELECTOR_EVALUABLE.value],
            "excluded": status_counts[EvaluationStatus.EXCLUDED.value],
            "hold": status_counts[EvaluationStatus.HOLD.value],
            "selector_candidate_cases": len(candidate_cases),
            "retrieval_misses": pool[PoolStatus.RETRIEVAL_MISS],
            "eligibility_misses": pool[PoolStatus.ELIGIBILITY_MISS],
            "budget_misses": pool[PoolStatus.BUDGET_MISS],
            "empty_pool": pool[PoolStatus.EMPTY_POOL],
            "selector_evaluable": len(evaluable),
            "primary_denominator": len(primary),
            "derived_non_primary": len(derived),
            "primary_retrieval_or_eligibility_misses": primary_misses,
        },
        "primary": {
            "metric": "exact_selector_accuracy",
            **primary_metrics,
            "retrieval_inclusive_accuracy": retrieval_inclusive,
        },
        "all_selector_evaluable": metrics(evaluable, results),
        "derived_non_primary": metrics(derived, results) if derived else None,
        "by_slice": by_slice,
        "none_matrix": _none_matrix(primary_pairs),
        "near_qna_matrix": near_qna_matrix(pairs_of(evaluable)),
        "general_specific_matrix": general_specific_matrix(pairs_of(evaluable)),
        "errors": {
            "case_ids": {o.value: [s.case.case_id for s, r in errors if r.outcome is o]
                         for o in (Outcome.INVALID_OUTPUT, Outcome.MODEL_ERROR, Outcome.TIMEOUT)},
            "invalid_reasons": dict(Counter(r.invalid_reason for _, r in errors if r.invalid_reason)),
            "failure_categories": dict(Counter(r.failure_category for _, r in errors
                                               if r.failure_category)),
        },
        "token_latency": _usage(pairs_of(evaluable), is_live),
    }


def _fmt(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def render_markdown(report: dict) -> str:
    lines = [f"# Selector V2 benchmark — {report['label']}", ""]
    if report["self_test"]:
        lines += [f"> **{SELF_TEST_LABEL}.** These numbers validate the harness only.", ""]
    lines += [f"- Snapshot fingerprint: `{report['snapshot_fingerprint']}`",
              f"- Selector contract fingerprint: `{report['selector_contract_fingerprint']}`"]
    if report.get("run"):
        run = report["run"]
        lines.append(f"- Run: `{run.get('run_id')}` mode `{run.get('run_mode')}` "
                     f"config `{run.get('config_fingerprint')}`")
    lines += ["", "## Denominators", "", "| | count |", "|---|---:|"]
    lines += [f"| {k} | {_fmt(v)} |" for k, v in report["denominators"].items()
              if not isinstance(v, dict)]
    p = report["primary"]
    lines += ["", "## Primary — Exact Selector Accuracy", "", "| metric | value |", "|---|---:|"]
    for key in ("completed", "correct", "exact_selector_accuracy", "select_accuracy",
                "none_precision", "none_recall", "false_none", "false_select", "wrong_select",
                "invalid_output_rate", "model_error_rate", "timeout_rate",
                "retrieval_inclusive_accuracy"):
        lines.append(f"| {key} | {_fmt(p.get(key))} |")
    lines += ["", "## Slices", "", "| slice | cases | completed | exact acc | false NONE | invalid |",
              "|---|---:|---:|---:|---:|---:|"]
    for tag, m in report["by_slice"].items():
        lines.append(f"| {tag} | {m['cases']} | {m['completed']} | "
                     f"{_fmt(m['exact_selector_accuracy'])} | {m['false_none']} | "
                     f"{m['outcomes']['INVALID_OUTPUT']} |")
    lines += ["", "## NONE matrix (primary)", "", "| expected | " +
              " | ".join(next(iter(report["none_matrix"].values())).keys()) + " |",
              "|---|" + "---:|" * len(next(iter(report["none_matrix"].values())))]
    for expected, row in report["none_matrix"].items():
        lines.append(f"| {expected} | " + " | ".join(str(v) for v in row.values()) + " |")
    for title, key in (("Near-QnA matrix", "near_qna_matrix"),
                       ("General/specific matrix", "general_specific_matrix")):
        lines += ["", f"## {title}", ""]
        if report[key]:
            lines += [f"- `{k}`: {v}" for k, v in report[key].items()]
        else:
            lines.append("- no qualifying cases in this snapshot")
    lines += ["", "## Errors", "", f"- {report['errors']}", "",
              "## Token / latency", "", f"- {report['token_latency']}", ""]
    return "\n".join(lines)
