#!/usr/bin/env python3
"""Orchestrator (Spark) heartbeat tests (stdlib unittest only).

Covers the heartbeat record builder, the alive/dead status view, the
6-minute dead threshold, and persistence to data/orchestrator.json.

Run:  python3 -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("MUSE_PWA_TOKEN", "test-operator-token")
os.environ.setdefault("MUSE_PWA_ORCH_TOKEN", "test-orchestrator-token")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="muse-pwa-orch-tests-"))
server.DATA = TMP  # keep the real repo's data/ untouched


class RecordHeartbeatTest(unittest.TestCase):
    def test_explicit_timestamp_and_status(self):
        r = server.record_orchestrator_heartbeat(1000, "ok", now=1100)
        self.assertEqual(r["last_heartbeat"], 1000)
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["updated_at"], 1100)

    def test_missing_timestamp_defaults_to_now(self):
        r = server.record_orchestrator_heartbeat(None, "ok", now=1100)
        self.assertEqual(r["last_heartbeat"], 1100)

    def test_garbage_timestamp_defaults_to_now(self):
        r = server.record_orchestrator_heartbeat("not-a-time", "", now=1100)
        self.assertEqual(r["last_heartbeat"], 1100)
        self.assertEqual(r["status"], "ok")

    def test_status_truncated(self):
        r = server.record_orchestrator_heartbeat(1000, "x" * 200, now=1100)
        self.assertLessEqual(len(r["status"]), 80)


class OrchestratorStatusTest(unittest.TestCase):
    def test_fresh_heartbeat_is_alive(self):
        st = server.orchestrator_status({"last_heartbeat": 1000}, now=1100)
        self.assertEqual(st["status"], "alive")
        self.assertEqual(st["last_heartbeat"], 1000)
        self.assertEqual(st["seconds_since"], 100)

    def test_stale_over_six_minutes_is_dead(self):
        st = server.orchestrator_status({"last_heartbeat": 1000}, now=1000 + 361)
        self.assertEqual(st["status"], "dead")
        self.assertEqual(st["seconds_since"], 361)

    def test_boundary_at_six_minutes_is_alive(self):
        st = server.orchestrator_status({"last_heartbeat": 1000}, now=1000 + 360)
        self.assertEqual(st["status"], "alive")

    def test_never_seen_is_dead(self):
        for record in (None, {}, {"last_heartbeat": None}):
            st = server.orchestrator_status(record, now=1000)
            self.assertEqual(st["status"], "dead")
            self.assertIsNone(st["last_heartbeat"])
            self.assertIsNone(st["seconds_since"])

    def test_reported_status_surfaced(self):
        st = server.orchestrator_status(
            {"last_heartbeat": 1000, "status": "draining"}, now=1100)
        self.assertEqual(st["reported_status"], "draining")


class OrchestratorPersistenceTest(unittest.TestCase):
    def test_heartbeat_survives_as_json_file(self):
        record = server.record_orchestrator_heartbeat(2000, "ok", now=2050)
        server.save_json("orchestrator.json", record)
        reloaded = server.load_json("orchestrator.json", {})
        self.assertEqual(reloaded["last_heartbeat"], 2000)
        st = server.orchestrator_status(reloaded, now=2050)
        self.assertEqual(st["status"], "alive")

    def test_dead_threshold_is_six_minutes(self):
        self.assertEqual(server.ORCH_HEARTBEAT_DEAD, 360)


if __name__ == "__main__":
    unittest.main()
