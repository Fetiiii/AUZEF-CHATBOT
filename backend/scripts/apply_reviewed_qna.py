"""Apply the three-reviewer QnA workbook to a LOCAL/DEV database.

The reviewed workbook is the pre-pilot source of truth for QnA content:

  red question/answer/tags  -> the whole QnA is removed (soft: status = 0)
  red query_N cell          -> only that query/alias is removed from its QnA
  green                     -> approved; question, answer and approved queries
                               are synced to the workbook text

Nothing is matched by row number or fuzzily. Every workbook row is reconciled
against the database by normalized canonical question, disambiguated by a
normalized answer hash and then by tags. Rows whose question the reviewers
edited are paired ONLY through an explicit, hash-pinned decisions file
(``reviewed_qna_decisions.json``); the same file records which red rows with a
content difference were approved for removal and whether content sync is
approved at all. Without an approval nothing beyond exact red removals is
planned, and every unresolved row is a blocker.

Safety model (optimistic, fail-fast):

* ``--dry-run`` prints every planned operation with the OLD question/answer
  hashes it expects and the NEW reviewed hashes, plus a plan fingerprint.
* ``--apply`` recomputes the plan and requires ``--plan-fingerprint`` to equal
  it, so exactly the reviewed plan is applied. Inside ONE transaction it locks
  every touched QnA row (``SELECT ... FOR UPDATE``), re-checks each expected old
  state (status, question/answer hash, query ids + text) and aborts with a full
  rollback on any difference or error. There is no partial apply.
* A removed QnA is deactivated (``status = 0``), never hard-deleted, so the
  integration API keeps reporting it as ``delete`` in ``/qna/changes``.
  Question/answer updates bump ``updated_at`` with the statement clock like an
  admin edit. Query-only changes leave the parent row untouched: the
  integration payload carries no queries.
* The admin database host must be local/dev and ``--confirm-local-dev`` given.

Search indexes are NOT touched; rebuilding Meili/Qdrant is a separate step.

Usage (inside the backend container, backend on PYTHONPATH)::

    python -m scripts.apply_reviewed_qna --input /tmp/reviewed.xlsx --dry-run --out /tmp/reviewed-kb
    python -m scripts.apply_reviewed_qna --input /tmp/reviewed.xlsx --apply --confirm-local-dev \\
        --plan-fingerprint <from dry-run> --out /tmp/reviewed-kb
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

RED = "FFFF0000"
GREEN = "FF00FF00"
QUESTION_COL, ANSWER_COL, TAGS_COL = 2, 3, 4
QUERY_COLS = range(5, 25)  # query_1 .. query_20
LOCAL_DB_HOSTS = {"db", "localhost", "127.0.0.1", "auzef_db", "::1"}
DEFAULT_DECISIONS = Path(__file__).with_name("reviewed_qna_decisions.json")
ACTOR = "reviewed-kb-apply"

EXACT_MATCH = "EXACT_MATCH"
CONTENT_DIFFERENCE = "CONTENT_DIFFERENCE"
AMBIGUOUS = "AMBIGUOUS"
UNMATCHED = "UNMATCHED"
APPROVED_QUESTION_EDIT = "APPROVED_QUESTION_EDIT"
DB_ONLY = "DB_ONLY"


class ReviewedApplyError(RuntimeError):
    """Stop without mutating: the database is not in the reviewed/expected state."""


# ── normalization / hashing ─────────────────────────────────────────────────


def normalize(text: Optional[str]) -> str:
    """Deterministic comparison key: NFC, Turkish-aware lower case, single spaces."""
    if text is None:
        return ""
    value = unicodedata.normalize("NFC", str(text))
    value = value.replace("İ", "i").replace("I", "ı").lower()
    return " ".join(value.split())


def text_hash(text: Optional[str]) -> str:
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()


def raw_hash(text: Optional[str]) -> str:
    """Exact-value hash used for optimistic old-state checks."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def file_sha256(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def split_tags(raw: Optional[str]) -> list[str]:
    if raw is None:
        return []
    return sorted({t.strip() for t in str(raw).split(",") if t.strip()})


