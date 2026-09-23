"""Local HTTP tests for the stdlib load generator; never contacts AUZEF."""

from __future__ import annotations

import importlib.machinery
import importlib.util
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest


RUNNER = Path(__file__).resolve().parents[1] / "auzef-load-run"
SECRET_RESPONSE = "SUPER_SECRET_TEST_VALUE"
SECRET_QUESTION = "SECRET_QUESTION_TEST_VALUE"


class FakeState:
    def __init__(self, mode: str, wave_size: int = 0):
        self.mode = mode
        self.wave_size = wave_size
        self.lock = threading.Lock()
        self.release = threading.Event()
        self.seen = 0
        self.active = 0
        self.peak = 0
        self.payloads: list[dict] = []


class FakeWidgetHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        payload = json.loads(body)
        state = self.server.state
        with state.lock:
            state.seen += 1
            ordinal = state.seen
            state.active += 1
            state.peak = max(state.peak, state.active)
            state.payloads.append(payload)
            if state.wave_size and state.active >= state.wave_size:
                state.release.set()
        try:
            if state.wave_size:
                state.release.wait(timeout=5)
                time.sleep(0.03)
            if state.mode == "slow":
                time.sleep(0.2)
            if state.mode == "503":
                code, response = 503, b'{"detail":"unavailable"}'
            elif state.mode == "429":
                code, response = 429, b'{"detail":"rate limited"}'
            elif state.mode == "malformed":
                code, response = 200, b"not-json"
            elif state.mode == "missing_id":
                code, response = 200, b'{"answer":"cevap"}'
            elif state.mode == "missing_answer":
                code, response = 200, b'{"message_id":42}'
            elif state.mode == "mixed":
                code = 429 if ordinal == 1 else 503
                response = b'{"detail":"unavailable"}'
            elif state.mode == "redirect":
                code, response = 307, b"redirect"
            else:
                code = 200
                response = json.dumps({
                    "answer": "cevap", "message_id": 42, "debug": SECRET_RESPONSE,
                }).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            if state.mode == "redirect":
                self.send_header("Location", "/widget-chat")
            self.end_headers()
            try:
                self.wfile.write(response)
            except BrokenPipeError:
                pass  # deliberate client timeout in the local test
        finally:
            with state.lock:
                state.active -= 1


class LoadRunTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def run_case(self, mode: str, *, concurrency: int = 2, requests: int = 2,
                 wave_size: int = 0, extra: tuple[str, ...] = ()):
        state = FakeState(mode, wave_size)
        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeWidgetHandler)
        server.state = state
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            command = [
                sys.executable, str(RUNNER), "--url",
                f"http://127.0.0.1:{server.server_port}/widget-chat",
                "--concurrency", str(concurrency), "--requests", str(requests),
                "--timeout", "3", "--output-root", str(self.root / "runs"),
                "--confirm-load-test", *extra,
            ]
            process = subprocess.run(command, capture_output=True, text=True, timeout=15)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        run_dirs = list((self.root / "runs").glob("*"))
        self.assertEqual(len(run_dirs), 1, process.stderr)
        run_dir = run_dirs[0]
        summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
        return process, summary, run_dir, state

    def test_all_success_and_sensitive_output(self):
        questions = self.root / "questions.txt"
        questions.write_text(f"# comment\n{SECRET_QUESTION}\n\nikinci sentetik soru\n", encoding="utf-8")
        process, summary, run_dir, state = self.run_case(
            "success", concurrency=5, requests=10,
            extra=("--questions", str(questions)),
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(summary["result"], "PASS")
        self.assertEqual(summary["status_counts"], {"200": 10})
        self.assertEqual(summary["valid_json"], 10)
        self.assertEqual(summary["answer_present"], 10)
        self.assertEqual(summary["message_id_present"], 10)
        self.assertEqual([item["message"] for item in state.payloads].count(SECRET_QUESTION), 5)
        self.assertTrue(all(set(item) == {"message"} for item in state.payloads))
        self.assertEqual(stat.S_IMODE(run_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((run_dir / "summary.json").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((run_dir / "requests.csv").stat().st_mode), 0o600)
        output = process.stdout + process.stderr
        for filename in ("summary.json", "requests.csv"):
            output += (run_dir / filename).read_text(encoding="utf-8")
        self.assertNotIn(SECRET_RESPONSE, output)
        self.assertNotIn(SECRET_QUESTION, output)
        self.assertEqual(summary["launch_skew_ms"] >= 0, True)

    def test_503_is_failure(self):
        process, summary, _, _ = self.run_case("503", requests=3)
        self.assertEqual(process.returncode, 1)
        self.assertEqual(summary["status_counts"], {"503": 3})
        self.assertEqual(summary["result"], "FAIL")

    def test_429_is_rate_limited_not_backend_5xx(self):
        process, summary, _, _ = self.run_case("429", requests=3)
        self.assertEqual(process.returncode, 2)
        self.assertEqual(summary["status_counts"], {"429": 3})
        self.assertEqual(summary["result"], "RATE_LIMITED")
        self.assertNotIn("503", summary["status_counts"])

    def test_malformed_200_is_failure(self):
        process, summary, _, _ = self.run_case("malformed")
        self.assertEqual(process.returncode, 1)
        self.assertEqual(summary["status_counts"], {"200": 2})
        self.assertEqual(summary["http_200_malformed"], 2)
        self.assertEqual(summary["result"], "FAIL")

    def test_missing_message_id_is_failure(self):
        process, summary, _, _ = self.run_case("missing_id")
        self.assertEqual(process.returncode, 1)
        self.assertEqual(summary["answer_present"], 2)
        self.assertEqual(summary["message_id_present"], 0)
        self.assertEqual(summary["http_200_missing_message_id"], 2)

    def test_missing_answer_is_failure(self):
        process, summary, _, _ = self.run_case("missing_answer")
        self.assertEqual(process.returncode, 1)
        self.assertEqual(summary["answer_present"], 0)
        self.assertEqual(summary["http_200_missing_answer"], 2)

    def test_transport_timeout_is_separate(self):
        process, summary, _, _ = self.run_case(
            "slow", extra=("--timeout", "0.05"),
        )
        self.assertEqual(process.returncode, 1)
        self.assertEqual(summary["transport_timeouts"], 2)
        self.assertEqual(summary["transport_errors"], 0)
        self.assertEqual(summary["http_responses"], 0)

    def test_5xx_takes_priority_over_429(self):
        process, summary, _, _ = self.run_case("mixed")
        self.assertEqual(process.returncode, 1)
        self.assertEqual(summary["result"], "FAIL")
        self.assertEqual(summary["status_counts"], {"429": 1, "503": 1})

    def test_first_wave_really_overlaps(self):
        process, summary, _, state = self.run_case(
            "success", concurrency=5, requests=10, wave_size=5,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertGreaterEqual(state.peak, 5)
        self.assertLess(summary["launch_skew_ms"], 2000)
        self.assertEqual(summary["completed_requests"], 10)

    def test_redirect_is_reported_without_following(self):
        process, summary, _, state = self.run_case("redirect", requests=1)
        self.assertEqual(process.returncode, 1)
        self.assertEqual(summary["status_counts"], {"307": 1})
        self.assertEqual(state.seen, 1)

    def test_nearest_rank_percentiles(self):
        loader = importlib.machinery.SourceFileLoader("auzef_load_run", str(RUNNER))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        loader.exec_module(module)
        values = list(range(1, 21))
        self.assertEqual(module.nearest_rank(values, 50), 10)
        self.assertEqual(module.nearest_rank(values, 95), 19)
        self.assertEqual(module.nearest_rank(values, 99), 20)

    def test_confirmation_and_front_door_guards(self):
        cases = [
            ["--url", "http://127.0.0.1/widget-chat", "--concurrency", "1", "--requests", "1"],
            ["--url", "http://backend:8000/widget-chat", "--concurrency", "1", "--requests", "1", "--confirm-load-test"],
            ["--url", "https://user:password@example.test/widget-chat", "--concurrency", "1", "--requests", "1", "--confirm-load-test"],
            ["--url", "https://example.test/widget-chat?secret=value", "--concurrency", "1", "--requests", "1", "--confirm-load-test"],
            ["--url", "https://example.test/widget-chat", "--concurrency", "31", "--requests", "31", "--confirm-load-test"],
        ]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                process = subprocess.run([sys.executable, str(RUNNER), *arguments], capture_output=True, text=True, timeout=5)
                self.assertEqual(process.returncode, 3)
                self.assertNotIn("password", process.stdout + process.stderr)
                self.assertNotIn("secret=value", process.stdout + process.stderr)

    def test_unwritable_output_path_stops_before_traffic(self):
        file_path = self.root / "not-a-directory"
        file_path.write_text("existing file", encoding="utf-8")
        process = subprocess.run([
            sys.executable, str(RUNNER), "--url", "http://127.0.0.1:1/widget-chat",
            "--concurrency", "1", "--requests", "1", "--confirm-load-test",
            "--output-root", str(file_path),
        ], capture_output=True, text=True, timeout=5)
        self.assertEqual(process.returncode, 3)
        self.assertIn("no traffic sent", process.stderr)


if __name__ == "__main__":
    unittest.main()
