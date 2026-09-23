"""A slow provider must never occupy the configured PostgreSQL pool(s)."""
from concurrent.futures import ThreadPoolExecutor
from threading import Condition, Event, Thread
import os
import time

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from services.llm_config import resolve_llm_config_set
from services.llm_types import (
    IntentAnalysis, IntentAnalyzerResult, IntentItem, LLMOutcomeStatus,
    LLMParseStatus, SelectorResult,
)


class Provider:
    configs = resolve_llm_config_set("openai", environ={})

    def __init__(self, gates=None):
        self.gates = gates or {}

    def effective_config(self, capability):
        return self.configs.for_capability(capability)

    def _wait(self, name):
        gate = self.gates.get(name)
        if gate:
            entered, release = gate
            entered.set()
            assert release.wait(15), f"{name} was not released"

    def analyze_intents_with_result(self, current, previous_user_turns=()):
        self._wait("analyzer")
        return IntentAnalyzerResult(analysis=IntentAnalysis(
            intent_count=1,
            intents=[IntentItem(
                source_text=current, normalized_text=current,
                resolved_text=current, context_used=False,
                calendar_relevant=False,
            )],
        ))

    def ask_with_result(self, question, candidates):
        self._wait("selector")
        candidate = candidates[0]
        return SelectorResult(
            status=LLMOutcomeStatus.SUCCESS,
            parse_status=LLMParseStatus.SUCCESS,
            answer=candidate.answer_text,
            decision="SELECT",
            selected_candidate_ref=candidate.candidate_ref,
            selected_kind=candidate.kind.value,
            selected_qna_id=candidate.qna_id,
        )


@pytest.fixture(params=("configured", "shared"))
def tiny_pools(monkeypatch, request):
    """Use one small pool in compatibility mode, or one per split database."""
    from core import database
    from core import deps
    from routers import chat
    from services import answer_pipeline as pipeline

    if request.param == "shared":
        shared_url = (
            database.ADMIN_DATABASE_URL
            if database.ADMIN_DATABASE_URL == database.CHAT_DATABASE_URL
            else os.getenv("TEST_COMPAT_DATABASE_URL")
        )
        if not shared_url:
            pytest.skip("shared test database URL is unavailable")
        admin_url = chat_url = shared_url
    else:
        admin_url = database.ADMIN_DATABASE_URL
        chat_url = database.CHAT_DATABASE_URL

    admin = create_engine(admin_url, pool_size=2,
                          max_overflow=0, pool_timeout=2)
    chat_engine = (
        admin if admin_url == chat_url
        else create_engine(chat_url, pool_size=2,
                           max_overflow=0, pool_timeout=2)
    )
    if request.param == "shared":
        assert chat_engine is admin
        database.Base.metadata.create_all(bind=admin)
        names = ", ".join(table.name for table in database.Base.metadata.sorted_tables)
        with admin.begin() as connection:
            connection.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    binds = {model: admin for model in database.ADMIN_MODELS}
    binds.update({model: chat_engine for model in database.CHAT_MODELS})
    factory = sessionmaker(autocommit=False, autoflush=False, binds=binds)
    monkeypatch.setattr(chat, "SessionLocal", factory)
    monkeypatch.setattr(pipeline, "SessionLocal", factory)
    monkeypatch.setattr(deps, "SessionLocal", factory)
    if request.param == "shared":
        # Seed helpers must write to the same physical DB as the widget.
        monkeypatch.setattr(database, "SessionLocal", factory)
    try:
        yield admin, chat_engine
    finally:
        admin.dispose()
        if chat_engine is not admin:
            chat_engine.dispose()


def _wire(monkeypatch, provider, *, qdrant_gate=None, meili_gate=None,
          mock_config=True):
    from services import answer_pipeline as pipeline

    if mock_config:
        monkeypatch.setattr(pipeline, "resolve_llm_request_state",
                            lambda _db: (True, provider, None))
    hit = {"qna_id": 1, "question": "soru", "answer": "cevap",
           "score": 0.99, "source": "qdrant"}

    def qdrant(_query, limit=24):
        if qdrant_gate:
            entered, release = qdrant_gate
            entered.set()
            assert release.wait(15)
        return [hit]

    def meili(_query, limit=5):
        if meili_gate:
            entered, release = meili_gate
            entered.set()
            assert release.wait(15)
        return []

    monkeypatch.setattr(pipeline.QDRANT_PROVIDER, "search", qdrant)
    monkeypatch.setattr(pipeline, "meili_search_safe", meili)
    monkeypatch.setattr(pipeline, "meili_is_available", lambda: True)


