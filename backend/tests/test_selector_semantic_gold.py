"""Phase 7B semantic adjudication lock → Semantic Gold V1 → re-score.
Offline: no provider call (module-wide network guard)."""
import csv
import hashlib
import io
import json
import os
from pathlib import Path
from urllib.parse import urlparse

import pytest

from benchmarks.selector_v2 import adjudication as adj
from benchmarks.selector_v2 import prompt_experiment as px
from benchmarks.selector_v2 import semantic_gold as sg
from benchmarks.selector_v2.contract import sha256_text
from benchmarks.selector_v2.prompt_contract import load_committed_manifest, prompt_manifest
from benchmarks.selector_v2.safety import no_live_calls
from benchmarks.selector_v2.schema import BenchmarkResult, EvaluationStatus, Outcome
from tests.test_selector_adjudication import _world

REPO = Path(__file__).resolve().parents[2]
OUT = REPO / "outputs" / "selector-v2-benchmark"


@pytest.fixture(autouse=True)
def _no_network():
    db_hosts = [urlparse(os.environ[name]).hostname
                for name in ("DATABASE_URL", "ADMIN_DATABASE_URL", "CHAT_DATABASE_URL")
                if os.environ.get(name)]
    with no_live_calls(allow_hosts=db_hosts):
        yield


def _jl(rows):
    return "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows)


def _csv(rows, columns):
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, lineterminator="\r\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({c: row.get(c, "") for c in columns})
    return buf.getvalue()


@pytest.fixture()
def env(tmp_path):
    """Locked-able review packet + follow-up + decisions on the synthetic world."""
    snaps, scope, packet = _world()
    review = tmp_path / "review"
    files = {"review-cases.jsonl": _jl(packet["primary"]),
             "review-template.csv": adj.review_template_csv(packet["primary"]),
             "audit-view.jsonl": _jl(packet["audit"])}
    hashes = adj.write_immutable(review, files)
    (review / "manifest.json").write_text(json.dumps({
        "review_packet_fingerprint": "pkt", "source_snapshot_fingerprint": "snap",
        "artifact_sha256": hashes}))
    ids = [r["case_id"] for r in packet["primary"]]
    follow_ids = ids[:2]
    audit = {a["case_id"]: a for a in packet["audit"]}

    def gold_label(cid):
        return audit[cid]["current_gold_labels"][0]

    def other_label(cid):
        return next(l for l, ref in audit[cid]["label_to_ref"].items()
                    if ref not in audit[cid]["current_gold_refs"])

    first_rows, final_rows = [], []
    plan = {ids[2]: ("SELECT_ACCEPTABLE", gold_label(ids[2])),           # KEEP_CURRENT
            ids[3]: ("SELECT_ACCEPTABLE", other_label(ids[3])),          # CHANGE_GOLD
            ids[4]: ("SELECT_ACCEPTABLE", f"{gold_label(ids[4])} {other_label(ids[4])}"),  # MULTI
            ids[5]: ("EXPECT_NONE", ""),
            ids[6]: ("EXCLUDE_AMBIGUOUS", ""),
            ids[7]: ("CONTENT_REVIEW_REQUIRED", "")}
    for cid in ids:
        text = next(r["intent_text"] for r in packet["primary"] if r["case_id"] == cid)
        if cid in follow_ids:
            first_rows.append({"case_id": cid, "intent_text": text,
                               "blind_decision": "NEED_FULL_CANDIDATES"})
            decision = ("RETRIEVAL_OR_KB_MAPPING_REVIEW", "") if cid == follow_ids[0] \
                else ("SELECT_ACCEPTABLE", gold_label(cid))
            final_rows.append({"case_id": cid, "intent_text": text, "blind_decision": decision[0],
                               "acceptable_labels": decision[1],
                               "review_round": "FULL_CANDIDATE_FOLLOWUP"})
        else:
            decision = plan.get(cid, ("SELECT_ACCEPTABLE", gold_label(cid)))
            row = {"case_id": cid, "intent_text": text, "blind_decision": decision[0],
                   "acceptable_labels": decision[1]}
            first_rows.append(row)
            final_rows.append({**row, "review_round": "FIRST_PASS"})
    follow = tmp_path / "follow"
    fp_text = "﻿" + _csv(first_rows, adj.CSV_COLUMNS)
    fh = adj.write_immutable(follow, {"first-pass-decisions.csv": fp_text})
    (follow / "manifest.json").write_text(json.dumps({
        "case_ids": follow_ids, "followup_fingerprint": "fup", "artifact_sha256": fh}))
    columns = ["case_id", "intent_text", "blind_decision", "acceptable_labels", "review_note",
               "reviewer", "reviewed_at", "review_round"]
    decisions = ("﻿" + _csv(final_rows, columns)).encode("utf-8")
    return {"snaps": snaps, "packet": packet, "review": review, "follow": follow,
            "decisions": decisions, "rows": final_rows, "columns": columns, "ids": ids,
            "plan": plan, "follow_ids": follow_ids}


