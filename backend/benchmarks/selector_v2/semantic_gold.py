"""Phase 7B semantic adjudication: review lock → child Semantic Gold → re-score.

Order is enforced:

1. ``lock_review`` validates the 106 human decisions against the blind packet
   (never the audit view) and writes an immutable review lock.
2. ``open_audit_after_lock`` is the ONLY reader of ``audit-view.jsonl``; it
   refuses unless a valid lock exists.
3. Labels are mapped to candidate refs by recomputing the blind labelling
   from the snapshot (sha256(case_id|candidate_ref)) and cross-checking it
   against the audit mapping — never from model outputs or retrieval order.
4. ``build_semantic_gold`` derives KEEP/CHANGE/MULTI/NONE/exclusions and
   writes a CHILD case set; the parent reviewed Gold is never mutated.
5. ``rescore_run`` re-scores SAVED outputs (no provider call).
"""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Mapping, Optional, Sequence

from benchmarks.selector_v2.adjudication import (
    BLIND_DECISIONS,
    FOLLOWUP_DECISIONS,
    REVIEW_SCHEMA_VERSION,
    _hash,
    _labels,
    load_primary_review,
    reclassify,
    write_immutable,
)
from benchmarks.selector_v2.contract import fingerprint, sha256_text
from benchmarks.selector_v2.postmortem import _case_key
from benchmarks.selector_v2.schema import (
    CORRECT_OUTCOMES,
    BenchmarkCase,
    BenchmarkResult,
    CaseSnapshot,
    EvaluationStatus,
    PoolStatus,
)

LOCK_SCHEMA_VERSION = "selector-semantic-review-lock-v1"
SEMANTIC_GOLD_VERSION = "selector-semantic-gold-v1"
LOCK_DIR = "locked"
EXCLUSION_REASON = {
    "EXCLUDE_AMBIGUOUS": (EvaluationStatus.EXCLUDED, "ambiguous_user_intent"),
    "CONTENT_REVIEW_REQUIRED": (EvaluationStatus.HOLD, "content_review_required"),
    "RETRIEVAL_OR_KB_MAPPING_REVIEW": (EvaluationStatus.HOLD, "retrieval_or_kb_mapping_review"),
}


def _read_exact(path: Path) -> str:
    """Text without newline translation (hashes must match written bytes)."""
    with open(path, encoding="utf-8", newline="") as handle:
        return handle.read()


def _labels_of(value: str) -> list[str]:
    return (value or "").replace(",", " ").split()


def read_csv_rows(text: str) -> list[dict]:
    return list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))


# ── 1. review lock (no audit access) ───────────────────────────────────────

