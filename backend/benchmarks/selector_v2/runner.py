"""Case-by-case selector runs with resumable, identity-scoped result files.

A run is identified by (selector contract fingerprint, selector config
fingerprint, snapshot fingerprint, candidate order, run mode). Results live
in ``<out>/runs/<run_id>/results.jsonl``; each line carries a ``result_key``
derived from that identity plus the case id. Resuming skips completed keys,
never reuses a result from a different identity and ignores (and counts)
corrupted lines instead of trusting them.
"""
from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Protocol, Sequence

from pydantic import ValidationError

from benchmarks.selector_v2.contract import PRODUCTION_ORDER, fingerprint, order_candidates
from benchmarks.selector_v2.schema import (
    BenchmarkResult,
    CaseSnapshot,
    Outcome,
    CORRECT_OUTCOMES,
)
from services.llm_config import EffectiveLLMConfig
from services.llm_types import LLMOutcomeStatus, LLMParseStatus, SelectorResult

RESULTS_FILE = "results.jsonl"
RUN_MANIFEST = "run-manifest.json"
MAX_CONCURRENCY = 4
RAW_RESPONSE_LIMIT = 256
RETRYABLE = frozenset({Outcome.MODEL_ERROR, Outcome.TIMEOUT})


class SelectorBackend(Protocol):
    def select(self, snapshot: CaseSnapshot, candidates) -> SelectorResult: ...


class ForeignResultError(RuntimeError):
    """A result file contains results from another run identity."""


@dataclass(frozen=True)
class RunIdentity:
    selector_contract_fingerprint: str
    config: EffectiveLLMConfig
    snapshot_fingerprint: str
    run_mode: str
    candidate_order: str = PRODUCTION_ORDER
    # Prompt-experiment identity (Phase 7B). None keeps pre-existing run ids
    # (e.g. the Stage A production run) unchanged.
    prompt_fingerprint: Optional[str] = None
    split_fingerprint: Optional[str] = None
    # Request params dropped for a model that does not support them (model
    # experiment). Empty keeps pre-existing run ids unchanged.
    omitted_request_params: tuple = ()

    def to_dict(self) -> dict:
        identity = {
            "selector_contract_fingerprint": self.selector_contract_fingerprint,
            "config_fingerprint": self.config.fingerprint,
            "config": self.config.to_dict(),
            "snapshot_fingerprint": self.snapshot_fingerprint,
            "run_mode": self.run_mode,
            "candidate_order": self.candidate_order,
        }
        if self.prompt_fingerprint is not None:
            identity["prompt_fingerprint"] = self.prompt_fingerprint
        if self.split_fingerprint is not None:
            identity["split_fingerprint"] = self.split_fingerprint
        if self.omitted_request_params:
            identity["omitted_request_params"] = sorted(self.omitted_request_params)
        return identity

    @property
    def run_id(self) -> str:
        return fingerprint(self.to_dict())[:16]

    def result_key(self, case_id: str) -> str:
        return fingerprint({**self.to_dict(), "case_id": case_id})[:24]


@dataclass
class LoadedResults:
    by_case: dict[str, BenchmarkResult] = field(default_factory=dict)
    corrupt_lines: int = 0
    superseded: int = 0


