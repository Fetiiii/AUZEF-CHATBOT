"""Internal-pilot chat input policy: widget and backend share one 500-char limit."""
import re

import services.providers as providers
from core.deps import MAX_MESSAGE_LEN
from core.limits import CHAT_MESSAGE_MAX_CHARS
from services.internal_pilot_freeze import repo_root


def _seed_meili_hit():
    from core.database import QnA, SessionLocal

    with SessionLocal() as db:
        db.add(QnA(id=1, question_text="kayıt nasıl yapılır",
                   answer_text="Kayıt için OBS'yi kullanın.", status=1))
        db.commit()
    providers.FakeMeili.hits = [{
        "id": 1, "qna_id": 1, "question": "kayıt nasıl yapılır",
        "answer": "Kayıt için OBS'yi kullanın.", "score": 0.95, "source": "meilisearch",
    }]


def _post(client, message):
    return client.post("/widget-chat", json={"message": message})


def test_pilot_limit_is_500():
    assert CHAT_MESSAGE_MAX_CHARS == 500


def test_message_at_the_limit_is_accepted(client):
    _seed_meili_hit()
    message = "kayıt nasıl yapılır " + "a" * (CHAT_MESSAGE_MAX_CHARS - 20)
    assert len(message) == CHAT_MESSAGE_MAX_CHARS
    response = _post(client, message)
    assert response.status_code == 200
    assert response.json()["answer"]


def test_message_over_the_limit_is_rejected_with_422(client):
    response = _post(client, "a" * (CHAT_MESSAGE_MAX_CHARS + 1))
    assert response.status_code == 422


def test_limit_counts_characters_not_bytes(client):
    """Turkish letters are 2 bytes in UTF-8 but one character each."""
    _seed_meili_hit()
    assert _post(client, "ğ" * CHAT_MESSAGE_MAX_CHARS).status_code == 200
    assert _post(client, "ğ" * (CHAT_MESSAGE_MAX_CHARS + 1)).status_code == 422


def test_normal_message_behaviour_is_unchanged(client):
    _seed_meili_hit()
    data = _post(client, "kayıt nasıl yapılır").json()
    assert data["answer"] == "Kayıt için OBS'yi kullanın."
    assert data["conversation_id"] and data["conversation_token"]
    assert _post(client, "   ").json()["answer"] == "Lütfen bir soru yazın."


def test_widget_chat_input_carries_the_same_maxlength():
    widget = (repo_root() / "chatbot-web" / "src" / "widget.js").read_text(encoding="utf-8")
    match = re.search(r'<textarea id="w-input"[^>]*\bmaxlength="(\d+)"', widget)
    assert match, "widget chat textarea must declare maxlength"
    assert int(match.group(1)) == CHAT_MESSAGE_MAX_CHARS


def test_other_limits_are_not_changed_by_the_chat_policy():
    """Solution Center description and /search keep their own 1000 limit."""
    assert MAX_MESSAGE_LEN == 1000
    widget = (repo_root() / "chatbot-web" / "src" / "widget.js").read_text(encoding="utf-8")
    assert "ta.maxLength = 1000;" in widget  # Solution Center description textarea
    chat_source = (repo_root() / "backend" / "routers" / "chat.py").read_text(encoding="utf-8")
    assert "max_length=1000" in chat_source  # /search q
