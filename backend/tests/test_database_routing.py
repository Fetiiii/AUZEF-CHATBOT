"""DB-ADMIN / DB-CHAT ownership ve compatibility acceptance testleri."""
import os
import subprocess
import sys
from datetime import timedelta

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import UnboundExecutionError


ADMIN_TABLE_NAMES = {
    "qna",
    "qna_queries",
    "tags",
    "qna_tags",
    "system_config",
    "admin_users",
    "admin_sessions",
    "admin_login_attempts",
    "academic_calendar",
}

CHAT_TABLE_NAMES = {
    "query_logs",
    "conversations",
    "conversation_messages",
    "solution_center_sessions",
    "sc_rate_limits",
}


def test_database_url_fallback_contract():
    from core.database import resolve_database_urls

    fallback = "postgresql://single"
    assert resolve_database_urls({"DATABASE_URL": fallback}) == (
        fallback,
        fallback,
        fallback,
    )
    assert resolve_database_urls({
        "DATABASE_URL": fallback,
        "ADMIN_DATABASE_URL": "postgresql://admin",
        "CHAT_DATABASE_URL": "postgresql://chat",
    }) == (
        fallback,
        "postgresql://admin",
        "postgresql://chat",
    )


def test_model_to_engine_bind_contract():
    from core.database import (
        ADMIN_MODELS,
        CHAT_MODELS,
        SessionLocal,
        admin_engine,
        chat_engine,
    )

    db = SessionLocal()
    try:
        for model in ADMIN_MODELS:
            assert db.get_bind(mapper=model) is admin_engine
        for model in CHAT_MODELS:
            assert db.get_bind(mapper=model) is chat_engine
        with pytest.raises(UnboundExecutionError):
            db.execute(text("SELECT 1"))
    finally:
        db.close()


def test_qna_crud_routes_to_admin(make_user, login):
    from core.database import admin_engine, chat_engine

    if admin_engine is chat_engine:
        pytest.skip("Bu test ayrı TEST_ADMIN_DATABASE_URL/TEST_CHAT_DATABASE_URL ister")

    make_user("routing-admin@iu.test", role="super_admin")
    client = login("routing-admin@iu.test")
    created = client.post(
        "/api/qna",
        json={"question_text": "CRUD routing", "answer_text": "ilk cevap"},
    )
    assert created.status_code == 201
    qna_id = created.json()["id"]
    assert any(row["id"] == qna_id for row in client.get("/api/qna").json())

    updated = client.put(
        f"/api/qna/{qna_id}",
        json={"answer_text": "güncel cevap"},
    )
    assert updated.status_code == 200
    with admin_engine.connect() as conn:
        assert conn.execute(
            text("SELECT answer_text FROM qna WHERE id = :id"), {"id": qna_id}
        ).scalar_one() == "güncel cevap"

    assert client.delete(f"/api/qna/{qna_id}").status_code == 204
    with admin_engine.connect() as conn:
        assert conn.execute(
            text("SELECT COUNT(*) FROM qna WHERE id = :id"), {"id": qna_id}
        ).scalar_one() == 0


def test_split_schema_and_ddl_ownership():
    from core.database import admin_engine, chat_engine

    if admin_engine is chat_engine:
        pytest.skip("Bu test ayrı TEST_ADMIN_DATABASE_URL/TEST_CHAT_DATABASE_URL ister")

    admin_inspector = inspect(admin_engine)
    chat_inspector = inspect(chat_engine)
    admin_tables = set(admin_inspector.get_table_names())
    chat_tables = set(chat_inspector.get_table_names())

    assert admin_tables & (ADMIN_TABLE_NAMES | CHAT_TABLE_NAMES) == ADMIN_TABLE_NAMES
    assert chat_tables & (ADMIN_TABLE_NAMES | CHAT_TABLE_NAMES) == CHAT_TABLE_NAMES
    assert "qna_search_view" in admin_inspector.get_view_names()
    assert "qna_search_view" not in chat_inspector.get_view_names()

    assert "role" in {c["name"] for c in admin_inspector.get_columns("admin_users")}
    assert "updated_by" in {c["name"] for c in admin_inspector.get_columns("qna")}
    assert "updated_by" in {
        c["name"] for c in admin_inspector.get_columns("academic_calendar")
    }
    assert "client_token" in {
        c["name"] for c in chat_inspector.get_columns("conversations")
    }
    assert "otp_attempts" in {
        c["name"] for c in chat_inspector.get_columns("solution_center_sessions")
    }
    assert "ix_conversation_messages_content_trgm" in {
        index["name"]
        for index in chat_inspector.get_indexes("conversation_messages")
    }
    with chat_engine.connect() as conn:
        assert conn.execute(
            text("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm')")
        ).scalar()
    with admin_engine.connect() as conn:
        assert not conn.execute(
            text("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm')")
        ).scalar()


