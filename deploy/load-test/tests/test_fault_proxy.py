"""TEST-ONLY fault-injection proxy: determinism, format, labelling, hygiene."""
import importlib.machinery
import importlib.util
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from argparse import Namespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "fault-proxy.py"


def load_proxy():
    loader = importlib.machinery.SourceFileLoader("fault_proxy_test", str(SCRIPT))
    spec = importlib.util.spec_from_loader("fault_proxy_test", loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules["fault_proxy_test"] = module
    loader.exec_module(module)
    return module


class Upstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        payload = ({"error": {"code": 429, "message": "real upstream"}} if body["model"] == "real/429"
                   else {"id": "g", "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]})
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class FaultProxyTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_proxy()
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.tmp = tempfile.TemporaryDirectory()
        self.servers = [self.upstream]

    def tearDown(self):
        for server in self.servers:
            server.shutdown()
            server.server_close()
        self.tmp.cleanup()

    def start(self, **overrides):
        args = Namespace(port=0, upstream=f"http://127.0.0.1:{self.upstream.server_port}", rate_429=0.0,
                         hang_rate=0.0, hang_seconds=0.2, seed=7, models=[], upstream_timeout=5.0,
                         log=str(Path(self.tmp.name) / "log.jsonl"))
        for key, value in overrides.items():
            setattr(args, key, value)
        injector = self.mod.Injector(args.seed, args.rate_429, args.hang_rate, args.models,
                                     getattr(args, "only_max_tokens", None), getattr(args, "pattern", None))
        log = open(args.log, "a", encoding="utf-8")
        server = ThreadingHTTPServer(("127.0.0.1", 0), self.mod.make_handler(args, injector, threading.Lock(), log))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.servers.append(server)
        return server.server_port, args

    def post(self, port, model):
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/v1/chat/completions",
                                     data=json.dumps({"model": model, "max_tokens": 32}).encode(),
                                     headers={"Content-Type": "application/json",
                                              "Authorization": "Bearer secret-token-xyz"}, method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())

    def records(self, args):
        return [json.loads(l) for l in Path(args.log).read_text().splitlines() if l.strip()]

    def test_seeded_injection_is_reproducible_and_well_formed(self):
        port, args = self.start(rate_429=0.3, seed=11)
        results = [self.post(port, "m")[1] for _ in range(40)]
        injected = [("error" in r) for r in results]
        reference = self.mod.Injector(11, 0.3, 0.0, [])
        expected = [reference.decide("m")[1] for _ in range(40)]
        self.assertEqual(injected, expected)
        self.assertTrue(0 < sum(injected) < 40)
        first = next(r for r in results if "error" in r)
        self.assertEqual(first, {"error": {"code": 429, "message": "injected rate limit"}})

    def test_real_upstream_429_is_passed_through_and_labelled(self):
        port, args = self.start(rate_429=0.0)
        status, body = self.post(port, "real/429")
        self.assertEqual((status, body["error"]["code"]), (200, 429))
        rec = self.records(args)[-1]
        self.assertFalse(rec["injected_429"])
        self.assertEqual(rec["upstream_body_error_code"], 429)

    def test_model_filter_limits_injection(self):
        port, _ = self.start(rate_429=1.0, models=["target/model"])
        self.assertIn("error", self.post(port, "target/model")[1])
        self.assertIn("choices", self.post(port, "other/model")[1])

    def test_pattern_replay_and_max_tokens_filter(self):
        port, _ = self.start(pattern=[True, False, False], only_max_tokens=32)
        got = ["error" in self.post(port, "m")[1] for _ in range(6)]
        self.assertEqual(got, [True, False, False, True, False, False])
        payload = json.dumps({"model": "m", "max_tokens": 300}).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/v1/chat/completions", payload,
                                     {"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertIn("choices", json.loads(resp.read()))

    def test_hang_delays_the_answer(self):
        port, args = self.start(hang_rate=1.0, hang_seconds=0.3)
        started = time.perf_counter()
        self.post(port, "m")
        self.assertGreaterEqual(time.perf_counter() - started, 0.3)
        self.assertTrue(self.records(args)[-1]["injected_hang"])

    def test_log_has_no_secrets_or_content(self):
        port, args = self.start(rate_429=0.5)
        for _ in range(5):
            self.post(port, "m")
        text = Path(args.log).read_text()
        self.assertNotIn("secret-token-xyz", text)
        self.assertNotIn("Authorization", text)
        self.assertNotIn("content", text)


if __name__ == "__main__":
    unittest.main()
