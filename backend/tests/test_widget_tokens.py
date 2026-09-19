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


def test_context_enabled_passes_only_owned_previous_messages(client, monkeypatch):
    import routers.chat as chat

    captured = []

    def answer(question, db, conversation_context=(), trace=None):
        del trace
        captured.append((question, conversation_context))
        return f"cevap-{len(captured)}", "llm"

    monkeypatch.setattr(chat, "CHAT_CONTEXT_ENABLED", True)
    monkeypatch.setattr(chat, "_answer_question", answer)

    first = _chat(client, "İlk soru")
    second = _chat(
        client,
        "Fakat şimdi farklı",
        first["conversation_id"],
        first["conversation_token"],
    )

    assert captured[0] == ("İlk soru", ())
    assert captured[1][0] == "Fakat şimdi farklı"
    assert captured[1][1] == (
        {"role": "user", "content": "İlk soru"},
    )
    assert second["conversation_id"] == first["conversation_id"]

    _chat(client, "Başka kullanıcı", first["conversation_id"], "yanlis-token")
    assert captured[2] == ("Başka kullanıcı", ())


def test_context_applies_message_and_total_character_caps(db, monkeypatch):
    from core.database import Conversation, ConversationMessage
    from routers import chat

    monkeypatch.setattr(chat, "CHAT_CONTEXT_MAX_MESSAGES", 2)
    monkeypatch.setattr(chat, "CHAT_CONTEXT_MAX_CHARS", 10)
    conv = Conversation(client_token="token")
    db.add(conv)
    db.flush()
    db.add_all([
        ConversationMessage(conversation_id=conv.id, role="user", content="abcdefgh"),
        ConversationMessage(conversation_id=conv.id, role="bot", content="12345678"),
        ConversationMessage(conversation_id=conv.id, role="user", content="ABCDEFGH"),
    ])
    db.commit()

    assert chat._load_recent_context(db, conv.id) == (
        {"role": "user", "content": "abcde"},
        {"role": "user", "content": "ABCDE"},
    )


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


def test_widget_falls_back_to_suggestions_when_no_answer(client, db):
    from core.database import QnA

    providers.FakeMeili.hits = []          # eşik altında hiçbir sonuç yok
    rows = [QnA(question_text=q, answer_text="a", status=1)
            for q in ("kayıt nasıl yapılır", "harç ne kadar")]
    db.add_all(rows)
    db.commit()
    # Öneriler guard/aktiflik kontrolü için QnA kimliğiyle gelir.
    providers.FakeMeili.suggestions = [
        {"qna_id": row.id, "question": row.question_text} for row in rows
    ]
    d = _chat(client, "hiçbir şeyle eşleşmeyen soru")
    assert "suggestions" in d and len(d["suggestions"]) == 2
    assert d["conversation_token"]         # önerili cevapta da token verilir


def test_message_length_cap(client):
    d = _chat(client, "x" * 2000)
    assert "çok uzun" in d["answer"]


def test_widget_request_uses_one_correlated_pii_safe_trace(client, monkeypatch):
    import routers.chat as chat

    captured = {}

    def answer(question, db, conversation_context=(), trace=None):
        del question, db, conversation_context
        captured["pipeline_trace"] = trace
        return "cevap", "meilisearch"

    def emit(trace):
        captured["emitted"] = trace.to_dict()

    monkeypatch.setattr(chat, "_answer_question", answer)
    monkeypatch.setattr(chat, "emit_decision_trace", emit)
    response = _chat(client, "kimlikNo 12345678901")
    snapshot = captured["emitted"]
    assert response["answer"] == "cevap"
    assert captured["pipeline_trace"].request_id == snapshot["request"]["request_id"]
    assert snapshot["request"]["endpoint"] == "widget_chat"
    assert snapshot["request"]["conversation_id"] == response["conversation_id"]
    assert "12345678901" not in str(snapshot)
