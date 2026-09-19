"""Frozen candidate snapshots: generate once, reuse for every model/config.

Generation calls the production pool builder
(``answer_pipeline._build_candidate_pool_result``: Qdrant 24 + Meili 5 QnA
retrieval, objective eligibility, deterministic budget) exactly as a
production intent would, with the Calendar route closed unless the dataset
explicitly states Calendar relevance. No selector and no Intent Analyzer is
called. The result is written as versioned JSONL plus a manifest and a
content fingerprint; loading re-verifies that fingerprint.
"""
from __future__ import annotations

import inspect
import json
import math
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

from benchmarks.selector_v2.contract import fingerprint, sha256_text
from benchmarks.selector_v2.schema import (
    BenchmarkCase,
    CaseSnapshot,
    EvaluationStatus,
    FrozenCandidate,
    MISS_STATUSES,
    PoolStatus,
)
from services.candidate_eligibility import CandidateSetBuild

SNAPSHOT_FORMAT = 1
SNAPSHOT_FILE = "snapshot.jsonl"
MANIFEST_FILE = "snapshot-manifest.json"
DEFAULT_NEAR_PAIRS = Path(__file__).with_name("near_qna_pairs.json")

BuildPool = Callable[[str, list], CandidateSetBuild]
CalendarLookup = Callable[[str], list]


# ── near-QnA / general-specific fixture ────────────────────────────────────

@dataclass(frozen=True)
class NearPair:
    a: int
    b: int
    relation: str
    questions: tuple[str, str]
    roles: tuple[Optional[str], Optional[str]] = (None, None)

    @property
    def key(self) -> str:
        return f"{self.a}-{self.b}"

    def sibling(self, qna_id: int) -> Optional[int]:
        return self.b if qna_id == self.a else self.a if qna_id == self.b else None

    def role(self, qna_id: int) -> Optional[str]:
        return self.roles[0] if qna_id == self.a else self.roles[1] if qna_id == self.b else None


def load_near_pairs(path: Path = DEFAULT_NEAR_PAIRS) -> list[NearPair]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [
        NearPair(
            a=int(item["a"]["qna_id"]),
            b=int(item["b"]["qna_id"]),
            relation=item["relation"],
            questions=(item["a"]["question"], item["b"]["question"]),
            roles=(item["a"].get("role"), item["b"].get("role")),
        )
        for item in data["pairs"]
    ]


def derive_tags(snapshot: CaseSnapshot, pairs: Sequence[NearPair]) -> list[str]:
    """Post-snapshot slices: a near pair counts only when the non-acceptable
    sibling is actually competing in this case's frozen candidate set."""
    in_pool = {c.candidate_ref for c in snapshot.candidates}
    acceptable = set(snapshot.case.acceptable_candidate_refs)
    tags: set[str] = set()
    for pair in pairs:
        for member in (pair.a, pair.b):
            sibling = pair.sibling(member)
            if f"qna:{member}" in acceptable and f"qna:{sibling}" in in_pool \
                    and f"qna:{sibling}" not in acceptable:
                tags.update({"near_qna", f"near_qna_pair:{pair.key}"})
                if pair.relation == "general_specific":
                    tags.update({"general_specific", f"gs_expected:{pair.role(member)}"})
    if any(c.kind == "CALENDAR" for c in snapshot.candidates):
        tags.add("calendar_candidate_present")
    if any(r.startswith("calendar:") for r in acceptable):
        tags.add("calendar")
    return sorted(tags)


# ── pool classification ────────────────────────────────────────────────────