def _seed_qna():
    from core.database import QnA, SessionLocal
    with SessionLocal() as db:
        db.add(QnA(id=1, question_text="soru", answer_text="cevap", status=1))
        db.commit()


def _wait_for(event):
    assert event.wait(10), "provider phase was not reached"


def test_real_config_resolution_releases_pool_before_analyzer(
    client, monkeypatch, tiny_pools,
):
    from core import deps
    from core.database import SessionLocal, SystemConfig

    _seed_qna()
    with SessionLocal() as db:
        db.add_all([
            SystemConfig(key="LLM_ENABLED", value="true"),
            SystemConfig(key="OPENROUTER_API_KEY", value="test-only-key"),
        ])
        db.commit()

    entered, release = Event(), Event()
    provider = Provider({"analyzer": (entered, release)})
    created_with = []
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setattr(deps, "_dyn_llm", {"provider": None, "key": None})

    def make_provider(*, api_key):
        created_with.append(api_key)
        return provider

    monkeypatch.setattr(deps, "OpenRouterProvider", make_provider)
    _wire(monkeypatch, provider, mock_config=False)
    result = []
    worker = Thread(target=lambda: result.append(
        client.post("/widget-chat", json={"message": "soru"})
    ))
    worker.start()
    try:
        _wait_for(entered)
        assert created_with == ["test-only-key"]
        assert all(engine.pool.checkedout() == 0 for engine in tiny_pools)
    finally:
        release.set()
        worker.join(15)
    assert not worker.is_alive()
    assert result[0].status_code == 200
    assert result[0].json()["answer"] == "cevap"


def test_all_external_waits_have_no_checked_out_db_connection(
    client, monkeypatch, tiny_pools,
):
    _seed_qna()
    admin, chat_engine = tiny_pools
    for stage in ("analyzer", "qdrant", "meili", "selector"):
        entered, release = Event(), Event()
        gate = (entered, release)
        provider = Provider({stage: gate})
        _wire(monkeypatch, provider,
              qdrant_gate=gate if stage == "qdrant" else None,
              meili_gate=gate if stage == "meili" else None)
        result = []
        worker = Thread(target=lambda: result.append(
            client.post("/widget-chat", json={"message": "soru"})
        ))
        worker.start()
        try:
            _wait_for(entered)
            assert admin.pool.checkedout() == 0, stage
            assert chat_engine.pool.checkedout() == 0, stage
        finally:
            release.set()
            worker.join(15)
        assert not worker.is_alive()
        assert result[0].status_code == 200
        assert result[0].json()["answer"] == "cevap"
        assert result[0].json()["message_id"]


def test_24_concurrent_widget_requests_do_not_exhaust_tiny_pools(
    client, monkeypatch, tiny_pools, caplog,
):
    _seed_qna()
    admin, chat_engine = tiny_pools
    assert (admin is chat_engine) == (
        admin.url == chat_engine.url
    )
    assert admin.pool.size() == 2 and admin.pool._max_overflow == 0
    waiting = Condition()
    release = Event()
    arrivals = 0

    class WaitingProvider(Provider):
        def analyze_intents_with_result(self, current, previous_user_turns=()):
            nonlocal arrivals
            with waiting:
                arrivals += 1
                waiting.notify_all()
            assert release.wait(15)
            return super().analyze_intents_with_result(current, previous_user_turns)

    _wire(monkeypatch, WaitingProvider())
    with ThreadPoolExecutor(max_workers=24) as workers:
        futures = [workers.submit(client.post, "/widget-chat", json={"message": "soru"})
                   for _ in range(24)]
        try:
            deadline = time.monotonic() + 12
            with waiting:
                while arrivals < 24 and time.monotonic() < deadline:
                    waiting.wait(timeout=deadline - time.monotonic())
            assert arrivals == 24
            assert admin.pool.checkedout() == 0
            assert chat_engine.pool.checkedout() == 0
        finally:
            release.set()
        responses = [future.result(timeout=15) for future in futures]
    assert all(response.status_code == 200 for response in responses)
    assert all(response.json()["answer"] == "cevap" for response in responses)
    assert all(response.json()["message_id"] for response in responses)
    assert "QueuePool limit" not in caplog.text
    from core.database import QueryLog, SessionLocal
    with SessionLocal() as db:
        assert db.query(QueryLog).count() == 24