# ── workbook ────────────────────────────────────────────────────────────────


@dataclass
class ReviewedRow:
    excel_row: int
    question: str
    answer: str
    tags: list[str]
    queries: list[dict]            # {"column", "text", "red"}
    red: bool                      # whole QnA red
    colors: dict = field(default_factory=dict)

    def approved_queries(self) -> list[str]:
        """Approved query texts, first occurrence per normalized key, workbook order."""
        seen, out = set(), []
        for q in self.queries:
            key = normalize(q["text"])
            if q["red"] or key in seen:
                continue
            seen.add(key)
            out.append(q["text"])
        return out


def _color(cell) -> str:
    if cell.fill is None or cell.fill.fill_type != "solid":
        return "none"
    rgb = getattr(cell.fill.fgColor, "rgb", None)
    return "red" if rgb == RED else ("green" if rgb == GREEN else f"other:{rgb}")


def load_workbook_rows(path: str) -> tuple[list[ReviewedRow], dict]:
    import openpyxl  # read-only use; the workbook is never saved

    workbook = openpyxl.load_workbook(path, read_only=False)
    sheet = workbook.worksheets[0]
    header = [sheet.cell(1, c).value for c in range(1, sheet.max_column + 1)]
    expected = ["question", "answer", "tags"] + [f"query_{i}" for i in range(1, 21)]
    if header[1:24] != expected:
        raise SystemExit(f"unexpected workbook header: {header[:24]}")
    rows, anomalies = [], []
    for r in range(2, sheet.max_row + 1):
        question = sheet.cell(r, QUESTION_COL).value
        if question is None or not str(question).strip():
            if any(sheet.cell(r, c).value not in (None, "") for c in range(2, 25)):
                anomalies.append({"excel_row": r, "issue": "content without question"})
            continue
        colors = {name: _color(sheet.cell(r, col)) for name, col in
                  (("question", QUESTION_COL), ("answer", ANSWER_COL), ("tags", TAGS_COL))}
        if len(set(colors.values())) != 1 or colors["question"] not in ("red", "green"):
            anomalies.append({"excel_row": r, "issue": f"mixed QnA colors {colors}"})
        queries = []
        for col in QUERY_COLS:
            value = sheet.cell(r, col).value
            if value is None or not str(value).strip():
                continue
            color = _color(sheet.cell(r, col))
            if color not in ("red", "green"):
                anomalies.append({"excel_row": r, "issue": f"query cell {col} color {color}"})
            queries.append({"column": f"query_{col - 4}", "text": str(value).strip(),
                             "red": color == "red"})
        rows.append(ReviewedRow(
            excel_row=r, question=str(question).strip(),
            answer=str(sheet.cell(r, ANSWER_COL).value or "").strip(),
            tags=split_tags(sheet.cell(r, TAGS_COL).value), queries=queries,
            red=colors["question"] == "red", colors=colors,
        ))
    summary = {
        "sheet": sheet.title,
        "qna_rows": len(rows),
        "red_qna": sum(r.red for r in rows),
        "kept_qna": sum(not r.red for r in rows),
        "filled_queries": sum(len(r.queries) for r in rows),
        "red_queries_total": sum(q["red"] for r in rows for q in r.queries),
        "red_queries_in_red_qna": sum(q["red"] for r in rows if r.red for q in r.queries),
        "red_queries_in_kept_qna": sum(q["red"] for r in rows if not r.red for q in r.queries),
        "green_queries_in_red_qna": sum(not q["red"] for r in rows if r.red for q in r.queries),
        "reviewer_column_distinct_values": len({sheet.cell(r.excel_row, 1).value for r in rows}),
        "anomalies": anomalies,
    }
    summary["active_queries_after_cleanup"] = sum(len(r.approved_queries()) for r in rows if not r.red)
    return rows, summary


