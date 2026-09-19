"""Paired model comparison on one frozen snapshot (no ranking verdict)."""
from __future__ import annotations

from math import comb
from typing import Mapping, Optional

from benchmarks.selector_v2.schema import BenchmarkResult, CaseSnapshot


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value: binomial(b + c, 0.5) on discordant pairs."""
    if b < 0 or c < 0:
        raise ValueError("counts must be non-negative")
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(comb(n, k) for k in range(min(b, c) + 1))
    return min(1.0, 2 * tail / 2 ** n)


def mcnemar_chi2_cc(b: int, c: int) -> Optional[float]:
    """Continuity-corrected McNemar chi-square statistic (secondary)."""
    if b + c == 0:
        return None
    return (abs(b - c) - 1) ** 2 / (b + c)


def paired_compare(
    snapshots_by_case: Mapping[str, CaseSnapshot],
    results_a: Mapping[str, BenchmarkResult],
    results_b: Mapping[str, BenchmarkResult],
    *,
    primary_only: bool = True,
) -> dict:
    """Both/only-A/only-B/neither counts over cases completed by both runs."""
    fp_a = {r.snapshot_fingerprint for r in results_a.values()}
    fp_b = {r.snapshot_fingerprint for r in results_b.values()}
    if len(fp_a | fp_b) > 1:
        raise ValueError(f"runs use different candidate snapshots: {sorted(fp_a | fp_b)}")
    contracts = {r.selector_contract_fingerprint for r in [*results_a.values(), *results_b.values()]}
    cases = sorted(
        case_id for case_id in set(results_a) & set(results_b)
        if case_id in snapshots_by_case
        and (snapshots_by_case[case_id].case.primary or not primary_only)
    )
    both = only_a = only_b = neither = 0
    for case_id in cases:
        a, b = results_a[case_id].correct, results_b[case_id].correct
        if a and b:
            both += 1
        elif a:
            only_a += 1
        elif b:
            only_b += 1
        else:
            neither += 1
    n = len(cases)
    return {
        "paired_cases": n,
        "primary_only": primary_only,
        "same_selector_contract": len(contracts) <= 1,
        "only_in_a": len(set(results_a) - set(results_b)),
        "only_in_b": len(set(results_b) - set(results_a)),
        "both_correct": both,
        "only_a_correct": only_a,
        "only_b_correct": only_b,
        "both_wrong": neither,
        "accuracy_a": round((both + only_a) / n, 6) if n else None,
        "accuracy_b": round((both + only_b) / n, 6) if n else None,
        "mcnemar_exact_p_two_sided": mcnemar_exact(only_a, only_b),
        "mcnemar_chi2_continuity_corrected": mcnemar_chi2_cc(only_a, only_b),
        "note": "Statistical evidence only; no automatic ranking verdict.",
    }
