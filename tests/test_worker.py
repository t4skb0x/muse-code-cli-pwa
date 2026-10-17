#!/usr/bin/env python3
"""Worker helper tests (stdlib unittest only — no dependencies).

Imports worker.py (import-safe: the poll loop lives in main()) with a
stubbed api() and verifies the timeout partial-output path and the
heartbeat poster.

Run:  python3 -m unittest discover -s tests -v
"""
import os
import subprocess
import sys
import threading
import unittest
from pathlib import Path

os.environ.setdefault("MUSE_PWA_TOKEN", "test-operator-token")
os.environ.setdefault("MUSE_PWA_ORCH_TOKEN", "test-orchestrator-token")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import worker  # noqa: E402


class TimeoutTest(unittest.TestCase):
    def test_partial_output_decodes_bytes(self):
        try:
            worker.sh(["bash", "-c", "echo PARTIAL-OUT; sleep 5"],
                      "/tmp", timeout=1)
            self.fail("no timeout raised")
        except subprocess.TimeoutExpired as e:
            # text=True runs still deliver bytes on timeout — must decode.
            self.assertIn("PARTIAL-OUT", worker._partial_output(e))

    def test_timed_out_posts_failed_with_partial(self):
        calls = []
        orig, worker.api = worker.api, lambda path, body=None: calls.append((path, body))
        try:
            worker.timed_out("t9", 900, "PARTIAL-OUT")
        finally:
            worker.api = orig
        path, body = calls[-1]
        self.assertEqual(path, "/api/tasks/update")
        self.assertEqual(body["id"], "t9")
        self.assertEqual(body["status"], "failed")
        self.assertIn("PARTIAL-OUT", body["result"])


class HeartbeatTest(unittest.TestCase):
    def test_heartbeat_forever_posts_until_stopped(self):
        calls = []
        orig_api, worker.api = worker.api, lambda path, body=None: calls.append((path, body))
        orig_iv, worker.HEARTBEAT_INTERVAL = worker.HEARTBEAT_INTERVAL, 0.01
        stop = threading.Event()
        try:
            th = threading.Thread(target=worker.heartbeat_forever,
                                  args=("t1", stop), daemon=True)
            th.start()
            th.join(timeout=5)
            stop.set()
            th.join(timeout=5)
        finally:
            worker.api = orig_api
            worker.HEARTBEAT_INTERVAL = orig_iv
        self.assertTrue(calls, "no heartbeat posted")
        path, body = calls[0]
        self.assertEqual(path, "/api/tasks/heartbeat")
        self.assertEqual(body["id"], "t1")
        self.assertTrue(body["worker"])


if __name__ == "__main__":
    unittest.main()