def classify_pool(case: BenchmarkCase, build: CandidateSetBuild) -> tuple[PoolStatus, dict]:
    selector_refs = [c.candidate_ref for c in build.candidates]
    truncated = list(build.trace_snapshot.get("truncated_candidate_refs") or [])
    excluded = [
        {"candidate_ref": e.candidate_ref, "reason": e.reason}
        for e in build.exclusions if e.candidate_ref is not None
    ]
    retrieved: list[str] = []
    for ref in [*selector_refs, *truncated, *(e["candidate_ref"] for e in excluded)]:
        if ref not in retrieved:
            retrieved.append(ref)
    detail = {"retrieved_refs": retrieved, "excluded": excluded, "truncated_refs": truncated}
    if case.expected_decision == "NONE":
        return (PoolStatus.NOT_APPLICABLE if selector_refs else PoolStatus.EMPTY_POOL), detail
    acceptable = set(case.acceptable_candidate_refs)
    if acceptable & set(selector_refs):
        return PoolStatus.IN_POOL, detail
    if acceptable & set(truncated):
        return PoolStatus.BUDGET_MISS, detail
    if acceptable & {e["candidate_ref"] for e in excluded}:
        return PoolStatus.ELIGIBILITY_MISS, detail
    return PoolStatus.RETRIEVAL_MISS, detail


def generate_snapshots(
    cases: Iterable[BenchmarkCase],
    *,
    build_pool: BuildPool,
    calendar_lookup: Optional[CalendarLookup] = None,
    pairs: Sequence[NearPair] = (),
) -> list[CaseSnapshot]:
    snapshots = []
    for case in cases:
        if case.evaluation_status is not EvaluationStatus.SELECTOR_EVALUABLE:
            snapshots.append(CaseSnapshot(case=case, pool_status=PoolStatus.NOT_SNAPSHOTTED))
            continue
        calendar_entries: list = []
        if case.calendar_relevant:
            if calendar_lookup is None:
                raise RuntimeError(f"case {case.case_id} is calendar_relevant but no Calendar lookup")
            calendar_entries = list(calendar_lookup(case.intent_text))
        build = build_pool(case.intent_text, calendar_entries)
        status, detail = classify_pool(case, build)
        trace = build.trace_snapshot
        snap = CaseSnapshot(
            case=case,
            pool_status=status,
            candidates=[
                FrozenCandidate.from_selector_candidate(c, order)
                for order, c in enumerate(build.candidates, start=1)
            ],
            retrieved_refs=detail["retrieved_refs"],
            excluded=detail["excluded"],
            truncated_refs=detail["truncated_refs"],
            retrieval_counts={
                "qdrant": trace.get("qdrant_candidate_count", 0),
                "meili": trace.get("meili_candidate_count", 0),
                "calendar": trace.get("calendar_candidate_count", 0),
                "before_eligibility": trace.get("candidate_count_before_eligibility", 0),
                "after_eligibility": trace.get("candidate_count_after_eligibility", 0),
                "selector": trace.get("selector_candidate_count", 0),
                "budget": trace.get("candidate_budget"),
            },
        )
        snap.derived_tags = derive_tags(snap, pairs)
        snapshots.append(snap)
    return snapshots


# ── fingerprint, diagnostics, persistence ──────────────────────────────────

def _identity(snapshot: CaseSnapshot) -> dict:
    """Model-visible content + classification (diagnostic floats excluded)."""
    case = snapshot.case
    return {
        "case_id": case.case_id,
        "intent_text": case.intent_text,
        "expected_decision": case.expected_decision,
        "acceptable_candidate_refs": case.acceptable_candidate_refs,
        "evaluation_status": case.evaluation_status.value,
        "primary": case.primary,
        "pool_status": snapshot.pool_status.value,
        "candidates": [
            [c.order, c.candidate_ref, c.kind, c.canonical_text, c.answer_text]
            for c in sorted(snapshot.candidates, key=lambda c: c.order)
        ],
    }


def snapshot_fingerprint(snapshots: Sequence[CaseSnapshot]) -> str:
    return fingerprint([_identity(s) for s in snapshots])


