"""Runtime startup ile explicit infrastructure initialization sözleşmesi."""

import importlib
import os
from pathlib import Path
import subprocess
import sys

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _unexpected(name):
    def fail(*args, **kwargs):
        raise AssertionError(f"runtime startup {name} çağırmamalı")

    return fail


def test_fastapi_import_and_startup_do_not_initialize_infrastructure(monkeypatch):
    from fastapi.testclient import TestClient
    import core.database as database
    import core.deps as deps
    import main as main_module

    monkeypatch.setattr(database, "init_db", _unexpected("init_db"))
    monkeypatch.setattr(database, "init_admin_db", _unexpected("init_admin_db"))
    monkeypatch.setattr(database, "init_chat_db", _unexpected("init_chat_db"))
    monkeypatch.setattr(
        deps.QDRANT_PROVIDER,
        "ensure_collection",
        _unexpected("QDRANT_PROVIDER.ensure_collection"),
    )

    reloaded = importlib.reload(main_module)
    with TestClient(reloaded.app) as client:
        response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_db_command_runs_only_db_steps_in_order(monkeypatch):
    import scripts.init_system as init_system

    calls = []
    monkeypatch.setattr(init_system, "init_admin_db", lambda: calls.append("admin"))
    monkeypatch.setattr(init_system, "init_chat_db", lambda: calls.append("chat"))
    monkeypatch.setattr(
        init_system, "seed_default_config", lambda: calls.append("seed")
    )
    monkeypatch.setattr(
        init_system, "initialize_qdrant", _unexpected("Qdrant provisioning")
    )

    assert init_system.main(["db"]) == 0
    assert calls == ["admin", "chat", "seed"]


def test_qdrant_command_runs_ensure_collection_once_without_db(monkeypatch):
    import core.deps as deps
    import scripts.init_system as init_system

    calls = []
    monkeypatch.setattr(init_system, "initialize_db", _unexpected("DB init"))
    monkeypatch.setattr(
        deps.QDRANT_PROVIDER,
        "ensure_collection",
        lambda: calls.append("qdrant"),
    )

    assert init_system.main(["qdrant"]) == 0
    assert calls == ["qdrant"]


def test_all_command_runs_db_seed_then_qdrant(monkeypatch):
    import core.deps as deps
    import scripts.init_system as init_system

    calls = []
    monkeypatch.setattr(init_system, "init_admin_db", lambda: calls.append("admin"))
    monkeypatch.setattr(init_system, "init_chat_db", lambda: calls.append("chat"))
    monkeypatch.setattr(
        init_system, "seed_default_config", lambda: calls.append("seed")
    )
    monkeypatch.setattr(
        deps.QDRANT_PROVIDER,
        "ensure_collection",
        lambda: calls.append("qdrant"),
    )

    assert init_system.main(["all"]) == 0
    assert calls == ["admin", "chat", "seed", "qdrant"]


def test_cli_requires_an_explicit_command():
    import scripts.init_system as init_system

    with pytest.raises(SystemExit) as exc_info:
        init_system.main([])

    assert exc_info.value.code == 2


def test_initialization_exception_is_not_swallowed(monkeypatch):
    import scripts.init_system as init_system

    def failure():
        raise RuntimeError("operator-visible failure")

    monkeypatch.setattr(init_system, "init_admin_db", failure)

    with pytest.raises(RuntimeError, match="operator-visible failure"):
        init_system.main(["db"])


def test_qdrant_initialization_exception_is_not_swallowed(monkeypatch):
    import core.deps as deps
    import scripts.init_system as init_system

    def failure():
        raise RuntimeError("qdrant operator-visible failure")

    monkeypatch.setattr(deps.QDRANT_PROVIDER, "ensure_collection", failure)

    with pytest.raises(RuntimeError, match="qdrant operator-visible failure"):
        init_system.main(["qdrant"])


def test_failed_db_cli_process_exits_non_zero():
    env = os.environ.copy()
    unavailable = "postgresql://admin:test@127.0.0.1:1/unavailable"
    env.update({
        "DATABASE_URL": unavailable,
        "ADMIN_DATABASE_URL": unavailable,
        "CHAT_DATABASE_URL": unavailable,
    })

    result = subprocess.run(
        [sys.executable, "-m", "scripts.init_system", "db"],
        cwd=BACKEND_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )

    assert result.returncode != 0
    assert result.stderr


def test_db_command_is_idempotent_and_preserves_existing_config():
    from core.database import SessionLocal, SystemConfig
    import scripts.init_system as init_system

    with SessionLocal() as db:
        db.query(SystemConfig).filter(SystemConfig.key == "LLM_ENABLED").delete()
        db.commit()

    assert init_system.main(["db"]) == 0
    with SessionLocal() as db:
        row = db.get(SystemConfig, "LLM_ENABLED")
        assert row is not None and row.value == "false"
        row.value = "true"
        db.commit()

    assert init_system.main(["db"]) == 0
    assert init_system.main(["db"]) == 0
    with SessionLocal() as db:
        assert db.get(SystemConfig, "LLM_ENABLED").value == "true"


def test_development_entrypoint_runs_explicit_init_before_unchanged_uvicorn():
    script = (BACKEND_ROOT / "entrypoint.sh").read_text(encoding="utf-8")
    init_command = "python -m scripts.init_system all"
    uvicorn_command = "exec uvicorn main:app"

    assert init_command in script
    assert script.index(init_command) < script.index(uvicorn_command)
    assert "--host 0.0.0.0" in script
    assert "--port 8000" in script
    assert "--workers 2" in script
    assert "--proxy-headers" in script
    assert '--forwarded-allow-ips="*"' in script
