"""Rota sırası koruması: aynı kaynak içindeki literal yollar ({id} param
rotasından ÖNCE çözülmeli). Router split bu sırayı bozarsa burası kırmızıya döner.
Suite'in aksi halde yalnızca temel yolları (taban path) test ettiği uçlar."""


def test_conversations_literal_routes_not_shadowed_by_id(make_user, login):
    make_user("admin@iu.tr", role="admin")
    c = login("admin@iu.tr")
    # /export ve /ids literal; /{conversation_id} int bekler. Literal'ler önce
    # çözülmezse "export"/"ids" int'e parse edilmeye çalışılır → 422.
    assert c.get("/api/conversations/export?ids=1").status_code == 200
    assert c.get("/api/conversations/ids").status_code == 200
    # Gerçek param rota: olmayan id → 404 (route var, kayıt yok)
    assert c.get("/api/conversations/999999").status_code == 404


def test_stats_conversations_distinct_from_stats(make_user, login):
    make_user("admin@iu.tr", role="admin")
    c = login("admin@iu.tr")
    assert c.get("/api/stats").status_code == 200
    assert c.get("/api/stats/conversations").status_code == 200


def test_search_is_public_and_routed():
    from fastapi.testclient import TestClient
    import main
    pub = TestClient(main.app)
    # /api/search public (rol kuralı eşleşmez) ve doğru handler'a gider
    r = pub.get("/api/search", params={"q": "merhaba"})
    assert r.status_code == 200
    assert "status" in r.json()


def test_qna_and_calendar_bulk_update_not_shadowed(make_user, login):
    make_user("editor@iu.tr", role="editor")
    c = login("editor@iu.tr")
    # /bulk-update literal; /{id} PUT int bekler. Boş liste ile 200 (bulk handler).
    assert c.put("/api/qna/bulk-update", json=[]).status_code == 200
    assert c.put("/api/academic-calendar/bulk-update", json=[]).status_code == 200