def reviewed_dataset(rows: list[ReviewedRow]) -> tuple[list[dict], list[dict]]:
    """Approved QnA with approved, de-duplicated queries (text kept verbatim)."""
    dataset, duplicates = [], []
    for row in rows:
        if row.red:
            continue
        approved = row.approved_queries()
        seen = set()
        for q in row.queries:
            key = normalize(q["text"])
            if q["red"]:
                continue
            if key in seen:
                duplicates.append({"excel_row": row.excel_row, "column": q["column"],
                                   "query_text": q["text"]})
            seen.add(key)
        dataset.append({"question": row.question, "answer": row.answer, "tags": row.tags,
                        "queries": approved, "source_excel_row": row.excel_row,
                        "review_status": "approved"})
    return dataset, duplicates


# ── database snapshot + reconciliation ──────────────────────────────────────


def db_snapshot(db, lock_ids: Optional[list[int]] = None) -> dict[int, dict]:
    """QnA rows with tags and queries. With ``lock_ids`` those rows are locked FOR UPDATE."""
    from sqlalchemy import text
    from core.database import execute_admin_sql

    if lock_ids:
        execute_admin_sql(db, text("SELECT id FROM qna WHERE id = ANY(:ids) ORDER BY id FOR UPDATE"),
                          {"ids": sorted(lock_ids)}).all()
    rows = {}
    for r in execute_admin_sql(db, text(
            "SELECT id, question_text, answer_text, status FROM qna")).mappings():
        rows[r["id"]] = {"id": r["id"], "question": r["question_text"], "answer": r["answer_text"],
                         "status": r["status"], "tags": [], "queries": []}
    for r in execute_admin_sql(db, text(
            "SELECT qt.qna_id, t.name FROM qna_tags qt JOIN tags t ON t.id = qt.tag_id")).mappings():
        if r["qna_id"] in rows:
            rows[r["qna_id"]]["tags"].append(r["name"])
    for r in execute_admin_sql(db, text(
            "SELECT id, qna_id, query_text FROM qna_queries ORDER BY id")).mappings():
        if r["qna_id"] in rows:
            rows[r["qna_id"]]["queries"].append({"id": r["id"], "text": r["query_text"]})
    for row in rows.values():
        row["tags"] = sorted(set(row["tags"]))
    return rows


def load_decisions(path: Optional[Path]) -> dict:
    if path is None or not Path(path).exists():
        return {"sync_reviewed_content": False, "approved_red_content_difference": [],
                "question_edits": []}
    return json.loads(Path(path).read_text(encoding="utf-8"))