def lock_review(*, decisions_bytes: bytes, expected_sha: str, review_dir: Path,
                followup_dir: Path) -> dict:
    """Validate the merged human decisions; return the lock (not yet written).

    The sha256 is taken over the raw file bytes (no newline translation)."""
    import hashlib

    decisions_sha = hashlib.new("sha256", decisions_bytes).hexdigest()
    decisions_text = decisions_bytes.decode("utf-8")
    if decisions_sha != expected_sha:
        raise SystemExit(f"decision file sha256 {decisions_sha} != expected {expected_sha}")
    parent, primary, template = load_primary_review(review_dir)
    fmanifest = json.loads((Path(followup_dir) / "manifest.json").read_text(encoding="utf-8"))
    for name, digest in fmanifest["artifact_sha256"].items():
        if sha256_text(_read_exact(Path(followup_dir) / name)) != digest:
            raise SystemExit(f"follow-up artifact {name} changed")
    first_pass = {r["case_id"]: r for r in read_csv_rows(
        _read_exact(Path(followup_dir) / "first-pass-decisions.csv"))}
    rows = read_csv_rows(decisions_text)
    problems = []
    ids = [r["case_id"] for r in rows]
    if len(rows) != len(template) or len(set(ids)) != len(ids):
        problems.append(f"expected {len(template)} unique rows, got {len(rows)} ({len(set(ids))} unique)")
    if set(ids) != {r["case_id"] for r in template}:
        problems.append("case ids differ from the blind packet")
    labels = {rec["case_id"]: {c["label"] for c in rec["all_candidates"]} for rec in primary}
    followup_ids = set(fmanifest["case_ids"])
    round_ids = {r["case_id"] for r in rows if r.get("review_round") == "FULL_CANDIDATE_FOLLOWUP"}
    if round_ids != followup_ids:
        problems.append("FULL_CANDIDATE_FOLLOWUP rows differ from the follow-up case ids")
    records = []
    for row in rows:
        cid, decision = row["case_id"], (row.get("blind_decision") or "").strip()
        chosen = _labels_of(row.get("acceptable_labels"))
        allowed = FOLLOWUP_DECISIONS if cid in followup_ids else BLIND_DECISIONS
        if not decision:
            problems.append(f"{cid}: blank decision")
        elif decision == "NEED_FULL_CANDIDATES" or decision not in allowed:
            problems.append(f"{cid}: decision {decision!r} not allowed")
        if decision == "SELECT_ACCEPTABLE" and not chosen:
            problems.append(f"{cid}: SELECT_ACCEPTABLE without labels")
        if decision != "SELECT_ACCEPTABLE" and chosen:
            problems.append(f"{cid}: labels on {decision}")
        unknown = [c for c in chosen if c not in labels.get(cid, set())]
        if unknown:
            problems.append(f"{cid}: unknown labels {unknown}")
        if cid not in followup_ids:
            before = first_pass.get(cid, {})
            if ((before.get("blind_decision") or "").strip(), _labels_of(before.get("acceptable_labels"))) \
                    != (decision, chosen):
                problems.append(f"{cid}: first-pass decision changed")
        elif (first_pass.get(cid, {}).get("blind_decision") or "").strip() != "NEED_FULL_CANDIDATES":
            problems.append(f"{cid}: follow-up case was not NEED_FULL_CANDIDATES in the first pass")
        records.append({"case_id": cid, "blind_decision": decision, "acceptable_labels": chosen,
                        "review_note": row.get("review_note", ""), "reviewer": row.get("reviewer", ""),
                        "reviewed_at": row.get("reviewed_at", ""),
                        "review_round": row.get("review_round", ""),
                        "candidate_label_set_sha256": sha256_text(json.dumps(
                            sorted(labels.get(cid, set())), ensure_ascii=False))})
    if problems:
        raise SystemExit(f"review lock refused: {problems[:15]}")
    records.sort(key=lambda r: _case_key(r["case_id"]))
    counts: dict[str, int] = {}
    for rec in records:
        counts[rec["blind_decision"]] = counts.get(rec["blind_decision"], 0) + 1
    identity = {
        "schema": LOCK_SCHEMA_VERSION, "review_schema": REVIEW_SCHEMA_VERSION,
        "source_decision_sha256": decisions_sha,
        "parent_review_packet_fingerprint": parent["review_packet_fingerprint"],
        "followup_fingerprint": fmanifest["followup_fingerprint"],
        "snapshot_fingerprint": parent["source_snapshot_fingerprint"],
        "records": records,
    }
    return {"records": records, "identity": identity, "decision_counts": counts,
            "lock_fingerprint": fingerprint(identity),
            "rounds": {"FIRST_PASS": len(records) - len(round_ids),
                       "FULL_CANDIDATE_FOLLOWUP": len(round_ids)}}


