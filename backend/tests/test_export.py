"""Streaming CSV export: içerik doğruluğu + BOM + formül koruması + round-trip sütunları."""
import csv
import io


def _parse(text_body):
    # BOM'u ayıkla, CSV'yi çözümle (ilk hücrede BOM olmamalı)
    assert text_body.startswith("﻿"), "BOM (\\ufeff) ilk baytta olmalı (Excel Türkçe)"
    body = text_body.lstrip("﻿")
    return list(csv.reader(io.StringIO(body), delimiter=";"))


def test_qna_export_roundtrip_columns(make_user, login):
    make_user("editor@iu.tr", role="editor")
    c = login("editor@iu.tr")
    c.post("/api/qna", json={"question_text": "Soru A", "answer_text": "Cevap A"})
    c.post("/api/qna", json={"question_text": "Soru B", "answer_text": "Cevap B"})

    r = c.get("/api/qna/export")
    assert r.status_code == 200
    rows = _parse(r.text)
    # başlık: question;answer;tags;query_1..query_20 = 23 sütun
    assert rows[0] == ["question", "answer", "tags"] + [f"query_{i}" for i in range(1, 21)]
    assert len(rows[0]) == 23
    data = {row[0]: row[1] for row in rows[1:]}
    assert data["Soru A"] == "Cevap A" and data["Soru B"] == "Cevap B"


def test_calendar_export_comma_delimited(make_user, login, db):
    from database import AcademicCalendar
    db.add(AcademicCalendar(period="Güz", event="Vize", start_date="01.11.2025", end_date="05.11.2025"))
    db.commit()
    make_user("editor@iu.tr", role="editor")
    c = login("editor@iu.tr")

    r = c.get("/api/academic-calendar/export")
    assert r.status_code == 200
    assert r.text.startswith("﻿")
    rows = list(csv.reader(io.StringIO(r.text.lstrip("﻿")), delimiter=","))
    assert rows[0] == ["Donem", "Etkinlik", "Baslangic_Tarihi", "Bitis_Tarihi"]
    assert ["Güz", "Vize", "01.11.2025", "05.11.2025"] in rows[1:]


def test_conversations_export_streams_and_neutralizes_formula(make_user, login, db):
    from database import Conversation, ConversationMessage
    conv = Conversation(ip_address="10.0.0.5", talep_status="declined", client_token="t1")
    db.add(conv); db.flush()
    db.add(ConversationMessage(conversation_id=conv.id, role="user", content="normal soru"))
    # Formül enjeksiyonu denemesi: export'ta ' ile etkisizleşmeli
    db.add(ConversationMessage(conversation_id=conv.id, role="bot", content="=HYPERLINK(1)"))
    db.commit()

    make_user("admin@iu.tr", role="admin")
    c = login("admin@iu.tr")
    r = c.post("/api/conversations/export", json={"ids": [conv.id]})
    assert r.status_code == 200
    rows = _parse(r.text)
    assert rows[0][0] == "konusma_id"
    contents = [row[6] for row in rows[1:]]         # 'icerik' sütunu
    assert "normal soru" in contents
    assert "'=HYPERLINK(1)" in contents             # formül önekle etkisizleşti


def test_conversations_export_requires_admin(make_user, login):
    make_user("editor@iu.tr", role="editor")
    c = login("editor@iu.tr")
    # editor konuşma export'una erişemez (admin gerekir) → 403
    assert c.post("/api/conversations/export", json={"ids": [1]}).status_code == 403


def test_qna_export_chunking_handles_many_rows(make_user, login, db):
    """500'lük öbekleme sınırının ötesinde de tüm satırlar gelmeli."""
    from database import QnA
    db.bulk_save_objects([QnA(question_text=f"S{i}", answer_text=f"C{i}", status=1) for i in range(1100)])
    db.commit()
    make_user("editor@iu.tr", role="editor")
    c = login("editor@iu.tr")
    r = c.get("/api/qna/export")
    rows = _parse(r.text)
    assert len(rows) - 1 == 1100    # başlık hariç tüm kayıtlar (3 öbek: 500+500+100)
