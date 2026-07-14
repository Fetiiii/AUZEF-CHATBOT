"""CSV import: batch provider sync (satır satır değil) + doğru satır işleme."""
import io

import providers


def _csv(rows_text: str):
    return {"file": ("q.csv", io.BytesIO(rows_text.encode("utf-8")), "text/csv")}


def test_qna_import_uses_single_batch_sync(make_user, login):
    make_user("editor@iu.tr", role="editor")
    c = login("editor@iu.tr")

    csv_text = (
        "question;answer;tags;query_1\n"
        "Soru 1;Cevap 1;tag1,tag2;alternatif 1\n"
        "Soru 2;Cevap 2;;\n"
        "Soru 3;Cevap 3;tag1;\n"
    )
    r = c.post("/api/qna/import", files=_csv(csv_text))
    assert r.status_code == 200 and r.json()["imported"] == 3

    # KRİTİK: 3 kayıt için TEK batch upsert + TEK Meili add — satır satır DEĞİL.
    assert providers.FakeQdrant.batch_calls == [3]
    assert providers.FakeQdrant.single_calls == 0
    assert providers.FakeMeili.add_calls == [3]


def test_import_skips_incomplete_rows(make_user, login):
    make_user("editor@iu.tr", role="editor")
    c = login("editor@iu.tr")
    # 2. satır kısa (query_1 sütunu eksik) → None güvenli olmalı, 3. satır boş cevap
    csv_text = (
        "question;answer;query_1\n"
        "Tam soru;Tam cevap\n"          # kısa satır: query_1 yok → None
        "Cevapsız;;\n"                   # boş cevap → atlanır
        "İkinci;İkinci cevap;ek\n"
    )
    r = c.post("/api/qna/import", files=_csv(csv_text))
    assert r.status_code == 200 and r.json()["imported"] == 2  # boş-cevaplı atlandı


def test_import_requires_editor_role(client):
    # oturumsuz → 401 (middleware)
    assert client.post("/api/qna/import", files=_csv("question;answer\na;b\n")).status_code == 401


def test_import_rejects_oversize_file(make_user, login):
    make_user("editor@iu.tr", role="editor")
    c = login("editor@iu.tr")
    big = "question;answer\n" + ("x;y\n" * 2_000_000)  # > 5 MB
    r = c.post("/api/qna/import", files=_csv(big))
    assert r.status_code == 413