def test_all_owned_models_persist_to_their_physical_database():
    from core.database import (
        AcademicCalendar,
        AdminLoginAttempt,
        AdminSession,
        AdminUser,
        Conversation,
        ConversationMessage,
        QnA,
        QnAQuery,
        QnATag,
        QueryLog,
        SCRateLimit,
        SessionLocal,
        SolutionCenterSession,
        SystemConfig,
        Tag,
        admin_engine,
        chat_engine,
        utcnow,
    )

    if admin_engine is chat_engine:
        pytest.skip("Bu test ayrı TEST_ADMIN_DATABASE_URL/TEST_CHAT_DATABASE_URL ister")

    db = SessionLocal()
    try:
        qna = QnA(question_text="routing soru", answer_text="routing cevap")
        tag = Tag(name="routing-tag")
        qna.queries.append(QnAQuery(query_text="routing alias"))
        qna.tags.append(tag)
        user = AdminUser(
            email="routing@iu.test",
            password_hash="not-a-real-password-hash",
            role="super_admin",
            is_active=1,
        )
        user.sessions.append(
            AdminSession(token_hash="a" * 64, expires_at=utcnow() + timedelta(hours=1))
        )
        db.add_all([
            qna,
            SystemConfig(key="ROUTING_TEST", value="admin"),
            AdminLoginAttempt(
                scope="email",
                identifier="routing@iu.test",
                count=1,
                window_started_at=utcnow(),
            ),
            AcademicCalendar(
                period="Test",
                event="Routing",
                start_date="01.01.2099",
                end_date="01.01.2099",
            ),
            user,
        ])
        db.commit()

        # Aynı request/session bağlamında admin okuması ve chat yazması.
        assert db.query(SystemConfig).filter_by(key="ROUTING_TEST").one().value == "admin"
        conversation = Conversation(client_token="routing-token")
        conversation.messages.append(
            ConversationMessage(role="user", content="routing message")
        )
        db.add_all([
            conversation,
            QueryLog(source="none", status="success"),
            SCRateLimit(
                scope="ip",
                key_hash="b" * 64,
                count=1,
                window_started_at=utcnow(),
            ),
        ])
        db.flush()
        db.add(SolutionCenterSession(conversation_id=conversation.id))
        db.commit()

        # QnATag association row'u ilişki üzerinden gerçekten yazılmış olmalı.
        assert db.query(QnATag).count() == 1
    finally:
        db.close()

    with admin_engine.connect() as conn:
        for table in ADMIN_TABLE_NAMES:
            assert conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar() >= 1
    with chat_engine.connect() as conn:
        for table in CHAT_TABLE_NAMES:
            assert conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar() >= 1


def test_single_database_compatibility_init_in_subprocess():
    compat_url = os.getenv("TEST_COMPAT_DATABASE_URL")
    if not compat_url:
        pytest.skip("TEST_COMPAT_DATABASE_URL sağlanmadı")

    code = f"""
from sqlalchemy import inspect
from core.database import (
    ADMIN_DATABASE_URL, CHAT_DATABASE_URL, admin_engine, chat_engine, init_db
)

expected = {sorted(ADMIN_TABLE_NAMES | CHAT_TABLE_NAMES)!r}
assert ADMIN_DATABASE_URL == {compat_url!r}
assert CHAT_DATABASE_URL == {compat_url!r}
assert admin_engine is chat_engine
init_db()
inspector = inspect(admin_engine)
assert set(inspector.get_table_names()) == set(expected)
assert "qna_search_view" in inspector.get_view_names()
"""
    env = os.environ.copy()
    env["DATABASE_URL"] = compat_url
    # Boş değerler fallback kabul edilir ve repository .env dosyasının bu iki
    # değişkeni subprocess'e yeniden doldurmasını da engeller.
    env["ADMIN_DATABASE_URL"] = ""
    env["CHAT_DATABASE_URL"] = ""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=os.path.dirname(os.path.dirname(__file__)),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
