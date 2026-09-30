"""Apply the KB Freeze v1 content decisions to a LOCAL/DEV database.

Source of truth is the content owner's filled decision form, translated into an
explicit, hash-pinned decision package (``kb_freeze_v1_content_decisions.json``):

  updates[]          revise an existing QnA (question and/or answer) + alias add/remove
  new_qna[]          insert a new QnA with existing tags and aliases
  deactivate_qna[]   merge: soft-delete (status = 0) a redundant QnA into ``merge_into``
  query_changes[]    alias moves; must be mirrored by the updates' add/remove lists
  data_quality[]     answer-only cleanups with an old-state hash
  conscious_backlog  recorded only; never produces a mutation

Nothing is matched fuzzily: every existing row is addressed by DB id and guarded by
the exact sha256 of its expected old question/answer; a new QnA may not collide
(normalized) with any existing canonical question; an added alias may not collide
with an active alias or canonical question of another QnA. A row already in the
target state is skipped, so a second run plans zero mutations.

Safety model (same discipline as ``apply_reviewed_qna``): ``--dry-run`` writes the
plan and a plan fingerprint; ``--apply`` needs ``--confirm-local-dev``, a local
admin DB host, the matching ``--plan-fingerprint`` and the decision form whose
sha256 the package pins. One transaction: touched rows are locked FOR UPDATE, every
expected old state is re-checked, row counts must equal the plan and the post-state
must verify, otherwise everything is rolled back. Search indexes are NOT touched.

Usage (inside the backend container)::

    python -m scripts.apply_kb_freeze_v1_content --source-document /tmp/form.docx --dry-run --out /tmp/kbf
    python -m scripts.apply_kb_freeze_v1_content --source-document /tmp/form.docx --apply \\
        --confirm-local-dev --plan-fingerprint <from dry-run> --out /tmp/kbf
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.apply_reviewed_qna import (  # noqa: E402  (shared, tested helpers)
    _write,
    assert_local_database,
    db_snapshot,
    file_sha256,
    normalize,
    raw_hash,
)

DEFAULT_DECISIONS = Path(__file__).with_name("kb_freeze_v1_content_decisions.json")
ACTOR = "kb-freeze-v1-apply"


class FreezeApplyError(RuntimeError):
    """Stop without mutating: the database is not in the expected state."""


# ── package ─────────────────────────────────────────────────────────────────


def load_package(path: Path) -> dict:
    package = json.loads(Path(path).read_text(encoding="utf-8"))
    for key in ("updates", "new_qna", "deactivate_qna", "query_changes", "data_quality", "conscious_backlog"):
        package.setdefault(key, [])
    return package


def package_sha256(package: dict) -> str:
    return hashlib.sha256(json.dumps(package, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def check_source_document(package: dict, path: Optional[str]) -> str:
    if not path:
        raise SystemExit("--source-document is required (the filled decision form)")
    actual = file_sha256(path)
    if actual != package["source_document"]["sha256"]:
        raise SystemExit("decision package was approved for a different source document")
    return actual


def existing_tags(db) -> set[str]:
    from sqlalchemy import text
    from core.database import execute_admin_sql

    return {r[0] for r in execute_admin_sql(db, text("SELECT name FROM tags")).all()}


# ── plan ────────────────────────────────────────────────────────────────────


def _content_targets(package: dict) -> list[dict]:
    """updates + data_quality as one list of {db_id, expected hashes, new q/a, aliases}."""
    items = []
    for u in package["updates"]:
        items.append({**u, "kind": "update"})
    for d in package["data_quality"]:
        items.append({"db_id": d["db_id"], "kind": "data_quality", "source_section": d.get("source_section"),
                      "expected_old_question_sha256": d["expected_old_question_sha256"],
                      "expected_old_answer_sha256": d["expected_old_answer_sha256"],
                      "new_question": None, "new_answer": d["new_answer"],
                      "add_queries": [], "remove_queries": []})
    return items


def build_plan(package: dict, snapshot: dict[int, dict], tags: set[str]) -> dict:
    ops, blockers = [], []
    deactivated = {d["db_id"] for d in package["deactivate_qna"]}
    targets = _content_targets(package)
    touched = [t["db_id"] for t in targets] + list(deactivated)
    if len(touched) != len(set(touched)):
        blockers.append({"reason": "a DB id is addressed by more than one decision"})

    # Post-state alias/canonical ownership, used for cross-QnA collision checks.
    removed = defaultdict(set)
    for t in targets:
        for r in t["remove_queries"]:
            removed[t["db_id"]].add(normalize(r["text"]))
    owner = defaultdict(set)                   # normalized text -> active QnA ids that keep it
    for qid, row in snapshot.items():
        if row["status"] != 1 or qid in deactivated:
            continue
        owner[normalize(row["question"])].add(qid)
        for q in row["queries"]:
            key = normalize(q["text"])
            if key not in removed[qid]:
                owner[key].add(qid)
    for t in targets:
        if t.get("new_question"):
            owner[normalize(t["new_question"])].add(t["db_id"])

    def alias_collision(text_value: str, target) -> bool:
        return bool(owner.get(normalize(text_value), set()) - {target})

    # Existing rows: content + aliases.
    for t in targets:
        db_id = t["db_id"]
        row = snapshot.get(db_id)
        if row is None:
            blockers.append({"db_id": db_id, "reason": "QnA not found"})
            continue
        if row["status"] != 1:
            blockers.append({"db_id": db_id, "reason": "QnA to revise is inactive"})
            continue
        new_q = t["new_question"]
        q_old = raw_hash(row["question"]) == t["expected_old_question_sha256"]
        q_done = new_q is not None and row["question"] == new_q
        a_old = raw_hash(row["answer"]) == t["expected_old_answer_sha256"]
        a_done = t["new_answer"] is None or row["answer"] == t["new_answer"]
        if not ((q_old or q_done) and (a_old or a_done)):
            blockers.append({"db_id": db_id, "reason": "QnA is neither in the expected old nor the target state"})
            continue
        target_q = new_q if new_q is not None else row["question"]
        target_a = t["new_answer"] if t["new_answer"] is not None else row["answer"]
        if row["question"] != target_q or row["answer"] != target_a:
            ops.append({"op": "update_qna", "kind": t["kind"], "db_id": db_id,
                        "expect": {"status": 1, "question_sha256": raw_hash(row["question"]),
                                   "answer_sha256": raw_hash(row["answer"])},
                        "question": target_q, "answer": target_a,
                        "question_changed": row["question"] != target_q,
                        "answer_changed": row["answer"] != target_a,
                        "source_section": t.get("source_section")})
        have = {normalize(q["text"]): q for q in row["queries"]}
        inserts, seen = [], set()
        for text_value in t["add_queries"]:
            key = normalize(text_value)
            if key in have or key in seen:
                continue
            seen.add(key)
            if alias_collision(text_value, db_id):
                blockers.append({"db_id": db_id, "query": text_value,
                                 "reason": "alias collides with another active QnA"})
                continue
            inserts.append(text_value)
        deletes = []
        for r in t["remove_queries"]:
            matches = [q for q in row["queries"] if q["text"] == r["text"]]
            if matches and raw_hash(matches[0]["text"]) != r["sha256"]:
                blockers.append({"db_id": db_id, "reason": "query to remove has an unexpected hash"})
            deletes += [{"id": q["id"], "old_sha256": raw_hash(q["text"])} for q in matches]
        if inserts or deletes:
            ops.append({"op": "sync_queries", "db_id": db_id, "insert": inserts, "delete": deletes})

    # Merges (soft delete).
    content_ids = {t["db_id"] for t in targets}
    for d in package["deactivate_qna"]:
        row = snapshot.get(d["db_id"])
        if d.get("merge_into") is not None and d["merge_into"] not in content_ids:
            blockers.append({"db_id": d["db_id"], "reason": "merge target has no content decision"})
        if row is None:
            blockers.append({"db_id": d["db_id"], "reason": "QnA not found"})
            continue
        if raw_hash(row["question"]) != d["expected_old_question_sha256"] \
                or raw_hash(row["answer"]) != d["expected_old_answer_sha256"]:
            blockers.append({"db_id": d["db_id"], "reason": "QnA to deactivate is not in the expected old state"})
            continue
        if row["status"] == 1:
            ops.append({"op": "deactivate", "db_id": d["db_id"], "merge_into": d.get("merge_into"),
                        "expect": {"status": 1, "question_sha256": raw_hash(row["question"]),
                                   "answer_sha256": raw_hash(row["answer"])}})

    # Alias moves must be mirrored by the content decisions.
    by_id = {t["db_id"]: t for t in targets}
    for m in package["query_changes"]:
        src, dst = by_id.get(m["from_db_id"]), by_id.get(m.get("to_db_id"))
        if src is None or dst is None or m["text"] not in [r["text"] for r in src["remove_queries"]] \
                or m["text"] not in dst["add_queries"]:
            blockers.append({"reason": "query move not mirrored by remove/add decisions", "text": m["text"]})

    # New QnA.
    canonical = defaultdict(list)
    for qid, row in snapshot.items():
        canonical[normalize(row["question"])].append(qid)
    new_keys = set()
    for n in package["new_qna"]:
        key = normalize(n["question"])
        if key in new_keys:
            blockers.append({"key": n["key"], "reason": "duplicate canonical inside the package"})
            continue
        new_keys.add(key)
        missing_tags = sorted(set(n["tags"]) - tags)
        if missing_tags:
            blockers.append({"key": n["key"], "reason": f"unknown tags {missing_tags} (no tag creation)"})
        hits = canonical.get(key, [])
        if hits:
            exact = [i for i in hits if snapshot[i]["status"] == 1 and snapshot[i]["question"] == n["question"]
                     and snapshot[i]["answer"] == n["answer"]]
            if len(exact) == 1 and len(hits) == 1:
                continue                                   # already applied
            blockers.append({"key": n["key"], "reason": "duplicate canonical question in DB", "db_ids": hits})
            continue
        queries, seen = [], set()
        for text_value in n["queries"]:
            qkey = normalize(text_value)
            if qkey in seen or qkey == key:
                continue
            seen.add(qkey)
            if alias_collision(text_value, None):
                blockers.append({"key": n["key"], "query": text_value,
                                 "reason": "alias collides with another active QnA"})
                continue
            queries.append(text_value)
        for text_value in [n["question"], *queries]:       # later new QnA may not reuse these
            owner[normalize(text_value)].add(f"new:{n['key']}")
        ops.append({"op": "insert_qna", "key": n["key"], "question": n["question"], "answer": n["answer"],
                    "tags": sorted(n["tags"]), "queries": queries, "source_section": n.get("source_section")})

    plan = {"ops": ops, "blockers": blockers}
    plan["summary"] = plan_summary(plan)
    plan["fingerprint"] = plan_fingerprint(plan, package)
    return plan


def plan_summary(plan: dict) -> dict:
    ops = plan["ops"]
    upd = [o for o in ops if o["op"] == "update_qna"]
    sync = [o for o in ops if o["op"] == "sync_queries"]
    ins = [o for o in ops if o["op"] == "insert_qna"]
    summary = {
        "update_qna": sum(o["kind"] == "update" for o in upd),
        "update_question": sum(o["question_changed"] for o in upd),
        "update_answer": sum(o["answer_changed"] for o in upd),
        "data_quality_cleanup": sum(o["kind"] == "data_quality" for o in upd),
        "insert_qna": len(ins),
        "insert_tag_links": sum(len(o["tags"]) for o in ins),
        "soft_delete_qna": sum(o["op"] == "deactivate" for o in ops),
        "query_add": sum(len(o["insert"]) for o in sync) + sum(len(o["queries"]) for o in ins),
        "query_remove": sum(len(o["delete"]) for o in sync),
        "blockers": len(plan["blockers"]),
    }
    summary["total_mutations"] = (len(upd) + summary["insert_qna"] + summary["insert_tag_links"]
                                  + summary["soft_delete_qna"] + summary["query_add"] + summary["query_remove"])
    return summary


def plan_fingerprint(plan: dict, package: dict) -> str:
    payload = json.dumps({"ops": plan["ops"], "blockers": plan["blockers"],
                          "package_sha256": package_sha256(package),
                          "source_document_sha256": package["source_document"]["sha256"]},
                         sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def plan_report(plan: dict) -> list[dict]:
    out = []
    for o in plan["ops"]:
        item = {k: o[k] for k in ("op", "db_id", "key", "kind", "merge_into", "source_section") if k in o}
        if "expect" in o:
            item["old_question_sha256"] = o["expect"]["question_sha256"]
            item["old_answer_sha256"] = o["expect"]["answer_sha256"]
        if o["op"] == "update_qna":
            item.update(question_changed=o["question_changed"], answer_changed=o["answer_changed"],
                        new_question_sha256=raw_hash(o["question"]), new_answer_sha256=raw_hash(o["answer"]))
        if o["op"] == "sync_queries":
            item.update(insert=o["insert"], delete_ids=[d["id"] for d in o["delete"]])
        if o["op"] == "insert_qna":
            item.update(question=o["question"], tags=o["tags"], queries=o["queries"],
                        answer_sha256=raw_hash(o["answer"]))
        out.append(item)
    return out


# ── apply ───────────────────────────────────────────────────────────────────


def apply_plan(db, plan: dict) -> dict:
    """Apply inside the caller's transaction. Raises on any mismatch (caller rolls back)."""
    from sqlalchemy import text
    from core.database import execute_admin_sql

    if plan["blockers"]:
        raise FreezeApplyError("plan has blockers")
    lock_ids = [o["db_id"] for o in plan["ops"] if "db_id" in o]
    locked = db_snapshot(db, lock_ids=lock_ids)
    for o in plan["ops"]:
        if o["op"] in ("update_qna", "deactivate"):
            row = locked.get(o["db_id"])
            if row is None or {"status": row["status"], "question_sha256": raw_hash(row["question"]),
                               "answer_sha256": raw_hash(row["answer"])} != o["expect"]:
                raise FreezeApplyError(f"QnA {o['db_id']} is not in the expected old state")
        elif o["op"] == "sync_queries":
            current = {q["id"]: raw_hash(q["text"]) for q in locked[o["db_id"]]["queries"]}
            for d in o["delete"]:
                if current.get(d["id"]) != d["old_sha256"]:
                    raise FreezeApplyError(f"query {d['id']} of QnA {o['db_id']} changed")
        elif o["op"] == "insert_qna":
            if any(normalize(r["question"]) == normalize(o["question"]) for r in locked.values()):
                raise FreezeApplyError(f"canonical question for {o['key']} appeared concurrently")
    stamp = "updated_by = :actor, updated_at = timezone('UTC', clock_timestamp())"
    done, new_ids = defaultdict(int), {}
    for o in plan["ops"]:
        if o["op"] == "update_qna":
            done["updated_qna"] += execute_admin_sql(db, text(
                f"UPDATE qna SET question_text = :q, answer_text = :a, {stamp} WHERE id = :id AND status = 1"),
                {"id": o["db_id"], "q": o["question"], "a": o["answer"], "actor": ACTOR}).rowcount
        elif o["op"] == "deactivate":
            done["soft_deleted_qna"] += execute_admin_sql(db, text(
                f"UPDATE qna SET status = 0, {stamp} WHERE id = :id AND status = 1"),
                {"id": o["db_id"], "actor": ACTOR}).rowcount
        elif o["op"] == "sync_queries":
            if o["delete"]:
                done["deleted_queries"] += execute_admin_sql(db, text(
                    "DELETE FROM qna_queries WHERE qna_id = :qid AND id = ANY(:ids)"),
                    {"qid": o["db_id"], "ids": [d["id"] for d in o["delete"]]}).rowcount
            for text_value in o["insert"]:
                execute_admin_sql(db, text("INSERT INTO qna_queries (qna_id, query_text) VALUES (:qid, :t)"),
                                  {"qid": o["db_id"], "t": text_value})
                done["inserted_queries"] += 1
        else:
            new_id = execute_admin_sql(db, text(
                "INSERT INTO qna (question_text, answer_text, status, updated_by) "
                "VALUES (:q, :a, 1, :actor) RETURNING id"),
                {"q": o["question"], "a": o["answer"], "actor": ACTOR}).scalar_one()
            new_ids[o["key"]] = new_id
            done["inserted_qna"] += 1
            for tag in o["tags"]:
                done["inserted_tag_links"] += execute_admin_sql(db, text(
                    "INSERT INTO qna_tags (qna_id, tag_id) SELECT :qid, id FROM tags WHERE name = :name"),
                    {"qid": new_id, "name": tag}).rowcount
            for text_value in o["queries"]:
                execute_admin_sql(db, text("INSERT INTO qna_queries (qna_id, query_text) VALUES (:qid, :t)"),
                                  {"qid": new_id, "t": text_value})
                done["inserted_queries"] += 1
    s = plan["summary"]
    expected = {"updated_qna": s["update_qna"] + s["data_quality_cleanup"], "soft_deleted_qna": s["soft_delete_qna"],
                "deleted_queries": s["query_remove"], "inserted_queries": s["query_add"],
                "inserted_qna": s["insert_qna"], "inserted_tag_links": s["insert_tag_links"]}
    if {k: done.get(k, 0) for k in expected} != expected:
        raise FreezeApplyError(f"row counts differ from the plan: {dict(done)} != {expected}")
    return {"counts": dict(done), "new_ids": new_ids}


