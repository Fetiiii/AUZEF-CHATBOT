"""Blind semantic Gold adjudication (Phase 7B): packet, apply, re-score.

Anti-bias design
----------------
The PRIMARY review view shows, per case, only: case id, user text, and a
bounded set of candidates under anonymous labels (A, B, C, ...) with their
canonical question and curated answer, in a retrieval-rank-neutral order
(sha256(case_id|candidate_ref)). It never contains: any model decision
(production / variant A / variant B), the first-candidate result,
rescue/corruption/correctness, prompt names, retrieval rank/score/source,
candidate refs, taxonomy/scope reason, the current Gold, or alias
provenance.

The reviewer records a blind decision (which labels are acceptable, or
NONE / ambiguous / content / mapping / need-full-candidates). Only after
the review is frozen are decisions compared with the current Gold to derive
KEEP_CURRENT / CHANGE_GOLD / MULTI_ACCEPTABLE. The SECONDARY audit view
(label→ref map, current Gold, alias provenance, prior review state, scope
reason) is a separate file; model outputs are in neither view.

Scope bias control: the scope comes from the Stage A postmortem, i.e. from
cases the production selector got wrong. To avoid correcting Gold only where
one model disagreed, a hash-selected blind CONTROL sample of cases outside
that scope is added; the reviewer cannot tell scope from control.

The parent reviewed Gold is never modified; decisions produce a CHILD case
set with per-case provenance, and saved model outputs are re-scored against
it without any provider call.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import string
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence

from benchmarks.selector_v2.contract import fingerprint, sha256_text
from benchmarks.selector_v2.postmortem import _case_key, terms
from benchmarks.selector_v2.schema import (
    CORRECT_OUTCOMES,
    BenchmarkCase,
    BenchmarkResult,
    CaseSnapshot,
    EvaluationStatus,
    Outcome,
)
from benchmarks.selector_v2.snapshot import NearPair

REVIEW_SCHEMA_VERSION = "selector-semantic-adjudication-v1"
CONTROL_SIZE = 20
TOP_RETRIEVED = 5
TOP_LEXICAL = 3
MAX_SHOWN = 10

SCOPE_GROUPS = {"B": "GOLD_ALIAS_QUESTIONABLE", "C": "CONTRACT_MISMATCH",
                "U": "NEEDS_HUMAN_REVIEW"}
EXISTING_REVIEW_QUEUE = ("404", "19", "221", "35", "158", "74", "436")

# Blind (primary) decisions recorded by the reviewer.
BLIND_DECISIONS = (
    "SELECT_ACCEPTABLE",               # one or more labels acceptable
    "EXPECT_NONE",                     # no shown candidate reasonably meets the need
    "EXCLUDE_AMBIGUOUS",               # message too ambiguous to benchmark
    "CONTENT_REVIEW_REQUIRED",         # intent clear, KB content itself deficient
    "RETRIEVAL_OR_KB_MAPPING_REVIEW",  # right answer absent / mapping clearly wrong
    "NEED_FULL_CANDIDATES",            # shown subset insufficient; review full list
)
# Final adjudication decisions (derived after freeze, vs current Gold).
FINAL_DECISIONS = ("KEEP_CURRENT", "CHANGE_GOLD", "MULTI_ACCEPTABLE", "EXPECT_NONE",
                   "EXCLUDE_AMBIGUOUS", "CONTENT_REVIEW_REQUIRED",
                   "RETRIEVAL_OR_KB_MAPPING_REVIEW", "UNREVIEWED", "NEED_FULL_CANDIDATES")
PRIMARY_FORBIDDEN_KEYS = {
    "model_decision", "selected_candidate_ref", "outcome", "model_correct", "value_class",
    "first_candidate_ref", "first_candidate_correct", "gold_rank", "classification",
    "acceptable_refs", "acceptable_candidate_refs", "expected_decision", "tags", "membership",
    "score", "source", "retrieval_stage", "order", "position", "alias_match", "candidate_ref",
    "intent_alias_owner_qna_ids", "prompt", "rescue", "corruption",
}
CSV_COLUMNS = ["case_id", "intent_text", "shown_labels", "candidate_view_complete",
               "all_candidate_count", "blind_decision", "acceptable_labels",
               "review_note", "reviewer", "reviewed_at"]


def _hash(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _labels(n: int) -> list[str]:
    letters = string.ascii_uppercase
    return [letters[i] if i < 26 else f"A{letters[i - 26]}" for i in range(n)]


# ── scope ──────────────────────────────────────────────────────────────────

def review_scope(failure_cases: Sequence[dict], challenge_ids: Sequence[str],
                 control_size: int = CONTROL_SIZE,
                 review_queue: Sequence[str] = EXISTING_REVIEW_QUEUE) -> dict:
    """Case ids + scope reasons. Only ids/groups are read from the postmortem."""
    reasons: dict[str, list[str]] = {}

    def add(cid, reason):
        reasons.setdefault(cid, [])
        if reason not in reasons[cid]:
            reasons[cid].append(reason)

    for case in failure_cases:
        group = case["classification"]["mismatch_group"]
        if group in SCOPE_GROUPS:
            add(case["case_id"], SCOPE_GROUPS[group])
        if case["classification"]["primary"] == "KB_OVERLAP_INTRINSIC":
            add(case["case_id"], "KB_OVERLAP_INTRINSIC")
        if "intent_states_selected_distinction" in case["classification"]["secondary"]:
            add(case["case_id"], "POSTMORTEM_GOLD_ALIAS_RECOMMENDED")
    for cid in review_queue:
        add(cid, "EXISTING_REVIEW_QUEUE")
    clear = sorted({c["case_id"] for c in failure_cases
                    if c["classification"]["mismatch_group"] == "A"}, key=_case_key)
    pool = [cid for cid in challenge_ids if cid not in reasons and cid not in clear]
    control = sorted(pool, key=lambda cid: _hash(REVIEW_SCHEMA_VERSION, "control", cid))[:control_size]
    for cid in control:
        add(cid, "BLIND_CONTROL")
    counts = {name: sum(1 for r in reasons.values() if name in r) for name in (
        *SCOPE_GROUPS.values(), "KB_OVERLAP_INTRINSIC", "POSTMORTEM_GOLD_ALIAS_RECOMMENDED",
        "EXISTING_REVIEW_QUEUE", "BLIND_CONTROL")}
    counts["unique_union_excluding_control"] = sum(1 for r in reasons.values() if r != ["BLIND_CONTROL"])
    counts["unique_union"] = len(reasons)
    return {"case_ids": sorted(reasons, key=_case_key), "reasons": reasons,
            "clear_selector_error_diagnostic": clear, "control": control, "counts": counts}


# ── candidate packet ───────────────────────────────────────────────────────

def candidate_subset(snapshot: CaseSnapshot, pairs: Sequence[NearPair]) -> list[str]:
    """Deterministic, model-independent bounded subset (refs)."""
    by_order = [c.candidate_ref for c in sorted(snapshot.candidates, key=lambda c: c.order)]
    present = set(by_order)
    gold = [r for r in snapshot.case.acceptable_candidate_refs if r in present]
    near = set()
    for ref in snapshot.case.acceptable_candidate_refs:
        qid = int(ref.split(":")[1])
        for pair in pairs:
            sibling = pair.sibling(qid)
            if sibling is not None and f"qna:{sibling}" in present:
                near.add(f"qna:{sibling}")
    intent = terms(snapshot.case.intent_text)
    lexical = sorted(snapshot.candidates,
                     key=lambda c: (-len(intent & terms(c.canonical_text)), c.candidate_ref))
    chosen: list[str] = []
    for ref in [*gold, *sorted(near), *by_order[:TOP_RETRIEVED],
                *[c.candidate_ref for c in lexical[:TOP_LEXICAL]]]:
        if ref not in chosen:
            chosen.append(ref)
    return chosen[:max(MAX_SHOWN, len(gold) + len(near))]


def build_packet(snapshots: Sequence[CaseSnapshot], scope: dict, pairs: Sequence[NearPair],
                 alias_owners: Mapping[str, list]) -> dict:
    """Primary (blind) records, secondary audit records, fingerprint inputs."""
    by_id = {s.case.case_id: s for s in snapshots}
    primary, audit, content_hashes = [], [], {}
    for cid in scope["case_ids"]:
        snap = by_id[cid]
        ordered = sorted(snap.candidates, key=lambda c: _hash(cid, c.candidate_ref))
        label_of = dict(zip([c.candidate_ref for c in ordered], _labels(len(ordered))))
        shown = set(candidate_subset(snap, pairs))
        show = [c for c in ordered if c.candidate_ref in shown]
        full = [{"label": label_of[c.candidate_ref], "question": c.canonical_text,
                 "answer": c.answer_text} for c in ordered]
        view = [{"label": label_of[c.candidate_ref], "question": c.canonical_text,
                 "answer": c.answer_text} for c in show]
        record = {
            "case_id": cid,
            "intent_text": snap.case.intent_text,
            "candidates": view,
            "candidate_view_complete": len(show) == len(ordered),
            "all_candidate_count": len(ordered),
            "all_candidates": full,   # for NEED_FULL_CANDIDATES; same blind format
        }
        primary.append(record)
        content_hashes[cid] = sha256_text(json.dumps(record, ensure_ascii=False, sort_keys=True))
        gold_refs = list(snap.case.acceptable_candidate_refs)
        owners = alias_owners.get(cid, [])
        audit.append({
            "case_id": cid,
            "label_to_ref": {label_of[c.candidate_ref]: c.candidate_ref for c in ordered},
            "retrieval_rank_by_label": {label_of[c.candidate_ref]: c.order for c in snap.candidates},
            "current_gold_refs": gold_refs,
            "current_gold_labels": [label_of[r] for r in gold_refs if r in label_of],
            "current_gold_in_candidate_set": any(r in label_of for r in gold_refs),
            "current_expected_decision": snap.case.expected_decision,
            "alias_owner_qna_ids": owners,
            "alias_collision": bool(owners) and not any(f"qna:{o}" in gold_refs for o in owners),
            "prior_review_decision": snap.case.review_decision,
            "prior_review_tags": [t for t in snap.case.tags if t.startswith("review:")
                                  or t == "kb_overlap_flagged"],
            "scope_reasons": scope["reasons"][cid],
            "pool_status": snap.pool_status.value,
        })
    return {"primary": primary, "audit": audit, "content_hashes": content_hashes}


def packet_fingerprint(scope: dict, content_hashes: Mapping[str, str], snapshot_fp: str,
                       postmortem_fp: str) -> str:
    return fingerprint({"schema": REVIEW_SCHEMA_VERSION, "snapshot": snapshot_fp,
                        "postmortem": postmortem_fp, "case_ids": scope["case_ids"],
                        "content": [content_hashes[c] for c in scope["case_ids"]]})


def review_template_csv(primary: Sequence[dict]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for rec in primary:
        writer.writerow({
            "case_id": rec["case_id"], "intent_text": rec["intent_text"],
            "shown_labels": " ".join(c["label"] for c in rec["candidates"]),
            "candidate_view_complete": str(rec["candidate_view_complete"]).lower(),
            "all_candidate_count": rec["all_candidate_count"],
            "blind_decision": "", "acceptable_labels": "", "review_note": "",
            "reviewer": "", "reviewed_at": "",
        })
    return buf.getvalue()


def review_markdown(primary: Sequence[dict]) -> str:
    lines = ["# Blind semantic review packet", "",
             "Decide from the user text and the candidates only. Record the decision in "
             "`review-template.csv`. See README.md for the decision rules.", ""]
    for rec in primary:
        flag = "" if rec["candidate_view_complete"] else \
            f" — subset shown ({len(rec['candidates'])}/{rec['all_candidate_count']}); " \
            "use NEED_FULL_CANDIDATES if none fits"
        lines += [f"## Case {rec['case_id']}{flag}", "", f"> {rec['intent_text']}", ""]
        for cand in rec["candidates"]:
            lines += [f"**{cand['label']}.** {cand['question']}", "",
                      f"{cand['answer']}", ""]
    return "\n".join(lines)


# ── apply (child Gold) ─────────────────────────────────────────────────────

def read_decisions(text: str) -> list[dict]:
    return list(csv.DictReader(io.StringIO(text)))


def derive_decision(row: dict, audit: dict) -> dict:
    """Blind decision → final decision vs current Gold (after freeze)."""
    blind = (row.get("blind_decision") or "").strip()
    if not blind:
        return {"final": "UNREVIEWED"}
    if blind not in BLIND_DECISIONS:
        raise ValueError(f"case {row['case_id']}: unknown blind_decision {blind!r}")
    if blind != "SELECT_ACCEPTABLE":
        if (row.get("acceptable_labels") or "").strip():
            raise ValueError(f"case {row['case_id']}: labels only allowed with SELECT_ACCEPTABLE")
        return {"final": blind}
    labels = [label for label in (row.get("acceptable_labels") or "").replace(",", " ").split()]
    if not labels:
        raise ValueError(f"case {row['case_id']}: SELECT_ACCEPTABLE requires acceptable_labels")
    unknown = [label for label in labels if label not in audit["label_to_ref"]]
    if unknown:
        raise ValueError(f"case {row['case_id']}: unknown labels {unknown}")
    refs = sorted({audit["label_to_ref"][label] for label in labels})
    current = sorted(audit["current_gold_refs"])
    if refs == current:
        final = "KEEP_CURRENT"
    elif len(refs) == 1:
        final = "CHANGE_GOLD"
    else:
        final = "MULTI_ACCEPTABLE"
    return {"final": final, "refs": refs}


def apply_adjudication(parent_cases: Sequence[BenchmarkCase], audit: Sequence[dict],
                       rows: Sequence[dict], *, packet_fp: str, expected_packet_fp: str,
                       parent_fp: str) -> dict:
    """Child case set + provenance; the parent objects are never mutated."""
    if packet_fp != expected_packet_fp:
        raise SystemExit("decisions belong to a different review packet (fingerprint mismatch)")
    audit_by = {a["case_id"]: a for a in audit}
    rows_by = {r["case_id"]: r for r in rows}
    unknown = sorted(set(rows_by) - set(audit_by))
    if unknown:
        raise SystemExit(f"decision rows for cases outside the packet: {unknown}")
    child, provenance, finals = [], [], {}
    for case in parent_cases:
        new = case
        if case.case_id in audit_by:
            row = rows_by.get(case.case_id, {"case_id": case.case_id})
            derived = derive_decision(row, audit_by[case.case_id])
            finals[case.case_id] = derived["final"]
            update = None
            if derived["final"] in ("CHANGE_GOLD", "MULTI_ACCEPTABLE"):
                update = {"expected_decision": "SELECT", "acceptable_candidate_refs": derived["refs"],
                          "evaluation_status": EvaluationStatus.SELECTOR_EVALUABLE,
                          "status_reason": None}
            elif derived["final"] == "EXPECT_NONE":
                update = {"expected_decision": "NONE", "acceptable_candidate_refs": [],
                          "evaluation_status": EvaluationStatus.SELECTOR_EVALUABLE,
                          "status_reason": None}
            elif derived["final"] == "EXCLUDE_AMBIGUOUS":
                update = {"evaluation_status": EvaluationStatus.EXCLUDED,
                          "status_reason": "SEMANTIC_ADJUDICATION_EXCLUDE_AMBIGUOUS"}
            elif derived["final"] in ("CONTENT_REVIEW_REQUIRED", "RETRIEVAL_OR_KB_MAPPING_REVIEW"):
                update = {"evaluation_status": EvaluationStatus.HOLD,
                          "status_reason": f"SEMANTIC_ADJUDICATION_{derived['final']}"}
            if update is not None:
                new = BenchmarkCase.model_validate({**case.model_dump(), **update,
                                                    "review_decision": derived["final"]})
                provenance.append({
                    "case_id": case.case_id,
                    "previous": {"expected_decision": case.expected_decision,
                                 "acceptable_candidate_refs": case.acceptable_candidate_refs,
                                 "evaluation_status": case.evaluation_status.value},
                    "new": {"expected_decision": new.expected_decision,
                            "acceptable_candidate_refs": new.acceptable_candidate_refs,
                            "evaluation_status": new.evaluation_status.value},
                    "decision": derived["final"],
                    "review_note": row.get("review_note"), "reviewer": row.get("reviewer"),
                    "reviewed_at": row.get("reviewed_at"),
                    "source_packet_fingerprint": packet_fp, "parent_fingerprint": parent_fp,
                })
        child.append(new)
    counts: dict[str, int] = {}
    for final in finals.values():
        counts[final] = counts.get(final, 0) + 1
    child_fp = fingerprint([c.model_dump(mode="json") for c in child])
    return {"cases": child, "provenance": provenance, "decision_counts": counts,
            "child_fingerprint": child_fp, "parent_fingerprint": parent_fp,
            "source_packet_fingerprint": packet_fp}


# ── re-score saved outputs (no provider call) ──────────────────────────────

def reclassify(result: BenchmarkResult, case: BenchmarkCase) -> Outcome:
    """Outcome of a SAVED model output against (possibly new) expectations."""
    if result.outcome in (Outcome.INVALID_OUTPUT, Outcome.MODEL_ERROR, Outcome.TIMEOUT):
        return result.outcome
    if result.decision == "NONE":
        return Outcome.CORRECT_NONE if case.expected_decision == "NONE" else Outcome.FALSE_NONE
    if case.expected_decision == "NONE":
        return Outcome.FALSE_SELECT
    if result.selected_candidate_ref in case.acceptable_candidate_refs:
        return Outcome.CORRECT_SELECT
    return Outcome.WRONG_SELECT


def rescore(snapshots: Sequence[CaseSnapshot], child_cases: Iterable[BenchmarkCase],
            results: Mapping[str, BenchmarkResult]) -> tuple[list[CaseSnapshot], dict]:
    """New snapshots (same candidates/order, new expectations) + re-scored results."""
    by_case = {c.case_id: c for c in child_cases}
    new_snaps, new_results = [], {}
    for snap in snapshots:
        case = by_case.get(snap.case.case_id, snap.case)
        new_snaps.append(snap.model_copy(update={"case": case}))
        result = results.get(snap.case.case_id)
        if result is None or case.evaluation_status is not EvaluationStatus.SELECTOR_EVALUABLE:
            continue
        outcome = reclassify(result, case)
        new_results[snap.case.case_id] = result.model_copy(update={
            "outcome": outcome, "correct": outcome in CORRECT_OUTCOMES,
            "expected_decision": case.expected_decision,
            "expected_refs": list(case.acceptable_candidate_refs)})
    return new_snaps, new_results


def write_immutable(directory: Path, files: Mapping[str, str]) -> dict:
    """Write once; an existing artifact must be byte-identical."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    hashes = {}
    for name, text in files.items():
        path = directory / name
        if path.exists() and path.read_text(encoding="utf-8") != text:
            raise SystemExit(f"{path} exists with different content (review packet is immutable)")
        hashes[name] = sha256_text(text)
    for name, text in files.items():
        path = directory / name
        if not path.exists():
            path.write_text(text, encoding="utf-8")
    return hashes


