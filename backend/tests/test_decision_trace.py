"""Decision trace is correlated, structured, and PII-safe."""
import json
import logging
import threading

from services.decision_trace import DecisionTrace, emit_decision_trace
from services.llm_config import resolve_llm_config_set
from services.llm_types import (
    IntentAnalysis,
    IntentAnalyzerResult,
    IntentItem,
    LLMInvocationResult,
    LLMOutcomeStatus,
    LLMParseStatus,
    LLMResponseMetadata,
    SelectorResult,
)


def _selector_result(status=LLMOutcomeStatus.SUCCESS, qna_id=42):
    call_status = (
        status
        if status in (LLMOutcomeStatus.MODEL_ERROR, LLMOutcomeStatus.TIMEOUT)
        else LLMOutcomeStatus.SUCCESS
    )
    invocation = LLMInvocationResult(
        status=call_status,
        text="1" if status is LLMOutcomeStatus.SUCCESS else None,
        latency_ms=4.2,
        metadata=LLMResponseMetadata(
            requested_model="gpt-4o-mini", input_tokens=10, output_tokens=1
        ),
    )
    return SelectorResult(
        status=status,
        parse_status=(
            LLMParseStatus.SUCCESS
            if status is LLMOutcomeStatus.SUCCESS
            else LLMParseStatus.NOT_APPLICABLE
        ),
        answer="curated" if status is LLMOutcomeStatus.SUCCESS else None,
        selected_index=0 if status is LLMOutcomeStatus.SUCCESS else None,
        selected_qna_id=qna_id if status is LLMOutcomeStatus.SUCCESS else None,
        raw_numeric_value=1 if status is LLMOutcomeStatus.SUCCESS else None,
        invocation=invocation,
    )


def test_trace_has_unique_request_correlation_and_config_fingerprint():
    first = DecisionTrace(endpoint="widget_chat", conversation_id=7)
    second = DecisionTrace(endpoint="api_search")
    configs = resolve_llm_config_set("openai", environ={})
    first.set_llm(enabled=True, configs=configs)
    snapshot = first.to_dict()
    assert first.request_id != second.request_id
    assert snapshot["request"]["conversation_id"] == 7
    assert snapshot["request"]["endpoint"] == "widget_chat"
    assert snapshot["request"]["ai_config_fingerprint"] == configs.fingerprint
    assert snapshot["request"]["effective_configs"]["selector"]["max_tokens"] == 5


def test_trace_records_candidates_selection_fallback_and_final_qna():
    trace = DecisionTrace(endpoint="widget_chat")
    trace.record_retrieval({
        "calendar_candidate_count": 1,
        "qdrant_candidate_count": 2,
        "meili_candidate_count": 1,
        "context_qdrant_candidate_count": 0,
        "context_meili_candidate_count": 0,
        "deduped_candidate_count": 3,
        "eligible_after_guard_count": 3,
        "candidate_qna_ids": [42, 99],
        "candidate_order": [
            {"order": 1, "source": "academic_calendar", "qna_id": None},
            {"order": 2, "source": "qdrant", "qna_id": 42},
        ],
    })
    config = resolve_llm_config_set("openai", environ={}).selector.to_dict()
    trace.record_selector(
        _selector_result(),
        config=config,
        candidate_count=3,
        candidate_qna_ids=[42, 99],
        purpose="subquestion",
        used_in_final=True,
    )
    trace.record_fallback(
        reason="selector_semantic_none", selected_source="meilisearch", selected_qna_id=99
    )
    trace.finalize(outcome="answer", source="llm", qna_ids=[42], answer_count=1)
    snapshot = trace.to_dict()
    assert snapshot["retrieval"][0]["candidate_qna_ids"] == [42, 99]
    assert snapshot["selectors"][0]["selected_qna_id"] == 42
    assert snapshot["fallback"]["fallback_reason"] == "selector_semantic_none"
    assert snapshot["final"]["final_qna_ids"] == [42]


def test_trace_serialization_contains_counts_not_sensitive_raw_text():
    sensitive_values = (
        "12345678901",
        "+905551112233",
        "student@example.org",
        "verification-839201",
    )
    trace = DecisionTrace(endpoint="widget_chat")
    messages = tuple(
        {"role": "user", "content": value} for value in sensitive_values
    )
    trace.set_context(enabled=True, messages=messages)
    trace.finalize(outcome="no_answer", source="none", qna_ids=[], answer_count=0)
    serialized = json.dumps(trace.to_dict(), ensure_ascii=False)
    assert trace.to_dict()["context"]["context_message_count"] == 4
    assert all(value not in serialized for value in sensitive_values)
    assert "content" not in serialized