def _lock(env, decisions=None, sha=None):
    data = env["decisions"] if decisions is None else decisions
    return sg.lock_review(decisions_bytes=data,
                          expected_sha=sha or hashlib.new("sha256", data).hexdigest(),
                          review_dir=env["review"], followup_dir=env["follow"])


# ── lock ────────────────────────────────────────────────────────────────────

def test_lock_validates_and_is_immutable(env, tmp_path):
    lock = _lock(env)
    assert len(lock["records"]) == len(env["ids"])
    assert lock["rounds"] == {"FIRST_PASS": len(env["ids"]) - 2, "FULL_CANDIDATE_FOLLOWUP": 2}
    assert "NEED_FULL_CANDIDATES" not in lock["decision_counts"]
    assert _lock(env)["lock_fingerprint"] == lock["lock_fingerprint"]
    manifest = sg.write_lock(env["review"] / "locked", lock, "t0")
    assert sg.write_lock(env["review"] / "locked", lock, "t1")["locked_at"] == "t0"
    loaded, records = sg.load_lock(env["review"] / "locked")
    assert loaded["lock_fingerprint"] == manifest["lock_fingerprint"] and records == lock["records"]
    path = env["review"] / "locked" / "locked-review.jsonl"
    path.write_text(path.read_text().replace("SELECT_ACCEPTABLE", "EXPECT_NONE", 1))
    with pytest.raises(SystemExit):
        sg.load_lock(env["review"] / "locked")


def test_lock_rejects_wrong_sha_and_invalid_rows(env):
    with pytest.raises(SystemExit):
        _lock(env, sha="0" * 64)

    def with_rows(mutate):
        rows = [dict(r) for r in env["rows"]]
        mutate(rows)
        return ("﻿" + _csv(rows, env["columns"])).encode("utf-8")

    def set_row(i, **kw):
        return lambda rows: rows[i].update(kw)

    bad = [
        set_row(2, blind_decision=""),                                          # blank
        set_row(2, blind_decision="NEED_FULL_CANDIDATES", acceptable_labels=""),
        set_row(0, blind_decision="NEED_FULL_CANDIDATES"),                      # follow-up
        set_row(2, acceptable_labels="ZZ"),                                     # unknown label
        set_row(5, acceptable_labels="A"),                                      # labels on NONE
        set_row(2, blind_decision="SELECT_ACCEPTABLE", acceptable_labels=""),
        set_row(8, blind_decision="EXCLUDE_AMBIGUOUS", acceptable_labels=""),   # first pass changed
        set_row(0, review_round="FIRST_PASS"),                                  # follow-up ids
        lambda rows: rows.pop(),                                                # 105 rows
        lambda rows: rows.append(dict(rows[0])),                                # duplicate id
    ]
    for mutate in bad:
        with pytest.raises(SystemExit):
            _lock(env, decisions=with_rows(mutate))


def test_audit_is_inaccessible_before_lock(env):
    with pytest.raises(SystemExit):
        sg.open_audit_after_lock(env["review"], env["review"] / "locked")
    sg.write_lock(env["review"] / "locked", _lock(env), "t")
    _manifest, records, audit = sg.open_audit_after_lock(env["review"], env["review"] / "locked")
    assert len(records) == len(audit)