def load_locked_review(directory: Path) -> tuple[dict, list[dict]]:
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    for name, digest in manifest["artifact_sha256"].items():
        if sha256_text((directory / name).read_text(encoding="utf-8")) != digest:
            raise SystemExit(f"review artifact {name} changed after freeze")
    audit = [json.loads(line) for line in (directory / "audit-view.jsonl").read_text(
        encoding="utf-8").splitlines() if line.strip()]
    return manifest, audit


def primary_view_violations(primary: Sequence[dict], forbidden_texts: Iterable[str] = ()) -> list[str]:
    """Contamination check used by tests and at build time."""
    problems = []

    def walk(value, path):
        if isinstance(value, dict):
            for key, inner in value.items():
                if key in PRIMARY_FORBIDDEN_KEYS:
                    problems.append(f"{path}.{key}")
                walk(inner, f"{path}.{key}")
        elif isinstance(value, list):
            for i, inner in enumerate(value):
                walk(inner, f"{path}[{i}]")

    for rec in primary:
        walk(rec, rec.get("case_id", "?"))
    blob = json.dumps(list(primary), ensure_ascii=False)
    for text in ("qna:", "calendar:", *forbidden_texts):
        if text and text in blob:
            problems.append(f"forbidden text {text!r}")
    return problems


