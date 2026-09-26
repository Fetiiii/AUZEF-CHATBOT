#!/usr/bin/env python3
"""TEST-ONLY fault-injection proxy for OpenAI-compatible chat completions.

Sits between the backend and the real upstream (OpenRouter) on a loopback
port. Every POST is forwarded upstream unchanged; afterwards, a deterministic
(seeded) fraction of SUCCESSFUL upstream answers is rewritten to

    HTTP 200  {"error": {"code": 429, "message": "injected rate limit"}}

and an optional fraction of requests is held for ``--hang-seconds`` before
being answered (to exercise client timeouts). Injection can be limited to
given model ids. The backend reaches it only through the gated test hook
AUZEF_TEST_OPENROUTER_BASE_URL (+ AUZEF_LOAD_METRICS=1).

Logged per request (JSONL, no content, no headers, no keys): index, model,
max_tokens, injected_429, injected_hang, upstream status, upstream body-error
code (real upstream 429s stay distinguishable), durations.

Usage:
  python fault-proxy.py --port 18080 --rate-429 0.3 --seed 20260926 \
      --models openai/gpt-6-luna --log /tmp/fault-proxy.jsonl \
      [--hang-rate 0.2 --hang-seconds 40] [--upstream https://openrouter.ai]
"""
import argparse
import json
import random
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FORWARD_HEADERS = ("authorization", "content-type", "http-referer", "x-title", "accept", "user-agent")
INJECTED = {"error": {"code": 429, "message": "injected rate limit"}}


class Injector:
    """Deterministic decisions: request i always gets the same draw for a seed."""

    def __init__(self, seed, rate_429, hang_rate, models, only_max_tokens=None, pattern=None):
        self.rng = random.Random(seed)
        self.rate_429, self.hang_rate = rate_429, hang_rate
        self.models = set(models or [])
        self.only_max_tokens = only_max_tokens
        # Optional replay of a recorded reject sequence (True = reject); applied
        # to eligible requests in arrival order, cycling. Overrides rate_429.
        self.pattern = list(pattern or [])
        self.lock = threading.Lock()
        self.index = 0
        self.eligible_index = 0

    def decide(self, model, max_tokens=None):
        eligible = (not self.models or model in self.models) and (
            self.only_max_tokens is None or max_tokens == self.only_max_tokens)
        with self.lock:
            self.index += 1
            draw_429, draw_hang = self.rng.random(), self.rng.random()
            index = self.index
            if eligible:
                self.eligible_index += 1
            slot = self.eligible_index
        if self.pattern and eligible:
            inject_429 = self.pattern[(slot - 1) % len(self.pattern)]
        else:
            inject_429 = eligible and draw_429 < self.rate_429
        return index, inject_429, eligible and draw_hang < self.hang_rate


def make_handler(args, injector, log_lock, log_file):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_):
            pass

        def _write(self, status, payload):
            raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass                       # client gave up (timeout) - expected for hangs

        def do_POST(self):
            started = time.perf_counter()
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            try:
                request_json = json.loads(body or b"{}")
            except ValueError:
                request_json = {}
            model = request_json.get("model")
            index, inject_429, inject_hang = injector.decide(model, request_json.get("max_tokens"))
            if inject_hang:
                time.sleep(args.hang_seconds)
            headers = {k: v for k, v in self.headers.items() if k.lower() in FORWARD_HEADERS}
            upstream_req = urllib.request.Request(args.upstream.rstrip("/") + self.path, data=body,
                                                  headers=headers, method="POST")
            upstream_status, upstream_error, payload = None, None, b""
            try:
                with urllib.request.urlopen(upstream_req, timeout=args.upstream_timeout) as resp:
                    upstream_status, payload = resp.status, resp.read()
            except urllib.error.HTTPError as err:
                upstream_status, payload = err.code, err.read()
            except Exception as exc:  # network failure -> 502 to the client
                upstream_status, payload = 502, json.dumps({"error": {"code": 502, "message": type(exc).__name__}}).encode()
            try:
                parsed = json.loads(payload or b"{}")
                if isinstance(parsed.get("error"), dict):
                    upstream_error = parsed["error"].get("code")
                has_choices = bool(parsed.get("choices"))
            except ValueError:
                has_choices = False
            injected = inject_429 and upstream_status == 200 and has_choices
            upstream_ms = round((time.perf_counter() - started) * 1000, 1)
            self._write(200 if injected else upstream_status, INJECTED if injected else payload)
            record = {"i": index, "t": round(time.time(), 3), "model": model,
                      "max_tokens": request_json.get("max_tokens"),
                      "injected_429": injected, "injected_hang": inject_hang,
                      "upstream_status": upstream_status, "upstream_body_error_code": upstream_error,
                      "duration_ms": upstream_ms}
            with log_lock:
                log_file.write(json.dumps(record) + "\n")
                log_file.flush()

    return Handler


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=18080)
    p.add_argument("--upstream", default="https://openrouter.ai")
    p.add_argument("--rate-429", type=float, default=0.0)
    p.add_argument("--hang-rate", type=float, default=0.0)
    p.add_argument("--hang-seconds", type=float, default=40.0)
    p.add_argument("--seed", type=int, default=20260926)
    p.add_argument("--models", nargs="*", default=[])
    p.add_argument("--only-max-tokens", type=int, default=None)
    p.add_argument("--pattern-file", default=None,
                   help="JSON list of booleans (True = inject 429), replayed in eligible-request order")
    p.add_argument("--upstream-timeout", type=float, default=100.0)
    p.add_argument("--log", required=True)
    args = p.parse_args()
    if not (0 <= args.rate_429 <= 1 and 0 <= args.hang_rate <= 1):
        raise SystemExit("rates must be within [0, 1]")
    pattern = json.load(open(args.pattern_file)) if args.pattern_file else None
    injector = Injector(args.seed, args.rate_429, args.hang_rate, args.models, args.only_max_tokens, pattern)
    log_file = open(args.log, "a", encoding="utf-8")
    log_file.write(json.dumps({"config": {k: v for k, v in vars(args).items() if k != "log"},
                               "started": round(time.time(), 3)}) + "\n")
    log_file.flush()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(args, injector, threading.Lock(), log_file))
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    main()