# ── semantic gold ───────────────────────────────────────────────────────────

def _built(env):
    sg.write_lock(env["review"] / "locked", _lock(env), "t")
    manifest, records, audit = sg.open_audit_after_lock(env["review"], env["review"] / "locked")
    return sg.build_semantic_gold(env["snaps"], records, audit,
                                  lock_fp=manifest["lock_fingerprint"], parent_fp="parent"), audit


def test_semantic_gold_outcomes_provenance_and_parent_immutable(env):
    before = [s.case.model_dump() for s in env["snaps"]]
    built, audit = _built(env)
    assert [s.case.model_dump() for s in env["snaps"]] == before
    ids, plan = env["ids"], env["plan"]
    derived = {cid: d["outcome"] for cid, d in built["derived"].items()}
    assert derived[ids[2]] == "KEEP_CURRENT" and derived[ids[3]] == "CHANGE_GOLD"
    assert derived[ids[4]] == "MULTI_ACCEPTABLE" and derived[ids[5]] == "EXPECT_NONE"
    assert derived[ids[0]] == "RETRIEVAL_OR_KB_MAPPING_REVIEW"
    child = {c.case_id: c for c in built["cases"]}
    assert child[ids[5]].expected_decision == "NONE" and child[ids[5]].acceptable_candidate_refs == []
    assert (child[ids[6]].evaluation_status, child[ids[6]].status_reason) == \
        (EvaluationStatus.EXCLUDED, "ambiguous_user_intent")
    assert (child[ids[7]].evaluation_status, child[ids[7]].status_reason) == \
        (EvaluationStatus.HOLD, "content_review_required")
    assert (child[ids[0]].evaluation_status, child[ids[0]].status_reason) == \
        (EvaluationStatus.HOLD, "retrieval_or_kb_mapping_review")
    assert child[ids[0]].expected_decision == "SELECT"  # never turned into NONE
    assert len(child[ids[4]].acceptable_candidate_refs) == 2
    assert {p["case_id"] for p in built["provenance"]} == set(ids)
    prov = next(p for p in built["provenance"] if p["case_id"] == ids[3])
    for key in ("previous", "new", "human_blind_decision", "derived_outcome", "review_round",
                "source_review_lock_fingerprint", "parent_gold_fingerprint"):
        assert key in prov
    again, _ = _built(env)
    assert again["fingerprint"] == built["fingerprint"]
    acct = sg.accounting(sg.load_lock(env["review"] / "locked")[1], audit, built["derived"])
    assert acct["outcomes"]["CHANGE_GOLD"] >= 1 and acct["old_gold_rejected"] >= 2


def test_label_mapping_is_recomputed_and_cross_checked(env):
    sg.write_lock(env["review"] / "locked", _lock(env), "t")
    manifest, records, audit = sg.open_audit_after_lock(env["review"], env["review"] / "locked")
    by_id = {s.case.case_id: s for s in env["snaps"]}
    for a in audit:
        assert sg.blind_label_map(by_id[a["case_id"]]) == a["label_to_ref"]
    tampered = [dict(a, label_to_ref=dict(zip(a["label_to_ref"], reversed(list(
        a["label_to_ref"].values()))))) if i == 0 else a for i, a in enumerate(audit)]
    with pytest.raises(SystemExit):
        sg.build_semantic_gold(env["snaps"], records, tampered, lock_fp="l", parent_fp="p")


def _saved(cid, decision, ref):
    outcome = Outcome.FALSE_NONE if decision == "NONE" else Outcome.WRONG_SELECT
    return BenchmarkResult(result_key="k", case_id=cid, run_id="r", run_mode="LIVE",
                           expected_decision="SELECT", expected_refs=["qna:1"], decision=decision,
                           selected_candidate_ref=ref, outcome=outcome, correct=False,
                           selector_status="success", provider="openrouter", model="m",
                           config_fingerprint="c", selector_contract_fingerprint="x",
                           snapshot_fingerprint="s")