def test_parallel_updates_do_not_share_or_drop_selector_events():
    trace = DecisionTrace(endpoint="widget_chat")
    config = resolve_llm_config_set("openai", environ={}).selector.to_dict()

    def add(qna_id):
        trace.record_selector(
            _selector_result(qna_id=qna_id),
            config=config,
            candidate_count=1,
            candidate_qna_ids=[qna_id],
            purpose="subquestion",
            used_in_final=True,
        )

    threads = [threading.Thread(target=add, args=(qna_id,)) for qna_id in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert {item["selected_qna_id"] for item in trace.to_dict()["selectors"]} == {1, 2}


def test_trace_distinguishes_semantic_none_invalid_model_error_and_timeout():
    trace = DecisionTrace(endpoint="test")
    config = resolve_llm_config_set("openai", environ={}).selector.to_dict()
    statuses = (
        LLMOutcomeStatus.SEMANTIC_NONE,
        LLMOutcomeStatus.INVALID_OUTPUT,
        LLMOutcomeStatus.MODEL_ERROR,
        LLMOutcomeStatus.TIMEOUT,
    )
    for status in statuses:
        trace.record_selector(
            _selector_result(status=status, qna_id=None),
            config=config,
            candidate_count=1,
            candidate_qna_ids=[1],
            purpose="subquestion",
            used_in_final=False,
        )
    selectors = trace.to_dict()["selectors"]
    assert selectors[0]["semantic_none"] is True
    assert selectors[0]["call_status"] == "success"
    assert selectors[1]["invalid_output"] is True
    assert selectors[2]["model_error"] is True
    assert selectors[3]["call_status"] == "timeout"
    assert selectors[3]["timeout"] is True


def test_intent_analyzer_trace_keeps_error_cause_and_never_logs_text():
    sensitive = "student@example.org verification-839201"
    analysis = IntentAnalysis(
        intent_count=1,
        intents=[IntentItem(
            source_text=sensitive,
            normalized_text=sensitive,
            resolved_text=sensitive,
            context_used=False,
            calendar_relevant=False,
        )],
    )
    invocation = LLMInvocationResult(
        status=LLMOutcomeStatus.TIMEOUT,
        text=None,
        latency_ms=15.5,
        metadata=LLMResponseMetadata(
            requested_model="gpt-4o-mini", input_tokens=20, output_tokens=0
        ),
        error_type="TimeoutError",
    )
    result = IntentAnalyzerResult(
        analysis=analysis,
        status=LLMOutcomeStatus.TIMEOUT,
        parse_status=LLMParseStatus.FALLBACK,
        fallback_to_single=True,
        invocation=invocation,
    )
    config = resolve_llm_config_set("openai", environ={}).intent_analyzer
    trace = DecisionTrace(endpoint="test")
    trace.record_intent_analyzer(
        result,
        config.to_dict(),
        config_fingerprint=config.fingerprint,
        current_input_length=len(sensitive),
        previous_user_context_count=2,
    )
    serialized = json.dumps(trace.to_dict(), ensure_ascii=False)
    analyzer = trace.to_dict()["intent_analyzer"]
    assert analyzer["call_status"] == "timeout"
    assert analyzer["outcome_status"] == "timeout"
    assert analyzer["parse_status"] == "fallback"
    assert analyzer["fallback_to_single"] is True
    assert analyzer["usage"] == {"input_tokens": 20, "output_tokens": 0}
    assert sensitive not in serialized


def test_emit_is_one_structured_log_record(caplog):
    caplog.set_level(logging.INFO, logger="auzef")
    trace = DecisionTrace(endpoint="api_search")
    trace.finalize(outcome="no_answer", source="none", qna_ids=[], answer_count=0)
    emit_decision_trace(trace)
    records = [r.message for r in caplog.records if r.message.startswith("decision_trace=")]
    assert len(records) == 1
    payload = json.loads(records[0].split("=", 1)[1])
    assert payload["request"]["request_id"] == trace.request_id
