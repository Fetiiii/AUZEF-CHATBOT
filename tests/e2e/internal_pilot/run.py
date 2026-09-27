"""Live pilot transport and decision-trace runner.

Semantic targets are recorded for human review, never graded by answer text.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib import error, parse, request


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
CATALOG = HERE / "scenarios.json"
SMOKE_IDS = {"IP-E2E-001", "IP-E2E-025", "IP-E2E-029", "IP-E2E-033", "IP-E2E-034", "IP-E2E-038", "IP-E2E-041"}
FAULTS = ("openrouter_unavailable", "openrouter_timeout", "admin_llm_off", "selector_circuit_open", "meilisearch_unavailable", "qdrant_unavailable")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def choose(args):
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    if catalog.get("schema_version") != 1:
        raise ValueError("unsupported catalog version")
    scenarios = catalog["scenarios"]
    known = {s["id"] for s in scenarios}
    if set(args.id or []) - known:
        raise ValueError("unknown scenario ID")
    chosen = []
    for scenario in scenarios:
        if args.id and scenario["id"] not in args.id:
            continue
        if args.category and scenario["category"] not in args.category:
            continue
        if not args.id and not args.category and args.suite == "smoke" and scenario["id"] not in SMOKE_IDS:
            continue
        fault = scenario.get("fault_injection")
        if (fault and fault != args.fault_ready) or (args.fault_ready and not fault):
            continue
        chosen.append(scenario)
    if not chosen:
        raise ValueError("no scenarios selected; fault cases require --fault-ready")
    return chosen


def preflight(mode, via):
    if mode == "none":
        return {"mode": mode, "status": "SKIPPED"}
    command = [sys.executable, "-m", "scripts.internal_pilot_preflight", mode, "--json"]
    cwd = ROOT / "backend"
    if via == "compose":
        command = ["docker", "compose", "run", "--rm", "-T", "--no-deps",
                   "-v", f"{ROOT}:/workspace:ro", "-w", "/workspace/backend",
                   "backend", "python", "-m", "scripts.internal_pilot_preflight", mode, "--json"]
        cwd = ROOT
    try:
        proc = subprocess.run(
            command, cwd=cwd, capture_output=True, text=True, check=False,
        )
    except OSError as exc:
        return {"mode": mode, "via": via, "status": "FAIL", "error_type": type(exc).__name__}
    try:
        detail = json.loads(proc.stdout)
    except ValueError:
        detail = None
    return {"mode": mode, "via": via, "status": "PASS" if proc.returncode == 0 and isinstance(detail, dict) else "FAIL",
            "exit_code": proc.returncode, "detail": detail}


def send(base_url, payload, timeout):
    req = request.Request(
        base_url.rstrip("/") + "/widget-chat",
        json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        {"Content-Type": "application/json"}, method="POST",
    )
    try:
        with request.urlopen(req, timeout=timeout) as response:
            status, raw = response.status, response.read(1_000_001)
    except error.HTTPError as exc:
        return exc.code, None, "http_error"
    except (error.URLError, TimeoutError) as exc:
        return 0, None, type(exc).__name__
    if len(raw) > 1_000_000:
        return status, None, "response_too_large"
    try:
        value = json.loads(raw)
    except ValueError:
        return status, None, "invalid_json"
    return status, value if isinstance(value, dict) else None, None


def parse_traces(raw):
    traces = []
    for line in raw.splitlines():
        if "decision_trace=" not in line:
            continue
        try:
            item = json.loads(line.split("decision_trace=", 1)[1])
        except ValueError:
            continue
        if item.get("event") == "answer_pipeline_decision_trace" and item.get("schema_version") == 7:
            traces.append(item)
    return traces


def read_traces(args, since):
    if args.trace_log:
        return parse_traces(args.trace_log.read_text(encoding="utf-8"))
    if args.trace_source == "compose":
        cmd = ["docker", "compose", "logs", "--no-color", "--since", since, "backend"]
    else:
        journal_since = datetime.fromisoformat(since.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M:%S UTC")
        cmd = ["journalctl", "-u", "auzef-backend.service", "--since", journal_since,
               "--no-pager", "-o", "cat"]
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=False)
    if proc.returncode:
        raise RuntimeError(f"trace source failed: {args.trace_source} ({proc.returncode})")
    return parse_traces(proc.stdout)


def attach_traces(rows, traces, since):
    grouped = {}
    for trace in traces:
        req = trace.get("request") or {}
        if (req.get("endpoint") == "widget_chat"
                and isinstance(req.get("conversation_id"), int)
                and (req.get("timestamp") or "") >= since):
            grouped.setdefault(req["conversation_id"], []).append(trace)
    for group in grouped.values():
        group.sort(key=lambda t: (t.get("request") or {}).get("timestamp") or "")
    for row in rows:
        for turn in row["turns"]:
            group = grouped.get(turn.get("conversation_id"), [])
            if not group:
                turn["checks"].append("missing_decision_trace")
                continue
            trace = group.pop(0)
            selectors = trace.get("selectors") or []
            turn["trace"] = {
                "request_id": (trace.get("request") or {}).get("request_id"),
                "timestamp": (trace.get("request") or {}).get("timestamp"),
                "intent_count": (trace.get("intent_analyzer") or {}).get("intent_count"),
                "intent_mode": (trace.get("execution") or {}).get("execution_mode"),
                "calendar_relevant": [i.get("calendar_relevant") for i in (trace.get("intent_analyzer") or {}).get("intents") or []],
                "selector_decisions": [s.get("selector_decision") for s in selectors if s.get("selector_called")],
                "selected_qna_ids": [s.get("selected_qna_id") for s in selectors if s.get("used_in_final")],
                "final": trace.get("final"),
                "degraded_reason": (trace.get("execution") or {}).get("degraded_reason"),
                "degraded_runs": len(trace.get("degraded") or []),
                "circuit_states": [c.get("circuit_state_after") for c in trace.get("circuit") or []],
                "circuit_skipped_calls": sum(bool(c.get("llm_call_skipped")) for c in trace.get("circuit") or []),
                "source_availability": trace.get("source_availability"),
            }
            observed = turn["trace"]
            differences = []
            expected_mode = row.get("expected_intent_mode")
            if expected_mode in ("SINGLE", "MULTI") and observed["intent_count"] is not None:
                actual_mode = "MULTI" if observed["intent_count"] > 1 else "SINGLE"
                if actual_mode != expected_mode:
                    differences.append("intent_mode")
            expected_decision = row.get("expected_selector_decision")
            if expected_decision in ("SELECT", "NONE") and expected_decision not in observed["selector_decisions"]:
                differences.append("selector_decision")
            expected_calendar = row.get("calendar_relevant")
            if isinstance(expected_calendar, bool) and observed["calendar_relevant"]:
                if any(observed["calendar_relevant"]) != expected_calendar:
                    differences.append("calendar_relevance")
            expected_degraded = row.get("degraded_expected")
            if isinstance(expected_degraded, bool) and bool(observed["degraded_runs"]) != expected_degraded:
                differences.append("degraded_mode")
            turn["behavior_review"] = differences


def save(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--suite", choices=("smoke", "standard"), default="smoke")
    parser.add_argument("--id", action="append")
    parser.add_argument("--category", action="append")
    parser.add_argument("--fault-ready", choices=FAULTS)
    parser.add_argument("--preflight", choices=("none", "runtime", "candidate"), default="none")
    parser.add_argument("--preflight-via", choices=("host", "compose"), default="host")
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--report", type=Path, help="unique report path; defaults to a timestamped file")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--trace-log", type=Path)
    source.add_argument("--trace-source", choices=("compose", "journal"))
    args = parser.parse_args()
    parsed_url = parse.urlsplit(args.base_url)
    if parsed_url.scheme not in ("http", "https") or not parsed_url.netloc:
        parser.error("--base-url must be an HTTP(S) URL")
    if args.report is None:
        stamp = now().replace(":", "").replace("-", "")
        args.report = ROOT / "outputs/internal-pilot-e2e" / f"run-{stamp}.json"
    if args.report.exists():
        parser.error("report already exists; choose a new --report path")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    try:
        scenarios = choose(args)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    report = {"schema_version": 1, "started_at": now(), "suite": args.suite,
              "preflight": preflight(args.preflight, args.preflight_via), "scenarios": []}
    if report["preflight"]["status"] == "FAIL":
        report.update(status="TECHNICAL_FAIL", finished_at=now())
        save(args.report, report)
        print(f"preflight failed; report: {args.report}", file=sys.stderr)
        return 1
    for scenario in scenarios:
        row = {key: scenario.get(key) for key in (
            "id", "category", "semantic_target", "expected_intent_mode",
            "expected_selector_decision", "calendar_relevant", "degraded_expected", "known_issue_id", "critical")}
        row.update(human_review="PENDING", turns=[])
        conversation_id = conversation_token = None
        for index, question in enumerate(scenario["turns"], 1):
            payload = {"message": question}
            if conversation_id is not None:
                payload.update(conversation_id=conversation_id, conversation_token=conversation_token)
            start = time.monotonic()
            status, response, problem = send(args.base_url, payload, args.timeout)
            answer = response.get("answer") if response else None
            received_id = response.get("conversation_id") if response else None
            received_token = response.get("conversation_token") if response else None
            checks = []
            if status != 200 or problem or response is None:
                checks.append(problem or f"http_{status}")
            if not isinstance(answer, str) or not answer.strip():
                checks.append("empty_answer")
            if not isinstance(received_id, int) or not isinstance(received_token, str) or not received_token:
                checks.append("missing_conversation_identity")
            elif conversation_id is not None and received_id != conversation_id:
                checks.append("conversation_changed")
            row["turns"].append({"index": index, "http_status": status,
                                 "latency_ms": round((time.monotonic() - start) * 1000, 3),
                                 "answer_chars": len(answer) if isinstance(answer, str) else None,
                                 "conversation_id": received_id, "checks": checks})
            if checks:
                break
            conversation_id, conversation_token = received_id, received_token
        report["scenarios"].append(row)
    try:
        attach_traces(report["scenarios"], read_traces(args, report["started_at"]), report["started_at"])
    except (OSError, RuntimeError) as exc:
        report["trace_error"] = type(exc).__name__
        for row in report["scenarios"]:
            for turn in row["turns"]:
                turn["checks"].append("trace_collection_failed")
    failed = sum(bool(turn["checks"]) for row in report["scenarios"] for turn in row["turns"])
    behavior = sum(bool(turn.get("behavior_review")) for row in report["scenarios"] for turn in row["turns"])
    report.update(finished_at=now(), technical_failures=failed, behavior_review_turns=behavior,
                  status="TECHNICAL_FAIL" if failed else "MANUAL_REQUIRED")
    save(args.report, report)
    print(f"{report['status']}: {len(report['scenarios'])} scenarios, {failed} technical failures, {behavior} behavior review turns; {args.report}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
