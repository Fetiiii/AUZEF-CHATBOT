"""answer_question routing by execution mode (Phase 1 → Phase 5 contract)."""
from types import SimpleNamespace

from services.decision_trace import DecisionTrace
from services.llm_config import resolve_llm_config_set
from services.llm_types import ExecutionMode, IntentResolution, SelectionOutcome
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


def _capture_degraded(monkeypatch, answer_pipeline, result=None):
    calls = []

    def degraded(query, _db, **kwargs):
        calls.append({"query": query, **kwargs})
        return result or answer_pipeline.DegradedAnswer(None, "none")

    monkeypatch.setattr(answer_pipeline, "answer_in_degraded_mode", degraded)
    return calls


def test_unexpected_llm_pipeline_exception_is_request_degraded_on_raw_turn(
    monkeypatch, db
):
    from services import answer_pipeline

    _patch_common(monkeypatch, answer_pipeline)

    def fail(*_args, **_kwargs):
        raise RuntimeError("provider missing")

    monkeypatch.setattr(answer_pipeline, "_llm_answer", fail)
    calls = _capture_degraded(
        monkeypatch, answer_pipeline,
        answer_pipeline.DegradedAnswer("fallback", "academic_calendar", calendar_id=4),
    )
    trace = DecisionTrace(endpoint="test")
    assert answer_pipeline.answer_question("soru", db, trace=trace) == (
        "fallback", "academic_calendar"
    )
    assert calls[0]["query"] == "soru"
    assert calls[0]["calendar_gate"] == "date_query"
    assert calls[0]["reason"] == "llm_pipeline_error"
    execution = trace.to_dict()["execution"]
    assert execution["execution_mode"] == "REQUEST_DEGRADED"
    assert execution["intents"][0]["calendar_id"] == 4


def test_llm_off_is_admin_degraded_on_raw_turn(monkeypatch, db):
    from services import answer_pipeline

    _patch_common(monkeypatch, answer_pipeline, enabled=False)
    calls = _capture_degraded(monkeypatch, answer_pipeline)
    trace = DecisionTrace(endpoint="test")
    assert answer_pipeline.answer_question("soru", db, trace=trace) == (None, "none")
    assert calls == [{
        "query": "soru",
        "routing_policy": calls[0]["routing_policy"],
        "calendar_gate": "date_query",
        "reason": "llm_disabled_or_unavailable",
        "trace": trace,
        "purpose": "request",
        "query_kind": "raw_current_turn",
    }]
    assert trace.to_dict()["execution"]["execution_mode"] == "ADMIN_DEGRADED"


def test_llm_success_preserves_curated_answer_source_and_qna_trace(monkeypatch, db):
    from services import answer_pipeline

    _patch_common(monkeypatch, answer_pipeline)
    intent = answer_pipeline.IntentResult(
        1, IntentResolution.SELECTED, ExecutionMode.NORMAL_LLM,
        answer="curated answer", source="llm", qna_id=73,
        selection_outcome=SelectionOutcome.SELECTED,
    )
    monkeypatch.setattr(
        answer_pipeline,
        "_llm_answer",
        lambda *_args, **_kwargs: answer_pipeline._compose(
            (intent,), mode=ExecutionMode.NORMAL_LLM
        ),
    )
    trace = DecisionTrace(endpoint="test")
    assert answer_pipeline.answer_question("soru", db, trace=trace) == (
        "curated answer", "llm"
    )
    snapshot = trace.to_dict()
    assert snapshot["final"]["final_qna_ids"] == [73]
    assert snapshot["execution"]["execution_mode"] == "NORMAL_LLM"
    assert snapshot["fallback"]["fallback_entered"] is False
