"""Phase 1 preserves V1 routing while retaining typed decision causes."""
from types import SimpleNamespace

import pytest

from services.decision_trace import DecisionTrace
from services.llm_config import resolve_llm_config_set
from services.llm_types import LLMOutcomeStatus
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
    ("status", "expected_reason"),
    [
        (LLMOutcomeStatus.SEMANTIC_NONE, "selector_semantic_none"),
        (LLMOutcomeStatus.INVALID_OUTPUT, "selector_invalid_output"),
    ],
)
def test_none_like_results_keep_phase0_no_calendar_fallback(
    monkeypatch, db, status, expected_reason
):
    from services import answer_pipeline

    _patch_common(monkeypatch, answer_pipeline)
    monkeypatch.setattr(
        answer_pipeline,
        "_llm_answer",
        lambda *_args, **_kwargs: answer_pipeline.LLMAnswerResult(None, status, []),
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
    assert captured["use_calendar"] is False
    assert captured["fallback_reason"] == expected_reason


@pytest.mark.parametrize(
    ("status", "expected_reason"),
    [
        (LLMOutcomeStatus.MODEL_ERROR, "llm_model_error"),
        (LLMOutcomeStatus.TIMEOUT, "llm_timeout"),
    ],
)
def test_model_failures_keep_phase0_full_fallback(
    monkeypatch, db, status, expected_reason
):
    from services import answer_pipeline

    _patch_common(monkeypatch, answer_pipeline)

    def fail(*_args, **_kwargs):
        raise answer_pipeline.LLMPipelineError("failed", status)

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
    assert captured["fallback_reason"] == expected_reason


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
            "curated answer", LLMOutcomeStatus.SUCCESS, [73], answer_count=1
        ),
    )
    trace = DecisionTrace(endpoint="test")
    assert answer_pipeline.answer_question("soru", db, trace=trace) == (
        "curated answer", "llm"
    )
    assert trace.to_dict()["final"]["final_qna_ids"] == [73]
