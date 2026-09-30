"""Pilot evaluation export: reproducible, read-only dump of pilot turns for HUMAN review.

Usage (inside the backend container, where the DB is reachable):

  docker logs auzef_backend 2>&1 | docker exec -i auzef_backend \\
      python -m scripts.pilot_evaluation_export --from 2026-10-01 --to 2026-10-14 --output-dir /tmp/pilot-export
  docker cp auzef_backend:/tmp/pilot-export ./pilot-export

Inputs: decision_trace log lines (stdin or --traces FILE) + read-only SELECTs on
conversation_messages / qna. For every widget_chat trace in the UTC date range
it writes one row joined on ``request.user_message_id`` (trace schema >= 8):
user query, up to two previous user turns (the context the analyzer may use),
analyzer summary, candidate refs, selector model/prompt identity, selected ref
or NONE, final QnA ref, latency/retry/failure, and the bot answer text.

What it does NOT do: it never writes to the database, never produces gold and
never decides correct/incorrect. ``watchlist`` labels are review hints from
observable signals only. Obvious PII (TC-like 11-digit numbers, phone numbers,
e-mail addresses, other long digit runs such as SMS codes) is masked in every
text field; conversation tokens and IP addresses are never read.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

EXPORT_SCHEMA = 1
DEADLINE_PROXIMITY_MS = 13000
LATENCY_TAIL_MS = 10000
NEGATION = re.compile(r"bulunmamakta|yapılmamakta|uygulanmamakta|verilmemekte|yararlanamaz|bulunmuyor|"
                      r"yapılmaz|mümkün değil|yoktur|yapılamaz|alınamaz|durdurulmuş", re.I)

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
_PHONE = re.compile(r"(?<!\d)(\+?90[\s-]?)?0?5\d{2}[\s-]?\d{3}[\s-]?\d{2}[\s-]?\d{2}(?!\d)")
_TC = re.compile(r"(?<!\d)[1-9]\d{10}(?!\d)")
_LONG_DIGITS = re.compile(r"(?<!\d)\d{4,}(?!\d)")


def mask(text: Optional[str]) -> Optional[str]:
    """Mask obvious PII. Years (19xx/20xx) and short numbers stay readable."""
    if text is None:
        return None
    text = _EMAIL.sub("[EMAIL]", text)
    text = _PHONE.sub("[PHONE]", text)
    text = _TC.sub("[TC_OR_ID]", text)
    return _LONG_DIGITS.sub(lambda m: m.group(0) if re.fullmatch(r"(19|20)\d{2}", m.group(0)) else "[NUMBER]", text)


def iter_traces(lines: Iterable[str], start: date, end: date):
    lo, hi = start.isoformat(), end.isoformat()
    seen = set()
    for line in lines:
        if "decision_trace=" not in line:
            continue
        try:
            trace = json.loads(line.split("decision_trace=", 1)[1])
        except ValueError:
            continue
        if trace.get("event") != "answer_pipeline_decision_trace":
            continue
        req = trace.get("request") or {}
        day = str(req.get("timestamp") or "")[:10]
        if req.get("endpoint") != "widget_chat" or not (lo <= day <= hi):
            continue
        if req.get("request_id") in seen:
            continue
        seen.add(req.get("request_id"))
        yield trace


def _selector_rows(trace: dict) -> list[dict]:
    rows = []
    for s in trace.get("selectors") or []:
        rows.append({
            "purpose": s.get("purpose"), "called": bool(s.get("selector_called")),
            "status": s.get("selector_status"), "decision": s.get("selector_decision"),
            "selected_ref": s.get("selected_candidate_ref"), "used_in_final": s.get("used_in_final"),
            "candidate_refs": s.get("candidate_refs") or [], "model": s.get("model"),
            "actual_model": s.get("actual_model"), "config_fingerprint": s.get("config_fingerprint"),
            "prompt_version": s.get("prompt_version"), "prompt_fingerprint": s.get("prompt_fingerprint"),
            "latency_ms": s.get("latency_ms"), "retry_count": s.get("retry_count"),
            "failure_category": s.get("failure_category"), "timeout": bool(s.get("timeout")),
            "invalid_output": bool(s.get("invalid_output")),
        })
    return rows


def _analyzer_row(trace: dict) -> Optional[dict]:
    a = trace.get("intent_analyzer")
    if not a:
        return None
    meta = a.get("provider_metadata") or {}
    return {"intent_count": a.get("intent_count"), "outcome_status": a.get("outcome_status"),
            "fallback_to_single": a.get("fallback_to_single"),
            "model": a.get("actual_model") or a.get("requested_model"),
            "config_fingerprint": a.get("config_fingerprint"), "latency_ms": a.get("latency_ms"),
            "retry_count": meta.get("retry_count"), "failure_category": a.get("failure_category"),
            "previous_user_context_count": a.get("previous_user_context_count"),
            # Resolved (contextualized) text is deliberately not logged; only its length and flags.
            "intents": [{k: i.get(k) for k in ("calendar_relevant", "context_used", "resolved_length")}
                        for i in a.get("intents") or []]}


class Db:
    """Read-only lookups; the session is rolled back and closed, never committed."""

    def __init__(self):
        from core.database import SessionLocal

        self.session = SessionLocal()

    def close(self):
        self.session.rollback()
        self.session.close()

    def _one(self, sql, admin=False, **params):
        rows = self._all(sql, admin=admin, **params)
        return rows[0] if rows else None

    def _all(self, sql, admin=False, **params):
        from sqlalchemy import text

        from core.database import execute_admin_sql, execute_chat_sql

        run = execute_admin_sql if admin else execute_chat_sql   # qna: admin DB; conversations: chat DB
        return run(self.session, text(sql), params).mappings().all()

    def turn(self, conversation_id, user_message_id):
        user = self._one("SELECT id, content, created_at FROM conversation_messages "
                         "WHERE id = :id AND conversation_id = :c AND role = 'user'", id=user_message_id, c=conversation_id)
        if user is None:
            return None, [], None
        previous = self._all("SELECT content FROM conversation_messages WHERE conversation_id = :c AND role = 'user' "
                             "AND id < :id ORDER BY id DESC LIMIT 2", c=conversation_id, id=user_message_id)
        bot = self._one("SELECT id, content, source FROM conversation_messages WHERE conversation_id = :c "
                        "AND role = 'bot' AND id > :id ORDER BY id ASC LIMIT 1", c=conversation_id, id=user_message_id)
        return user, [r["content"] for r in reversed(previous)], bot

    def qna(self, qna_id):
        return self._one("SELECT id, question, answer FROM qna_search_view WHERE id = :id", admin=True, id=qna_id)


def _similar(a: str, b: str) -> float:
    ta = {t for t in re.findall(r"\w+", (a or "").lower()) if len(t) >= 3}
    tb = {t for t in re.findall(r"\w+", (b or "").lower()) if len(t) >= 3}
    return len(ta & tb) / len(ta | tb) if (ta | tb) else 0.0


def watchlist(row: dict) -> list[str]:
    """Observable review hints only — never a verdict."""
    labels = []
    called = [s for s in row["selectors"] if s["called"]]
    if any(s["decision"] == "NONE" and s["candidate_refs"] for s in called):
        labels.append("false_none_candidate")
    if row.get("final_answer_is_negation"):
        labels.append("premise_correction_candidate")
    if any(s["decision"] == "SELECT" for s in called) and row.get("final_outcome") == "answer":
        labels.append("select_review_for_wrong_specific_or_unsafe")
    if row.get("reask_of_previous_turn"):
        labels.append("semantic_instability_reask_signal")
    if any((s["latency_ms"] or 0) >= LATENCY_TAIL_MS for s in called):
        labels.append("selector_latency_tail")
    if any((s["latency_ms"] or 0) >= DEADLINE_PROXIMITY_MS or s["timeout"] for s in called):
        labels.append("deadline_proximity_or_timeout")
    if any((s["retry_count"] or 0) > 0 or s["failure_category"] for s in called):
        labels.append("provider_retry_or_failure")
    return labels


def build_rows(traces, db: Optional[Db]) -> list[dict]:
    rows = []
    for trace in traces:
        req, final = trace.get("request") or {}, trace.get("final") or {}
        row = {
            "request_id": req.get("request_id"), "timestamp": req.get("timestamp"),
            "conversation_id": req.get("conversation_id"), "user_message_id": req.get("user_message_id"),
            "trace_schema": trace.get("schema_version"), "ai_config_version": req.get("ai_config_version"),
            "analyzer": _analyzer_row(trace), "selectors": _selector_rows(trace),
            "final_outcome": final.get("final_outcome"), "final_source": final.get("final_source"),
            "final_qna_ids": final.get("final_qna_ids") or [], "total_latency_ms": final.get("total_latency_ms"),
            "user_query": None, "previous_user_turns": [], "bot_answer": None, "final_qna_question": None,
            "join": "no_user_message_id",
        }
        if db is not None and row["conversation_id"] and row["user_message_id"]:
            user, previous, bot = db.turn(row["conversation_id"], row["user_message_id"])
            if user is None:
                row["join"] = "user_message_not_found"
            else:
                row["join"] = "ok"
                row["user_query"] = mask(user["content"])
                row["previous_user_turns"] = [mask(t) for t in previous]
                row["bot_answer"] = mask(bot["content"]) if bot else None
                row["reask_of_previous_turn"] = bool(previous) and _similar(user["content"], previous[-1]) >= 0.6
            if row["final_qna_ids"]:
                qna = db.qna(row["final_qna_ids"][0])
                if qna is not None:
                    row["final_qna_question"] = qna["question"]
                    row["final_answer_is_negation"] = bool(NEGATION.search(qna["answer"] or ""))
        row["watchlist"] = watchlist(row)
        rows.append(row)
    return rows


def _pct(values, q):
    values = sorted(v for v in values if isinstance(v, (int, float)))
    return round(values[max(0, min(len(values) - 1, int((len(values) - 1) * q + 0.5)))], 1) if values else None


def summarize(rows: list[dict]) -> dict:
    sel = [s for r in rows for s in r["selectors"] if s["called"]]
    lat = [s["latency_ms"] for s in sel]
    labels = {}
    for r in rows:
        for label in r["watchlist"]:
            labels[label] = labels.get(label, 0) + 1
    return {"turns": len(rows), "joined": sum(r["join"] == "ok" for r in rows),
            "join_status": {k: sum(r["join"] == k for r in rows) for k in sorted({r["join"] for r in rows})},
            "selector_calls": len(sel),
            "selector_latency_ms": {"p50": _pct(lat, 0.5), "p95": _pct(lat, 0.95),
                                    "max": max((v for v in lat if isinstance(v, (int, float))), default=None),
                                    "ge_10s": sum((v or 0) >= 10000 for v in lat),
                                    "ge_13s": sum((v or 0) >= 13000 for v in lat),
                                    "ge_14s": sum((v or 0) >= 14000 for v in lat)},
            "selector_retries": sum(s["retry_count"] or 0 for s in sel),
            "selector_timeouts": sum(s["timeout"] for s in sel),
            "selector_identities": sorted({f"{s['actual_model']}|{s['prompt_version']}|{s['prompt_fingerprint']}"
                                           for s in sel}),
            "watchlist_counts": dict(sorted(labels.items()))}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--from", dest="start", required=True, help="UTC date YYYY-MM-DD (inclusive)")
    parser.add_argument("--to", dest="end", required=True, help="UTC date YYYY-MM-DD (inclusive)")
    parser.add_argument("--traces", type=Path, help="log file with decision_trace lines (default: stdin)")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--no-db", action="store_true", help="trace-only export (no message text)")
    args = parser.parse_args(argv)
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    raw = (args.traces.read_text(encoding="utf-8") if args.traces else sys.stdin.read()).splitlines()
    db = None if args.no_db else Db()
    try:
        rows = build_rows(iter_traces(raw, start, end), db)
    finally:
        if db is not None:
            db.close()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    with open(args.output_dir / "turns.jsonl", "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    with open(args.output_dir / "review.csv", "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "request_id", "conversation_id", "user_query", "previous_user_turns",
                         "selector_decisions", "selected_refs", "final_qna_ids", "final_qna_question",
                         "watchlist", "reviewer_verdict", "reviewer_note"])
        for r in rows:
            called = [s for s in r["selectors"] if s["called"]]
            writer.writerow([r["timestamp"], r["request_id"], r["conversation_id"], r["user_query"],
                             " || ".join(r["previous_user_turns"]), ";".join(str(s["decision"]) for s in called),
                             ";".join(str(s["selected_ref"]) for s in called), r["final_qna_ids"],
                             r["final_qna_question"], ";".join(r["watchlist"]), "", ""])
    manifest = {"export_schema": EXPORT_SCHEMA, "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "date_range_utc": [args.start, args.end], "trace_input_sha256": hashlib.sha256(
                    "\n".join(raw).encode("utf-8")).hexdigest(), "db_joined": db is not None,
                "no_gold": True, "labels_are_review_hints_only": True,
                "pii_masking": ["email", "phone", "11-digit TC-like", "other 4+ digit runs except years"],
                **summarize(rows)}
    (args.output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n",
                                                   encoding="utf-8")
    print(json.dumps({k: manifest[k] for k in ("turns", "joined", "selector_calls", "watchlist_counts")}))
    return 0 if rows else 2


if __name__ == "__main__":
    raise SystemExit(main())
