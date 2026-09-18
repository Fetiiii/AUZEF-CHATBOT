"""DB-ADMIN merkezli planlı bakım sözleşmesi ve widget kısa devresi."""

import logging

import pytest


@pytest.fixture()
def sup(make_user, login):
    make_user("super@iu.tr", role="super_admin")
    return login("super@iu.tr")


def test_missing_config_defaults_to_maintenance_off(sup):
    from core.database import SessionLocal, SystemConfig
    from core.deps import MAINTENANCE_CONFIG_KEY

    response = sup.get("/api/settings/maintenance")

    assert response.status_code == 200
    assert response.json() == {"on": False}
    # Varsayılanı okumak gereksiz bir kayıt oluşturmamalı.
    with SessionLocal() as db:
        assert db.get(SystemConfig, MAINTENANCE_CONFIG_KEY) is None


def test_maintenance_roundtrip_blocks_only_widget(
    sup, tmp_path, monkeypatch, caplog
):
    from core.database import SessionLocal, SystemConfig
    from core.deps import MAINTENANCE_CONFIG_KEY
    import services.providers as providers

    # Eski env tanımlı olsa bile panel artık local flag'e dokunmamalı.
    monkeypatch.setenv("MAINTENANCE_FLAG_DIR", str(tmp_path))
    caplog.set_level(logging.INFO, logger="auzef")

    enabled = sup.put("/api/settings/maintenance", json={"on": True})
    assert enabled.status_code == 200
    assert enabled.json() == {"on": True}
    assert sup.get("/api/settings/maintenance").json() == {"on": True}
    assert sup.put(
        "/api/settings/maintenance", json={"on": True}
    ).json() == {"on": True}

    blocked = sup.post("/widget-chat", json={"message": "Kayıt nasıl yapılır?"})
    assert blocked.status_code == 503
    assert blocked.json() == {"detail": "maintenance"}
    assert not (tmp_path / "maintenance.flag").exists()

    disabled = sup.put("/api/settings/maintenance", json={"on": False})
    assert disabled.status_code == 200
    assert disabled.json() == {"on": False}
    assert sup.get("/api/settings/maintenance").json() == {"on": False}
    assert sup.put(
        "/api/settings/maintenance", json={"on": False}
    ).json() == {"on": False}

    with SessionLocal() as db:
        assert db.get(SystemConfig, MAINTENANCE_CONFIG_KEY).value == "false"

    providers.FakeMeili.hits = [{
        "id": 1,
        "question": "kayıt nasıl yapılır",
        "answer": "Kayıt için OBS'yi kullanın.",
        "score": 0.95,
        "source": "meilisearch",
    }]
    normal = sup.post("/widget-chat", json={"message": "Kayıt nasıl yapılır?"})
    assert normal.status_code == 200
    assert normal.json()["answer"] == "Kayıt için OBS'yi kullanın."

    assert "Bakım modu AÇILDI (super@iu.tr)" in caplog.text
    assert "Bakım modu kapatıldı (super@iu.tr)" in caplog.text


def test_maintenance_short_circuits_all_widget_work(sup, db, monkeypatch):
    from core.database import Conversation, ConversationMessage, QueryLog
    import routers.chat as chat

    assert sup.put("/api/settings/maintenance", json={"on": True}).status_code == 200

    def unexpected(*args, **kwargs):
        raise AssertionError("maintenance sırasında widget işi başlatılmamalı")

    monkeypatch.setattr(chat, "_get_or_create_conversation", unexpected)
    monkeypatch.setattr(chat, "_store_message", unexpected)
    monkeypatch.setattr(chat, "_answer_question", unexpected)
    monkeypatch.setattr(chat, "_log_query", unexpected)
    monkeypatch.setattr(chat.MEILI_PROVIDER, "get_suggestions", unexpected)

    response = sup.post("/widget-chat", json={"message": "Pahalı bir soru"})

    assert response.status_code == 503
    assert response.json() == {"detail": "maintenance"}
    assert db.query(Conversation).count() == 0
    assert db.query(ConversationMessage).count() == 0
    assert db.query(QueryLog).count() == 0


def test_api_search_is_not_blocked_by_maintenance(sup, monkeypatch):
    import routers.chat as chat

    assert sup.put("/api/settings/maintenance", json={"on": True}).status_code == 200
    calls = []

    def answer(question, db, conversation_context=(), trace=None):
        del trace
        calls.append(question)
        return "Arama çalışıyor.", "meilisearch"

    monkeypatch.setattr(chat, "_answer_question", answer)

    response = sup.get("/api/search", params={"q": "merhaba"})

    assert response.status_code == 200
    assert response.json()["answer"] == "Arama çalışıyor."
    assert calls == ["merhaba"]


def test_maintenance_requires_super_admin(client, make_user, login):
    # Anonim → 401
    assert client.get("/api/settings/maintenance").status_code == 401
    assert client.put("/api/settings/maintenance", json={"on": True}).status_code == 401
    # admin rolü yetmez → 403 (yalnızca super_admin)
    make_user("a@iu.tr", role="admin")
    admin_client = login("a@iu.tr")
    assert admin_client.get("/api/settings/maintenance").status_code == 403
    assert admin_client.put(
        "/api/settings/maintenance", json={"on": True}
    ).status_code == 403


def test_two_sessions_observe_same_central_state(sup):
    from core.database import SessionLocal
    from core.deps import is_maintenance_enabled

    assert sup.put("/api/settings/maintenance", json={"on": True}).status_code == 200

    with SessionLocal() as first, SessionLocal() as second:
        assert is_maintenance_enabled(first) is True
        assert is_maintenance_enabled(second) is True


def test_maintenance_config_is_owned_only_by_admin_db_in_split_mode(sup):
    from sqlalchemy import inspect, text
    from core.database import admin_engine, chat_engine
    from core.deps import MAINTENANCE_CONFIG_KEY

    if admin_engine is chat_engine:
        pytest.skip("Bu kabul testi yalnız split DB modunda uygulanır")

    assert sup.put("/api/settings/maintenance", json={"on": True}).status_code == 200

    with admin_engine.connect() as connection:
        value = connection.execute(
            text("SELECT value FROM system_config WHERE key = :key"),
            {"key": MAINTENANCE_CONFIG_KEY},
        ).scalar_one()

    assert value == "true"
    assert inspect(chat_engine).has_table("system_config") is False


def test_widget_db_failure_is_sanitized_and_stops_pipeline(client, monkeypatch):
    import routers.chat as chat

    def db_failure(*args, **kwargs):
        raise RuntimeError("postgresql://user:secret@db-admin.internal/database")

    def unexpected(*args, **kwargs):
        raise AssertionError("DB-ADMIN hatasında pipeline başlatılmamalı")

    monkeypatch.setattr(chat, "is_maintenance_enabled", db_failure)
    monkeypatch.setattr(chat, "_answer_question", unexpected)

    response = client.post("/widget-chat", json={"message": "Merhaba"})

    assert response.status_code == 503
    assert response.json() == {"detail": "service unavailable"}
    assert "secret" not in response.text
    assert "internal" not in response.text


def test_settings_db_failure_is_sanitized(sup, monkeypatch):
    import admin.settings_api as settings_api

    def db_failure(*args, **kwargs):
        raise RuntimeError("postgresql://user:secret@db-admin.internal/database")

    monkeypatch.setattr(settings_api, "is_maintenance_enabled", db_failure)

    response = sup.get("/api/settings/maintenance")

    assert response.status_code == 503
    assert response.json() == {"detail": "Bakım durumu okunamıyor."}
    assert "secret" not in response.text
    assert "internal" not in response.text
