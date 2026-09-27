"""Summarize one UTC day of internal-pilot decision_trace log lines.

Only trace identifiers and aggregate signals are emitted. Answer text and
conversation tokens are never read or written by this tool.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import date
from pathlib import Path


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * fraction + 0.5)))
    return round(ordered[index], 1)


def collect(lines, utc_day: str, sample_size: int) -> dict:
    result = {"utc_date": utc_day, "requests": 0, "malformed_trace_lines": 0,
              "duplicate_request_ids": 0, "final_outcomes": Counter(),
              "selector_decisions": Counter(), "failure_categories": Counter(),
              "degraded_reasons": Counter(), "source_unavailable": Counter(),
              "circuit_states": Counter(), "circuit_skipped_requests": 0,
              "provider_rate_limit_requests": 0, "retry_requests": 0,
              "retry_total": 0, "calendar_relevant_requests": 0,
              "context_used_requests": 0, "multi_intent_requests": 0,
              "latency_ms": {}, "review_queue": [], "review_queue_total": 0,
              "quality_sample": [], "quality_sample_pool": 0}
    seen = set()
    sample_candidates = []
    total_ms = []
    analyzer_ms = []
    selector_ms = []
    retrieval_ms = []
    for line in lines:
        if "decision_trace=" not in line:
            continue
        try:
            trace = json.loads(line.split("decision_trace=", 1)[1])
        except ValueError:
            result["malformed_trace_lines"] += 1
            continue
        if trace.get("event") != "answer_pipeline_decision_trace":
            continue
        req = trace.get("request") or {}
        if not str(req.get("timestamp") or "").startswith(utc_day):
            continue
        request_id = req.get("request_id")
        if not request_id or request_id in seen:
            result["duplicate_request_ids"] += 1
            continue
        seen.add(request_id)
        result["requests"] += 1
        final = trace.get("final") or {}
        result["final_outcomes"][final.get("final_outcome") or "missing"] += 1
        if isinstance(final.get("total_latency_ms"), (float, int)):
            total_ms.append(final["total_latency_ms"])
        analyzer = trace.get("intent_analyzer") or {}
        selectors = trace.get("selectors") or []
        calls = [analyzer] + [s for s in selectors if s.get("selector_called")]
        reasons = []
        if (analyzer.get("intent_count") or 0) > 1:
            result["multi_intent_requests"] += 1
            reasons.append("multi_intent")
        intents = analyzer.get("intents") or []
        if any(i.get("calendar_relevant") for i in intents):
            result["calendar_relevant_requests"] += 1
            reasons.append("calendar_relevant")
        if any(i.get("context_used") for i in intents):
            result["context_used_requests"] += 1
            reasons.append("context_used")
        if isinstance(analyzer.get("latency_ms"), (float, int)):
            analyzer_ms.append(analyzer["latency_ms"])
        limited = False
        retried = False
        for call in calls:
            category = call.get("failure_category")
            if category:
                result["failure_categories"][category] += 1
                if category == "RATE_LIMIT":
                    limited = True
            retries = call.get("retry_count")
            if isinstance(retries, int) and retries > 0:
                retried = True
                result["retry_total"] += retries
        for selector in selectors:
            if selector.get("selector_called") and isinstance(selector.get("latency_ms"), (float, int)):
                selector_ms.append(selector["latency_ms"])
        if limited:
            result["provider_rate_limit_requests"] += 1
            reasons.append("provider_rate_limit")
        if retried:
            result["retry_requests"] += 1
            reasons.append("retry")
        for selector in selectors:
            if selector.get("selector_called"):
                decision = selector.get("selector_decision") or selector.get("selector_status") or "missing"
                result["selector_decisions"][decision] += 1
                if selector.get("semantic_none") or decision == "NONE":
                    reasons.append("semantic_none")
        if final.get("final_qna_ids") or any(s.get("semantic_none") or s.get("selector_decision") == "NONE" for s in selectors):
            sample_candidates.append({
                "request_id": request_id, "conversation_id": req.get("conversation_id"),
                "timestamp": req.get("timestamp"),
                "final_qna_ids": final.get("final_qna_ids") or [],
                "selector_decisions": [s.get("selector_decision") for s in selectors if s.get("selector_called")],
            })
        for entry in trace.get("retrieval") or []:
            if isinstance(entry.get("retrieval_ms"), (float, int)):
                retrieval_ms.append(entry["retrieval_ms"])
        for source, state in (trace.get("source_availability") or {}).items():
            if state == "unavailable":
                result["source_unavailable"][source] += 1
                reasons.append(f"{source}_unavailable")
        skipped = False
        for circuit in trace.get("circuit") or []:
            state = circuit.get("circuit_state_after") or circuit.get("circuit_state_before")
            if state:
                result["circuit_states"][state] += 1
            if circuit.get("llm_call_skipped"):
                skipped = True
        if skipped:
            result["circuit_skipped_requests"] += 1
            reasons.append("circuit_skipped_call")
        degraded = trace.get("degraded") or []
        if degraded:
            reasons.append("degraded")
            for item in degraded:
                result["degraded_reasons"][item.get("degraded_reason") or "unknown"] += 1
        if final.get("final_outcome") in ("no_answer", "error"):
            reasons.append(final["final_outcome"])
        if reasons:
            result["review_queue_total"] += 1
        if reasons and len(result["review_queue"]) < sample_size:
            result["review_queue"].append({
                "request_id": request_id, "conversation_id": req.get("conversation_id"),
                "timestamp": req.get("timestamp"), "reasons": sorted(set(reasons)),
                "final_qna_ids": final.get("final_qna_ids") or [],
            })
    result["latency_ms"] = {
        "total_p50": percentile(total_ms, 0.5), "total_p95": percentile(total_ms, 0.95),
        "analyzer_p95": percentile(analyzer_ms, 0.95),
        "selector_p95": percentile(selector_ms, 0.95),
        "retrieval_p95": percentile(retrieval_ms, 0.95),
    }
    for key in ("final_outcomes", "selector_decisions", "failure_categories",
                "degraded_reasons", "source_unavailable", "circuit_states"):
        result[key] = dict(sorted(result[key].items()))
    result["quality_sample_pool"] = len(sample_candidates)
    result["quality_sample"] = sorted(sample_candidates, key=lambda item: item["request_id"])[:sample_size]
    return result


def markdown(report: dict) -> str:
    lines = [f"# Pilot daily trace summary — {report['utc_date']} UTC", "",
             f"Requests: {report['requests']} · malformed: {report['malformed_trace_lines']} · duplicate IDs: {report['duplicate_request_ids']}",
             f"Provider RATE_LIMIT requests: {report['provider_rate_limit_requests']} · retry requests: {report['retry_requests']} · retries: {report['retry_total']}",
             f"Circuit-skipped requests: {report['circuit_skipped_requests']}",
             f"Calendar relevant: {report['calendar_relevant_requests']} · context used: {report['context_used_requests']} · MULTI: {report['multi_intent_requests']}",
             "", "| Signal | Counts |", "|---|---|"]
    for key in ("final_outcomes", "selector_decisions", "failure_categories",
                "degraded_reasons", "source_unavailable", "circuit_states", "latency_ms"):
        lines.append(f"| {key} | {json.dumps(report[key], ensure_ascii=False)} |")
    lines.extend(["", f"## Human review queue ({len(report['review_queue'])}/{report['review_queue_total']})", "",
                  "Review answer and user question in the approved pilot interface; trace alone cannot grade relevance.", "",
                  "| UTC time | Request ID | Conversation ID | Reasons | Final QnA IDs |",
                  "|---|---|---:|---|---|"])
    for item in report["review_queue"]:
        lines.append(f"| {item['timestamp']} | {item['request_id']} | {item['conversation_id']} | {', '.join(item['reasons'])} | {item['final_qna_ids']} |")
    lines.extend(["", f"## SELECT/NONE quality sample ({len(report['quality_sample'])}/{report['quality_sample_pool']})", "",
                  "Review this sample even when there is no technical warning.", "",
                  "| UTC time | Request ID | Conversation ID | Decisions | Final QnA IDs |",
                  "|---|---|---:|---|---|"])
    for item in report["quality_sample"]:
        lines.append(f"| {item['timestamp']} | {item['request_id']} | {item['conversation_id']} | {item['selector_decisions']} | {item['final_qna_ids']} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--utc-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--input", type=Path, help="backend log file; stdin if omitted")
    parser.add_argument("--output", type=Path, help="write summary here; stdout if omitted")
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument("--review-limit", type=int, default=50)
    args = parser.parse_args()
    try:
        if date.fromisoformat(args.utc_date).isoformat() != args.utc_date:
            raise ValueError("noncanonical date")
    except ValueError:
        parser.error("--utc-date must be YYYY-MM-DD")
    if args.review_limit < 0:
        parser.error("--review-limit must be non-negative")
    with args.input.open(encoding="utf-8") if args.input else sys.stdin as stream:
        report = collect(stream, args.utc_date, args.review_limit)
    output = (json.dumps(report, ensure_ascii=False, indent=2) + "\n"
              if args.format == "json" else markdown(report))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output, encoding="utf-8")
    else:
        sys.stdout.write(output)
    return 0 if report["requests"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
