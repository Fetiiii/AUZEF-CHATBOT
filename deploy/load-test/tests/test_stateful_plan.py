"""Local fake-server checks; these tests never call a real provider or Compose."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock


HERE = Path(__file__).resolve().parents[1]


def load_plan_module():
    loader = importlib.machinery.SourceFileLoader("auzef_load_plan_test", str(HERE / "auzef-load-plan"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class WidgetHandler(BaseHTTPRequestHandler):
    lock = threading.Lock()
    seen = []
    next_id = 100

    def log_message(self, *_args):
        pass

    def do_POST(self):
        self.assert_path()
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        with self.lock:
            if body.get("conversation_id"):
                conv_id = body["conversation_id"]
                token = body["conversation_token"]
            else:
                type(self).next_id += 1
                conv_id = type(self).next_id
                token = f"secret-token-{conv_id}"
            type(self).seen.append((body["message"], conv_id, token, bool(body.get("conversation_id"))))
        response = json.dumps({"answer": "fixture answer", "message_id": conv_id,
                               "conversation_id": conv_id, "conversation_token": token}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def assert_path(self):
        if self.path != "/widget-chat":
            raise AssertionError(self.path)


class StatefulGeneratorTests(unittest.TestCase):
    def test_resource_overlay_is_rendered_from_one_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            contract = root / "contract.env"
            contract.write_text("AUZEF_EXPECT_MEMORY_BYTES=4294967296\n"
                                "AUZEF_EXPECT_MEMORY_SWAP_BYTES=8589934592\n"
                                "AUZEF_EXPECT_NANO_CPUS=2000000000\n")
            output = root / "overlay.yml"
            environment = os.environ.copy()
            environment["AUZEF_LOAD_CONTRACT_FILE"] = str(contract)
            result = subprocess.run([str(HERE / "auzef-load-render-overlay"), str(output)],
                                    text=True, capture_output=True, env=environment, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            rendered = output.read_text()
            self.assertIn('mem_limit: "4294967296"', rendered)
            self.assertIn('memswap_limit: "8589934592"', rendered)
            self.assertIn('cpus: "2"', rendered)
            self.assertIn('AUZEF_LOAD_METRICS: "1"', rendered)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)

    def test_followup_reuses_only_its_own_conversation_without_persisting_token(self):
        WidgetHandler.seen = []
        WidgetHandler.next_id = 100
        server = ThreadingHTTPServer(("127.0.0.1", 0), WidgetHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                profile = root / "workload.json"
                profile.write_text(json.dumps({"scenarios": [{"type": "context_followup", "turns": ["first fixture", "follow-up fixture"]}]}))
                result = subprocess.run([sys.executable, str(HERE / "auzef-load-run"),
                    "--url", f"http://127.0.0.1:{server.server_port}/widget-chat",
                    "--concurrency", "2", "--workload", str(profile), "--rounds", "2",
                    "--output-root", str(root / "runs"), "--confirm-load-test"],
                    text=True, capture_output=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                run_dir = next((root / "runs").iterdir())
                summary = json.loads((run_dir / "summary.json").read_text())
                records = [json.loads(line) for line in (run_dir / "client-results.jsonl").read_text().splitlines()]
                self.assertEqual(summary["request_count"], 8)
                self.assertEqual(summary["success_count"], 8)
                self.assertFalse(summary["p95_sample_sufficient"])
                self.assertEqual(len({row["request_id"] for row in records}), 8)
                self.assertEqual(sum(row["conversation_reused"] for row in records), 4)
                self.assertEqual(stat.S_IMODE((run_dir / "client-results.jsonl").stat().st_mode), 0o600)
                output = (run_dir / "client-results.jsonl").read_text()
                self.assertNotIn("secret-token", output)
                self.assertNotIn("first fixture", output)
                seen = WidgetHandler.seen
                self.assertEqual(len({conv for _, conv, _, reused in seen if not reused}), 4)
                for conv in {conv for _, conv, _, _ in seen}:
                    self.assertEqual(sum(item[1] == conv and item[3] for item in seen), 1)
        finally:
            server.shutdown()
            server.server_close()

    def test_plan_preflight_failure_sends_no_traffic(self):
        module = load_plan_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profile = root / "workload.json"
            profile.write_text(json.dumps({"scenarios": [{"type": "single_select", "turns": ["fixture"]}]}))
            failed = subprocess.CompletedProcess(["preflight"], 1, "FAIL memory mismatch\n", "")
            with mock.patch.object(module, "run", return_value=failed), \
                 mock.patch.object(module, "snapshot_environment", return_value={"git": {"commit": "fixture"}}), \
                 mock.patch.object(module.subprocess, "Popen", side_effect=AssertionError("traffic must not start")):
                code = module.main(["--url", "http://127.0.0.1/widget-chat", "--workload", str(profile),
                                    "--output-root", str(root / "results"), "--confirm-load-test"])
            self.assertEqual(code, 1)
            folder = next((root / "results").iterdir())
            self.assertEqual(json.loads((folder / "preflight.json").read_text())["result"], "FAIL")
            self.assertEqual(json.loads((folder / "stage-summary.json").read_text())["result"], "NO_GO")

    def test_trace_and_nginx_aggregation_keep_only_safe_fields(self):
        module = load_plan_module()
        trace = {"request": {"request_id": "trace-1", "conversation_id": 999},
                 "intent_analyzer": {"call_status": "success", "latency_ms": 100,
                                     "retry_count": None},
                 "retrieval": [{"retrieval_ms": 20, "qdrant_available": True, "meili_available": True}],
                 "selectors": [{"call_status": "semantic_none", "latency_ms": 40,
                                "retry_count": None, "semantic_none": True}],
                 "final": {"final_outcome": "no_answer"}, "degraded": [], "circuit": []}
        link = {"event": "trace_link", "request_id": "client-1", "trace_request_id": "trace-1"}
        phase = {"event": "phase_end", "request_id": "client-1", "phase": "embedding",
                 "duration_ms": 12.5, "active": 0, "error_type": None}
        threadpool = {"event": "threadpool", "request_id": "client-1", "point": "arrival",
                      "borrowed_tokens": 3, "total_tokens": 40}
        backend_lines = ("INFO AUZEF_LOAD_METRIC " + json.dumps(link) + "\n"
                         "INFO AUZEF_LOAD_METRIC " + json.dumps(phase) + "\n"
                         "INFO AUZEF_LOAD_METRIC " + json.dumps(threadpool) + "\n"
                         "INFO decision_trace=" + json.dumps(trace) + "\n"
                         "INFO private prompt must not be copied\n")
        nginx_line = json.dumps({"kind": "AUZEF_LOAD_NGINX", "request_id": "client-1",
                                 "status": 200, "request_time": "0.125",
                                 "upstream_response_time": "0.120", "upstream_addr": "backend:8000"}) + "\n"
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "client-results.jsonl").write_text(json.dumps({
                "request_id": "client-1", "stage": 15, "phase": "steady",
                "none_observed": None, "degraded_observed": None,
                "provider_failure_observed": None}) + "\n")
            def fake_run(command, **_kwargs):
                return subprocess.CompletedProcess(command, 0,
                    backend_lines if command[-1] == "backend-id" else nginx_line, "")
            with mock.patch.object(module, "run", side_effect=fake_run):
                provider, traces, links = module.collect_server_events(
                    folder, {"backend": "backend-id", "frontend": "frontend-id"}, "2026-09-24T00:00:00Z", {})
            join = module.correlate_client_results(folder, [], traces, links)
            self.assertEqual(join["trace_joined"], 1)
            record = json.loads((folder / "client-results.jsonl").read_text())
            self.assertTrue(record["none_observed"])
            self.assertFalse(record["degraded_observed"])
            self.assertEqual(provider["actual_sdk_retry_count"], None)
            self.assertEqual(module.summarize_nginx(folder)["groups"][0]["request_time"]["p50_ms"], 125.0)
            self.assertEqual(module.summarize_server_phases(folder)["groups"][0]["timing"]["p50_ms"], 12.5)
            self.assertEqual(module.summarize_server_phases(folder)["threadpool_samples"][0]["max_borrowed_tokens"], 3)
            output = "".join(path.read_text() for path in folder.iterdir() if path.is_file())
            self.assertNotIn("private prompt", output)
            self.assertNotIn("conversation_id", output)

    def test_plan_runs_warmup_and_steady_against_local_fixture(self):
        module = load_plan_module()
        WidgetHandler.seen = []
        WidgetHandler.next_id = 100
        server = ThreadingHTTPServer(("127.0.0.1", 0), WidgetHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        class FakeSampler:
            def __init__(self, folder, *_args):
                self.folder = folder
                self.stage = 0
                self.phase = "setup"
            def start(self):
                (self.folder / "resource-samples.jsonl").write_text(json.dumps({
                    "stage": 0, "phase": "setup", "cgroup": {"memory_current": 1},
                    "processes": [{"role": "worker", "pid": 1}]}) + "\n")
                (self.folder / "db-samples.jsonl").write_text(json.dumps({
                    "stage": 0, "phase": "setup", "status": "ok", "connection_count": 0}) + "\n")
            def close(self): pass
        class FakeWatcher:
            status = "OK"
            process = None
            def __init__(self, *_args):
                self.process = mock.Mock()
            def start(self): pass
            def close(self): pass
        def fake_collect(folder, _ids, _started, _env):
            rows = [json.loads(line) for line in (folder / "client-results.jsonl").read_text().splitlines()]
            (folder / "server-metrics.jsonl").write_text("".join(json.dumps({
                "event": "phase_end", "request_id": row["request_id"], "phase": "embedding",
                "duration_ms": 1, "active": 0}) + "\n" for row in rows))
            (folder / "nginx-metrics.jsonl").write_text("".join(json.dumps({
                "kind": "AUZEF_LOAD_NGINX", "request_id": row["request_id"],
                "status": 200, "request_time": "0.01", "upstream_response_time": "0.01",
                "upstream_addr": "fixture:8000"}) + "\n" for row in rows))
            traces = {row["request_id"]: {"selectors": [], "degraded_count": 0,
                       "analyzer_status": "success"} for row in rows}
            links = {row["request_id"]: row["request_id"] for row in rows}
            return {"counts": {"backend:request_arrival": len(rows), "frontend:200": len(rows)}}, traces, links
        try:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                profile = root / "workload.json"
                profile.write_text(json.dumps({"scenarios": [{"type": "single_select", "turns": ["fixture"]}]}))
                with mock.patch.object(module, "run", return_value=subprocess.CompletedProcess([], 0, "PASS preflight\n", "")), \
                     mock.patch.object(module, "snapshot_environment", return_value={"git": {"commit": "fixture"}}), \
                     mock.patch.object(module, "container_id", side_effect=lambda service, _env: service + "-id"), \
                     mock.patch.object(module, "Sampler", FakeSampler), \
                     mock.patch.object(module, "Watcher", FakeWatcher), \
                     mock.patch.object(module, "collect_server_events", side_effect=fake_collect):
                    code = module.main(["--url", f"http://127.0.0.1:{server.server_port}/widget-chat",
                                        "--stages", "1", "--workload", str(profile),
                                        "--output-root", str(root / "results"), "--confirm-load-test"])
                self.assertEqual(code, 0)
                folder = next((root / "results").iterdir())
                stages = json.loads((folder / "stage-summary.json").read_text())
                self.assertEqual(stages["result"], "PASS")
                self.assertEqual([(item["phase"], item["request_count"]) for item in stages["stages"]],
                                 [("warmup", 1), ("steady", 100)])
                self.assertEqual(stages["measurement_checks"]["trace_joined"], 101)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
