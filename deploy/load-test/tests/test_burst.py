"""Burst barrier, response-ID capture and DB orphan regression checks."""

import importlib.machinery
import importlib.util
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "auzef-load-burst"


def load_burst():
    name = "auzef_load_burst_test"
    loader = importlib.machinery.SourceFileLoader(name, str(SCRIPT))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


class WidgetHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert "message" in body
        payload = json.dumps({"answer": "fixture answer", "message_id": 701,
                              "conversation_id": 101, "conversation_token": "secret-token"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):
        pass


class BurstTests(unittest.TestCase):
    def test_response_id_opt_in_omits_token_and_question(self):
        module = load_burst()
        server = ThreadingHTTPServer(("127.0.0.1", 0), WidgetHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            target = module.client.parse_target(f"http://127.0.0.1:{server.server_port}/widget-chat")
            record, conversation = module.client.send_stateful_turn(
                target=target, message="private fixture", conversation=None,
                request_id="request-1", user_id=1, scenario="short", stage=15,
                phase="burst_r1", turn_index=1, timeout=3, tls_context=None,
                include_response_ids=True)
            self.assertEqual(record["response_conversation_id"], 101)
            self.assertEqual(record["response_message_id"], 701)
            self.assertEqual(conversation, (101, "secret-token"))
            raw = json.dumps(record)
            self.assertNotIn("secret-token", raw)
            self.assertNotIn("private fixture", raw)
        finally:
            server.shutdown()
            server.server_close()

    def test_reconcile_finds_botless_user_and_empty_conversation(self):
        module = load_burst()
        before = {"max_conversation_id": 100, "max_message_id": 700}
        conversations = [{"id": 101, "token_present": True}, {"id": 102, "token_present": True}]
        messages = [
            {"id": 701, "conversation_id": 101, "role": "user", "nonempty": True},
            {"id": 702, "conversation_id": 101, "role": "bot", "nonempty": True},
            {"id": 703, "conversation_id": 101, "role": "user", "nonempty": True},
            {"id": 704, "conversation_id": 101, "role": "bot", "nonempty": True},
            {"id": 705, "conversation_id": 102, "role": "user", "nonempty": True},
            {"id": 706, "conversation_id": 102, "role": "bot", "nonempty": True},
        ]
        records = [
            {"request_id": "seed", "kind": "seed", "response_contract_ok": True,
             "response_conversation_id": 101, "response_message_id": 702},
            {"request_id": "followup", "kind": "burst", "conversation_reused": True,
             "expected_conversation_id": 101, "response_contract_ok": True,
             "response_conversation_id": 101, "response_message_id": 704},
            {"request_id": "fresh", "kind": "burst", "conversation_reused": False,
             "expected_conversation_id": None, "response_contract_ok": True,
             "response_conversation_id": 102, "response_message_id": 706},
        ]
        after = {"max_conversation_id": 102, "max_message_id": 706,
                 "conversations": conversations, "messages": messages}
        good = module.reconcile(before, after, records, 2)
        self.assertTrue(good["lifecycle_pass"])
        self.assertEqual((good["written_user_turns"], good["written_bot_turns"]), (3, 3))

        orphan = dict(after, messages=[row for row in messages if row["id"] != 704])
        broken = module.reconcile(before, orphan, records, 2)
        self.assertFalse(broken["lifecycle_pass"])
        self.assertEqual(broken["unanswered_user_turns"], 1)
        self.assertEqual(broken["response_message_error_request_ids"], ["followup"])

        empty = dict(after, conversations=conversations + [{"id": 103, "token_present": True}])
        broken = module.reconcile(before, empty, records, 2)
        self.assertFalse(broken["lifecycle_pass"])
        self.assertEqual(broken["empty_conversations"], 1)
        self.assertEqual(broken["unexpected_conversation_ids"], [103])

    def test_final_status_separates_late_answer_from_lost_answer(self):
        module = load_burst()
        waves = [{"db": {"unanswered_user_turns": 2, "per_conversation": [
            {"conversation_id": 101}, {"conversation_id": 102}]}}]
        final = {"conversations": [{"id": 101, "token_present": True}, {"id": 102, "token_present": True}],
                 "messages": [
                     {"id": 1, "conversation_id": 101, "role": "user", "nonempty": True},
                     {"id": 2, "conversation_id": 102, "role": "user", "nonempty": True},
                     {"id": 3, "conversation_id": 101, "role": "bot", "nonempty": True}]}
        summary = module.final_turn_status(waves, final)
        self.assertEqual(waves[0]["db"]["unanswered_user_turns_final"], 1)
        self.assertEqual(waves[0]["db"]["late_bot_turns"], 1)
        self.assertEqual(summary["unanswered_user_turns_whole_run"], 1)

    def test_default_workload_covers_context_depth(self):
        module = load_burst()
        cases = module.load_profile(SCRIPT.parent / "burst-workload.json")
        setups = [len(case["setup"]) for case in cases if case["type"] == "context_followup"]
        self.assertGreaterEqual(len(setups), 3)
        self.assertGreater(max(setups), 200)

    def test_preflight_fail_sends_no_requests(self):
        module = load_burst()
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.object(module.plan, "run", return_value=subprocess.CompletedProcess([], 1, "FAIL fixture\n", "")), \
                 mock.patch.object(module.plan, "snapshot_environment", return_value={}), \
                 mock.patch.object(module, "send_case", side_effect=AssertionError("traffic sent")):
                result = module.main(["--url", "http://127.0.0.1/widget-chat", "--stages", "15",
                                      "--repeats", "1", "--output-root", temporary,
                                      "--confirm-load-test"])
            self.assertEqual(result, 1)
            folder = next(Path(temporary).iterdir())
            self.assertEqual(json.loads((folder / "burst-summary.json").read_text())["result"], "NO_GO")


if __name__ == "__main__":
    unittest.main()
