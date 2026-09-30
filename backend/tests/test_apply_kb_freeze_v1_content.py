"""KB Freeze v1 content apply: hash-guarded decisions, atomic apply, idempotency."""
from __future__ import annotations

import json

import pytest

from scripts import apply_kb_freeze_v1_content as kf
from scripts.apply_reviewed_qna import raw_hash

TAG = "YENI_KAYIT-YATAY_GECIS-BILGI"


def _seed(db):
    from core.database import QnA, QnAQuery, Tag

    db.add(Tag(name=TAG))
    ids = {}
    for key, question, answer, queries in (
        ("general", "Staj hakkında bilgi alabilir miyim?", "Staj sayfasına bakınız.", ["staj bilgisi"]),
        ("program", "Bankacılık stajı hakkında bilgi alabilir miyim?", "Staj sayfasına bakınız.", ["bankacılık stajı"]),
        ("fee", "Ödeyeceğim harç miktarına nereden ulaşabilirim?", "Materyal ücretleri linki.", ["harç ne kadar"]),
        ("special", "Merkezi yatay geçiş başvurusu nasıl yapılır?", "Duyurular.", ["Yatay geçiş", "Ek madde 1"]),
        ("other", "Sınav sonucuna nasıl itiraz edebilirim?", "2 iş günü.", ["itiraz"]),
    ):
        row = QnA(question_text=question, answer_text=answer, status=1)
        db.add(row)
        db.flush()
        for text in queries:
            db.add(QnAQuery(qna_id=row.id, query_text=text))
        ids[key] = row.id
    db.commit()
    return ids


def _old(db, db_id):
    from core.database import QnA

    row = db.get(QnA, db_id)
    return {"expected_old_question_sha256": raw_hash(row.question_text),
            "expected_old_answer_sha256": raw_hash(row.answer_text)}


def _package(db, ids, source_sha="0" * 64):
    return {
        "source_document": {"file_name": "form.docx", "sha256": source_sha},
        "updates": [
            {"db_id": ids["general"], **_old(db, ids["general"]), "new_question": None,
             "new_answer": "Staj süresi programa göre değişir.",
             "add_queries": ["Staj kaç gün sürüyor?", "Bankacılık stajı hakkında bilgi alabilir miyim?",
                             "bankacılık stajı"], "remove_queries": []},
            {"db_id": ids["fee"], **_old(db, ids["fee"]),
             "new_question": "Harç ve materyal ücreti tutarlarına nereden ulaşabilirim?",
             "new_answer": "Aday Öğrenci > Ücretler.", "add_queries": ["Materyal ücreti ne kadar?"],
             "remove_queries": []},
            {"db_id": ids["special"], **_old(db, ids["special"]), "new_question": None, "new_answer": None,
             "add_queries": [], "remove_queries": [{"text": "Yatay geçiş", "sha256": raw_hash("Yatay geçiş")}]},
        ],
        "new_qna": [{"key": "cift", "question": "AUZEF'te çift anadal programı var mı?",
                     "answer": "AUZEF programlarında çift anadal uygulaması bulunmamaktadır.",
                     "tags": [TAG], "queries": ["Çift anadal yapabilir miyim?", "ÇAP var mı?"]}],
        "deactivate_qna": [{"db_id": ids["program"], **_old(db, ids["program"]), "merge_into": ids["general"]}],
        "query_changes": [],
        "data_quality": [{"db_id": ids["other"], **_old(db, ids["other"]),
                          "new_answer": "2 iş günü. (tekrar temizlendi)"}],
        "conscious_backlog": [{"id": "P2-C1", "topic": "Kampüste otopark var mı?", "status": "CONSCIOUS_PILOT_NONE"}],
    }


def _plan(db, package):
    from scripts.apply_reviewed_qna import db_snapshot

    return kf.build_plan(package, db_snapshot(db), kf.existing_tags(db))


@pytest.fixture()
def seeded(db):
    return _seed(db)


def test_plan_covers_update_insert_soft_delete_queries_and_cleanup(db, seeded):
    plan = _plan(db, _package(db, seeded))
    s = plan["summary"]
    assert plan["blockers"] == []
    assert (s["update_qna"], s["update_question"], s["data_quality_cleanup"]) == (2, 1, 1)
    assert (s["insert_qna"], s["insert_tag_links"], s["soft_delete_qna"]) == (1, 1, 1)
    assert (s["query_add"], s["query_remove"]) == (6, 1)       # "bankacılık stajı" moves with the merge
    assert len(plan["fingerprint"]) == 64


def test_apply_writes_everything_and_second_run_is_a_no_op(db, seeded):
    from core.database import QnA, QnAQuery

    package = _package(db, seeded)
    before = {q.id for q in db.query(QnA).filter_by(status=1)}
    result = kf.apply_plan(db, _plan(db, package))
    db.commit()
    db.expire_all()
    assert db.get(QnA, seeded["program"]).status == 0                       # merged: soft delete only
    assert db.get(QnA, seeded["fee"]).question_text.startswith("Harç ve materyal")
    assert db.get(QnA, seeded["general"]).updated_by == kf.ACTOR
    new = db.get(QnA, result["new_ids"]["cift"])
    assert [t.name for t in new.tags] == [TAG]
    assert sorted(q.query_text for q in new.queries) == sorted(["Çift anadal yapabilir miyim?", "ÇAP var mı?"])
    special = [q.query_text for q in db.query(QnAQuery).filter_by(qna_id=seeded["special"])]
    assert special == ["Ek madde 1"]
    check = kf.verify(db, package, before)
    assert set(check["mismatch_counts"].values()) == {0}

    again = _plan(db, package)
    assert again["blockers"] == [] and again["summary"]["total_mutations"] == 0