def reconcile(rows: list[ReviewedRow], db_rows: dict[int, dict],
              decisions: Optional[dict] = None) -> dict:
    decisions = decisions or {}
    by_question = defaultdict(list)
    for row in db_rows.values():
        by_question[normalize(row["question"])].append(row)
    edits = {e["excel_row"]: e for e in decisions.get("question_edits", [])}
    by_row = {r.excel_row: r for r in rows}

    results, matched_db_ids, duplicate_canonical, edit_errors = [], defaultdict(list), [], []
    for row in rows:
        candidates = by_question.get(normalize(row.question), [])
        status, db_id, note = UNMATCHED, None, ""
        if len(candidates) == 1:
            db_id = candidates[0]["id"]
            status = EXACT_MATCH if text_hash(candidates[0]["answer"]) == text_hash(row.answer) \
                else CONTENT_DIFFERENCE
        elif len(candidates) > 1:
            by_answer = [c for c in candidates if text_hash(c["answer"]) == text_hash(row.answer)]
            if len(by_answer) > 1:
                by_answer = [c for c in by_answer if c["tags"] == row.tags]
            if len(by_answer) == 1:
                db_id, status, note = by_answer[0]["id"], EXACT_MATCH, "disambiguated"
            else:
                status, note = AMBIGUOUS, f"{len(candidates)} DB rows share the question"
                duplicate_canonical.append(row.excel_row)
        edit = edits.get(row.excel_row)
        if edit is not None:
            target = db_rows.get(edit["db_id"])
            reviewed_ok = raw_hash(row.question) == edit["reviewed_question_sha256"]
            if target is None or not reviewed_ok:
                edit_errors.append({"excel_row": row.excel_row, "reason":
                                    "decision does not match workbook or DB id missing"})
            elif raw_hash(target["question"]) in (edit["old_question_sha256"],
                                                   edit["reviewed_question_sha256"]):
                if status == UNMATCHED or db_id == edit["db_id"]:
                    db_id, status, note = edit["db_id"], APPROVED_QUESTION_EDIT, "decisions file"
            else:
                edit_errors.append({"excel_row": row.excel_row, "db_id": edit["db_id"],
                                    "reason": "DB question is neither the approved old nor the reviewed text"})
        entry = {"excel_row": row.excel_row, "status": status, "db_id": db_id,
                 "red_qna": row.red, "note": note}
        if db_id is not None:
            matched_db_ids[db_id].append(row.excel_row)
            db = db_rows[db_id]
            entry["db_status"] = db["status"]
            entry["tags_equal"] = db["tags"] == row.tags
            if status == CONTENT_DIFFERENCE:
                entry["answer_hash_excel"] = text_hash(row.answer)[:16]
                entry["answer_hash_db"] = text_hash(db["answer"])[:16]
            entry["queries"] = reconcile_queries(row, db)
        results.append(entry)

    multi = {k: v for k, v in matched_db_ids.items() if len(v) > 1}
    db_only = [{"db_id": i, "question": r["question"], "status": r["status"]}
               for i, r in sorted(db_rows.items()) if i not in matched_db_ids]
    counts = defaultdict(int)
    for e in results:
        counts[e["status"]] += 1
    return {"rows": results, "db_only": db_only, "db_rows_matched_by_many_excel_rows": multi,
            "duplicate_canonical_excel_rows": duplicate_canonical, "counts": dict(counts),
            "decision_errors": edit_errors,
            "unmatched_rows": [e["excel_row"] for e in results if e["status"] == UNMATCHED
                               and not by_row[e["excel_row"]].red]}


def reconcile_queries(row: ReviewedRow, db: dict) -> dict:
    db_by_key = defaultdict(list)
    for q in db["queries"]:
        db_by_key[normalize(q["text"])].append(q["id"])
    excel_keys = defaultdict(list)
    for q in row.queries:
        excel_keys[normalize(q["text"])].append(q)
    red_found, red_missing = [], []
    for q in row.queries:
        if q["red"]:
            ids = db_by_key.get(normalize(q["text"]), [])
            (red_found if ids else red_missing).append(
                {"column": q["column"], "text": q["text"], "db_query_ids": ids})
    green_keys = {k for k, qs in excel_keys.items() if any(not q["red"] for q in qs)}
    red_keys = {k for k, qs in excel_keys.items() if all(q["red"] for q in qs)}
    return {"red_found": red_found, "red_missing_in_db": red_missing,
            "green_in_excel_not_in_db": sorted(k for k in green_keys if k not in db_by_key),
            "db_queries_not_in_excel": sorted(k for k in db_by_key if k not in excel_keys),
            "db_duplicate_query_rows": {k: ids for k, ids in db_by_key.items() if len(ids) > 1},
            "red_and_green_same_text": sorted(k for k in excel_keys
                                              if k not in green_keys and k not in red_keys)}


# ── plan ────────────────────────────────────────────────────────────────────


