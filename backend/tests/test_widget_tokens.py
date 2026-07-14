"""S5 — Konuşma sahiplik token'ları: widget akışı, hijack ve puan sahteciliği."""
import services.providers as providers


def _seed_meili_hit():
    providers.FakeMeili.hits = [{
        "id": 1, "question": "kayıt nasıl yapılır",
        "answer": "Kayıt için OBS'yi kullanın.", "score": 0.95, "source": "meilisearch",
    }]


def _chat(client, message, conv_id=None, token=None):
    return client.post("/widget-chat", json={
        "message": message, "conversation_id": conv_id, "conversation_token": token,
    }).json()


def test_first_message_issues_token_and_continuation_works(client):
    _seed_meili_hit()
    d1 = _chat(client, "kayıt nasıl yapılır")
    assert d1["answer"] == "Kayıt için OBS'yi kullanın."
    assert d1["conversation_id"] and d1["conversation_token"]

    # Doğru token'la devam → aynı konuşma
    d2 = _chat(client, "tekrar soruyorum", d1["conversation_id"], d1["conversation_token"])
    assert d2["conversation_id"] == d1["conversation_id"]


def test_wrong_or_missing_token_starts_new_conversation(client):
    """Hijack denemesi öğrenciyi ASLA kırmaz: sessizce yeni konuşma açılır."""
    _seed_meili_hit()
    d1 = _chat(client, "ilk mesaj")

    d_wrong = _chat(client, "saldırgan mesajı", d1["conversation_id"], "yanlis-token")
    assert d_wrong["conversation_id"] != d1["conversation_id"]

    d_none = _chat(client, "token'sız deneme", d1["conversation_id"], None)
    assert d_none["conversation_id"] != d1["conversation_id"]


def test_rating_requires_matching_token(client):
    _seed_meili_hit()
    d = _chat(client, "soru")
    mid, tok = d["message_id"], d["conversation_token"]

    # Token yok / yanlış → 403 (eskiden herkes herkesin cevabını puanlayabiliyordu)
    assert client.post(f"/api/messages/{mid}/rating", json={"rating": 5}).status_code == 403
    assert client.post(f"/api/messages/{mid}/rating",
                       json={"rating": 5, "conversation_token": "sahte"}).status_code == 403
    # Doğru token → 200
    assert client.post(f"/api/messages/{mid}/rating",
                       json={"rating": 5, "conversation_token": tok}).status_code == 200
    # Aralık dışı puan → 400
    assert client.post(f"/api/messages/{mid}/rating",
                       json={"rating": 9, "conversation_token": tok}).status_code == 400


def test_talep_requires_matching_token(client):
    _seed_meili_hit()
    d = _chat(client, "soru")
    cid, tok = d["conversation_id"], d["conversation_token"]

    assert client.post(f"/api/conversations/{cid}/talep",
                       json={"status": "declined"}).status_code == 403
    assert client.post(f"/api/conversations/{cid}/talep",
                       json={"status": "declined", "conversation_token": "sahte"}).status_code == 403
    assert client.post(f"/api/conversations/{cid}/talep",
                       json={"status": "redirected", "conversation_token": tok}).status_code == 200
    assert client.post(f"/api/conversations/{cid}/talep",
                       json={"status": "gecersiz", "conversation_token": tok}).status_code == 400


def test_legacy_conversations_without_token_are_fail_closed(client, db):
    """Token kolonundan önceki (NULL) kayıtlar: okunur ama yazılamaz."""
    from core.database import Conversation, ConversationMessage
    conv = Conversation(ip_address="10.0.0.1", client_token=None)
    db.add(conv); db.flush()
    msg = ConversationMessage(conversation_id=conv.id, role="bot", content="eski cevap")
    db.add(msg); db.commit()

    assert client.post(f"/api/conversations/{conv.id}/talep",
                       json={"status": "declined", "conversation_token": "herhangi"}).status_code == 403
    assert client.post(f"/api/messages/{msg.id}/rating",
                       json={"rating": 5, "conversation_token": "herhangi"}).status_code == 403


def test_widget_falls_back_to_suggestions_when_no_answer(client):
    providers.FakeMeili.hits = []          # eşik altında hiçbir sonuç yok
    providers.FakeMeili.suggestions = ["kayıt nasıl yapılır", "harç ne kadar"]
    d = _chat(client, "hiçbir şeyle eşleşmeyen soru")
    assert "suggestions" in d and len(d["suggestions"]) == 2
    assert d["conversation_token"]         # önerili cevapta da token verilir


def test_message_length_cap(client):
    d = _chat(client, "x" * 2000)
    assert "çok uzun" in d["answer"]
