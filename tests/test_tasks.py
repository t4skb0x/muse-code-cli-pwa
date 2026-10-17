#!/usr/bin/env python3
"""Task orchestration tests (stdlib unittest only — no dependencies).

Covers the server's task helpers: timeout sweep, heartbeat aliveness,
priority claim order, retry, progress, elapsed/timeout enrichment,
pagination, filtering, and worker summaries.

Run:  python3 -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("MUSE_PWA_TOKEN", "test-operator-token")
os.environ.setdefault("MUSE_PWA_ORCH_TOKEN", "test-orchestrator-token")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="muse-pwa-tests-"))
server.DATA = TMP  # keep the real repo's data/ untouched


def make_task(i, status="queued", **kw):
    t = {"id": f"t{i}", "ts": "2026-10-17T00:00:00+0000",
         "started_at": 1000000, "text": f"task {i}", "repo": "muse-pwa",
         "model": "m", "effort": "high", "autonomy": "auto", "goal_id": "",
         "priority": "normal", "status": status, "result": "", "diff": "",
         "wt": "", "worker": "", "heartbeat_at": None, "progress": []}
    t.update(kw)
    return t


class SweepTest(unittest.TestCase):
    def test_expired_running_task_fails(self):
        tasks = [make_task(1, "running", started_at=1000000, effort="low")]
        self.assertTrue(server.sweep_timeouts(tasks, now=1000000 + 1801))
        self.assertEqual(tasks[0]["status"], "failed")
        self.assertIn("Timeout", tasks[0]["result"])
        self.assertEqual(tasks[0]["finished_at"], 1000000 + 1801)

    def test_fresh_task_survives(self):
        tasks = [make_task(1, "running", started_at=1000000, effort="low")]
        self.assertFalse(server.sweep_timeouts(tasks, now=1000000 + 100))
        self.assertEqual(tasks[0]["status"], "running")

    def test_partial_output_preserved_on_timeout(self):
        tasks = [make_task(1, "running", started_at=1000000, effort="low",
                            result="partial log line")]
        server.sweep_timeouts(tasks, now=1000000 + 1801)
        self.assertIn("partial log line", tasks[0]["result"])
        self.assertIn("Timeout", tasks[0]["result"])

    def test_fresh_heartbeat_skips_sweep(self):
        tasks = [make_task(1, "running", started_at=1000000, effort="low",
                            heartbeat_at=1000000 + 1700)]
        self.assertFalse(server.sweep_timeouts(tasks, now=1000000 + 1801))
        self.assertEqual(tasks[0]["status"], "running")

    def test_stale_heartbeat_does_not_save(self):
        tasks = [make_task(1, "running", started_at=1000000, effort="low",
                            heartbeat_at=1000000 + 100)]
        self.assertTrue(server.sweep_timeouts(tasks, now=1000000 + 1801))
        self.assertEqual(tasks[0]["status"], "failed")

    def test_legacy_task_without_started_at_gets_lease(self):
        t = make_task(1, "running")
        del t["started_at"]
        t["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        server.sweep_timeouts([t], now=int(time.time()))
        self.assertIn("started_at", t)
        self.assertEqual(t["status"], "running")

    def test_non_running_untouched(self):
        tasks = [make_task(1, "queued"), make_task(2, "done")]
        self.assertFalse(server.sweep_timeouts(tasks, now=99999999))
        self.assertEqual([t["status"] for t in tasks], ["queued", "done"])


class ClaimTest(unittest.TestCase):
    def test_high_priority_first(self):
        tasks = [make_task(1, priority="low"), make_task(2, priority="high"),
                 make_task(3, priority="normal")]
        t = server.claim_task(tasks, worker="w1", now=2000000)
        self.assertEqual(t["id"], "t2")
        self.assertEqual(t["worker"], "w1")
        self.assertEqual(t["started_at"], 2000000)

    def test_fifo_within_priority(self):
        tasks = [make_task(1, priority="high"), make_task(2, priority="high")]
        t = server.claim_task(tasks, now=2000000)
        self.assertEqual(t["id"], "t1")

    def test_skips_running(self):
        tasks = [make_task(1, "running", priority="high"),
                 make_task(2, priority="low")]
        t = server.claim_task(tasks, now=2000000)
        self.assertEqual(t["id"], "t2")

    def test_empty_queue(self):
        self.assertIsNone(server.claim_task([], now=2000000))


class HeartbeatProgressTest(unittest.TestCase):
    def test_heartbeat_records(self):
        tasks = [make_task(1, "running")]
        t = server.heartbeat_task(tasks, "t1", worker="w9", now=3000000)
        self.assertEqual(t["heartbeat_at"], 3000000)
        self.assertEqual(t["worker"], "w9")

    def test_heartbeat_missing(self):
        self.assertIsNone(server.heartbeat_task([], "nope"))

    def test_progress_appends_and_heartbeats(self):
        tasks = [make_task(1, "running")]
        t, err = server.add_progress(tasks, "t1", "halfway", now=3000000)
        self.assertIsNone(err)
        self.assertEqual(len(t["progress"]), 1)
        self.assertEqual(t["progress"][0]["message"], "halfway")
        self.assertEqual(t["heartbeat_at"], 3000000)

    def test_progress_rejected_when_inactive(self):
        tasks = [make_task(1, "done")]
        t, err = server.add_progress(tasks, "t1", "late note")
        self.assertIsNone(t)
        self.assertEqual(err, "task not active")

    def test_progress_missing_and_empty(self):
        self.assertEqual(server.add_progress([], "x", "m")[1], "no task")
        tasks = [make_task(1, "running")]
        self.assertEqual(server.add_progress(tasks, "t1", "  ")[1],
                         "empty message")


class RetryTest(unittest.TestCase):
    def test_retry_redispatches_same_text(self):
        tasks = [make_task(1, "failed", priority="high", result="boom")]
        nt, err = server.retry_task(tasks, "t1", now=4000000)
        self.assertIsNone(err)
        self.assertEqual(nt["text"], "task 1")
        self.assertEqual(nt["priority"], "high")
        self.assertEqual(nt["status"], "queued")
        self.assertEqual(nt["result"], "")
        self.assertEqual(nt["retried_from"], "t1")
        self.assertEqual(len(tasks), 2)

    def test_retry_only_failed(self):
        tasks = [make_task(1, "done")]
        nt, err = server.retry_task(tasks, "t1")
        self.assertIsNone(nt)
        self.assertIn("only failed", err)

    def test_retry_missing(self):
        self.assertEqual(server.retry_task([], "x")[1], "no task")


class EnrichTest(unittest.TestCase):
    def test_running_gets_elapsed_and_timeout(self):
        t = make_task(1, "running", started_at=1000000, effort="low")
        e = server.enrich_task(t, now=1000600)
        self.assertEqual(e["elapsed_seconds"], 600)
        self.assertEqual(e["timeout_at"], 1000000 + 1800)

    def test_finished_elapsed_frozen(self):
        t = make_task(1, "done", started_at=1000000, finished_at=1000123)
        e = server.enrich_task(t, now=9999999)
        self.assertEqual(e["elapsed_seconds"], 123)
        self.assertIsNone(e["timeout_at"])

    def test_queued_zero(self):
        e = server.enrich_task(make_task(1), now=9999999)
        self.assertEqual(e["elapsed_seconds"], 0)
        self.assertIsNone(e["timeout_at"])


class ListTest(unittest.TestCase):
    def test_filter_by_status(self):
        tasks = [make_task(1, "queued"), make_task(2, "done")]
        self.assertEqual(len(server.filter_tasks(tasks, "done")), 1)
        self.assertEqual(len(server.filter_tasks(tasks, "")), 2)
        self.assertEqual(server.filter_tasks(tasks, "DONE")[0]["id"], "t2")

    def test_paginate(self):
        items = list(range(45))
        page, p, pp, total = server.paginate(items, 2, 20)
        self.assertEqual((p, pp, total), (2, 20, 45))
        self.assertEqual(page, list(range(20, 40)))
        last, _, _, _ = server.paginate(items, 3, 20)
        self.assertEqual(last, list(range(40, 45)))

    def test_paginate_clamps(self):
        _, p, pp, _ = server.paginate([1], "bad", 500)
        self.assertEqual((p, pp), (1, 100))


class WorkersTest(unittest.TestCase):
    def test_summary(self):
        tasks = [make_task(1, "running", worker="w1", heartbeat_at=5000,
                            text="doing things"),
                 make_task(2, "done", worker="w1", heartbeat_at=6000),
                 make_task(3, "queued", worker="w2")]
        out = server.summarize_workers(tasks)
        self.assertEqual([w["worker"] for w in out], ["w1", "w2"])
        w1 = out[0]
        self.assertEqual(w1["last_heartbeat"], 6000)
        self.assertEqual(w1["current_task"], "t1")
        w2 = out[1]
        self.assertIsNone(w2["last_heartbeat"])
        self.assertIsNone(w2["current_task"])

    def test_no_workers(self):
        self.assertEqual(server.summarize_workers([make_task(1)]), [])


if __name__ == "__main__":
    unittest.main()
