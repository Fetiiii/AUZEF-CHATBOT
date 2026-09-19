"""Selector challenge set + first-candidate baseline + selector-value metrics.

Phase 7A showed that picking the first frozen candidate is already correct on
most primary cases, so full-set accuracy separates models poorly. This module
freezes a model-independent CHALLENGE set (derived only from dataset and
frozen-retrieval properties) and measures what a selector adds over the
first-candidate baseline:

    RESCUE      first candidate wrong, model correct
    CORRUPTION  first candidate right, model wrong
    PRESERVE    both right
    UNRESOLVED  both wrong

FULL_REFERENCE_EXACT (all primary selector-evaluable cases) and
CHALLENGE_EXACT (deliberately over-sampled hard cases) are always reported
under those separate names; challenge accuracy is never a global accuracy.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Mapping, Optional, Sequence

from benchmarks.selector_v2.contract import fingerprint, sha256_text
from benchmarks.selector_v2.evaluator import metrics
from benchmarks.selector_v2.schema import BenchmarkResult, CaseSnapshot
from benchmarks.selector_v2.stats import mcnemar_exact

CHALLENGE_DEFINITION_VERSION = "selector-challenge-v1"
EASY_CONTROL_SIZE = 30
FULL = "FULL_REFERENCE_EXACT"
CHALLENGE = "CHALLENGE_EXACT"

SELECTION_RULE = {
    "universe": "primary selector-evaluable cases of the frozen snapshot "
                "(case.primary and pool_status IN_POOL/NOT_APPLICABLE)",
    "union_of": {
        "A_first_candidate_wrong": "first frozen candidate not in acceptable_candidate_refs",
        "B_near_qna": "Phase 7A derived tag near_qna",
        "C_general_specific": "Phase 7A derived tag general_specific",
        "D_kb_overlap_flagged": "dataset tag kb_overlap_flagged",
        "E_multi_acceptable": "more than one acceptable ref",
    },
    "easy_control": {
        "pool": "universe AND first_candidate_correct AND NOT near_qna AND NOT "
                "general_specific AND NOT kb_overlap_flagged (and not already in the union)",
        "order": f"sha256('{CHALLENGE_DEFINITION_VERSION}|' + case_id) ascending",
        "size": EASY_CONTROL_SIZE,
    },
    "model_independent": "uses only dataset fields and frozen retrieval order; "
                         "no model output is read",
}


# ── first-candidate baseline ───────────────────────────────────────────────

def ordered_refs(snapshot: CaseSnapshot) -> list[str]:
    return [c.candidate_ref for c in sorted(snapshot.candidates, key=lambda c: c.order)]


def first_candidate_ref(snapshot: CaseSnapshot) -> Optional[str]:
    refs = ordered_refs(snapshot)
    return refs[0] if refs else None


def first_candidate_correct(snapshot: CaseSnapshot) -> bool:
    """Never NONE: an expected-NONE case is always wrong for this baseline."""
    if snapshot.case.expected_decision != "SELECT":
        return False
    return first_candidate_ref(snapshot) in snapshot.case.acceptable_candidate_refs


def gold_rank(snapshot: CaseSnapshot) -> Optional[int]:
    """1-based position of the best-placed acceptable ref (offline only)."""
    acceptable = set(snapshot.case.acceptable_candidate_refs)
    for position, ref in enumerate(ordered_refs(snapshot), start=1):
        if ref in acceptable:
            return position
    return None


def reference_universe(snapshots: Sequence[CaseSnapshot]) -> list[CaseSnapshot]:
    return [s for s in snapshots if s.selector_evaluable and s.case.primary]


def _case_sort_key(case_id: str):
    head = case_id.split("#", 1)[0]
    return (int(head) if head.isdigit() else float("inf"), case_id)


def _control_key(case_id: str) -> str:
    return hashlib.sha256(f"{CHALLENGE_DEFINITION_VERSION}|{case_id}".encode()).hexdigest()


# ── challenge construction ─────────────────────────────────────────────────

def build_challenge(snapshots: Sequence[CaseSnapshot], *, control_size: int = EASY_CONTROL_SIZE) -> dict:
    universe = reference_universe(snapshots)
    groups: dict[str, list[str]] = {name: [] for name in SELECTION_RULE["union_of"]}
    for s in universe:
        tags = set(s.all_tags)
        cid = s.case.case_id
        if not first_candidate_correct(s):
            groups["A_first_candidate_wrong"].append(cid)
        if "near_qna" in tags:
            groups["B_near_qna"].append(cid)
        if "general_specific" in tags:
            groups["C_general_specific"].append(cid)
        if "kb_overlap_flagged" in tags:
            groups["D_kb_overlap_flagged"].append(cid)
        if s.case.multi_acceptable:
            groups["E_multi_acceptable"].append(cid)
    union = {cid for ids in groups.values() for cid in ids}
    pool = [
        s.case.case_id for s in universe
        if s.case.case_id not in union and first_candidate_correct(s)
        and not ({"near_qna", "general_specific", "kb_overlap_flagged"} & set(s.all_tags))
    ]
    control = sorted(pool, key=_control_key)[:control_size]
    members: dict[str, list[str]] = {}
    for name, ids in groups.items():
        for cid in ids:
            members.setdefault(cid, []).append(name)
    for cid in control:
        members.setdefault(cid, []).append("easy_control")
    by_id = {s.case.case_id: s for s in universe}
    cases = []
    for cid in sorted(members, key=_case_sort_key):
        s = by_id[cid]
        cases.append({
            "case_id": cid,
            "membership": members[cid],
            "first_candidate_ref": first_candidate_ref(s),
            "first_candidate_correct": first_candidate_correct(s),
            "gold_rank": gold_rank(s),
            "acceptable_candidate_refs": s.case.acceptable_candidate_refs,
            "tags": s.all_tags,
        })
    counts = {
        "universe_primary_cases": len(universe),
        "first_candidate_wrong": len(groups["A_first_candidate_wrong"]),
        "near_qna": len(groups["B_near_qna"]),
        "general_specific": len(groups["C_general_specific"]),
        "kb_overlap_flagged": len(groups["D_kb_overlap_flagged"]),
        "multi_acceptable": len(groups["E_multi_acceptable"]),
        "union_before_control": len(union),
        "easy_control_pool": len(pool),
        "easy_control": len(control),
        "final_unique_cases": len(cases),
    }
    return {"cases": cases, "groups": groups, "control": control, "counts": counts}


def challenge_fingerprint(snapshot_fp: str, cases: Sequence[dict]) -> str:
    return fingerprint({
        "definition": CHALLENGE_DEFINITION_VERSION,
        "source_snapshot_fingerprint": snapshot_fp,
        "cases": [[c["case_id"], c["membership"]] for c in cases],
    })


def write_challenge(directory: Path, snapshot_manifest: dict, built: dict,
                    summary: dict) -> dict:
    """Immutable: an existing artifact is kept only if byte-identical."""
    directory = Path(directory)
    body = "".join(json.dumps(c, ensure_ascii=False, sort_keys=True) + "\n" for c in built["cases"])
    snapshot_fp = snapshot_manifest["snapshot_fingerprint"]
    manifest = {
        "challenge_definition_version": CHALLENGE_DEFINITION_VERSION,
        "source_snapshot_fingerprint": snapshot_fp,
        "selector_contract_fingerprint": snapshot_manifest.get("selector_contract_fingerprint"),
        "selection_rule": SELECTION_RULE,
        "case_count": len(built["cases"]),
        "counts": built["counts"],
        "slice_case_ids": built["groups"],
        "easy_control_case_ids": built["control"],
        "case_ids": [c["case_id"] for c in built["cases"]],
        "cases_file_sha256": sha256_text(body),
        "challenge_fingerprint": challenge_fingerprint(snapshot_fp, built["cases"]),
    }
    manifest_text = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if (directory / "manifest.json").exists():
        existing = (directory / "manifest.json").read_text(encoding="utf-8")
        if existing != manifest_text or (directory / "cases.jsonl").read_text(encoding="utf-8") != body:
            raise SystemExit(f"{directory} already holds a different challenge artifact (immutable)")
        return manifest
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "cases.jsonl").write_text(body, encoding="utf-8")
    (directory / "manifest.json").write_text(manifest_text, encoding="utf-8")
    (directory / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def load_challenge(directory: Path, snapshot_manifest: Optional[dict] = None) -> tuple[dict, list[dict]]:
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    body = (directory / "cases.jsonl").read_text(encoding="utf-8")
    if sha256_text(body) != manifest["cases_file_sha256"]:
        raise ValueError("challenge cases.jsonl does not match its manifest sha256")
    cases = [json.loads(line) for line in body.splitlines() if line.strip()]
    if challenge_fingerprint(manifest["source_snapshot_fingerprint"], cases) != manifest["challenge_fingerprint"]:
        raise ValueError("challenge fingerprint mismatch")
    if snapshot_manifest is not None and \
            manifest["source_snapshot_fingerprint"] != snapshot_manifest["snapshot_fingerprint"]:
        raise ValueError("challenge was built from a different candidate snapshot")
    return manifest, cases


# ── selector-value metrics ─────────────────────────────────────────────────

def _rate(num: int, den: int) -> Optional[float]:
    return round(num / den, 6) if den else None


def correctness(snapshots: Sequence[CaseSnapshot], results: Mapping[str, BenchmarkResult]) -> dict[str, bool]:
    return {s.case.case_id: results[s.case.case_id].correct
            for s in snapshots if s.case.case_id in results}


def first_candidate_correctness(snapshots: Sequence[CaseSnapshot]) -> dict[str, bool]:
    return {s.case.case_id: first_candidate_correct(s) for s in snapshots}


def selector_value(snapshots: Sequence[CaseSnapshot], model: Mapping[str, bool]) -> dict:
    rescue = corruption = preserve = unresolved = 0
    fc_wrong = fc_right = 0
    for s in snapshots:
        cid = s.case.case_id
        if cid not in model:
            continue
        baseline = first_candidate_correct(s)
        fc_right += baseline
        fc_wrong += not baseline
        if baseline and model[cid]:
            preserve += 1
        elif baseline:
            corruption += 1
        elif model[cid]:
            rescue += 1
        else:
            unresolved += 1
    return {
        "compared_cases": fc_right + fc_wrong,
        "first_candidate_wrong": fc_wrong,
        "first_candidate_right": fc_right,
        "rescue_count": rescue,
        "rescue_rate": _rate(rescue, fc_wrong),
        "corruption_count": corruption,
        "corruption_rate": _rate(corruption, fc_right),
        "preserve_count": preserve,
        "unresolved_count": unresolved,
        "net_corrections": rescue - corruption,
    }


def paired_bool(a: Mapping[str, bool], b: Mapping[str, bool], cases: Sequence[str]) -> dict:
    both = only_a = only_b = neither = 0
    for cid in cases:
        if cid not in a or cid not in b:
            continue
        if a[cid] and b[cid]:
            both += 1
        elif a[cid]:
            only_a += 1
        elif b[cid]:
            only_b += 1
        else:
            neither += 1
    return {"paired_cases": both + only_a + only_b + neither, "both_correct": both,
            "only_a_correct": only_a, "only_b_correct": only_b, "both_wrong": neither,
            "mcnemar_exact_p_two_sided": mcnemar_exact(only_a, only_b),
            "note": "Statistical evidence only; no automatic winner."}


def _acc(snapshots, model: Mapping[str, bool]) -> dict:
    done = [s.case.case_id for s in snapshots if s.case.case_id in model]
    correct = sum(1 for cid in done if model[cid])
    return {"cases": len(snapshots), "completed": len(done), "correct": correct,
            "accuracy": _rate(correct, len(done))}


def _pair_keys(snapshot: CaseSnapshot) -> list[str]:
    return [t.split(":", 1)[1] for t in snapshot.derived_tags if t.startswith("near_qna_pair:")]


def layer_report(name: str, snapshots: Sequence[CaseSnapshot], model: Mapping[str, bool],
                 results: Optional[Mapping[str, BenchmarkResult]] = None) -> dict:
    """One benchmark layer: exact accuracy + value metrics + diagnostic slices."""
    def subset(pred):
        return [s for s in snapshots if pred(s)]

    rank1 = subset(lambda s: gold_rank(s) == 1)
    not_rank1 = subset(lambda s: gold_rank(s) != 1)
    right_first = subset(first_candidate_correct)
    pairs: dict[str, list] = {}
    for s in snapshots:
        for key in _pair_keys(s):
            pairs.setdefault(key, []).append(s)
    general = subset(lambda s: "gs_expected:general" in s.derived_tags)
    specific = subset(lambda s: "gs_expected:specific" in s.derived_tags)
    kb = subset(lambda s: "kb_overlap_flagged" in s.all_tags)
    multi = subset(lambda s: s.case.multi_acceptable)
    report = {
        "layer": name,
        "note": ("final reference over all primary selector-evaluable cases" if name == FULL
                 else "deliberately over-samples hard cases; NOT a global/production accuracy"),
        "exact": _acc(snapshots, model),
        "selector_value": selector_value(snapshots, model),
        "position": {
            "gold_at_position_1": {**_acc(rank1, model), **{
                "accuracy_when_gold_rank1": _acc(rank1, model)["accuracy"]}},
            "gold_not_at_position_1": {**_acc(not_rank1, model), **{
                "accuracy_when_gold_not_rank1": _acc(not_rank1, model)["accuracy"]}},
        },
        "rank1_wrong": selector_value(subset(lambda s: not first_candidate_correct(s)), model),
        "right_first": selector_value(right_first, model),
        "general_specific": {
            "cases": len(general) + len(specific),
            "general_accuracy": _acc(general, model),
            "specific_accuracy": _acc(specific, model),
            "value": selector_value([*general, *specific], model),
        },
        "near_qna_pairs": {
            key: {**_acc(items, model), **{k: v for k, v in selector_value(items, model).items()
                                           if k in ("rescue_count", "corruption_count")}}
            for key, items in sorted(pairs.items())
        },
        "kb_overlap_flagged": {
            **_acc(kb, model), "value": selector_value(kb, model),
            "caveat": "reviewer-selected alias-collision cases (selection bias); "
                      "not evidence of global model superiority",
        },
        "multi_acceptable": {**_acc(multi, model), "rule": "any acceptable ref is correct"},
        "none_limitation": "reviewed Gold has no expected-NONE cases: NONE precision/recall "
                           "cannot be measured; NONE output appears only as false_none",
    }
    if results is not None:
        report["exact_metrics"] = metrics(list(snapshots), results)
    return report


def two_layer_report(snapshots: Sequence[CaseSnapshot], challenge_ids: Sequence[str],
                     model: Mapping[str, bool], *, label: str,
                     results: Optional[Mapping[str, BenchmarkResult]] = None) -> dict:
    universe = reference_universe(snapshots)
    wanted = set(challenge_ids)
    challenge = [s for s in universe if s.case.case_id in wanted]
    baseline = first_candidate_correctness(universe)
    return {
        "label": label,
        FULL: layer_report(FULL, universe, model, results),
        CHALLENGE: layer_report(CHALLENGE, challenge, model, results),
        "paired_vs_first_candidate": {
            FULL: paired_bool(model, baseline, [s.case.case_id for s in universe]),
            CHALLENGE: paired_bool(model, baseline, [s.case.case_id for s in challenge]),
        },
        "winner": None,
        "winner_note": "no automatic model winner; decision is a human review",
    }


# ── case-level + error-review artifacts (Stage runs) ───────────────────────

VALUE_CLASSES = {(True, True): "PRESERVE", (True, False): "CORRUPTION",
                 (False, True): "RESCUE", (False, False): "UNRESOLVED"}


def case_records(snapshots: Sequence[CaseSnapshot], challenge_cases: Sequence[dict],
                 results: Mapping[str, BenchmarkResult]) -> list[dict]:
    by_id = {s.case.case_id: s for s in snapshots}
    records = []
    for item in challenge_cases:
        s = by_id[item["case_id"]]
        texts = {c.candidate_ref: c.canonical_text for c in s.candidates}
        r = results.get(s.case.case_id)
        baseline = first_candidate_correct(s)
        records.append({
            "case_id": s.case.case_id,
            "membership": item["membership"],
            "tags": s.all_tags,
            "expected_refs": s.case.acceptable_candidate_refs,
            "candidate_refs": ordered_refs(s),
            "first_candidate_ref": first_candidate_ref(s),
            "first_candidate_correct": baseline,
            "gold_rank": gold_rank(s),
            "status": r.outcome.value if r else "MISSING",
            "selector_status": r.selector_status if r else None,
            "invalid_reason": r.invalid_reason if r else None,
            "model_decision": r.decision if r else None,
            "selected_candidate_ref": r.selected_candidate_ref if r else None,
            "model_correct": r.correct if r else None,
            "value_class": VALUE_CLASSES[(baseline, r.correct)] if r else None,
            "input_tokens": r.input_tokens if r else None,
            "output_tokens": r.output_tokens if r else None,
            "total_tokens": r.total_tokens if r else None,
            "latency_ms": r.latency_ms if r else None,
            "actual_model": r.actual_model if r else None,
            "finish_reason": r.finish_reason if r else None,
            "canonical_text": {
                "expected": {ref: texts.get(ref) for ref in s.case.acceptable_candidate_refs},
                "first_candidate": texts.get(first_candidate_ref(s)),
                "selected": texts.get(r.selected_candidate_ref) if r and r.selected_candidate_ref else None,
            },
        })
    return records


def error_review(records: Sequence[dict]) -> dict:
    def pick(pred):
        return [{k: rec[k] for k in ("case_id", "expected_refs", "first_candidate_ref",
                                     "model_decision", "selected_candidate_ref", "status",
                                     "candidate_refs", "canonical_text", "tags")}
                for rec in records if pred(rec)]

    return {
        "model_wrong_cases": pick(lambda r: r["model_correct"] is False),
        "rescued_cases": pick(lambda r: r["value_class"] == "RESCUE"),
        "corrupted_cases": pick(lambda r: r["value_class"] == "CORRUPTION"),
        "unresolved_cases": pick(lambda r: r["value_class"] == "UNRESOLVED"),
        "invalid_or_error_cases": pick(
            lambda r: r["status"] in ("INVALID_OUTPUT", "MODEL_ERROR", "TIMEOUT", "MISSING")),
    }


def run_summary(records: Sequence[dict]) -> dict:
    from benchmarks.selector_v2.snapshot import describe

    def values(key):
        return [r[key] for r in records if r[key] is not None]

    status = {}
    for r in records:
        status[r["status"]] = status.get(r["status"], 0) + 1
    decisions = [r["model_decision"] for r in records]
    latency = sorted(((r["latency_ms"], r["case_id"]) for r in records
                      if r["latency_ms"] is not None), reverse=True)
    with_usage = [r for r in records if r["input_tokens"] is not None and r["output_tokens"] is not None]
    return {
        "cases": len(records),
        "completed": sum(1 for r in records if r["status"] != "MISSING"),
        "outcomes": status,
        "valid_select": sum(1 for r in records if r["status"] in
                            ("CORRECT_SELECT", "WRONG_SELECT", "FALSE_SELECT")),
        "valid_none": decisions.count("NONE"),
        "false_none": status.get("FALSE_NONE", 0),
        "invalid_output": status.get("INVALID_OUTPUT", 0),
        "model_error": status.get("MODEL_ERROR", 0),
        "timeout": status.get("TIMEOUT", 0),
        "value_classes": {v: sum(1 for r in records if r["value_class"] == v)
                          for v in VALUE_CLASSES.values()},
        "usage_metadata_coverage": f"{len(with_usage)}/{len(records)}",
        "input_tokens": describe(values("input_tokens")),
        "output_tokens": describe(values("output_tokens")),
        "total_tokens": describe(values("total_tokens")),
        "latency_ms": describe(values("latency_ms")),
        "slowest_cases": [{"case_id": c, "latency_ms": ms} for ms, c in latency[:5]],
        "actual_models": sorted({r["actual_model"] for r in records if r["actual_model"]}),
        "cost": "NOT_CALCULATED — no explicit price inputs",
    }