class ResultStore:
    def __init__(self, run_dir: Path, identity: RunIdentity):
        self.run_dir = Path(run_dir)
        self.identity = identity
        self.path = self.run_dir / RESULTS_FILE
        self._lock = threading.Lock()

    def prepare(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = self.run_dir / RUN_MANIFEST
        wanted = {"run_id": self.identity.run_id, **self.identity.to_dict()}
        if manifest_path.exists():
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            if existing != wanted:
                raise ForeignResultError(f"{manifest_path} belongs to another run identity")
        else:
            manifest_path.write_text(json.dumps(wanted, indent=2, sort_keys=True) + "\n",
                                     encoding="utf-8")

    def load(self) -> LoadedResults:
        return load_results(self.path, self.identity)

    def append(self, result: BenchmarkResult) -> None:
        line = result.model_dump_json() + "\n"
        with self._lock:
            needs_newline = False
            if self.path.exists() and self.path.stat().st_size:
                with open(self.path, "rb") as handle:
                    handle.seek(-1, 2)
                    needs_newline = handle.read(1) != b"\n"
            with open(self.path, "a", encoding="utf-8") as handle:
                if needs_newline:  # never glue a new record onto a torn line
                    handle.write("\n")
                handle.write(line)
                handle.flush()


def load_results(path: Path, identity: Optional[RunIdentity] = None) -> LoadedResults:
    loaded = LoadedResults()
    if not Path(path).exists():
        return loaded
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                result = BenchmarkResult.model_validate_json(line)
            except (ValidationError, ValueError):
                loaded.corrupt_lines += 1
                continue
            if identity is not None and (
                result.run_id != identity.run_id
                or result.result_key != identity.result_key(result.case_id)
            ):
                raise ForeignResultError(
                    f"{path}: result for case {result.case_id} has another identity"
                )
            if result.case_id in loaded.by_case:
                loaded.superseded += 1
            loaded.by_case[result.case_id] = result  # latest attempt wins
    return loaded


def classify(snapshot: CaseSnapshot, result: SelectorResult, allowed_refs: set[str]) -> Outcome:
    expected = snapshot.case.expected_decision
    status = result.status
    if status is LLMOutcomeStatus.SUCCESS:
        if result.selected_candidate_ref not in allowed_refs:
            return Outcome.INVALID_OUTPUT  # production re-check (_select_from_pool)
        if expected == "NONE":
            return Outcome.FALSE_SELECT
        if result.selected_candidate_ref in snapshot.case.acceptable_candidate_refs:
            return Outcome.CORRECT_SELECT
        return Outcome.WRONG_SELECT
    if status is LLMOutcomeStatus.SEMANTIC_NONE:
        return Outcome.CORRECT_NONE if expected == "NONE" else Outcome.FALSE_NONE
    if status is LLMOutcomeStatus.TIMEOUT:
        return Outcome.TIMEOUT
    if status is LLMOutcomeStatus.MODEL_ERROR:
        return Outcome.MODEL_ERROR
    return Outcome.INVALID_OUTPUT


def _result_record(identity: RunIdentity, snapshot: CaseSnapshot, result: SelectorResult,
                   outcome: Outcome, attempt: int) -> BenchmarkResult:
    invocation = result.invocation
    metadata = invocation.metadata if invocation else None
    input_tokens = metadata.input_tokens if metadata else None
    output_tokens = metadata.output_tokens if metadata else None
    total = (input_tokens + output_tokens
             if input_tokens is not None and output_tokens is not None else None)
    raw = invocation.text if invocation else None
    config = identity.config
    return BenchmarkResult(
        result_key=identity.result_key(snapshot.case.case_id),
        case_id=snapshot.case.case_id,
        run_id=identity.run_id,
        run_mode=identity.run_mode,
        expected_decision=snapshot.case.expected_decision,
        expected_refs=list(snapshot.case.acceptable_candidate_refs),
        decision=result.decision if outcome not in (Outcome.INVALID_OUTPUT,) else None,
        selected_candidate_ref=(
            result.selected_candidate_ref if outcome is not Outcome.INVALID_OUTPUT else None
        ),
        outcome=outcome,
        correct=outcome in CORRECT_OUTCOMES,
        selector_status=result.status.value,
        invalid_reason=result.invalid_reason,
        provider=config.provider,
        model=config.model,
        actual_model=metadata.actual_model if metadata else None,
        reasoning_effort=config.reasoning_effort.value if config.reasoning_effort else None,
        config_fingerprint=config.fingerprint,
        selector_contract_fingerprint=identity.selector_contract_fingerprint,
        snapshot_fingerprint=identity.snapshot_fingerprint,
        candidate_order=identity.candidate_order,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total,
        latency_ms=round(invocation.latency_ms, 3) if invocation else None,
        finish_reason=metadata.finish_reason if metadata else None,
        error_type=invocation.error_type if invocation else None,
        failure_category=invocation.failure_category if invocation else None,
        raw_response=raw[:RAW_RESPONSE_LIMIT] if raw else None,
        attempt=attempt,
    )


def run_one(identity: RunIdentity, backend: SelectorBackend, snapshot: CaseSnapshot,
            attempt: int = 1) -> BenchmarkResult:
    candidates = order_candidates(
        snapshot.selector_candidates(), case_id=snapshot.case.case_id,
        order=identity.candidate_order,
    )
    allowed = {c.candidate_ref for c in candidates}
    try:
        result = backend.select(snapshot, candidates)
    except Exception as exc:  # backend bug/unexpected: final MODEL_ERROR, never NONE
        result = SelectorResult(status=LLMOutcomeStatus.MODEL_ERROR,
                                parse_status=LLMParseStatus.NOT_APPLICABLE, answer=None)
        record = _result_record(identity, snapshot, result, Outcome.MODEL_ERROR, attempt)
        return record.model_copy(update={"error_type": exc.__class__.__name__})
    return _result_record(identity, snapshot, result, classify(snapshot, result, allowed), attempt)


@dataclass
class RunSummary:
    run_id: str
    run_dir: str
    planned: int
    skipped_completed: int
    executed: int
    corrupt_lines_ignored: int
    backend_calls: Optional[int] = None


def run_benchmark(
    snapshots: Sequence[CaseSnapshot],
    backend: SelectorBackend,
    identity: RunIdentity,
    out_root: Path,
    *,
    concurrency: int = 1,
    max_cases: Optional[int] = None,
    retry_errors: bool = False,
    primary_only: bool = False,
    case_ids: Optional[set] = None,
) -> RunSummary:
    if not 1 <= concurrency <= MAX_CONCURRENCY:
        raise ValueError(f"concurrency must be 1..{MAX_CONCURRENCY}")
    store = ResultStore(Path(out_root) / "runs" / identity.run_id, identity)
    store.prepare()
    existing = store.load()

    evaluable = [s for s in snapshots if s.selector_evaluable and (s.case.primary or not primary_only)
                 and (case_ids is None or s.case.case_id in case_ids)]
    todo: list[tuple[CaseSnapshot, int]] = []
    skipped = 0
    for snapshot in evaluable:
        previous = existing.by_case.get(snapshot.case.case_id)
        if previous is None:
            todo.append((snapshot, 1))
        elif retry_errors and previous.outcome in RETRYABLE:
            todo.append((snapshot, previous.attempt + 1))
        else:
            skipped += 1
    if max_cases is not None:
        todo = todo[:max_cases]

    def work(item):
        snapshot, attempt = item
        store.append(run_one(identity, backend, snapshot, attempt))

    if concurrency == 1:
        for item in todo:
            work(item)
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            list(pool.map(work, todo))
    return RunSummary(
        run_id=identity.run_id,
        run_dir=str(store.run_dir),
        planned=len(evaluable),
        skipped_completed=skipped,
        executed=len(todo),
        corrupt_lines_ignored=existing.corrupt_lines,
        backend_calls=getattr(backend, "calls", None),
    )
