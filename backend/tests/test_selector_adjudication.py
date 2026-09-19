"""Phase 7B blind semantic Gold adjudication prep. Offline: no provider call."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

from benchmarks.selector_v2 import adjudication as adj
from benchmarks.selector_v2 import prompt_experiment as px
from benchmarks.selector_v2.safety import no_live_calls
from benchmarks.selector_v2.schema import BenchmarkResult, EvaluationStatus, Outcome
from benchmarks.selector_v2.snapshot import load_near_pairs
from tests.test_selector_challenge import _snapshots

REPO = Path(__file__).resolve().parents[2]
PACKET = REPO / "outputs" / "selector-v2-benchmark" / "semantic-adjudication-v1"


@pytest.fixture(autouse=True)
def _no_network():
    db_hosts = [urlparse(os.environ[name]).hostname
                for name in ("DATABASE_URL", "ADMIN_DATABASE_URL", "CHAT_DATABASE_URL")
                if os.environ.get(name)]
    with no_live_calls(allow_hosts=db_hosts):
        yield


def _failure(cid, group, primary="NEEDS_HUMAN_REVIEW", secondary=()):
    # Deliberately carries model fields: they must never reach the primary view.
    return {"case_id": cid, "model_decision": "SELECT", "selected_candidate_ref": "qna:999",
            "model_correct": False, "value_class": "CORRUPTION", "outcome": "WRONG_SELECT",
            "classification": {"mismatch_group": group, "primary": primary,
                               "secondary": list(secondary)}}


def _world():
    snaps = _snapshots()
    failures = [_failure("1", "B", "GOLD_OR_ALIAS_QUESTIONABLE", ["intent_states_selected_distinction"]),
                _failure("2", "C", "KB_OVERLAP_INTRINSIC"), _failure("3", "U"),
                _failure("4", "A", "UNSTATED_QUALIFIER_SPECIFICITY")]
    ids = [s.case.case_id for s in snaps if s.selector_evaluable]
    scope = adj.review_scope(failures, ids, control_size=5, review_queue=("6",))
    packet = adj.build_packet(snaps, scope, load_near_pairs(), {"1": [11], "3": [30]})
    return snaps, scope, packet


# ── scope ───────────────────────────────────────────────────────────────────

def test_scope_is_deterministic_and_counts_each_source():
    snaps, scope, _p = _world()
    counts = scope["counts"]
    assert counts["GOLD_ALIAS_QUESTIONABLE"] == 1 and counts["CONTRACT_MISMATCH"] == 1
    assert counts["NEEDS_HUMAN_REVIEW"] == 1 and counts["KB_OVERLAP_INTRINSIC"] == 1
    assert counts["EXISTING_REVIEW_QUEUE"] == 1 and counts["BLIND_CONTROL"] == 5
    assert counts["unique_union"] == 4 + 5
    assert "4" not in scope["case_ids"] and scope["clear_selector_error_diagnostic"] == ["4"]
    assert not set(scope["control"]) & {"1", "2", "3", "4", "6"}
    again = adj.review_scope([_failure("3", "U"), _failure("2", "C", "KB_OVERLAP_INTRINSIC"),
                              _failure("1", "B", "GOLD_OR_ALIAS_QUESTIONABLE",
                                       ["intent_states_selected_distinction"]),
                              _failure("4", "A")],
                             list(reversed([s.case.case_id for s in snaps if s.selector_evaluable])),
                             control_size=5, review_queue=("6",))
    assert again["case_ids"] == scope["case_ids"] and again["control"] == scope["control"]


# ── packet: blindness, determinism, completeness ────────────────────────────

def test_primary_view_is_model_output_blind_and_hides_gold_rank_and_refs():
    _s, _scope, packet = _world()
    assert adj.primary_view_violations(packet["primary"]) == []
    blob = json.dumps(packet["primary"], ensure_ascii=False)
    for leaked in ("qna:999", "WRONG_SELECT", "CORRUPTION", "model_decision", "value_class",
                   "first_candidate", "gold_rank", "acceptable", "rescue", "score", "variant"):
        assert leaked not in blob
    for rec in packet["primary"]:
        assert set(rec) == {"case_id", "intent_text", "candidates", "candidate_view_complete",
                            "all_candidate_count", "all_candidates"}
        for cand in rec["candidates"] + rec["all_candidates"]:
            assert set(cand) == {"label", "question", "answer"} and cand["answer"]
    csv_text = adj.review_template_csv(packet["primary"])
    assert "qna:" not in csv_text and csv_text.splitlines()[0].split(",") == adj.CSV_COLUMNS
    assert "qna:" not in adj.review_markdown(packet["primary"])


def test_violation_detector_catches_contamination():
    dirty = [{"case_id": "1", "intent_text": "x", "model_decision": "NONE",
              "candidates": [{"label": "A", "question": "q", "answer": "qna:5"}]}]
    problems = adj.primary_view_violations(dirty, ("variant_a_v1",))
    assert any("model_decision" in p for p in problems) and any("qna:" in p for p in problems)


def test_gold_shown_order_rank_neutral_and_audit_holds_secondary_info():
    snaps, scope, packet = _world()
    by_id = {s.case.case_id: s for s in snaps}
    for rec, audit in zip(packet["primary"], packet["audit"]):
        shown = {c["label"] for c in rec["candidates"]}
        assert set(audit["current_gold_labels"]) <= shown
        assert rec["candidate_view_complete"] == (len(rec["candidates"]) == rec["all_candidate_count"])
        assert set(audit["label_to_ref"].values()) == {c.candidate_ref
                                                      for c in by_id[rec["case_id"]].candidates}
        assert audit["scope_reasons"] and "current_gold_refs" in audit
    # reversing retrieval order does not change labels or packet content
    reordered = [s.model_copy(update={"candidates": [
        c.model_copy(update={"order": len(s.candidates) - c.order + 1}) for c in s.candidates]})
        for s in snaps]
    packet2 = adj.build_packet(reordered, scope, load_near_pairs(), {"1": [11], "3": [30]})
    assert [r["all_candidates"] for r in packet2["primary"]] == \
        [r["all_candidates"] for r in packet["primary"]]
    assert packet["audit"][0]["alias_collision"] in (True, False)


def test_packet_fingerprint_deterministic_and_immutable(tmp_path):
    _s, scope, packet = _world()
    fp = adj.packet_fingerprint(scope, packet["content_hashes"], "s", "p")
    assert fp == adj.packet_fingerprint(scope, packet["content_hashes"], "s", "p")
    changed = dict(packet["content_hashes"], **{scope["case_ids"][0]: "0" * 64})
    assert adj.packet_fingerprint(scope, changed, "s", "p") != fp
    adj.write_immutable(tmp_path, {"a.txt": "one"})
    adj.write_immutable(tmp_path, {"a.txt": "one"})
    with pytest.raises(SystemExit):
        adj.write_immutable(tmp_path, {"a.txt": "two"})


# ── decisions, apply, re-score ──────────────────────────────────────────────

def _audit(gold=("qna:10",)):
    return {"case_id": "9", "label_to_ref": {"A": "qna:10", "B": "qna:11", "C": "qna:12"},
            "current_gold_refs": list(gold)}


@pytest.mark.parametrize(("row", "final"), [
    ({"blind_decision": "", "acceptable_labels": ""}, "UNREVIEWED"),
    ({"blind_decision": "SELECT_ACCEPTABLE", "acceptable_labels": "A"}, "KEEP_CURRENT"),
    ({"blind_decision": "SELECT_ACCEPTABLE", "acceptable_labels": "B"}, "CHANGE_GOLD"),
    ({"blind_decision": "SELECT_ACCEPTABLE", "acceptable_labels": "A B"}, "MULTI_ACCEPTABLE"),
    ({"blind_decision": "EXPECT_NONE", "acceptable_labels": ""}, "EXPECT_NONE"),
    ({"blind_decision": "EXCLUDE_AMBIGUOUS", "acceptable_labels": ""}, "EXCLUDE_AMBIGUOUS"),
    ({"blind_decision": "CONTENT_REVIEW_REQUIRED", "acceptable_labels": ""}, "CONTENT_REVIEW_REQUIRED"),
    ({"blind_decision": "RETRIEVAL_OR_KB_MAPPING_REVIEW", "acceptable_labels": ""},
     "RETRIEVAL_OR_KB_MAPPING_REVIEW"),
    ({"blind_decision": "NEED_FULL_CANDIDATES", "acceptable_labels": ""}, "NEED_FULL_CANDIDATES"),
])
def test_blind_decisions_derive_final_decisions(row, final):
    assert adj.derive_decision({"case_id": "9", **row}, _audit())["final"] == final


@pytest.mark.parametrize("row", [
    {"blind_decision": "SELECT_ACCEPTABLE", "acceptable_labels": ""},
    {"blind_decision": "SELECT_ACCEPTABLE", "acceptable_labels": "Z"},
    {"blind_decision": "EXPECT_NONE", "acceptable_labels": "A"},
    {"blind_decision": "MAYBE", "acceptable_labels": ""},
])
def test_invalid_decision_rows_are_rejected(row):
    with pytest.raises(ValueError):
        adj.derive_decision({"case_id": "9", **row}, _audit())


def test_apply_builds_child_with_provenance_and_keeps_parent():
    snaps, _scope, packet = _world()
    parent = [s.case for s in snaps]
    before = [c.model_dump() for c in parent]
    audit = {a["case_id"]: a for a in packet["audit"]}
    first, second = packet["primary"][0]["case_id"], packet["primary"][1]["case_id"]
    non_gold = next(label for label, ref in audit[first]["label_to_ref"].items()
                    if ref not in audit[first]["current_gold_refs"])
    rows = [{"case_id": first, "blind_decision": "SELECT_ACCEPTABLE", "acceptable_labels": non_gold,
             "review_note": "n", "reviewer": "r", "reviewed_at": "t"},
            {"case_id": second, "blind_decision": "EXPECT_NONE", "acceptable_labels": ""}]
    result = adj.apply_adjudication(parent, packet["audit"], rows, packet_fp="p",
                                    expected_packet_fp="p", parent_fp="parent")
    assert [c.model_dump() for c in parent] == before  # parent immutable
    child = {c.case_id: c for c in result["cases"]}
    assert child[first].acceptable_candidate_refs == [audit[first]["label_to_ref"][non_gold]]
    assert child[second].expected_decision == "NONE" and child[second].acceptable_candidate_refs == []
    assert {p["case_id"] for p in result["provenance"]} == {first, second}
    assert result["provenance"][0]["previous"] and result["provenance"][0]["source_packet_fingerprint"] == "p"
    assert result["decision_counts"]["UNREVIEWED"] == len(packet["audit"]) - 2
    unreviewed = adj.apply_adjudication(parent, packet["audit"], [], packet_fp="p",
                                        expected_packet_fp="p", parent_fp="parent")
    assert [c.model_dump() for c in unreviewed["cases"]] == before and not unreviewed["provenance"]
    with pytest.raises(SystemExit):
        adj.apply_adjudication(parent, packet["audit"], rows, packet_fp="p",
                               expected_packet_fp="other", parent_fp="parent")
    with pytest.raises(SystemExit):
        adj.apply_adjudication(parent, packet["audit"], [{"case_id": "nope"}], packet_fp="p",
                               expected_packet_fp="p", parent_fp="parent")


def _result(cid, decision, ref, outcome):
    return BenchmarkResult(result_key="k", case_id=cid, run_id="r", run_mode="LIVE",
                           expected_decision="SELECT", expected_refs=["qna:1"], decision=decision,
                           selected_candidate_ref=ref, outcome=outcome,
                           correct=outcome is Outcome.CORRECT_SELECT, selector_status="success",
                           provider="openrouter", model="m", config_fingerprint="c",
                           selector_contract_fingerprint="x", snapshot_fingerprint="s")


def test_rescore_saved_outputs_supports_expected_none_without_calls():
    snaps = [s for s in _snapshots() if s.selector_evaluable][:4]
    ids = [s.case.case_id for s in snaps]
    other = next(c.candidate_ref for c in snaps[1].candidates
                 if c.candidate_ref not in snaps[1].case.acceptable_candidate_refs)
    results = {
        ids[0]: _result(ids[0], "NONE", None, Outcome.FALSE_NONE),
        ids[1]: _result(ids[1], "SELECT", other, Outcome.WRONG_SELECT),
        ids[2]: _result(ids[2], "SELECT", snaps[2].case.acceptable_candidate_refs[0],
                        Outcome.CORRECT_SELECT),
        ids[3]: _result(ids[3], None, None, Outcome.INVALID_OUTPUT),
    }
    child = [
        snaps[0].case.model_copy(update={"expected_decision": "NONE", "acceptable_candidate_refs": []}),
        snaps[1].case.model_copy(update={"acceptable_candidate_refs": [other]}),
        snaps[2].case.model_copy(update={"expected_decision": "NONE", "acceptable_candidate_refs": []}),
        snaps[3].case.model_copy(update={"evaluation_status": EvaluationStatus.EXCLUDED,
                                         "status_reason": "x"}),
    ]
    _new_snaps, rescored = adj.rescore(snaps, child, results)
    assert rescored[ids[0]].outcome is Outcome.CORRECT_NONE and rescored[ids[0]].correct
    assert rescored[ids[1]].outcome is Outcome.CORRECT_SELECT
    assert rescored[ids[2]].outcome is Outcome.FALSE_SELECT
    assert ids[3] not in rescored  # excluded cases leave the denominator
    assert results[ids[0]].outcome is Outcome.FALSE_NONE  # saved outputs untouched


def test_prompt_gate_definition_unchanged():
    report = {"completed": 1, "cases": 1, "false_none": 0, "gs_case_correctness": {},
              "selector_value": {"net_corrections": 0, "corruption_count": 0}}
    assert list(px.selection_gate(report, {**report, "false_none": 1, "selector_value": {
        "net_corrections": 0, "corruption_count": 1}})["checks"]) == [
        "complete", "net_corrections >= 0", "corruption < production",
        "false_none < production", "no general/specific regression vs production"]


# ── real packet (private artifacts) ─────────────────────────────────────────

@pytest.mark.skipif(not (PACKET / "manifest.json").exists(), reason="private artifacts absent")
def test_real_packet_is_blind_locked_and_unreviewed():
    manifest, audit = adj.load_locked_review(PACKET)
    primary = [json.loads(line) for line in (PACKET / "review-cases.jsonl").read_text().splitlines()]
    assert adj.primary_view_violations(primary, ("variant_a_v1", "variant_b_v1")) == []
    assert [r["case_id"] for r in primary] == manifest["case_ids"] == [a["case_id"] for a in audit]
    rows = adj.read_decisions((PACKET / "review-template.csv").read_text())
    assert len(rows) == len(primary) and all(not r["blind_decision"] for r in rows)
    assert manifest["human_review_status"] == "WAITING_FOR_HUMAN_REVIEW"
    for rec, a in zip(primary, audit):
        if a["current_gold_in_candidate_set"]:
            assert set(a["current_gold_labels"]) <= {c["label"] for c in rec["candidates"]}


# ── full-candidate follow-up (second blind pass) ───────────────────────────

FOLLOWUP = PACKET / "full-candidate-followup"
EXPECTED_FOLLOWUP = ["19", "74", "112", "158", "160", "184", "215", "221", "230", "355", "371",
                     "375", "383", "384", "412", "415", "436", "461", "503"]


def _filled(packet, need):
    rows = []
    for rec in packet["primary"]:
        cid = rec["case_id"]
        if cid in need:
            rows.append({"case_id": cid, "intent_text": rec["intent_text"],
                         "blind_decision": "NEED_FULL_CANDIDATES", "acceptable_labels": ""})
        else:
            rows.append({"case_id": cid, "intent_text": rec["intent_text"],
                         "blind_decision": "SELECT_ACCEPTABLE",
                         "acceptable_labels": rec["candidates"][0]["label"]})
    return rows


def test_followup_lists_every_candidate_once_with_first_pass_labels():
    snaps, _scope, packet = _world()
    need = [packet["primary"][0]["case_id"], packet["primary"][2]["case_id"]]
    records = adj.build_followup(snaps, packet["primary"], need)
    by_id = {s.case.case_id: s for s in snaps}
    first = {r["case_id"]: r for r in packet["primary"]}
    assert [r["case_id"] for r in records] == need
    for rec in records:
        assert rec["candidate_view_complete"] is True
        assert rec["candidate_count"] == len(by_id[rec["case_id"]].candidates)
        labels = [c["label"] for c in rec["candidates"]]
        assert len(labels) == len(set(labels))
        questions = sorted(c["question"] for c in rec["candidates"])
        assert questions == sorted(c.canonical_text for c in by_id[rec["case_id"]].candidates)
        assert rec["candidates"] == first[rec["case_id"]]["all_candidates"]
    assert adj.build_followup(snaps, packet["primary"], need) == records  # deterministic


def test_followup_packet_is_blind_field_level():
    snaps, _scope, packet = _world()
    need = [packet["primary"][0]["case_id"]]
    records = adj.build_followup(snaps, packet["primary"], need)
    csv_text, md_text = adj.followup_template_csv(records), adj.followup_markdown(records)
    readme = adj.FOLLOWUP_README.format(schema=adj.FOLLOWUP_SCHEMA_VERSION)
    assert adj.followup_visible_violations(records, csv_text, md_text, readme) == []
    # content may naturally contain such words: not a false positive
    natural = [dict(records[0], candidates=[dict(records[0]["candidates"][0],
                                                 answer="İnternet provider ve alias ayarı")])]
    assert adj.followup_visible_violations(natural, adj.followup_template_csv(natural),
                                           adj.followup_markdown(natural), readme) == []
    # field names, refs and non-content text are caught
    assert adj.followup_visible_violations([dict(records[0], gold="x")], csv_text, md_text, readme)
    leaked = [dict(records[0], candidates=[dict(records[0]["candidates"][0], answer="bkz. qna:12")])]
    assert adj.followup_visible_violations(leaked, csv_text, md_text, readme)
    assert adj.followup_visible_violations(records, csv_text, md_text + "\ncurrent gold: A", readme)
    assert adj.followup_visible_violations(records, csv_text, md_text, readme + " retrieval rank")
    for word in ("model_decision", "variant", "first_candidate", "score", "rank"):
        assert word not in json.dumps(records, ensure_ascii=False)


def test_first_pass_validation_and_merge_preserve_completed_reviews():
    _snaps, _scope, packet = _world()
    need = {packet["primary"][0]["case_id"]}
    template = adj.read_decisions(adj.review_template_csv(packet["primary"]))
    filled = _filled(packet, need)
    assert adj.validate_first_pass(filled, template, packet["primary"]) == []
    bad = [dict(filled[0], intent_text="edited")] + filled[1:]
    assert adj.validate_first_pass(bad, template, packet["primary"])
    bad = [dict(filled[1], acceptable_labels="ZZ") if i == 1 else r for i, r in enumerate(filled)]
    assert adj.validate_first_pass(bad, template, packet["primary"])
    follow = [{"case_id": cid, "blind_decision": "EXPECT_NONE", "acceptable_labels": ""}
              for cid in need]
    merged = adj.merge_reviews(filled, follow)
    assert len(merged) == len(filled)
    for before, after in zip(filled, merged):
        if before["case_id"] in need:
            assert after["blind_decision"] == "EXPECT_NONE"
            assert after["review_pass"] == "full_candidate_followup"
        else:
            assert {k: after[k] for k in before} == before and after["review_pass"] == "first"
    with pytest.raises(SystemExit):
        adj.merge_reviews(filled, [dict(follow[0], blind_decision="NEED_FULL_CANDIDATES")])
    with pytest.raises(SystemExit):
        adj.merge_reviews(filled, [])
    assert "NEED_FULL_CANDIDATES" not in adj.FOLLOWUP_DECISIONS


def test_primary_loader_never_needs_the_audit_view(tmp_path):
    _snaps, _scope, packet = _world()
    files = {"review-cases.jsonl": "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n"
                                           for r in packet["primary"]),
             "review-template.csv": adj.review_template_csv(packet["primary"])}
    hashes = adj.write_immutable(tmp_path, files)
    (tmp_path / "manifest.json").write_text(json.dumps({"artifact_sha256": hashes}))
    assert not (tmp_path / "audit-view.jsonl").exists()
    _manifest, primary, template = adj.load_primary_review(tmp_path)
    assert len(primary) == len(template) == len(packet["primary"])
    (tmp_path / "review-template.csv").write_text("tampered")
    with pytest.raises(SystemExit):
        adj.load_primary_review(tmp_path)


@pytest.mark.skipif(not (FOLLOWUP / "manifest.json").exists(), reason="private artifacts absent")
def test_real_followup_packet():
    from benchmarks.selector_v2.contract import sha256_text

    manifest = json.loads((FOLLOWUP / "manifest.json").read_text())
    for name, digest in manifest["artifact_sha256"].items():
        assert sha256_text((FOLLOWUP / name).read_text(encoding="utf-8")) == digest
    assert manifest["case_ids"] == EXPECTED_FOLLOWUP and manifest["case_count"] == 19
    assert manifest["parent_review_packet_fingerprint"] == \
        "2c229e4fb31d998511e54566dea2130bbdb489754070cbc02b13549eff2df269"
    assert manifest["audit_view_read"] is False
    records = [json.loads(line) for line in (FOLLOWUP / "review-cases-full.jsonl").read_text().splitlines()]
    assert [r["case_id"] for r in records] == EXPECTED_FOLLOWUP
    parent = {json.loads(line)["case_id"]: json.loads(line)
              for line in (PACKET / "review-cases.jsonl").read_text().splitlines()}
    for rec in records:
        assert rec["candidate_view_complete"] is True
        assert rec["candidates"] == parent[rec["case_id"]]["all_candidates"]
        assert rec["candidate_count"] == parent[rec["case_id"]]["all_candidate_count"]
        assert len({c["label"] for c in rec["candidates"]}) == rec["candidate_count"]
    readme = (FOLLOWUP / "README.md").read_text()
    assert adj.followup_visible_violations(
        records, (FOLLOWUP / "review-template-full.csv").read_text(),
        (FOLLOWUP / "review-packet-full.md").read_text(), readme) == []
    first = adj.read_decisions((FOLLOWUP / "first-pass-decisions.csv").read_text().lstrip("﻿"))
    completed = [r for r in first if r["blind_decision"].strip() not in ("", "NEED_FULL_CANDIDATES")]
    assert len(completed) == 87 == manifest["first_pass"]["completed_count"]
    assert [r["case_id"] for r in completed] == manifest["first_pass"]["completed_case_ids"]
    assert all(not r["blind_decision"] for r in adj.read_decisions(
        (FOLLOWUP / "review-template-full.csv").read_text()))
