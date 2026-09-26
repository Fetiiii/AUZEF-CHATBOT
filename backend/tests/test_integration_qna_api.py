"""Solution Center read-only QnA integration contract."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event
from time import sleep

from sqlalchemy import inspect
from sqlalchemy.exc import OperationalError

from core.database import QnA, QnAIntegrationDeletion, Tag, admin_engine


API_ROOT = "/api/integrations/v1"
TEST_API_KEY = "pytest-integration-key"


def _headers():
    return {"X-API-Key": TEST_API_KEY}


def _qna(question: str, *, status: int = 1, changed_at: datetime | None = None):
    row = QnA(question_text=question, answer_text=f"{question} answer", status=status)
    if changed_at is not None:
        naive = changed_at.astimezone(timezone.utc).replace(tzinfo=None)
        row.created_at = naive
        row.updated_at = naive
    return row


def _get(client, path: str, **kwargs):
    headers = dict(_headers())
    headers.update(kwargs.pop("headers", {}))
    return client.get(f"{API_ROOT}{path}", headers=headers, **kwargs)


def _collect_pages(client, path: str, *, params: dict):
    items = []
    cursor = None
    first_since = None
    until = None
    while True:
        query = dict(params)
        if cursor:
            query["cursor"] = cursor
        response = _get(client, path, params=query)
        assert response.status_code == 200
        page = response.json()
        until = until or page["until"]
        assert page["until"] == until
        if "since" in page:
            first_since = first_since or page["since"]
            assert page["since"] == first_since
        items.extend(page["items"])
        cursor = page["pagination"]["next_cursor"]
        if not page["pagination"]["has_more"]:
            return until, items


def test_api_key_missing_and_wrong_are_unauthorized(client, monkeypatch):
    monkeypatch.setenv("INTEGRATION_API_KEY", TEST_API_KEY)

    missing = client.get(f"{API_ROOT}/meta")
    wrong = client.get(f"{API_ROOT}/meta", headers={"X-API-Key": "wrong"})

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert missing.json() == {"detail": "Invalid or missing API key."}


def test_api_key_is_not_written_to_request_logs(client, monkeypatch, caplog):
    monkeypatch.setenv("INTEGRATION_API_KEY", TEST_API_KEY)
    with caplog.at_level("INFO", logger="auzef"):
        assert _get(client, "/meta").status_code == 200
    assert TEST_API_KEY not in caplog.text


def test_correct_api_key_succeeds_and_unconfigured_api_is_closed(client, monkeypatch):
    monkeypatch.setenv("INTEGRATION_API_KEY", TEST_API_KEY)
    assert _get(client, "/meta").status_code == 200

    monkeypatch.delenv("INTEGRATION_API_KEY")
    assert client.get(f"{API_ROOT}/meta", headers=_headers()).status_code == 503


def test_qna_returns_only_active_records_with_stable_schema_and_cursor_pagination(
    client, db, monkeypatch
):
    monkeypatch.setenv("INTEGRATION_API_KEY", TEST_API_KEY)
    first = _qna("First")
    first.tags = [Tag(name="Kayit"), Tag(name="Ogrenci")]
    second = _qna("Second")
    inactive = _qna("Inactive", status=0)
    db.add_all([first, second, inactive])
    db.commit()

    page_1 = _get(client, "/qna", params={"limit": 1})
    assert page_1.status_code == 200
    body_1 = page_1.json()
    assert set(body_1) == {"until", "items", "pagination"}
    assert set(body_1["items"][0]) == {
        "id",
        "question",
        "answer",
        "categories",
        "updated_at",
    }
    assert body_1["items"][0]["categories"] == ["Kayit", "Ogrenci"]
    assert body_1["pagination"]["has_more"] is True

    page_2 = _get(
        client,
        "/qna",
        params={
            "limit": 1,
            "cursor": body_1["pagination"]["next_cursor"],
        },
    )
    body_2 = page_2.json()
    assert [body_1["items"][0]["id"], body_2["items"][0]["id"]] == [first.id, second.id]
    assert body_2["until"] == body_1["until"]
    assert body_2["pagination"] == {"next_cursor": None, "has_more": False}


def test_qna_validates_limit_and_cursor(client, monkeypatch):
    monkeypatch.setenv("INTEGRATION_API_KEY", TEST_API_KEY)
    assert _get(client, "/qna", params={"limit": 0}).status_code == 422
    assert _get(client, "/qna", params={"limit": 1001}).status_code == 422
    invalid = _get(client, "/qna", params={"cursor": "not-a-cursor"})
    assert invalid.status_code == 400
    assert invalid.json() == {"detail": "Invalid pagination cursor."}


def test_changes_reports_created_updated_and_excludes_older_rows(
    client, db, monkeypatch
):
    monkeypatch.setenv("INTEGRATION_API_KEY", TEST_API_KEY)
    since = datetime(2026, 9, 20, 10, tzinfo=timezone.utc)
    created = _qna("Created", changed_at=since + timedelta(minutes=1))
    updated = _qna("Updated", changed_at=since + timedelta(minutes=2))
    updated.created_at = (since - timedelta(days=1)).replace(tzinfo=None)
    old = _qna("Old", changed_at=since - timedelta(seconds=1))
    db.add_all([created, updated, old])
    db.commit()

    response = _get(client, "/qna/changes", params={"since": since.isoformat()})
    assert response.status_code == 200
    body = response.json()
    assert body["since"] == "2026-09-20T10:00:00Z"
    assert [(item["id"], item["operation"]) for item in body["items"]] == [
        (created.id, "upsert"),
        (updated.id, "upsert"),
    ]
    assert old.id not in {item["id"] for item in body["items"]}


def test_hard_delete_is_propagated_as_tombstone(
    client, db, make_user, login, monkeypatch
):
    monkeypatch.setenv("INTEGRATION_API_KEY", TEST_API_KEY)
    make_user("editor@iu.tr", role="editor")
    admin_client = login("editor@iu.tr")
    row = _qna("Delete me")
    db.add(row)
    db.commit()
    db.refresh(row)
    qna_id = row.id
    since = datetime.now(timezone.utc) - timedelta(minutes=1)

    assert admin_client.delete(f"/api/qna/{qna_id}").status_code == 204
    db.expire_all()
    assert db.query(QnA).filter(QnA.id == qna_id).first() is None
    assert db.query(QnAIntegrationDeletion).filter_by(qna_id=qna_id).one()

    response = _get(client, "/qna/changes", params={"since": since.isoformat()})
    item = response.json()["items"][0]
    assert item["id"] == qna_id
    assert item["operation"] == "delete"
    assert set(item) == {"id", "operation", "updated_at"}


def test_inactive_qna_is_reported_as_delete(client, db, monkeypatch):
    monkeypatch.setenv("INTEGRATION_API_KEY", TEST_API_KEY)
    since = datetime(2026, 9, 20, 10, tzinfo=timezone.utc)
    row = _qna("Inactive", status=0, changed_at=since + timedelta(seconds=1))
    db.add(row)
    db.commit()

    body = _get(client, "/qna/changes", params={"since": since.isoformat()}).json()
    assert body["items"] == [
        {
            "id": row.id,
            "operation": "delete",
            "updated_at": "2026-09-20T10:00:01Z",
        }
    ]


def test_changes_cursor_keeps_all_rows_with_identical_timestamp(
    client, db, monkeypatch
):
    monkeypatch.setenv("INTEGRATION_API_KEY", TEST_API_KEY)
    since = datetime(2026, 9, 20, 10, tzinfo=timezone.utc)
    changed_at = since + timedelta(seconds=1)
    rows = [_qna(f"Q{i}", changed_at=changed_at) for i in range(3)]
    db.add_all(rows)
    db.commit()

    ids = []
    cursor = None
    until = None
    later_row = None
    while True:
        params = {"since": since.isoformat(), "limit": 1}
        if cursor:
            params["cursor"] = cursor
        response = _get(client, "/qna/changes", params=params)
        assert response.status_code == 200
        body = response.json()
        until = until or body["until"]
        assert body["since"] == "2026-09-20T10:00:00Z"
        assert body["until"] == until
        ids.extend(item["id"] for item in body["items"])
        cursor = body["pagination"]["next_cursor"]
        if later_row is None:
            # A write after page one belongs to the next sync window.
            sleep(0.002)
            later_row = _qna("after changes page one")
            db.add(later_row)
            db.commit()
        if not body["pagination"]["has_more"]:
            break

    assert ids == [row.id for row in rows]
    _, next_window = _collect_pages(
        client, "/qna/changes", params={"since": until, "limit": 1}
    )
    assert (later_row.id, "upsert") in {
        (item["id"], item["operation"]) for item in next_window
    }


def test_full_sync_catches_insert_committed_after_until_from_older_transaction(
    client, db, monkeypatch
):
    """A writer may begin before full sync but commit after its watermark."""
    from sqlalchemy import text

    from core.database import SessionLocal, execute_admin_sql

    monkeypatch.setenv("INTEGRATION_API_KEY", TEST_API_KEY)
    existing = _qna("existing")
    db.add(existing)
    db.commit()
    writer = SessionLocal()
    try:
        started = execute_admin_sql(writer, text("SELECT transaction_timestamp()"))
        started_at = started.scalar_one()
        writer_row = writer.query(QnA).filter_by(id=existing.id).one()
        full = _get(client, "/qna").json()
        assert started_at.replace(tzinfo=timezone.utc) < datetime.fromisoformat(
            full["until"].replace("Z", "+00:00")
        )
        row = _qna("late commit")
        writer_row.answer_text = "updated after watermark"
        writer.add(row)
        writer.commit()

        changes = _get(
            client, "/qna/changes", params={"since": full["until"]}
        ).json()
        assert {(item["id"], item["operation"]) for item in changes["items"]} == {
            (existing.id, "upsert"),
            (row.id, "upsert"),
        }
    finally:
        writer.close()


def test_full_sync_watermark_waits_for_an_uncommitted_qna_update(
    client, db, monkeypatch
):
    from core.database import SessionLocal

    monkeypatch.setenv("INTEGRATION_API_KEY", TEST_API_KEY)
    row = _qna("before")
    db.add(row)
    db.commit()
    writer = SessionLocal()
    writer_row = writer.query(QnA).filter_by(id=row.id).one()
    writer_row.answer_text = "after"
    writer.flush()
    started = Event()

    def read_full_sync():
        started.set()
        return _get(client, "/qna").json()

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(read_full_sync)
            assert started.wait(timeout=2)
            sleep(0.05)
            assert not future.done()
            writer.commit()
            writer.refresh(writer_row)
            changed_at = writer_row.updated_at
            full = future.result(timeout=5)
    finally:
        writer.close()

    assert full["items"][0]["answer"] == "after"
    assert datetime.fromisoformat(full["until"].replace("Z", "+00:00")) >= (
        changed_at.replace(tzinfo=timezone.utc)
    )


def test_full_then_incremental_sync_converges_after_interleaved_writes(
    client, db, make_user, login, monkeypatch
):
    monkeypatch.setenv("INTEGRATION_API_KEY", TEST_API_KEY)
    rows = [_qna(f"Initial {index}") for index in range(4)]
    for row, qna_id in zip(rows, (10, 20, 30, 40)):
        row.id = qna_id
    db.add_all(rows)
    db.commit()
    ids = [row.id for row in rows]
    make_user("sync-editor@iu.tr", role="editor")
    admin_client = login("sync-editor@iu.tr")

    first = _get(client, "/qna", params={"limit": 1}).json()
    full_until = first["until"]
    replica = {item["id"]: item for item in first["items"]}
    cursor = first["pagination"]["next_cursor"]

    # Update a row already exported, create a row, hard-delete an unexported
    # row, and inactivate another unexported row between full-sync pages.
    assert admin_client.put(
        f"/api/qna/{ids[0]}", json={"answer_text": "updated answer"}
    ).status_code == 200
    created = admin_client.post(
        "/api/qna", json={"question_text": "New", "answer_text": "new answer"}
    )
    assert created.status_code == 201
    assert created.json()["id"] < ids[0]
    assert admin_client.delete(f"/api/qna/{ids[1]}").status_code == 204
    assert admin_client.put(
        f"/api/qna/{ids[2]}", json={"status": 0}
    ).status_code == 200

    while cursor:
        page = _get(client, "/qna", params={"limit": 1, "cursor": cursor}).json()
        assert page["until"] == full_until
        replica.update({item["id"]: item for item in page["items"]})
        cursor = page["pagination"]["next_cursor"]
    assert created.json()["id"] not in replica

    changes_until, changes = _collect_pages(
        client, "/qna/changes", params={"since": full_until, "limit": 1}
    )
    operations = {(item["id"], item["operation"]) for item in changes}
    assert (ids[0], "upsert") in operations
    assert (created.json()["id"], "upsert") in operations
    assert (ids[1], "delete") in operations
    assert (ids[2], "delete") in operations
    for item in changes:
        if item["operation"] == "delete":
            replica.pop(item["id"], None)
        else:
            replica[item["id"]] = item

    db.expire_all()
    active = db.query(QnA).filter(QnA.status == 1).all()
    assert {item_id: item["answer"] for item_id, item in replica.items()} == {
        row.id: row.answer_text for row in active
    }

    # A later reactivation must be an upsert in the next window.
    assert admin_client.put(
        f"/api/qna/{ids[2]}", json={"status": 1}
    ).status_code == 200
    _, later_changes = _collect_pages(
        client, "/qna/changes", params={"since": changes_until, "limit": 1}
    )
    assert (ids[2], "upsert") in {
        (item["id"], item["operation"]) for item in later_changes
    }


def test_changes_validates_since_and_cursor_context(client, monkeypatch):
    monkeypatch.setenv("INTEGRATION_API_KEY", TEST_API_KEY)
    assert _get(client, "/qna/changes", params={"since": "invalid"}).status_code == 422
    assert (
        _get(
            client, "/qna/changes", params={"since": "2026-09-20T10:00:00"}
        ).status_code
        == 422
    )
    invalid = _get(
        client,
        "/qna/changes",
        params={
            "since": "2026-09-20T10:00:00Z",
            "cursor": "not-a-cursor",
        },
    )
    assert invalid.status_code == 400


def test_meta_handles_empty_dataset_and_uses_latest_change(client, db, monkeypatch):
    monkeypatch.setenv("INTEGRATION_API_KEY", TEST_API_KEY)
    assert _get(client, "/meta").json() == {
        "dataset_version": None,
        "total_active_records": 0,
        "last_updated_at": None,
    }

    changed_at = datetime(2026, 9, 20, 10, tzinfo=timezone.utc)
    db.add_all(
        [
            _qna("Active A", changed_at=changed_at),
            _qna("Active B", changed_at=changed_at + timedelta(seconds=1)),
            _qna("Inactive", status=0, changed_at=changed_at + timedelta(seconds=2)),
        ]
    )
    db.commit()

    body = _get(client, "/meta").json()
    assert body["total_active_records"] == 2
    assert body["last_updated_at"] == "2026-09-20T10:00:02Z"
    assert body["dataset_version"].endswith("|delete|3")


def test_openapi_exposes_api_key_scheme_and_read_only_routes(app):
    schema = app.openapi()
    integration_paths = {
        path: methods
        for path, methods in schema["paths"].items()
        if path.startswith(API_ROOT)
    }
    assert set(integration_paths) == {
        f"{API_ROOT}/qna",
        f"{API_ROOT}/qna/changes",
        f"{API_ROOT}/meta",
    }
    assert all(set(methods) == {"get"} for methods in integration_paths.values())
    assert all(methods["get"].get("security") for methods in integration_paths.values())
    security_schemes = schema["components"]["securitySchemes"]
    assert any(
        scheme.get("type") == "apiKey" and scheme.get("name") == "X-API-Key"
        for scheme in security_schemes.values()
    )


def test_integration_swagger_is_public_but_only_documents_protected_qna_routes(
    client, app, monkeypatch
):
    monkeypatch.setenv("INTEGRATION_API_KEY", TEST_API_KEY)

    docs = client.get(f"{API_ROOT}/docs")
    schema_response = client.get(f"{API_ROOT}/openapi.json")
    assert docs.status_code == 200
    assert f"{API_ROOT}/openapi.json" in docs.text
    assert schema_response.status_code == 200

    schema = schema_response.json()
    paths = {f"{API_ROOT}/qna", f"{API_ROOT}/qna/changes", f"{API_ROOT}/meta"}
    assert set(schema["paths"]) == paths
    assert all(set(schema["paths"][path]) == {"get"} for path in paths)
    scheme = schema["components"]["securitySchemes"]["APIKeyHeader"]
    assert scheme == {"type": "apiKey", "in": "header", "name": "X-API-Key"}
    assert all(
        schema["paths"][path]["get"]["security"] == [{"APIKeyHeader": []}]
        for path in paths
    )
    assert "example" not in scheme and "default" not in scheme
    assert TEST_API_KEY not in schema_response.text

    for path in paths:
        assert client.get(path).status_code == 401

    # Documentation routes must not alter the main application's OpenAPI surface.
    main_paths = app.openapi()["paths"]
    assert f"{API_ROOT}/docs" not in main_paths
    assert f"{API_ROOT}/openapi.json" not in main_paths
    assert "/api/qna" in main_paths
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200


def test_database_errors_return_safe_503(client, monkeypatch, caplog):
    from integrations.solution_center_qna.service import QnAIntegrationService

    monkeypatch.setenv("INTEGRATION_API_KEY", TEST_API_KEY)

    def fail(_self):
        raise OperationalError(
            "SELECT secret", {"password": "do-not-leak"}, Exception("boom")
        )

    monkeypatch.setattr(QnAIntegrationService, "meta", fail)
    with caplog.at_level("ERROR", logger="auzef"):
        response = _get(client, "/meta")
    assert response.status_code == 503
    assert response.json() == {"detail": "Integration data is temporarily unavailable."}
    assert "password" not in response.text
    assert "do-not-leak" not in caplog.text


def test_tombstone_migration_is_reversible():
    from scripts.migrate_qna_integration_deletions import (
        apply_qna_integration_deletions_migration,
    )

    try:
        apply_qna_integration_deletions_migration("downgrade")
        assert (
            "qna_integration_deletions" not in inspect(admin_engine).get_table_names()
        )
    finally:
        apply_qna_integration_deletions_migration("upgrade")
    assert "qna_integration_deletions" in inspect(admin_engine).get_table_names()