README = """# Blind semantic Gold review ({schema})

Files:
- `review-packet.md` — human-readable cases (user text + anonymous candidates A, B, ...
  with canonical question and curated answer). Order is neutral (not retrieval rank).
- `review-cases.jsonl` — same, machine-readable; `all_candidates` lists every candidate
  when the shown subset is incomplete.
- `review-template.csv` — record decisions here (one row per case).
- `audit-view.jsonl` — SECONDARY view (label→ref map, current Gold, alias provenance,
  prior review, scope reason). Do NOT open it before all blind decisions are recorded.
- `manifest.json` — fingerprints; the packet is immutable once review starts.

Model outputs (production / variants / first-candidate) are not part of this packet.

Blind decision (`blind_decision`):
- SELECT_ACCEPTABLE + `acceptable_labels` (e.g. `B` or `B D`): the labeled candidate(s)
  meet the user's practical need. Several labels = several acceptable answers for ONE
  intent (not multi-intent).
- EXPECT_NONE: none of the shown candidates reasonably meets the need. Not for "unsure
  which one" (use several labels) and not for unclear messages (use EXCLUDE_AMBIGUOUS).
- EXCLUDE_AMBIGUOUS: the message is too ambiguous to benchmark.
- CONTENT_REVIEW_REQUIRED: the intent is clear but the KB content itself is deficient.
- RETRIEVAL_OR_KB_MAPPING_REVIEW: the right answer is not among the candidates / the
  mapping is clearly wrong.
- NEED_FULL_CANDIDATES: the shown subset is insufficient; review `all_candidates`.

Rules:
- Practical answer: does the candidate (question AND answer) meet the user's practical
  information need? Exact wording is not required; same topic alone is not enough.
- General vs specific: never assume a qualifier the user did not state (no "merkezi"
  → do not require the merkezi candidate). If the user explicitly states a qualifier
  (e.g. "ikinci üniversite", "sınavsız", "merkezi"), the specific candidate is natural.
  The historical alias mapping does not make the current Gold right.

After freeze, KEEP_CURRENT / CHANGE_GOLD / MULTI_ACCEPTABLE are derived automatically by
comparing your labels with the current Gold (you never need to know it).
"""