def _query_ops(row: ReviewedRow, db: dict) -> dict:
    """Make the DB query set equal to the approved queries (first text per key)."""
    want = {}
    for text_value in row.approved_queries():
        want[normalize(text_value)] = text_value
    keep_by_key, delete, update = {}, [], []
    for q in db["queries"]:            # ordered by id: first row per key is kept
        key = normalize(q["text"])
        if key in want and key not in keep_by_key:
            keep_by_key[key] = q
            if q["text"] != want[key]:
                update.append({"id": q["id"], "old_sha256": raw_hash(q["text"]),
                               "new_text": want[key]})
        else:
            delete.append({"id": q["id"], "old_sha256": raw_hash(q["text"])})
    insert = [t for k, t in want.items() if k not in keep_by_key]
    return {"delete": delete, "update": update, "insert": insert}


def build_plan(rows: list[ReviewedRow], rec: dict, db_rows: dict[int, dict],
               decisions: Optional[dict] = None) -> dict:
    decisions = decisions or {}
    sync = bool(decisions.get("sync_reviewed_content"))
    approved_red = {d["db_id"] for d in decisions.get("approved_red_content_difference", [])}
    by_row = {e["excel_row"]: e for e in rec["rows"]}
    ops, blockers = [], list(rec.get("decision_errors", []))

    for row in rows:
        entry = by_row[row.excel_row]
        db_id = entry["db_id"]
        if row.red:
            if entry["status"] == EXACT_MATCH or (entry["status"] == CONTENT_DIFFERENCE
                                                  and db_id in approved_red):
                if db_rows[db_id]["status"] == 1:
                    ops.append({"op": "deactivate", "db_id": db_id, "excel_row": row.excel_row,
                                "expect": _expect(db_rows[db_id])})
            else:
                blockers.append({"excel_row": row.excel_row,
                                 "reason": f"red QnA is {entry['status']} and not approved for removal"})
            continue
        if entry["status"] in (AMBIGUOUS, UNMATCHED):
            blockers.append({"excel_row": row.excel_row, "reason": f"kept QnA is {entry['status']}"})
            continue
        db = db_rows[db_id]
        if db["status"] != 1:
            blockers.append({"excel_row": row.excel_row, "db_id": db_id,
                             "reason": "approved QnA is inactive in DB"})
            continue
        if db["tags"] != row.tags:
            blockers.append({"excel_row": row.excel_row, "db_id": db_id,
                             "reason": "tag difference (tag sync is out of scope)"})
        content_change = db["question"] != row.question or db["answer"] != row.answer
        if content_change:
            if not sync and not (entry["status"] == EXACT_MATCH and db["question"] == row.question
                                 and db["answer"] == row.answer):
                blockers.append({"excel_row": row.excel_row, "db_id": db_id,
                                 "reason": "content differs and reviewed content sync is not approved"})
            else:
                ops.append({"op": "update_qna", "db_id": db_id, "excel_row": row.excel_row,
                            "expect": _expect(db),
                            "new": {"question_sha256": raw_hash(row.question),
                                    "answer_sha256": raw_hash(row.answer),
                                    "question_changed": db["question"] != row.question,
                                    "answer_changed": db["answer"] != row.answer},
                            "question": row.question, "answer": row.answer})
        qops = _query_ops(row, db)
        red_texts = {normalize(q["text"]) for q in row.queries if q["red"]}
        only_red_deletes = all(normalize(next(q["text"] for q in db["queries"] if q["id"] == d["id"]))
                               in red_texts for d in qops["delete"])
        if any(qops.values()):
            if not sync and not (only_red_deletes and not qops["insert"] and not qops["update"]):
                blockers.append({"excel_row": row.excel_row, "db_id": db_id,
                                 "reason": "query differences and reviewed content sync is not approved"})
            else:
                ops.append({"op": "sync_queries", "db_id": db_id, "excel_row": row.excel_row,
                            **qops})
    for key, label in (("duplicate_canonical_excel_rows", "duplicate canonical matches"),):
        if rec[key]:
            blockers.append({"reason": label, "excel_rows": rec[key]})
    if rec["db_rows_matched_by_many_excel_rows"]:
        blockers.append({"reason": "DB rows matched by several workbook rows",
                         "db_ids": sorted(rec["db_rows_matched_by_many_excel_rows"])})
    active_db_only = [d for d in rec["db_only"] if d["status"] == 1]
    if active_db_only:
        blockers.append({"reason": "active DB rows absent from the workbook",
                         "db_ids": [d["db_id"] for d in active_db_only]})
    plan = {"ops": ops, "blockers": blockers}
    plan["summary"] = plan_summary(plan)
    plan["fingerprint"] = plan_fingerprint(plan)
    return plan