def test_rescore_saved_outputs_under_semantic_gold(env):
    built, _audit = _built(env)
    ids = env["ids"]
    sem = sg.semantic_snapshots(env["snaps"], built["cases"])
    child = {c.case_id: c for c in built["cases"]}
    results = {
        ids[5]: _saved(ids[5], "NONE", None),                                     # EXPECT_NONE
        ids[4]: _saved(ids[4], "SELECT", child[ids[4]].acceptable_candidate_refs[1]),  # MULTI
        ids[0]: _saved(ids[0], "NONE", None),                                     # KB review
        ids[6]: _saved(ids[6], "SELECT", "qna:1"),                                # excluded
    }
    rescored = sg.rescore_run(sem, results)
    assert rescored[ids[5]].outcome is Outcome.CORRECT_NONE
    assert rescored[ids[4]].outcome is Outcome.CORRECT_SELECT
    assert ids[0] not in rescored and ids[6] not in rescored  # not scored, never as NONE
    assert results[ids[5]].outcome is Outcome.FALSE_NONE      # saved output untouched
    dens = sg.denominators(sem)
    assert dens["evaluable_none"] == 1
    assert dens["not_evaluable_by_reason"]["retrieval_or_kb_mapping_review"] == 1
    assert dens["not_evaluable_by_reason"]["ambiguous_user_intent"] == 1
    assert dens["not_evaluable_by_reason"]["content_review_required"] == 1


def test_prompts_and_gate_unchanged():
    assert prompt_manifest() == load_committed_manifest()
    report = {"completed": 1, "cases": 1, "false_none": 0, "gs_case_correctness": {},
              "selector_value": {"net_corrections": 0, "corruption_count": 0}}
    assert list(px.selection_gate(report, report)["checks"]) == [
        "complete", "net_corrections >= 0", "corruption < production",
        "false_none < production", "no general/specific regression vs production"]


def test_semantic_flow_leaves_production_config_untouched(env, db):
    from core.database import AICapabilityConfig, AIConfigAudit, AIConfigVersion

    before = [db.query(t).count() for t in (AIConfigVersion, AIConfigAudit, AICapabilityConfig)]
    built, _ = _built(env)
    sg.rescore_run(sg.semantic_snapshots(env["snaps"], built["cases"]), {})
    assert [db.query(t).count() for t in (AIConfigVersion, AIConfigAudit, AICapabilityConfig)] == before


# ── real artifacts ──────────────────────────────────────────────────────────

@pytest.mark.skipif(not (OUT / "semantic-adjudication-v1" / "locked").exists(),
                    reason="private artifacts absent")
def test_real_lock_and_semantic_gold():
    manifest, records = sg.load_lock(OUT / "semantic-adjudication-v1" / "locked")
    assert manifest["source_decision_sha256"] == \
        "b88ef9a8e02f232d1a48f9b9e5c3ce8824955488d86507ec1ca93e2f3d7fdab8"
    assert len(records) == manifest["review_case_count"] == 106
    assert manifest["rounds"] == {"FIRST_PASS": 87, "FULL_CANDIDATE_FOLLOWUP": 19}
    assert manifest["decision_counts"] == {"SELECT_ACCEPTABLE": 82, "RETRIEVAL_OR_KB_MAPPING_REVIEW": 11,
                                           "EXCLUDE_AMBIGUOUS": 10, "CONTENT_REVIEW_REQUIRED": 2,
                                           "EXPECT_NONE": 1}
    assert manifest["parent_review_packet_fingerprint"] == \
        "2c229e4fb31d998511e54566dea2130bbdb489754070cbc02b13549eff2df269"
    assert manifest["followup_fingerprint"] == \
        "078bec9f335e117ab95882e36caf983f79cca5310d897bb527a388fe7d00c1a6"
    gold = json.loads((OUT / "semantic-gold-v1" / "manifest.json").read_text())
    for name, digest in gold["artifact_sha256"].items():
        assert sha256_text((OUT / "semantic-gold-v1" / name).read_text()) == digest
    assert gold["review_lock_fingerprint"] == manifest["lock_fingerprint"]
    assert sum(gold["accounting"]["outcomes"].values()) == 106
    changes = (OUT / "semantic-gold-v1" / "changes.jsonl").read_text().splitlines()
    assert len(changes) == 106