# ── verification ────────────────────────────────────────────────────────────


def verify(db, package: dict, before_active: Optional[set] = None) -> dict:
    """Compare the DB with the package (exact text). Counts must all be 0."""
    snap = db_snapshot(db)
    active = {i: r for i, r in snap.items() if r["status"] == 1}
    result = defaultdict(list)
    for t in _content_targets(package):
        row = snap.get(t["db_id"])
        if row is None or row["status"] != 1:
            result["missing_approved_update"].append(t["db_id"])
            continue
        if t["new_question"] is not None and row["question"] != t["new_question"]:
            result["question_mismatch"].append(t["db_id"])
        if t["new_answer"] is not None and row["answer"] != t["new_answer"]:
            result["answer_mismatch"].append(t["db_id"])
        have = {normalize(q["text"]) for q in row["queries"]}
        for text_value in t["add_queries"]:
            if normalize(text_value) not in have and normalize(text_value) != normalize(row["question"]):
                result["query_mismatch"].append({"db_id": t["db_id"], "missing": text_value})
        for r in t["remove_queries"]:
            if any(q["text"] == r["text"] for q in row["queries"]):
                result["query_mismatch"].append({"db_id": t["db_id"], "still_present": r["text"]})
    for d in package["deactivate_qna"]:
        if d["db_id"] in active:
            result["unexpected_active_deprecated_qna"].append(d["db_id"])
    new_ids = set()
    for n in package["new_qna"]:
        hits = [i for i, r in active.items() if normalize(r["question"]) == normalize(n["question"])]
        if len(hits) != 1:
            result["missing_new_qna" if not hits else "unexpected_qna"].append(n["key"])
            continue
        row = active[hits[0]]
        new_ids.add(hits[0])
        if row["question"] != n["question"]:
            result["question_mismatch"].append(n["key"])
        if row["answer"] != n["answer"]:
            result["answer_mismatch"].append(n["key"])
        if row["tags"] != sorted(set(n["tags"])):
            result["tag_mismatch"].append(n["key"])
        have = {normalize(q["text"]) for q in row["queries"]}
        for text_value in n["queries"]:
            if normalize(text_value) not in have and normalize(text_value) != normalize(n["question"]):
                result["query_mismatch"].append({"key": n["key"], "missing": text_value})
    if before_active is not None:
        expected_active = (set(before_active) - {d["db_id"] for d in package["deactivate_qna"]}) | new_ids
        result["unexpected_qna"] += sorted(set(active) - expected_active)
        result["missing_active_qna"] += sorted(expected_active - set(active))
    keys = ("missing_approved_update", "missing_new_qna", "unexpected_active_deprecated_qna", "query_mismatch",
            "answer_mismatch", "question_mismatch", "tag_mismatch", "unexpected_qna", "missing_active_qna")
    return {"active_qna": len(active), "inactive_qna": len(snap) - len(active),
            "active_queries": sum(len(r["queries"]) for r in active.values()),
            "mismatch_counts": {k: len(result.get(k, [])) for k in keys},
            "details": {k: v[:20] for k, v in result.items() if v}}


