"""Production liveness/readiness sözleşmesi testleri."""

import pytest


class _ConnectionProbe:
    def __init__(self, owner):
        self.owner = owner

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, statement):
        self.owner.execute_calls += 1
        if self.owner.fail:
            raise RuntimeError(self.owner.secret_error)


class _EngineProbe:
    def __init__(self, fail=False):
        self.fail = fail
        self.connect_calls = 0
        self.execute_calls = 0
        self.secret_error = "postgresql://user:secret@private-db.internal/database"

    def connect(self):
        self.connect_calls += 1
        return _ConnectionProbe(self)


class _SearchProbe:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = 0
        self.secret_error = "https://service-key@private-search.internal"

    def healthcheck(self):
        self.calls += 1
        if self.fail:
            raise RuntimeError(self.secret_error)


def test_liveness_is_public_and_calls_no_downstream(client, monkeypatch):
    import main

    def unexpected_probe(*args, **kwargs):
        raise AssertionError("liveness must not probe downstream dependencies")

    monkeypatch.setattr(main, "_dependency_status", unexpected_probe)

    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize(
    (
        "admin_fail",
        "chat_fail",
        "meili_fail",
        "qdrant_fail",
        "expected_code",
        "expected_status",
    ),
    [
        (False, False, False, False, 200, "ready"),
        (True, False, False, False, 503, "unready"),
        (False, True, False, False, 503, "unready"),
        (False, False, True, False, 200, "degraded"),
        (False, False, False, True, 200, "degraded"),
        (False, False, True, True, 200, "degraded"),
    ],
)
def test_readiness_contract(
    client,
    monkeypatch,
    admin_fail,
    chat_fail,
    meili_fail,
    qdrant_fail,
    expected_code,
    expected_status,
):
    import main

    admin = _EngineProbe(fail=admin_fail)
    chat = _EngineProbe(fail=chat_fail)
    meili = _SearchProbe(fail=meili_fail)
    qdrant = _SearchProbe(fail=qdrant_fail)
    monkeypatch.setattr(main, "admin_engine", admin)
    monkeypatch.setattr(main, "chat_engine", chat)
    monkeypatch.setattr(main, "MEILI_PROVIDER", meili)
    monkeypatch.setattr(main, "QDRANT_PROVIDER", qdrant)

    response = client.get("/health/ready")

    assert response.status_code == expected_code
    payload = response.json()
    assert payload["status"] == expected_status
    assert payload["dependencies"] == {
        "db_admin": "error" if admin_fail else "ok",
        "db_chat": "error" if chat_fail else "ok",
        "meilisearch": "error" if meili_fail else "ok",
        "qdrant": "error" if qdrant_fail else "ok",
    }
    # Bir hata diğer probe'ları kısa devre etmemeli.
    assert admin.connect_calls == admin.execute_calls == 1
    assert chat.connect_calls == chat.execute_calls == 1
    assert meili.calls == qdrant.calls == 1
    # İç bağlantı/credential ayrıntıları dışarı sızmamalı.
    assert "secret" not in response.text
    assert "internal" not in response.text


def test_readiness_probes_both_physical_databases_in_split_mode(client, monkeypatch):
    from sqlalchemy import event
    import main
    from core.database import admin_engine, chat_engine

    if admin_engine is chat_engine:
        pytest.skip("Bu kabul testi yalnız split DB modunda uygulanır")

    calls = {"admin": 0, "chat": 0}

    def count_admin(_conn, _cursor, statement, _parameters, _context, _executemany):
        if str(statement).strip().upper() == "SELECT 1":
            calls["admin"] += 1

    def count_chat(_conn, _cursor, statement, _parameters, _context, _executemany):
        if str(statement).strip().upper() == "SELECT 1":
            calls["chat"] += 1

    monkeypatch.setattr(main.MEILI_PROVIDER, "healthcheck", lambda: None)
    monkeypatch.setattr(main.QDRANT_PROVIDER, "healthcheck", lambda: None)
    event.listen(admin_engine, "before_cursor_execute", count_admin)
    event.listen(chat_engine, "before_cursor_execute", count_chat)
    try:
        response = client.get("/health/ready")
    finally:
        event.remove(admin_engine, "before_cursor_execute", count_admin)
        event.remove(chat_engine, "before_cursor_execute", count_chat)

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert calls == {"admin": 1, "chat": 1}


def test_legacy_health_contract_is_preserved(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"ok": True}