def write_lock(lock_dir: Path, lock: dict, locked_at: str) -> dict:
    body = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in lock["records"])
    hashes = write_immutable(lock_dir, {"locked-review.jsonl": body})
    identity = {k: v for k, v in lock["identity"].items() if k != "records"}
    manifest = {**identity, "lock_fingerprint": lock["lock_fingerprint"],
                "review_case_count": len(lock["records"]), "decision_counts": lock["decision_counts"],
                "rounds": lock["rounds"], "locked_artifact_sha256": hashes, "locked_at": locked_at,
                "audit_view_read_before_lock": False}
    existing = Path(lock_dir) / "lock-manifest.json"
    if existing.exists():
        prior = json.loads(existing.read_text(encoding="utf-8"))
        if prior["lock_fingerprint"] != lock["lock_fingerprint"]:
            raise SystemExit("a different review lock already exists (immutable)")
        return prior
    write_immutable(lock_dir, {"lock-manifest.json": json.dumps(
        manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"})
    return manifest


def load_lock(lock_dir: Path) -> tuple[dict, list[dict]]:
    lock_dir = Path(lock_dir)
    manifest_path = lock_dir / "lock-manifest.json"
    if not manifest_path.exists():
        raise SystemExit("no review lock: the audit view stays closed")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    body = (lock_dir / "locked-review.jsonl").read_text(encoding="utf-8")
    if sha256_text(body) != manifest["locked_artifact_sha256"]["locked-review.jsonl"]:
        raise SystemExit("locked review changed after lock")
    records = [json.loads(line) for line in body.splitlines() if line.strip()]
    identity = {k: manifest[k] for k in ("schema", "review_schema", "source_decision_sha256",
                                         "parent_review_packet_fingerprint", "followup_fingerprint",
                                         "snapshot_fingerprint")}
    if fingerprint({**identity, "records": records}) != manifest["lock_fingerprint"]:
        raise SystemExit("review lock fingerprint mismatch")
    return manifest, records


def open_audit_after_lock(review_dir: Path, lock_dir: Path) -> tuple[dict, list[dict], list[dict]]:
    """The only reader of audit-view.jsonl; requires a valid lock."""
    lock_manifest, records = load_lock(lock_dir)
    parent = json.loads((Path(review_dir) / "manifest.json").read_text(encoding="utf-8"))
    if parent["review_packet_fingerprint"] != lock_manifest["parent_review_packet_fingerprint"]:
        raise SystemExit("lock belongs to another review packet")
    text = (Path(review_dir) / "audit-view.jsonl").read_text(encoding="utf-8")
    if sha256_text(text) != parent["artifact_sha256"]["audit-view.jsonl"]:
        raise SystemExit("audit view changed")
    audit = [json.loads(line) for line in text.splitlines() if line.strip()]
    return lock_manifest, records, audit


# ── 2. mapping + derived outcomes ──────────────────────────────────────────

def blind_label_map(snapshot: CaseSnapshot) -> dict[str, str]:
    cid = snapshot.case.case_id
    ordered = sorted(snapshot.candidates, key=lambda c: _hash(cid, c.candidate_ref))
    return dict(zip(_labels(len(ordered)), [c.candidate_ref for c in ordered]))


def derive(record: dict, audit: dict, label_map: Mapping[str, str]) -> dict:
    decision = record["blind_decision"]
    old = sorted(audit["current_gold_refs"])
    if decision != "SELECT_ACCEPTABLE":
        return {"outcome": decision, "refs": [], "old": old}
    refs = sorted({label_map[label] for label in record["acceptable_labels"]})
    if refs == old:
        outcome = "KEEP_CURRENT"
    elif len(refs) == 1:
        outcome = "CHANGE_GOLD"
    else:
        outcome = "MULTI_ACCEPTABLE"
    return {"outcome": outcome, "refs": refs, "old": old}


def build_semantic_gold(snapshots: Sequence[CaseSnapshot], records: Sequence[dict],
                        audit: Sequence[dict], *, lock_fp: str, parent_fp: str) -> dict:
    by_id = {s.case.case_id: s for s in snapshots}
    audit_by = {a["case_id"]: a for a in audit}
    derived, provenance = {}, []
    for rec in records:
        cid = rec["case_id"]
        label_map = blind_label_map(by_id[cid])
        if label_map != audit_by[cid]["label_to_ref"]:
            raise SystemExit(f"{cid}: audit label mapping differs from the blind labelling")
        d = derive(rec, audit_by[cid], label_map)
        derived[cid] = d
    child_cases = []
    for snap in snapshots:
        case = snap.case
        d = derived.get(case.case_id)
        new = case
        if d is not None:
            outcome = d["outcome"]
            if outcome in ("CHANGE_GOLD", "MULTI_ACCEPTABLE"):
                update = {"expected_decision": "SELECT", "acceptable_candidate_refs": d["refs"],
                          "evaluation_status": EvaluationStatus.SELECTOR_EVALUABLE,
                          "status_reason": None}
            elif outcome == "EXPECT_NONE":
                update = {"expected_decision": "NONE", "acceptable_candidate_refs": [],
                          "evaluation_status": EvaluationStatus.SELECTOR_EVALUABLE,
                          "status_reason": None}
            elif outcome in EXCLUSION_REASON:
                status, reason = EXCLUSION_REASON[outcome]
                update = {"evaluation_status": status, "status_reason": reason}
            else:
                update = {}
            if update:
                new = BenchmarkCase.model_validate({**case.model_dump(), **update,
                                                    "review_decision": f"SEMANTIC_{outcome}"})
            rec = next(r for r in records if r["case_id"] == case.case_id)
            provenance.append({
                "case_id": case.case_id,
                "previous": {"expected_decision": case.expected_decision,
                             "acceptable_candidate_refs": case.acceptable_candidate_refs,
                             "evaluation_status": case.evaluation_status.value},
                "new": {"expected_decision": new.expected_decision,
                        "acceptable_candidate_refs": new.acceptable_candidate_refs,
                        "evaluation_status": new.evaluation_status.value,
                        "status_reason": new.status_reason},
                "human_blind_decision": rec["blind_decision"],
                "human_labels": rec["acceptable_labels"],
                "derived_outcome": outcome,
                "review_note": rec["review_note"], "reviewer": rec["reviewer"],
                "reviewed_at": rec["reviewed_at"], "review_round": rec["review_round"],
                "source_review_lock_fingerprint": lock_fp, "parent_gold_fingerprint": parent_fp,
            })
        child_cases.append(new)
    return {"cases": child_cases, "derived": derived, "provenance": provenance,
            "fingerprint": fingerprint({"version": SEMANTIC_GOLD_VERSION, "lock": lock_fp,
                                        "parent": parent_fp,
                                        "cases": [c.model_dump(mode="json") for c in child_cases]})}


def accounting(records: Sequence[dict], audit: Sequence[dict], derived: Mapping[str, dict]) -> dict:
    audit_by = {a["case_id"]: a for a in audit}
    out = {"outcomes": {}, "old_gold_remains_acceptable": 0, "old_gold_rejected": 0,
           "old_gold_absent_from_candidate_set": 0, "old_gold_not_assessed": 0,
           "alias": {"old_gold_from_alias_ownership": 0, "alias_collision": 0,
                     "human_disagrees_with_alias_derived_gold": 0},
           "blind_control": {}, "old_gold_rejected_ids": []}
    for rec in records:
        cid, d, a = rec["case_id"], derived[rec["case_id"]], audit_by[rec["case_id"]]
        out["outcomes"][d["outcome"]] = out["outcomes"].get(d["outcome"], 0) + 1
        old = set(a["current_gold_refs"])
        if not a["current_gold_in_candidate_set"]:
            out["old_gold_absent_from_candidate_set"] += 1
        if rec["blind_decision"] in ("SELECT_ACCEPTABLE", "EXPECT_NONE"):
            if set(d["refs"]) & old:
                out["old_gold_remains_acceptable"] += 1
            else:
                out["old_gold_rejected"] += 1
                out["old_gold_rejected_ids"].append(cid)
        else:
            out["old_gold_not_assessed"] += 1
        owners = {f"qna:{o}" for o in a["alias_owner_qna_ids"]}
        alias_gold = bool(owners & old)
        assessed = rec["blind_decision"] in ("SELECT_ACCEPTABLE", "EXPECT_NONE")
        out["alias"]["old_gold_from_alias_ownership"] += alias_gold
        out["alias"]["alias_collision"] += bool(a["alias_collision"])
        # Disagreement only where the human actually judged the candidates.
        if alias_gold and assessed and not (set(d["refs"]) & old):
            out["alias"]["human_disagrees_with_alias_derived_gold"] += 1
        if alias_gold and not assessed:
            out["alias"]["alias_derived_gold_not_assessed"] = \
                out["alias"].get("alias_derived_gold_not_assessed", 0) + 1
        if "BLIND_CONTROL" in a["scope_reasons"]:
            key = d["outcome"]
            out["blind_control"][key] = out["blind_control"].get(key, 0) + 1
    out["blind_control"]["count"] = sum(v for k, v in out["blind_control"].items())
    return out


# ── 3. re-score saved outputs (no provider call) ───────────────────────────

def repool(snapshot: CaseSnapshot, case: BenchmarkCase) -> CaseSnapshot:
    refs = {c.candidate_ref for c in snapshot.candidates}
    if case.evaluation_status is not EvaluationStatus.SELECTOR_EVALUABLE:
        status = snapshot.pool_status
    elif case.expected_decision == "NONE":
        status = PoolStatus.NOT_APPLICABLE if refs else PoolStatus.EMPTY_POOL
    elif set(case.acceptable_candidate_refs) & refs:
        status = PoolStatus.IN_POOL
    else:
        status = snapshot.pool_status if snapshot.pool_status in (
            PoolStatus.RETRIEVAL_MISS, PoolStatus.ELIGIBILITY_MISS, PoolStatus.BUDGET_MISS) \
            else PoolStatus.RETRIEVAL_MISS
    return snapshot.model_copy(update={"case": case, "pool_status": status})


def semantic_snapshots(snapshots: Sequence[CaseSnapshot],
                       child_cases: Sequence[BenchmarkCase]) -> list[CaseSnapshot]:
    by_case = {c.case_id: c for c in child_cases}
    return [repool(s, by_case.get(s.case.case_id, s.case)) for s in snapshots]


def rescore_run(snapshots: Sequence[CaseSnapshot],
                results: Mapping[str, BenchmarkResult]) -> dict[str, BenchmarkResult]:
    """Saved outputs re-scored against the snapshots' (semantic) expectations."""
    out = {}
    for snap in snapshots:
        result = results.get(snap.case.case_id)
        if result is None or not snap.selector_evaluable:
            continue
        outcome = reclassify(result, snap.case)
        out[snap.case.case_id] = result.model_copy(update={
            "outcome": outcome, "correct": outcome in CORRECT_OUTCOMES,
            "expected_decision": snap.case.expected_decision,
            "expected_refs": list(snap.case.acceptable_candidate_refs)})
    return out


def denominators(snapshots: Sequence[CaseSnapshot]) -> dict:
    primary = [s for s in snapshots if s.case.primary]
    evaluable = [s for s in primary if s.selector_evaluable]
    reasons: dict[str, int] = {}
    for s in primary:
        if not s.selector_evaluable:
            key = s.case.status_reason or s.pool_status.value
            reasons[key] = reasons.get(key, 0) + 1
    return {
        "primary_cases": len(primary),
        "evaluable": len(evaluable),
        "evaluable_select": sum(1 for s in evaluable if s.case.expected_decision == "SELECT"),
        "evaluable_none": sum(1 for s in evaluable if s.case.expected_decision == "NONE"),
        "multi_acceptable": sum(1 for s in evaluable if s.case.multi_acceptable),
        "not_evaluable_by_reason": dict(sorted(reasons.items())),
    }