def test_bot_write_failure_keeps_answer_and_releases_connections(
    client, monkeypatch, tiny_pools,
):
    from routers import chat
    _seed_qna()
    _wire(monkeypatch, Provider())
    original = chat._store_message

    def failing_bot_write(db, conversation_id, role, content, source=None):
        if role == "bot":
            raise RuntimeError("test bot write failure")
        return original(db, conversation_id, role, content, source)

    monkeypatch.setattr(chat, "_store_message", failing_bot_write)
    response = client.post("/widget-chat", json={"message": "soru"})
    assert response.status_code == 200
    assert response.json() == {"answer": "cevap"}
    assert all(engine.pool.checkedout() == 0 for engine in tiny_pools)


def test_calendar_matching_uses_detached_snapshot(monkeypatch, tiny_pools):
    from core.database import AcademicCalendar, SessionLocal, SystemConfig
    from services import answer_pipeline as pipeline
    from services import calendar_retrieval

    with SessionLocal() as db:
        db.add_all([
            SystemConfig(key="ACADEMIC_CALENDAR_CURRENT_YEAR", value="2025-2026"),
            SystemConfig(key="ACADEMIC_CALENDAR_CURRENT_TERM", value="GUZ"),
            AcademicCalendar(
                period="Güz Dönemi", event="Bütünleme", start_date="01.02.2026",
                end_date="02.02.2026", academic_year="2025-2026", term="GUZ",
                aliases='["büt"]',
            ),
        ])
        db.commit()

    entered, release = Event(), Event()
    original = calendar_retrieval._evidence_score

    def waiting_match(query, row):
        entered.set()
        assert release.wait(15)
        return original(query, row)

    monkeypatch.setattr(calendar_retrieval, "_evidence_score", waiting_match)
    result = []
    worker = Thread(target=lambda: result.append(
        pipeline._calendar_candidates("Güz bütünleme ne zaman?", None)
    ))
    worker.start()
    try:
        _wait_for(entered)
        assert all(engine.pool.checkedout() == 0 for engine in tiny_pools)
    finally:
        release.set()
        worker.join(15)
    assert not worker.is_alive()
    assert len(result[0].candidates) == 1
    assert isinstance(result[0].candidates[0], calendar_retrieval.CalendarRowSnapshot)


def test_calendar_compatibility_does_not_rollback_borrowed_session(
    tiny_pools,
):
    from core.database import AcademicCalendar, SessionLocal, SystemConfig
    from services import answer_pipeline as pipeline
    from services.calendar_retrieval import CalendarRowSnapshot

    with SessionLocal() as seed:
        seed.add_all([
            SystemConfig(key="ACADEMIC_CALENDAR_CURRENT_YEAR", value="2025-2026"),
            SystemConfig(key="ACADEMIC_CALENDAR_CURRENT_TERM", value="GUZ"),
            AcademicCalendar(
                period="Güz Dönemi", event="Bütünleme", start_date="01.02.2026",
                end_date="02.02.2026", academic_year="2025-2026", term="GUZ",
                aliases='["büt"]',
            ),
        ])
        seed.commit()

    with SessionLocal() as db:
        pending = SystemConfig(key="UNCOMMITTED_TEST_VALUE", value="preserve")
        db.add(pending)
        result = pipeline._calendar_candidates("Güz bütünleme ne zaman?", db)
        assert pending in db.new
        assert len(result.candidates) == 1
        assert isinstance(result.candidates[0], CalendarRowSnapshot)
        assert all(engine.pool.checkedout() == 0 for engine in tiny_pools)
        db.rollback()


def test_public_search_waits_without_a_request_db_connection(
    client, monkeypatch, tiny_pools,
):
    _seed_qna()
    admin, chat_engine = tiny_pools
    entered, release = Event(), Event()
    _wire(monkeypatch, Provider({"analyzer": (entered, release)}))
    result = []
    worker = Thread(target=lambda: result.append(
        client.get("/api/search", params={"q": "soru"})
    ))
    worker.start()
    try:
        _wait_for(entered)
        assert admin.pool.checkedout() == 0
        assert chat_engine.pool.checkedout() == 0
    finally:
        release.set()
        worker.join(15)
    assert not worker.is_alive()
    assert result[0].status_code == 200
    assert result[0].json()["status"] == "success"
