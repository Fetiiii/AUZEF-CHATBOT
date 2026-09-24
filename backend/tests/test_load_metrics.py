"""Opt-in measurement must preserve sync response behavior and omit content."""

import json
import logging

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from services import load_metrics


def _test_app():
    app = FastAPI()

    @app.post("/widget-chat")
    def chat(request: Request):
        request.scope.setdefault("state", {})["load_handler_started"] = True
        load_metrics.chat_started()
        with load_metrics.Timer("embedding"):
            return {"answer": "fixed fixture"}

    app.add_middleware(load_metrics.LoadMetricsMiddleware)
    return app


def test_opt_in_metrics_correlate_sync_handler_without_content(monkeypatch, caplog):
    monkeypatch.setattr(load_metrics, "ENABLED", True)
    caplog.set_level(logging.INFO, logger="auzef.load_metrics")
    with TestClient(_test_app()) as client:
        response = client.post("/widget-chat", json={"message": "private fixture"},
                               headers={"X-AUZEF-Load-Request-ID": "test-request-1"})
    assert response.status_code == 200
    assert response.json() == {"answer": "fixed fixture"}
    payloads = [json.loads(record.message.split("AUZEF_LOAD_METRIC ", 1)[1])
                for record in caplog.records if "AUZEF_LOAD_METRIC " in record.message]
    assert {item["event"] for item in payloads} >= {
        "request_arrival", "handler_start", "phase_start", "phase_end", "handler_finish", "threadpool"
    }
    assert {item["point"] for item in payloads if item["event"] == "threadpool"} == {
        "arrival", "response_start"
    }
    assert all(item["borrowed_tokens"] <= item["total_tokens"]
               for item in payloads if item["event"] == "threadpool")
    assert all(item["request_id"] == "test-request-1" for item in payloads)
    assert "private fixture" not in caplog.text
    assert "fixed fixture" not in caplog.text


def test_metrics_off_emits_nothing(monkeypatch, caplog):
    monkeypatch.setattr(load_metrics, "ENABLED", False)
    caplog.set_level(logging.INFO, logger="auzef.load_metrics")
    with TestClient(_test_app()) as client:
        response = client.post("/widget-chat", json={"message": "private fixture"})
    assert response.json() == {"answer": "fixed fixture"}
    assert "AUZEF_LOAD_METRIC" not in caplog.text
