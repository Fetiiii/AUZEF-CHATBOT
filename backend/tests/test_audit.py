"""Denetim izi: QnA/takvim düzenlemelerinde son düzenleyen (updated_by) kaydı."""
import io


def test_qna_records_creator_and_last_editor(make_user, login):
    make_user("ayse@iu.tr", role="editor")
    make_user("mehmet@iu.tr", role="admin")
    ayse = login("ayse@iu.tr")
    mehmet = login("mehmet@iu.tr")

    r = ayse.post("/api/qna", json={"question_text": "S", "answer_text": "C"})
    assert r.status_code == 201
    qid = r.json()["id"]
    assert r.json()["updated_by"] == "ayse@iu.tr"

    # Başkası düzenlerse son düzenleyen değişir
    r = mehmet.put(f"/api/qna/{qid}", json={"answer_text": "C2"})
    assert r.json()["updated_by"] == "mehmet@iu.tr"

    # Listede de görünür
    rows = ayse.get("/api/qna").json()
    assert next(x for x in rows if x["id"] == qid)["updated_by"] == "mehmet@iu.tr"


def test_qna_bulk_update_records_editor(make_user, login):
    make_user("ayse@iu.tr", role="editor")
    c = login("ayse@iu.tr")
    qid = c.post("/api/qna", json={"question_text": "S", "answer_text": "C"}).json()["id"]

    make_user("veli@iu.tr", role="editor")
    veli = login("veli@iu.tr")
    veli.put("/api/qna/bulk-update", json=[{"id": qid, "answer_text": "yeni"}])
    rows = c.get("/api/qna").json()
    assert next(x for x in rows if x["id"] == qid)["updated_by"] == "veli@iu.tr"


def test_calendar_records_editor(make_user, login):
    make_user("ayse@iu.tr", role="editor")
    c = login("ayse@iu.tr")
    r = c.post("/api/academic-calendar", json={
        "period": "Güz", "event": "Vize", "start_date": "01.11.2025", "end_date": "05.11.2025"})
    assert r.status_code == 201 and r.json()["updated_by"] == "ayse@iu.tr"


def test_import_records_editor(make_user, login):
    make_user("ayse@iu.tr", role="editor")
    c = login("ayse@iu.tr")
    files = {"file": ("q.csv", io.BytesIO("question;answer\nS1;C1\n".encode("utf-8")), "text/csv")}
    assert c.post("/api/qna/import", files=files).json()["imported"] == 1
    rows = c.get("/api/qna").json()
    assert rows[0]["updated_by"] == "ayse@iu.tr"
