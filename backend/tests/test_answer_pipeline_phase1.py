"""answer_question routing by typed selection outcome (Phase 1 + Phase 4)."""
from types import SimpleNamespace

import pytest

from services.decision_trace import DecisionTrace
from services.llm_config import resolve_llm_config_set
from services.llm_types import SelectionOutcome
from services.routing_guards import RoutingGuardPolicy


class FakeProvider:
    configs = resolve_llm_config_set("openai", environ={})


def _patch_common(monkeypatch, answer_pipeline, *, enabled=True):
    monkeypatch.setattr(
        answer_pipeline.RoutingGuardPolicy,
        "load",
        classmethod(lambda cls, _db: RoutingGuardPolicy.empty()),
    )
    monkeypatch.setattr(answer_pipeline, "is_llm_enabled", lambda _db: enabled)
    monkeypatch.setattr(
        answer_pipeline, "get_llm_provider", lambda _db: FakeProvider() if enabled else None
    )


@pytest.mark.parametrize(
    "outcome",
    [SelectionOutcome.SEMANTIC_NONE, SelectionOutcome.NO_ELIGIBLE_CANDIDATES],
)
def test_semantic_none_and_no_eligible_are_final_without_fallback(
    monkeypatch, db, outcome
):
    """Phase 4 intentionally changes the Phase 1 contract: NONE is final."""
    from services import answer_pipeline

    _patch_common(monkeypatch, answer_pipeline)
    monkeypatch.setattr(
        answer_pipeline,
        "_llm_answer",
        lambda *_args, **_kwargs: answer_pipeline.LLMAnswerResult(None, outcome, []),
    )
    monkeypatch.setattr(
        answer_pipeline,
        "_fallback_answer",
        lambda *_args, **_kwargs: pytest.fail("semantic NONE must not enter fallback"),
    )
    trace = DecisionTrace(endpoint="test")
    assert answer_pipeline.answer_question("soru", db, trace=trace) == (None, "none")
    snapshot = trace.to_dict()
    assert snapshot["fallback"]["fallback_entered"] is False
    assert snapshot["selection"]["aggregate_outcome"] == outcome.value
    assert snapshot["selection"]["error_fallback_allowed"] is False


@pytest.mark.parametrize(
    ("outcome", "expected_reason", "use_calendar"),
    [
        (SelectionOutcome.INVALID_OUTPUT, "selector_invalid_output", False),
        (SelectionOutcome.MODEL_ERROR, "selector_model_error", True),
        (SelectionOutcome.TIMEOUT, "selector_timeout", True),
    ],
)
def test_selector_errors_use_explicit_compatibility_fallback(
    monkeypatch, db, outcome, expected_reason, use_calendar
):
    from services import answer_pipeline

    _patch_common(monkeypatch, answer_pipeline)
    monkeypatch.setattr(
        answer_pipeline,
        "_llm_answer",
        lambda *_args, **_kwargs: answer_pipeline.LLMAnswerResult(None, outcome, []),
    )
    captured = {}

    def fallback(_query, _db, **kwargs):
        captured.update(kwargs)
        return "fallback", "meilisearch"

    monkeypatch.setattr(answer_pipeline, "_fallback_answer", fallback)
    trace = DecisionTrace(endpoint="test")
    assert answer_pipeline.answer_question("soru", db, trace=trace) == (
        "fallback", "meilisearch"
    )
    assert captured["use_calendar"] is use_calendar
    assert captured["fallback_reason"] == expected_reason
    assert trace.to_dict()["selection"]["error_fallback_allowed"] is True


def test_unexpected_llm_pipeline_exception_keeps_phase0_full_fallback(monkeypatch, db):
    from services import answer_pipeline

    _patch_common(monkeypatch, answer_pipeline)

    def fail(*_args, **_kwargs):
        raise RuntimeError("provider missing")

    monkeypatch.setattr(answer_pipeline, "_llm_answer", fail)
    captured = {}

    def fallback(_query, _db, **kwargs):
        captured.update(kwargs)
        return "fallback", "academic_calendar"

    monkeypatch.setattr(answer_pipeline, "_fallback_answer", fallback)
    assert answer_pipeline.answer_question("soru", db) == (
        "fallback", "academic_calendar"
    )
    assert captured["use_calendar"] is True
    assert captured["fallback_reason"] == "llm_model_error"


def test_llm_off_keeps_phase0_full_fallback(monkeypatch, db):
    from services import answer_pipeline

    _patch_common(monkeypatch, answer_pipeline, enabled=False)
    captured = {}

    def fallback(_query, _db, **kwargs):
        captured.update(kwargs)
        return None, "none"

    monkeypatch.setattr(answer_pipeline, "_fallback_answer", fallback)
    answer_pipeline.answer_question("soru", db)
    assert captured["use_calendar"] is True
    assert captured["fallback_reason"] == "llm_disabled_or_unavailable"


def test_llm_success_preserves_curated_answer_source_and_qna_trace(monkeypatch, db):
    from services import answer_pipeline

    _patch_common(monkeypatch, answer_pipeline)
    monkeypatch.setattr(
        answer_pipeline,
        "_llm_answer",
        lambda *_args, **_kwargs: answer_pipeline.LLMAnswerResult(
            "curated answer", SelectionOutcome.SELECTED, [73], answer_count=1
        ),
    )
    trace = DecisionTrace(endpoint="test")
    assert answer_pipeline.answer_question("soru", db, trace=trace) == (
        "curated answer", "llm"
    )
    assert trace.to_dict()["final"]["final_qna_ids"] == [73]