def dataset_fingerprint(db) -> dict:
    """Active dataset hash: qna_search_view status=1 (id, question, answer, sorted tags, sorted distinct queries)."""
    from sqlalchemy import text
    from core.database import execute_admin_sql

    rows = execute_admin_sql(db, text("SELECT id, question, answer, tags, queries FROM qna_search_view "
                                      "WHERE status = 1 ORDER BY id")).mappings().all()
    payload = [{"id": r["id"], "question": r["question"], "answer": r["answer"],
                "tags": sorted(r["tags"] or []), "queries": sorted(r["queries"] or [])} for r in rows]
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                       separators=(",", ":")).encode("utf-8")).hexdigest()
    return {"active_qna": len(payload), "active_distinct_queries": sum(len(p["queries"]) for p in payload),
            "active_dataset_sha256": digest,
            "scope": "qna_search_view status=1 (id, question, answer, sorted tags, sorted distinct queries)"}


# ── CLI ─────────────────────────────────────────────────────────────────────


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--decisions", default=str(DEFAULT_DECISIONS))
    parser.add_argument("--source-document", default=None, help="filled decision form; its sha256 is pinned")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-local-dev", action="store_true")
    parser.add_argument("--plan-fingerprint", default=None)
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    package = load_package(Path(args.decisions))
    source_sha = check_source_document(package, args.source_document)

    from core.database import SessionLocal

    with SessionLocal() as db:
        snapshot = db_snapshot(db)
        before_active = {i for i, r in snapshot.items() if r["status"] == 1}
        plan = build_plan(package, snapshot, existing_tags(db))
        report = {"mode": "apply" if args.apply else "dry-run", "source_document_sha256": source_sha,
                  "package_sha256": package_sha256(package), "plan_summary": plan["summary"],
                  "plan_fingerprint": plan["fingerprint"], "blockers": plan["blockers"],
                  "conscious_backlog": [b["id"] for b in package["conscious_backlog"]],
                  "dataset_before": dataset_fingerprint(db)}
        if args.out:
            _write(Path(args.out), f"{report['mode']}-plan-operations.json", plan_report(plan))
        if args.apply:
            if not args.confirm_local_dev:
                raise SystemExit("--apply requires --confirm-local-dev")
            report["db_host"] = assert_local_database()
            if args.plan_fingerprint != plan["fingerprint"]:
                raise SystemExit("--plan-fingerprint does not match the current plan; re-run --dry-run")
            try:
                report["applied"] = apply_plan(db, plan)
                report["verification_before_commit"] = verify(db, package, before_active)
                if any(report["verification_before_commit"]["mismatch_counts"].values()):
                    raise FreezeApplyError("post-apply verification failed")
                db.commit()
            except Exception:
                db.rollback()
                raise
        report["verification"] = verify(db, package, before_active if args.apply else None)
        report["dataset_after"] = dataset_fingerprint(db)
        if args.out:
            _write(Path(args.out), f"{report['mode']}-report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
