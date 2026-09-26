"""Incident class: user turn committed -> selector provider hangs.

Before the application-level deadline the selector call waited on the SDK
default (600 s), nginx cut the request at 120 s (503) and no bot turn was
written. Now the adapter must end the selector invocation within its logical
deadline, the pipeline must take the per-intent deterministic degraded path,
and the widget must write the bot turn.
"""
import json
import time
from types import SimpleNamespace

import openai
import httpx
import pytest

import services.llm_config as llm_config
from services.llm_provider import OpenAIProvider

ANALYSIS = {"intent_count": 1, "intents": [{
    "source_text": "soru", "normalized_text": "soru", "resolved_text": "soru",
    "context_used": False, "calendar_relevant": False}]}


class HangingSelectorClient:
    max_retries = 2

    def __init__(self):
        self.selector_attempts = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def with_options(self, **_):
        return self

    def create(self, **kwargs):
        if kwargs["max_tokens"] == 32:          # selector call: never answers
            self.selector_attempts.append(kwargs["timeout"])
            time.sleep(kwargs["timeout"])
            raise openai.APITimeoutError(request=httpx.Request("POST", "https://x.invalid"))
        return SimpleNamespace(id="a", model="m", model_extra={}, usage=None, choices=[
            SimpleNamespace(message=SimpleNamespace(content=json.dumps(ANALYSIS)), finish_reason="stop")])


@pytest.fixture
def wired(monkeypatch):
    from services import answer_pipeline as pipeline
    from core.database import QnA, SessionLocal

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    # Small budget so the test runs in ~1 s; the invariant is the same.
    monkeypatch.setitem(llm_config.DEFAULT_ATTEMPT_TIMEOUT_SECONDS, llm_config.LLMCapability.SELECTOR, 0.4)
    monkeypatch.setitem(llm_config.LOGICAL_DEADLINE_SECONDS, llm_config.LLMCapability.SELECTOR, 1.0)
    provider = OpenAIProvider()
    provider.client = HangingSelectorClient()
    monkeypatch.setattr(pipeline, "resolve_llm_request_state", lambda _db: (True, provider, None))
    hit = {"qna_id": 1, "question": "soru", "answer": "cevap", "score": 0.99, "source": "qdrant"}
    monkeypatch.setattr(pipeline.QDRANT_PROVIDER, "search", lambda _q, limit=24: [hit])
    monkeypatch.setattr(pipeline, "meili_search_safe", lambda _q, limit=5: [])
    monkeypatch.setattr(pipeline, "meili_is_available", lambda: True)
    with SessionLocal() as db:
        db.add(QnA(id=1, question_text="soru", answer_text="cevap", status=1))
        db.commit()
    return provider


def test_selector_hang_yields_controlled_reply_and_bot_turn(client, wired):
    from core.database import ConversationMessage, SessionLocal

    started = time.perf_counter()
    response = client.post("/widget-chat", json={"message": "soru"})
    elapsed = time.perf_counter() - started

    assert response.status_code == 200
    assert elapsed < 5, "request must end on the backend deadline, not on nginx"
    body = response.json()
    assert body["answer"]                                   # deterministic degraded answer
    # The selector was attempted and cut by the logical deadline (not the SDK default).
    assert wired.client.selector_attempts and max(wired.client.selector_attempts) <= 0.4 + 1e-6
    with SessionLocal() as db:
        rows = (db.query(ConversationMessage)
                .filter(ConversationMessage.conversation_id == body["conversation_id"])
                .order_by(ConversationMessage.id).all())
    assert [row.role for row in rows] == ["user", "bot"]    # no orphan user turn
    assert rows[1].content.strip()