def test_unexpected_old_state_blocks_the_plan(db, seeded):
    package = _package(db, seeded)
    package["updates"][0]["expected_old_answer_sha256"] = "f" * 64
    plan = _plan(db, package)
    assert any("expected old nor the target" in b["reason"] for b in plan["blockers"])


def test_concurrent_edit_fails_before_writing(db, seeded):
    from core.database import QnA

    plan = _plan(db, _package(db, seeded))
    db.get(QnA, seeded["fee"]).answer_text = "Başka biri düzenledi."
    db.commit()
    with pytest.raises(kf.FreezeApplyError, match="expected old state"):
        kf.apply_plan(db, plan)
    db.rollback()
    db.expire_all()
    assert db.get(QnA, seeded["program"]).status == 1
    assert db.query(QnA).filter(QnA.question_text.like("%çift anadal%")).count() == 0


def test_duplicate_canonical_is_blocked(db, seeded):
    package = _package(db, seeded)
    package["new_qna"][0]["question"] = "sınav sonucuna nasıl  itiraz edebilirim?"   # normalized duplicate
    assert any("duplicate canonical" in b["reason"] for b in _plan(db, package)["blockers"])


def test_alias_colliding_with_another_active_qna_is_blocked(db, seeded):
    package = _package(db, seeded)
    package["new_qna"][0]["queries"].append("itiraz")                        # alias of another QnA
    assert any("collides" in b["reason"] for b in _plan(db, package)["blockers"])


def test_unknown_tag_is_blocked_no_tag_creation(db, seeded):
    package = _package(db, seeded)
    package["new_qna"][0]["tags"] = ["YENI-ETIKET"]
    assert any("unknown tags" in b["reason"] for b in _plan(db, package)["blockers"])


def test_error_mid_apply_rolls_back_everything(db, seeded):
    from core.database import QnA

    plan = _plan(db, _package(db, seeded))
    broken = {**plan, "summary": {**plan["summary"], "query_remove": 99}}
    with pytest.raises(kf.FreezeApplyError, match="row counts differ"):
        kf.apply_plan(db, broken)
    db.rollback()
    db.expire_all()
    assert db.get(QnA, seeded["program"]).status == 1
    assert db.get(QnA, seeded["general"]).answer_text == "Staj sayfasına bakınız."
    assert db.query(QnA).count() == 5


def test_conscious_backlog_produces_no_mutation(db, seeded):
    package = _package(db, seeded)
    for key in ("updates", "new_qna", "deactivate_qna", "data_quality"):
        package[key] = []
    plan = _plan(db, package)
    assert plan["blockers"] == [] and plan["summary"]["total_mutations"] == 0


def test_unmirrored_query_move_is_blocked(db, seeded):
    package = _package(db, seeded)
    package["query_changes"] = [{"from_db_id": seeded["special"], "to_db_id": seeded["general"],
                                 "text": "Yatay geçiş"}]                   # added nowhere
    assert any("not mirrored" in b["reason"] for b in _plan(db, package)["blockers"])


def test_source_document_hash_mismatch_is_refused(db, seeded, tmp_path):
    form = tmp_path / "form.docx"
    form.write_bytes(b"another form")
    decisions = tmp_path / "decisions.json"
    decisions.write_text(json.dumps(_package(db, seeded)), encoding="utf-8")
    with pytest.raises(SystemExit, match="different source document"):
        kf.main(["--decisions", str(decisions), "--source-document", str(form), "--dry-run"])


def test_apply_requires_local_confirmation_and_matching_fingerprint(db, seeded, tmp_path):
    form = tmp_path / "form.docx"
    form.write_bytes(b"the approved form")
    decisions = tmp_path / "decisions.json"
    decisions.write_text(json.dumps(_package(db, seeded, source_sha=kf.file_sha256(str(form)))),
                         encoding="utf-8")
    base = ["--decisions", str(decisions), "--source-document", str(form), "--apply"]
    with pytest.raises(SystemExit, match="confirm-local-dev"):
        kf.main(base + ["--plan-fingerprint", "x"])
    with pytest.raises(SystemExit, match="plan-fingerprint does not match"):
        kf.main(base + ["--confirm-local-dev", "--plan-fingerprint", "x"])


def test_non_local_database_is_refused(db, seeded, tmp_path, monkeypatch):
    form = tmp_path / "form.docx"
    form.write_bytes(b"the approved form")
    decisions = tmp_path / "decisions.json"
    decisions.write_text(json.dumps(_package(db, seeded, source_sha=kf.file_sha256(str(form)))),
                         encoding="utf-8")
    monkeypatch.setenv("ADMIN_DATABASE_URL", "postgresql://u:p@db-admin.prod.example:5432/auzef")
    with pytest.raises(SystemExit, match="not local/dev"):
        kf.main(["--decisions", str(decisions), "--source-document", str(form), "--apply",
                 "--confirm-local-dev", "--plan-fingerprint", "x"])
