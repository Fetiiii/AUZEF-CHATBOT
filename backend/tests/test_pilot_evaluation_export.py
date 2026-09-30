"""Pilot evaluation export: read-only, masked, no gold, review hints only."""
import json
from datetime import date

from scripts.pilot_evaluation_export import build_rows, iter_traces, mask, summarize


def _trace(request_id, *, day="2026-10-02", endpoint="widget_chat", decision="SELECT", latency=1200.0,
           retry=0, timeout=False, refs=("qna:1", "qna:2"), selected="qna:1", final_ids=(1,)):
    return "INFO decision_trace=" + json.dumps({
        "event": "answer_pipeline_decision_trace", "schema_version": 8,
        "request": {"request_id": request_id, "conversation_id": 5, "user_message_id": 9,
                    "timestamp": f"{day}T10:00:00.000Z", "endpoint": endpoint, "ai_config_version": 7},
        "intent_analyzer": {"intent_count": 1, "outcome_status": "success", "actual_model": "openai/gpt-6-luna",
                            "latency_ms": 900.0, "provider_metadata": {"retry_count": 0},
                            "intents": [{"calendar_relevant": False, "context_used": True, "resolved_length": 40}]},
        "selectors": [{"selector_called": True, "selector_decision": decision, "candidate_refs": list(refs),
                       "selected_candidate_ref": selected if decision == "SELECT" else None,
                       "actual_model": "openai/gpt-6-luna", "prompt_version": "variant_a_v3_contract",
                       "prompt_fingerprint": "cdeea795", "latency_ms": latency, "retry_count": retry,
                       "timeout": timeout, "failure_category": None}],
        "final": {"final_outcome": "answer" if decision == "SELECT" else "no_answer",
                  "final_qna_ids": list(final_ids) if decision == "SELECT" else [], "total_latency_ms": 2500.0},
    })


def test_mask_removes_obvious_pii_but_keeps_years():
    text = "TC 12345678901, tel 0532 123 45 67, a@b.com, kod 482913, 2026 bahar"
    masked = mask(text)
    assert "12345678901" not in masked and "0532" not in masked and "a@b.com" not in masked
    assert "482913" not in masked and "2026" in masked


def test_iter_traces_filters_date_range_endpoint_and_duplicates():
    lines = [_trace("a"), _trace("a"), _trace("b", day="2026-09-30"), _trace("c", endpoint="api_search"),
             "not a trace", "decision_trace={broken"]
    got = [t["request"]["request_id"] for t in iter_traces(lines, date(2026, 10, 1), date(2026, 10, 3))]
    assert got == ["a"]


def test_trace_only_rows_carry_identity_and_review_hints_without_verdicts():
    lines = [_trace("sel"), _trace("none", decision="NONE"), _trace("slow", latency=13500.0, retry=1)]
    rows = build_rows(iter_traces(lines, date(2026, 10, 1), date(2026, 10, 3)), db=None)
    by = {r["request_id"]: r for r in rows}
    assert by["sel"]["selectors"][0]["prompt_version"] == "variant_a_v3_contract"
    assert by["sel"]["join"] == "no_user_message_id" or by["sel"]["user_query"] is None
    assert "false_none_candidate" in by["none"]["watchlist"]
    assert {"selector_latency_tail", "deadline_proximity_or_timeout", "provider_retry_or_failure"} <= set(
        by["slow"]["watchlist"])
    assert not any("verdict" in key or "gold" in key for row in rows for key in row)
    summary = summarize(rows)
    assert summary["selector_latency_ms"]["ge_13s"] == 1 and summary["selector_retries"] == 1