def _expect(db: dict) -> dict:
    return {"status": db["status"], "question_sha256": raw_hash(db["question"]),
            "answer_sha256": raw_hash(db["answer"]),
            "query_ids": [q["id"] for q in db["queries"]]}


def plan_summary(plan: dict) -> dict:
    ops = plan["ops"]
    updates = [o for o in ops if o["op"] == "update_qna"]
    queries = [o for o in ops if o["op"] == "sync_queries"]
    return {"deactivate_qna": sum(o["op"] == "deactivate" for o in ops),
            "update_answer": sum(o["new"]["answer_changed"] for o in updates),
            "update_question": sum(o["new"]["question_changed"] for o in updates),
            "delete_queries": sum(len(o["delete"]) for o in queries),
            "insert_queries": sum(len(o["insert"]) for o in queries),
            "update_query_text": sum(len(o["update"]) for o in queries),
            "total_mutations": sum(o["op"] != "sync_queries" for o in ops)
                               + sum(len(o["delete"]) + len(o["insert"]) + len(o["update"])
                                     for o in queries),
            "blockers": len(plan["blockers"])}


def plan_fingerprint(plan: dict) -> str:
    payload = json.dumps({"ops": plan["ops"], "blockers": plan["blockers"]},
                         sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def plan_report(plan: dict) -> list[dict]:
    """Human-reviewable per-operation view (hashes, not full text)."""
    out = []
    for o in plan["ops"]:
        item = {"op": o["op"], "db_id": o["db_id"], "excel_row": o["excel_row"]}
        if o["op"] in ("deactivate", "update_qna"):
            item["old_question_sha256"] = o["expect"]["question_sha256"]
            item["old_answer_sha256"] = o["expect"]["answer_sha256"]
        if o["op"] == "update_qna":
            item["reviewed_question_sha256"] = o["new"]["question_sha256"]
            item["reviewed_answer_sha256"] = o["new"]["answer_sha256"]
            item["question_changed"] = o["new"]["question_changed"]
            item["answer_changed"] = o["new"]["answer_changed"]
        if o["op"] == "sync_queries":
            item["query_diff"] = {"delete_ids": [d["id"] for d in o["delete"]],
                                  "update_ids": [u["id"] for u in o["update"]],
                                  "insert": o["insert"]}
        out.append(item)
    return out


# ── apply ───────────────────────────────────────────────────────────────────


def assert_local_database() -> str:
    url = os.getenv("ADMIN_DATABASE_URL") or os.getenv("DATABASE_URL") or ""
    host = urlparse(url).hostname or ""
    if host not in LOCAL_DB_HOSTS:
        raise SystemExit(f"refusing to apply: admin database host {host!r} is not local/dev")
    return host


def _check_expected(plan: dict, locked: dict[int, dict]) -> None:
    for o in plan["ops"]:
        row = locked.get(o["db_id"])
        if row is None:
            raise ReviewedApplyError(f"QnA {o['db_id']} disappeared")
        if o["op"] in ("deactivate", "update_qna"):
            if _expect(row) != o["expect"]:
                raise ReviewedApplyError(f"QnA {o['db_id']} is not in the expected old state")
        else:
            current = {q["id"]: raw_hash(q["text"]) for q in row["queries"]}
            for d in o["delete"] + o["update"]:
                if current.get(d["id"]) != d["old_sha256"]:
                    raise ReviewedApplyError(f"query {d['id']} of QnA {o['db_id']} changed")


def apply_plan(db, plan: dict) -> dict:
    """Apply inside the caller's transaction. Raises before/while writing on any mismatch."""
    from sqlalchemy import text
    from core.database import execute_admin_sql

    if plan["blockers"]:
        raise ReviewedApplyError("plan has blockers")
    locked = db_snapshot(db, lock_ids=[o["db_id"] for o in plan["ops"]])
    _check_expected(plan, locked)
    stamp = "updated_by = :actor, updated_at = timezone('UTC', clock_timestamp())"
    done = defaultdict(int)
    for o in plan["ops"]:
        if o["op"] == "deactivate":
            done["deactivated_qna"] += execute_admin_sql(db, text(
                f"UPDATE qna SET status = 0, {stamp} WHERE id = :id AND status = 1"),
                {"id": o["db_id"], "actor": ACTOR}).rowcount
        elif o["op"] == "update_qna":
            done["updated_qna"] += execute_admin_sql(db, text(
                f"UPDATE qna SET question_text = :q, answer_text = :a, {stamp} WHERE id = :id"),
                {"id": o["db_id"], "q": o["question"], "a": o["answer"], "actor": ACTOR}).rowcount
        else:
            if o["delete"]:
                done["deleted_queries"] += execute_admin_sql(db, text(
                    "DELETE FROM qna_queries WHERE qna_id = :qid AND id = ANY(:ids)"),
                    {"qid": o["db_id"], "ids": [d["id"] for d in o["delete"]]}).rowcount
            for u in o["update"]:
                done["updated_queries"] += execute_admin_sql(db, text(
                    "UPDATE qna_queries SET query_text = :t WHERE qna_id = :qid AND id = :id"),
                    {"qid": o["db_id"], "id": u["id"], "t": u["new_text"]}).rowcount
            for text_value in o["insert"]:
                execute_admin_sql(db, text(
                    "INSERT INTO qna_queries (qna_id, query_text) VALUES (:qid, :t)"),
                    {"qid": o["db_id"], "t": text_value})
                done["inserted_queries"] += 1
    expected = plan["summary"]
    if (done["deactivated_qna"] != expected["deactivate_qna"]
            or done["deleted_queries"] != expected["delete_queries"]
            or done["updated_queries"] != expected["update_query_text"]):
        raise ReviewedApplyError(f"row counts differ from the plan: {dict(done)}")
    return dict(done)


# ── verification ────────────────────────────────────────────────────────────


def verify(db, rows: list[ReviewedRow], rec: dict) -> dict:
    """Compare the DB with the workbook row by row (exact text)."""
    snapshot = db_snapshot(db)
    by_row = {e["excel_row"]: e["db_id"] for e in rec["rows"]}
    kept_ids = {by_row[r.excel_row] for r in rows if not r.red and by_row.get(r.excel_row)}
    result = defaultdict(list)
    for row in rows:
        db_id = by_row.get(row.excel_row)
        db = snapshot.get(db_id) if db_id else None
        if row.red:
            if db is not None and db["status"] == 1:
                result["red_active_qna"].append(row.excel_row)
            continue
        if db is None or db["status"] != 1:
            result["reviewed_active_qna_missing"].append(row.excel_row)
            continue
        if db["question"] != row.question:
            result["question_mismatch"].append(row.excel_row)
        if db["answer"] != row.answer:
            result["answer_mismatch"].append(row.excel_row)
        if db["tags"] != row.tags:
            result["tag_mismatch"].append(row.excel_row)
        have = [q["text"] for q in db["queries"]]
        want = row.approved_queries()
        red = {normalize(q["text"]) for q in row.queries if q["red"]} - {normalize(t) for t in want}
        for t in want:
            if t not in have:
                result["approved_query_missing"].append({"excel_row": row.excel_row, "query": t})
        for t in have:
            if normalize(t) in red:
                result["red_query_still_active"].append({"excel_row": row.excel_row, "query": t})
            elif t not in want:
                result["unexpected_active_query"].append({"excel_row": row.excel_row, "query": t})
    active = {i: r for i, r in snapshot.items() if r["status"] == 1}
    result["active_qna_not_in_workbook"] = sorted(set(active) - kept_ids)
    counts = {k: len(v) for k, v in result.items()}
    return {"active_qna": len(active), "inactive_qna": len(snapshot) - len(active),
            "active_queries": sum(len(r["queries"]) for r in active.values()),
            "mismatch_counts": {k: counts.get(k, 0) for k in (
                "reviewed_active_qna_missing", "red_active_qna", "red_query_still_active",
                "approved_query_missing", "question_mismatch", "answer_mismatch",
                "tag_mismatch", "unexpected_active_query", "active_qna_not_in_workbook")},
            "details": {k: v[:20] for k, v in result.items() if v}}


# ── CLI ─────────────────────────────────────────────────────────────────────


def _write(out: Path, name: str, payload) -> None:
    out.mkdir(parents=True, exist_ok=True)
    target = out / name
    if name.endswith(".jsonl"):
        target.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in payload),
                          encoding="utf-8")
    else:
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--input", required=True, help="reviewed workbook (.xlsx), read only")
    parser.add_argument("--decisions", default=str(DEFAULT_DECISIONS),
                        help="approved reconciliation decisions (JSON)")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-local-dev", action="store_true",
                        help="required with --apply; the DB host must also be local")
    parser.add_argument("--plan-fingerprint", default=None,
                        help="required with --apply: the fingerprint printed by --dry-run")
    parser.add_argument("--out", default=None, help="directory for audit/plan/verification JSON")
    args = parser.parse_args(argv)

    decisions = load_decisions(Path(args.decisions) if args.decisions else None)
    workbook_sha = file_sha256(args.input)
    if decisions.get("workbook_sha256") and decisions["workbook_sha256"] != workbook_sha:
        raise SystemExit("decisions file was approved for a different workbook")
    rows, summary = load_workbook_rows(args.input)
    dataset, dataset_duplicates = reviewed_dataset(rows)

    from core.database import SessionLocal

    with SessionLocal() as db:
        snapshot = db_snapshot(db)
        rec = reconcile(rows, snapshot, decisions)
        plan = build_plan(rows, rec, snapshot, decisions)
        report = {"mode": "apply" if args.apply else "dry-run", "workbook_sha256": workbook_sha,
                  "workbook": summary, "reconciliation_counts": rec["counts"],
                  "plan_summary": plan["summary"], "plan_fingerprint": plan["fingerprint"],
                  "blockers": plan["blockers"], "dataset_qna": len(dataset),
                  "dataset_queries": sum(len(d["queries"]) for d in dataset),
                  "dataset_duplicates_removed": len(dataset_duplicates)}
        if args.out:
            out = Path(args.out)
            _write(out, "reconciliation.json", rec)
            _write(out, "plan-operations.json", plan_report(plan))
            _write(out, "reviewed-qna.jsonl", dataset)
        if args.apply:
            if not args.confirm_local_dev:
                raise SystemExit("--apply requires --confirm-local-dev")
            report["db_host"] = assert_local_database()
            if args.plan_fingerprint != plan["fingerprint"]:
                raise SystemExit("--plan-fingerprint does not match the current plan; re-run --dry-run")
            try:
                report["applied"] = apply_plan(db, plan)
                report["verification_before_commit"] = verify(db, rows, rec)
                if any(report["verification_before_commit"]["mismatch_counts"].values()):
                    raise ReviewedApplyError("post-apply verification failed")
                db.commit()
            except Exception:
                db.rollback()
                raise
        report["verification"] = verify(db, rows, rec)
        if args.out:
            _write(Path(args.out), f"{report['mode']}-report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
