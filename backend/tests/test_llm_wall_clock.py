"""Wall-clock bound per attempt through the REAL OpenAI SDK (local server).

httpx's timeout is an inactivity timeout; a trickling body would never trip
it. The adapter reads via the SDK streaming-response interface and aborts at
the attempt deadline, so the logical deadline holds wall-clock.
"""
import json
import threading
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import services.llm_config as llm_config
import services.llm_provider as llm_provider
from services.llm_config import LLMCapability
from services.llm_types import LLMOutcomeStatus

OK = json.dumps({"id": "gen-1", "object": "chat.completion", "created": 1, "model": "prov/model",
                 "usage": {"prompt_tokens": 11, "completion_tokens": 2},
                 "choices": [{"index": 0, "finish_reason": "stop",
                              "message": {"role": "assistant", "content": "{\"ok\":1}"}}]}).encode()


class Behaviour:
    mode = "ok"
    hits = 0


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        Behaviour.hits += 1
        mode = Behaviour.mode
        body = {"ok": OK, "body429": b'{"error":{"code":429,"message":"x"}}', "garbage": b"<html>"}.get(mode, OK)
        pad = 12 if mode == "trickle" else 0
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(pad + len(body)))
            self.end_headers()
            self.wfile.flush()
            for _ in range(pad):
                time.sleep(0.25)
                self.wfile.write(b" ")
                self.wfile.flush()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setitem(llm_config.DEFAULT_ATTEMPT_TIMEOUT_SECONDS, LLMCapability.SELECTOR, 0.6)
    monkeypatch.setitem(llm_config.LOGICAL_DEADLINE_SECONDS, LLMCapability.SELECTOR, 1.5)
    monkeypatch.setattr(llm_provider, "_sleep", lambda s: time.sleep(min(s, 0.05)))
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    p = llm_provider._OpenAICompatibleProvider(
        model="prov/model", api_key="test-key-not-real",
        base_url=f"http://127.0.0.1:{server.server_port}/v1", provider_name="openrouter")
    yield p
    server.shutdown()
    server.server_close()


def invoke(p, mode):
    Behaviour.mode, Behaviour.hits = mode, 0
    config = replace(p.effective_config(LLMCapability.SELECTOR), model="prov/model")
    started = time.perf_counter()
    result = p._invoke("s", "u", config)
    return result, time.perf_counter() - started


def test_success_is_parsed_through_the_streaming_path(provider):
    r, _ = invoke(provider, "ok")
    assert r.status is LLMOutcomeStatus.SUCCESS and r.text == '{"ok":1}'
    assert r.metadata.actual_model == "prov/model" and r.metadata.input_tokens == 11
    assert r.metadata.finish_reason == "stop" and r.metadata.retry_count == 0


def test_trickling_body_is_cut_at_the_logical_deadline(provider):
    r, elapsed = invoke(provider, "trickle")
    assert r.status is LLMOutcomeStatus.TIMEOUT and r.failure_category == "TIMEOUT"
    assert elapsed <= 1.5 + 0.3                  # 12 x 0.25 s = 3 s of trickle otherwise
    assert r.error_type == "AttemptWallClockTimeout"


def test_body_429_via_streaming_path_is_rate_limit(provider):
    r, _ = invoke(provider, "body429")
    assert r.failure_category == "RATE_LIMIT" and r.metadata.retry_count >= 1
    assert Behaviour.hits == r.metadata.retry_count + 1


def test_non_json_body_is_unknown_and_not_retried(provider):
    r, _ = invoke(provider, "garbage")
    assert r.status is LLMOutcomeStatus.MODEL_ERROR and r.failure_category == "UNKNOWN"
    assert Behaviour.hits == 1