def percentile(values: Sequence[float], q: float) -> Optional[float]:
    """Nearest-rank percentile (q in 0..100)."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[rank - 1]


def describe(values: Sequence[float]) -> dict:
    if not values:
        return {"n": 0, "mean": None, "median": None, "p95": None, "max": None, "min": None}
    return {
        "n": len(values),
        "mean": round(statistics.fmean(values), 3),
        "median": statistics.median(values),
        "p95": percentile(values, 95),
        "max": max(values),
        "min": min(values),
        "sum": sum(values),
    }


def retrieval_diagnostics(snapshots: Sequence[CaseSnapshot]) -> dict:
    """§44 — these are NOT selector accuracy."""
    candidate_cases = [s for s in snapshots if s.pool_status is not PoolStatus.NOT_SNAPSHOTTED]
    select_cases = [s for s in candidate_cases if s.case.expected_decision == "SELECT"]

    def count(status: PoolStatus, subset=candidate_cases) -> int:
        return sum(1 for s in subset if s.pool_status is status)

    per_status = {status.value: count(status) for status in PoolStatus}
    retrieved = [s for s in select_cases
                 if set(s.case.acceptable_candidate_refs) & set(s.retrieved_refs)]
    return {
        "total_candidate_cases": len(candidate_cases),
        "expected_select_cases": len(select_cases),
        "expected_candidate_retrieved": len(retrieved),
        "expected_candidate_eligible": count(PoolStatus.IN_POOL, select_cases)
        + count(PoolStatus.BUDGET_MISS, select_cases),
        "expected_candidate_in_selector_pool": count(PoolStatus.IN_POOL, select_cases),
        "retrieval_miss": count(PoolStatus.RETRIEVAL_MISS),
        "eligibility_miss": count(PoolStatus.ELIGIBILITY_MISS),
        "budget_miss": count(PoolStatus.BUDGET_MISS),
        "empty_pool": count(PoolStatus.EMPTY_POOL),
        "pool_status": per_status,
        "candidate_count": describe([len(s.candidates) for s in candidate_cases]),
        "truncated_cases": sum(1 for s in candidate_cases if s.truncated_refs),
        "eligibility_excluded_candidates": sum(len(s.excluded) for s in candidate_cases),
        "miss_case_ids": {
            status.value: [s.case.case_id for s in candidate_cases if s.pool_status is status]
            for status in MISS_STATUSES
        },
    }


def write_snapshot(directory: Path, snapshots: Sequence[CaseSnapshot], manifest: dict) -> dict:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    manifest = dict(manifest)
    manifest["snapshot_format"] = SNAPSHOT_FORMAT
    manifest["snapshot_fingerprint"] = snapshot_fingerprint(snapshots)
    lines = [s.model_dump_json() for s in snapshots]
    body = "\n".join(lines) + "\n"
    (directory / SNAPSHOT_FILE).write_text(body, encoding="utf-8")
    manifest["snapshot_file_sha256"] = sha256_text(body)
    (directory / MANIFEST_FILE).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def load_snapshot(directory: Path) -> tuple[dict, list[CaseSnapshot]]:
    directory = Path(directory)
    manifest = json.loads((directory / MANIFEST_FILE).read_text(encoding="utf-8"))
    if manifest.get("snapshot_format") != SNAPSHOT_FORMAT:
        raise ValueError(f"unsupported snapshot_format {manifest.get('snapshot_format')}")
    body = (directory / SNAPSHOT_FILE).read_text(encoding="utf-8")
    if sha256_text(body) != manifest.get("snapshot_file_sha256"):
        raise ValueError("snapshot file does not match its manifest sha256 (modified?)")
    snapshots = [CaseSnapshot.model_validate_json(line) for line in body.splitlines() if line.strip()]
    if snapshot_fingerprint(snapshots) != manifest.get("snapshot_fingerprint"):
        raise ValueError("snapshot fingerprint mismatch")
    return manifest, snapshots


# ── production wiring (read-only DB + local indexes; no LLM) ───────────────

def eligibility_version() -> dict:
    from services import answer_pipeline, candidate_eligibility

    sources = {
        "candidate_eligibility.py": inspect.getsource(candidate_eligibility),
        "answer_pipeline._build_candidate_pool_result": inspect.getsource(
            answer_pipeline._build_candidate_pool_result
        ),
        "answer_pipeline._candidate_sort_key": inspect.getsource(answer_pipeline._candidate_sort_key),
    }
    return {
        "source_sha256": {name: sha256_text(text) for name, text in sources.items()},
        "fingerprint": fingerprint({name: sha256_text(text) for name, text in sources.items()}),
    }


def kb_fingerprint(db) -> dict:
    from core.database import QnA, QnARoutingGuard

    rows = db.query(QnA.id, QnA.question_text, QnA.answer_text, QnA.status).order_by(QnA.id).all()
    guards = db.query(QnARoutingGuard).order_by(QnARoutingGuard.qna_id).all()
    guard_rows = [
        [int(g.qna_id), g.guard_ref, g.selector_mode, g.content_mode,
         str(g.valid_from) if g.valid_from else None,
         str(g.valid_until) if g.valid_until else None, g.on_expiry, int(g.exact_bypass_enabled)]
        for g in guards
    ]
    return {
        "method": "sha256(canonical_json([[id, question_text, answer_text, status] ...] ordered by id))",
        "qna_fingerprint": fingerprint([[int(r[0]), r[1], r[2], int(r[3])] for r in rows]),
        "qna_rows": len(rows),
        "qna_active": sum(1 for r in rows if int(r[3]) == 1),
        "routing_guard_fingerprint": fingerprint(guard_rows),
        "routing_guards": len(guard_rows),
    }


def verify_near_pairs(db, pairs: Sequence[NearPair]) -> list[dict]:
    from core.database import QnA

    ids = sorted({i for p in pairs for i in (p.a, p.b)})
    rows = {int(r[0]): (r[1], int(r[2])) for r in
            db.query(QnA.id, QnA.question_text, QnA.status).filter(QnA.id.in_(ids)).all()}
    result = []
    for pair in pairs:
        for qna_id, question in zip((pair.a, pair.b), pair.questions):
            kb = rows.get(qna_id)
            result.append({
                "pair": pair.key,
                "qna_id": qna_id,
                "exists": kb is not None,
                "active": bool(kb and kb[1] == 1),
                "question_matches_kb": bool(kb and kb[0].strip() == question.strip()),
            })
    return result


def expected_content_drift(db, gold_all_path: Path) -> dict:
    """Compare the Gold's accepted_answers question/answer with the live KB."""
    from core.database import QnA

    from benchmarks.selector_v2.gold_loader import read_jsonl

    gold: dict[int, dict] = {}
    for record in read_jsonl(gold_all_path):
        if record.get("status") != "READY":
            continue
        for intent in record.get("expected_intents") or []:
            for answer in intent.get("accepted_answers") or []:
                gold.setdefault(int(answer["qna_id"]), answer)
    rows = {int(r[0]): (r[1], r[2], int(r[3])) for r in
            db.query(QnA.id, QnA.question_text, QnA.answer_text, QnA.status)
            .filter(QnA.id.in_(sorted(gold))).all()}
    with_question = [i for i, a in gold.items() if a.get("question") is not None]
    with_answer = [i for i, a in gold.items() if a.get("answer") is not None]
    q_diff = [i for i in with_question
              if i not in rows or rows[i][0].strip() != gold[i]["question"].strip()]
    a_diff = [i for i in with_answer
              if i not in rows or rows[i][1].strip() != gold[i]["answer"].strip()]
    return {
        "expected_qna_ids": len(gold),
        "present_in_kb": sum(1 for i in gold if i in rows),
        "active_in_kb": sum(1 for i in gold if i in rows and rows[i][2] == 1),
        "compared_questions": len(with_question),
        "question_mismatch_ids": sorted(q_diff),
        "compared_answers": len(with_answer),
        "answer_mismatch_ids": sorted(a_diff),
    }


def production_pool_builder(db, *, as_of, max_candidates: int) -> BuildPool:
    from services import answer_pipeline
    from services.routing_guards import RoutingGuardPolicy

    policy = RoutingGuardPolicy.load(db, today=as_of)
    active_lookup = answer_pipeline._active_qna_lookup(db)

    def build(intent_text: str, calendar_entries: list) -> CandidateSetBuild:
        return answer_pipeline._build_candidate_pool_result(
            intent_text,
            calendar_entries,
            policy,
            active_qna_lookup=active_lookup,
            max_candidates=max_candidates,
        )

    return build
