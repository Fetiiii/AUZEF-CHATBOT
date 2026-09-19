"""Loaders: generic BenchmarkCase JSONL and the reviewed Gold v2 layout.

The reviewed Gold adapter reads the external (read-only) artifact layout

    <gold_dir>/gold-reviewed-all.jsonl + gold-reviewed-manifest.json
    <session_dir>/session-targets.jsonl + session-gold-manifest.json   (optional)

verifies every file against its manifest's ``outputs_sha256``, cross-checks
the manifest counts against the actual records and maps each record to
``BenchmarkCase`` without modifying the dataset. Review-decision strings are
carried as labels only; exclusion/hold is driven by record ``status``.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from benchmarks.selector_v2 import BENCHMARK_VERSION
from benchmarks.selector_v2.schema import BenchmarkCase, EvaluationStatus

GOLD_ALL = "gold-reviewed-all.jsonl"
GOLD_MANIFEST = "gold-reviewed-manifest.json"
SESSION_TARGETS = "session-targets.jsonl"
SESSION_MANIFEST = "session-gold-manifest.json"

# Documentation reference for reviewed-v2 (never used as a result): the
# loader recomputes these from the files and reports a WARN on mismatch.
REFERENCE_COUNTS = {"evaluation_targets": 503, "context_required": 17, "scorable": 486}

_HOLD_STATUSES = {"CONTENT_REVIEW_HOLD", "SOURCE_MISSING_HOLD", "PENDING_CONTENT"}
_TURN_TAGS = {
    "FIRST_TURN": "first_turn",
    "FOLLOW_UP_CONTEXT_AVAILABLE_BUT_NOT_REQUIRED": "follow_up_context_not_required",
    "FOLLOW_UP_CONTEXT_REQUIRED": "follow_up_context_required",
}

# Coarse PII screen over intent texts (reports case ids only, never text).
_PII_PATTERNS = {
    "email": re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),
    "phone": re.compile(r"(?:\+?90[\s-]?)?0?5\d{2}[\s-]?\d{3}[\s-]?\d{2}[\s-]?\d{2}"),
    "long_digit_id": re.compile(r"(?<!\d)\d{9,}(?!\d)"),
}


@dataclass
class Check:
    name: str
    status: str  # PASS | WARN | FAIL
    detail: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "status": self.status, "detail": self.detail}


@dataclass
class LoadedDataset:
    cases: list[BenchmarkCase]
    checks: list[Check] = field(default_factory=list)
    source: dict = field(default_factory=dict)
    counts: dict = field(default_factory=dict)

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.status == "FAIL"]

    def report(self) -> dict:
        return {
            "source": self.source,
            "counts": self.counts,
            "checks": [c.to_dict() for c in self.checks],
        }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_jsonl(path: Path) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{path}:{number}: invalid JSON ({exc.msg})") from None
    return rows


def load_cases_jsonl(path: Path) -> LoadedDataset:
    """Generic external contract: one ``BenchmarkCase`` JSON object per line."""
    cases = [BenchmarkCase.model_validate(row) for row in read_jsonl(path)]
    ids = Counter(case.case_id for case in cases)
    duplicates = sorted(case_id for case_id, count in ids.items() if count > 1)
    checks = [Check("unique_case_ids", "FAIL" if duplicates else "PASS",
                    f"duplicates: {duplicates}" if duplicates else "")]
    return LoadedDataset(
        cases=cases,
        checks=checks,
        source={"kind": "benchmark_case_jsonl", "path": str(path), "sha256": sha256_file(path)},
        counts=dict(Counter(case.evaluation_status.value for case in cases)),
    )


def _verify_manifest(directory: Path, manifest_name: str, files: Iterable[str],
                     checks: list[Check]) -> dict:
    manifest = json.loads((directory / manifest_name).read_text(encoding="utf-8"))
    expected = manifest.get("outputs_sha256") or {}
    for name in files:
        path = directory / name
        if not path.exists():
            checks.append(Check(f"file_present:{name}", "FAIL", str(path)))
            continue
        actual = sha256_file(path)
        want = expected.get(name)
        if want is None:
            checks.append(Check(f"sha256:{name}", "WARN", "not listed in manifest"))
        elif want != actual:
            checks.append(Check(f"sha256:{name}", "FAIL", f"manifest {want} != actual {actual}"))
        else:
            checks.append(Check(f"sha256:{name}", "PASS", actual))
    return manifest


def _refs(ids) -> list[str]:
    return [f"qna:{int(value)}" for value in ids]


def _pii_hits(text: str) -> list[str]:
    return [name for name, pattern in _PII_PATTERNS.items() if pattern.search(text or "")]


def load_reviewed_gold(gold_dir: Path, session_dir: Optional[Path] = None) -> LoadedDataset:
    gold_dir = Path(gold_dir)
    checks: list[Check] = []
    manifest = _verify_manifest(gold_dir, GOLD_MANIFEST, [GOLD_ALL], checks)
    records = read_jsonl(gold_dir / GOLD_ALL)

    session_targets: dict[int, dict] = {}
    session_manifest: dict = {}
    if session_dir is not None:
        session_dir = Path(session_dir)
        session_manifest = _verify_manifest(session_dir, SESSION_MANIFEST, [SESSION_TARGETS], checks)
        session_targets = {int(row["case_id"]): row for row in read_jsonl(session_dir / SESSION_TARGETS)}

    # ── count validation (actual records vs manifest vs reference) ─────────
    status_counts = Counter(record["status"] for record in records)
    manifest_counts = manifest.get("counts") or {}
    checks.append(Check(
        "manifest_status_counts",
        "PASS" if dict(status_counts) == manifest_counts else "FAIL",
        f"actual {dict(sorted(status_counts.items()))} manifest {manifest_counts}",
    ))
    targets = status_counts["READY"] + status_counts["CONTEXT_REQUIRED"]
    for key, actual, want in (
        ("evaluation_targets", targets, manifest.get("evaluation_targets")),
        ("scorable", status_counts["READY"], manifest.get("scorable_targets")),
    ):
        checks.append(Check(f"manifest_{key}", "PASS" if actual == want else "FAIL",
                            f"actual {actual} manifest {want}"))
    reference_actual = {
        "evaluation_targets": targets,
        "context_required": status_counts["CONTEXT_REQUIRED"],
        "scorable": status_counts["READY"],
    }
    checks.append(Check(
        "reference_denominator_503_17_486",
        "PASS" if reference_actual == REFERENCE_COUNTS else "WARN",
        f"actual {reference_actual} reference {REFERENCE_COUNTS}",
    ))
    if session_targets:
        ready_or_context = {int(r["case_id"]) for r in records
                            if r["status"] in ("READY", "CONTEXT_REQUIRED")}
        same = ready_or_context == set(session_targets)
        checks.append(Check("session_targets_match_gold_targets", "PASS" if same else "FAIL",
                            f"gold {len(ready_or_context)} session {len(session_targets)}"))
        mismatched = [
            case_id for case_id in sorted(ready_or_context & set(session_targets))
            if [sorted(g) for g in session_targets[case_id]["expected_intent_groups"]] != [
                sorted(i["accepted_qna_ids"])
                for i in next(r for r in records if int(r["case_id"]) == case_id)["expected_intents"]
            ]
        ]
        checks.append(Check("session_expected_groups_match_gold", "FAIL" if mismatched else "PASS",
                            f"mismatched case ids: {mismatched[:20]}" if mismatched else ""))

    decisions = {int(k): v for k, v in (manifest.get("decisions") or {}).items()}
    overlap_ids = {int(v) for v in (manifest.get("case_ids") or {}).get("kb_overlap_flagged", [])}

    # ── mapping ────────────────────────────────────────────────────────────
    cases: list[BenchmarkCase] = []
    pii: dict[str, list[str]] = {}
    for record in sorted(records, key=lambda r: int(r["case_id"])):
        case_id = int(record["case_id"])
        status = record["status"]
        base_tags = ["qna_only"]
        if record.get("temporal"):
            base_tags.append("temporal")
        if record.get("routing_guarded"):
            base_tags.append("routing_guarded")
        if case_id in overlap_ids or (session_targets.get(case_id) or {}).get("kb_overlap_flag"):
            base_tags.append("kb_overlap_flagged")
        turn_type = (session_targets.get(case_id) or {}).get("turn_type")
        if turn_type in _TURN_TAGS:
            base_tags.append(_TURN_TAGS[turn_type])
        if turn_type in ("FIRST_TURN", "FOLLOW_UP_CONTEXT_AVAILABLE_BUT_NOT_REQUIRED"):
            base_tags.append("context_independent")
        decision = decisions.get(case_id)
        if decision:
            base_tags.append(f"review:{decision}")
        common = {
            "benchmark_version": BENCHMARK_VERSION,
            "review_decision": decision,
        }

        if status == "EXCLUDED_FROM_EVAL":
            cases.append(BenchmarkCase(case_id=str(case_id), evaluation_status=EvaluationStatus.EXCLUDED,
                                       status_reason=status, tags=base_tags, **common))
            continue
        if status in _HOLD_STATUSES:
            cases.append(BenchmarkCase(case_id=str(case_id), evaluation_status=EvaluationStatus.HOLD,
                                       status_reason=status, tags=base_tags, **common))
            continue
        if status == "CONTEXT_REQUIRED" or record.get("context_required"):
            cases.append(BenchmarkCase(
                case_id=str(case_id), evaluation_status=EvaluationStatus.NOT_SELECTOR_EVALUABLE,
                status_reason="context_required_unresolved_intent", tags=base_tags, **common,
            ))
            continue
        if status != "READY":
            cases.append(BenchmarkCase(
                case_id=str(case_id), evaluation_status=EvaluationStatus.NOT_SELECTOR_EVALUABLE,
                status_reason=f"unknown_status:{status}", tags=base_tags, **common,
            ))
            continue

        intents = record.get("expected_intents") or []
        user_message = record.get("user_message") or ""
        multi = bool(record.get("multi_intent")) or len(intents) > 1
        if not intents or any(not (i.get("intent_text") or "").strip() for i in intents):
            # Raw message without a reviewed intent-level decomposition.
            cases.append(BenchmarkCase(
                case_id=str(case_id), evaluation_status=EvaluationStatus.NOT_SELECTOR_EVALUABLE,
                status_reason="no_intent_level_expectation", tags=base_tags, **common,
            ))
            continue
        for intent in intents:
            refs = _refs(intent["accepted_qna_ids"])
            intent_text = intent["intent_text"]
            verbatim = intent_text.strip() == user_message.strip()
            tags = list(base_tags)
            tags.append("multi_acceptable" if len(refs) > 1 else "single_acceptable")
            if multi:
                tags.append("multi_intent_derived")
            else:
                tags.append("single_intent")
            if not verbatim:
                tags.append("reformulated_intent_text")
            index = int(intent.get("intent_index") or 1)
            this_id = f"{case_id}#i{index}" if multi else str(case_id)
            hits = _pii_hits(intent_text)
            if hits:
                pii[this_id] = hits
            cases.append(BenchmarkCase(
                case_id=this_id,
                source_case_id=str(case_id),
                intent_index=index,
                intent_text=intent_text,
                expected_decision="SELECT",
                acceptable_candidate_refs=refs,
                primary=(not multi) and verbatim,
                tags=sorted(set(tags)),
                **common,
            ))

    checks.append(Check(
        "no_semantic_none_expectations",
        "WARN" if not any(c.expected_decision == "NONE" for c in cases) else "PASS",
        "reviewed Gold carries no expected-NONE targets; NONE precision/recall "
        "cannot be measured on this dataset",
    ))
    checks.append(Check(
        "pii_screen_intent_text",
        "WARN" if pii else "PASS",
        f"pattern hits (case ids only): {pii}" if pii else "no email/phone/long-id patterns",
    ))
    evaluable = [c for c in cases if c.evaluation_status is EvaluationStatus.SELECTOR_EVALUABLE]
    counts = {
        "records": len(records),
        "record_status": dict(sorted(status_counts.items())),
        "cases": len(cases),
        "case_status": dict(Counter(c.evaluation_status.value for c in cases)),
        "selector_evaluable_cases": len(evaluable),
        "primary_cases": sum(1 for c in evaluable if c.primary),
        "derived_non_primary_cases": sum(1 for c in evaluable if not c.primary),
        "expected_select": sum(1 for c in evaluable if c.expected_decision == "SELECT"),
        "expected_none": sum(1 for c in evaluable if c.expected_decision == "NONE"),
        "multi_acceptable": sum(1 for c in evaluable if c.multi_acceptable),
        "distinct_expected_refs": len({r for c in evaluable for r in c.acceptable_candidate_refs}),
        "tags": dict(sorted(Counter(t for c in evaluable for t in c.tags).items())),
    }
    return LoadedDataset(
        cases=cases,
        checks=checks,
        source={
            "kind": "reviewed_gold_v2",
            "gold_dir": str(gold_dir),
            "gold_layer": manifest.get("layer"),
            "gold_created_at": manifest.get("created_at"),
            "gold_git_commit": (manifest.get("git") or {}).get("commit"),
            "gold_all_sha256": sha256_file(gold_dir / GOLD_ALL),
            "gold_manifest_sha256": sha256_file(gold_dir / GOLD_MANIFEST),
            "kb_baseline_sha256": manifest.get("kb_baseline_sha256"),
            "session_dir": str(session_dir) if session_dir else None,
            "session_dataset_version": session_manifest.get("dataset_version"),
            "session_manifest_sha256": (
                sha256_file(session_dir / SESSION_MANIFEST) if session_dir else None
            ),
        },
        counts=counts,
    )


def write_cases_jsonl(cases: Iterable[BenchmarkCase], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for case in cases:
            handle.write(case.model_dump_json() + "\n")
