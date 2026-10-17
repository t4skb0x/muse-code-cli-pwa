#!/usr/bin/env python3
"""Worker observability tests (stdlib unittest only — no dependencies).

Covers process tracking on claim, per-task log append/tail, death
recording, worker status, and the timeout-sweep death-detection path.

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

TMP = Path(tempfile.mkdtemp(prefix="muse-pwa-obs-tests-"))
server.DATA = TMP  # keep the real repo's data/ untouched


def make_task(i, status="queued", **kw):
    t = {"id": f"t{i}", "ts": "2026-10-17T00:00:00+0000",
         "started_at": 1000000, "text": f"task {i}", "repo": "muse-pwa",
         "model": "m", "effort": "high", "autonomy": "auto", "goal_id": "",
         "priority": "normal", "status": status, "result": "", "diff": "",
         "wt": "", "worker": "", "heartbeat_at": None, "progress": [],
         "worker_pid": None, "worker_cmd": "",
         "worker_started_at": None, "exit_code": None,
         "death_at": None, "death_reason": ""}
    t.update(kw)
    return t


class ClaimTrackingTest(unittest.TestCase):
    def test_claim_records_pid_cmd_start(self):
        tasks = [make_task(1)]
        t = server.claim_task(tasks, worker="w1", now=2000000,
                              pid=os.getpid(), cmd="muse exec")
        self.assertEqual(t["worker_pid"], os.getpid())
        self.assertEqual(t["worker_cmd"], "muse exec")
        self.assertEqual(t["worker_started_at"], 2000000)
        self.assertIsNone(t["exit_code"])
        self.assertEqual(t["death_reason"], "")

    def test_claim_without_pid_still_works(self):
        tasks = [make_task(1)]
        t = server.claim_task(tasks, worker="w1", now=2000000)
        self.assertIsNone(t["worker_pid"])
        self.assertEqual(t["worker"], "w1")

    def test_heartbeat_updates_pid(self):
        tasks = [make_task(1, "running", worker="w1")]
        t = server.heartbeat_task(tasks, "t1", worker="w1", now=3000000,
                                  pid=12345)
        self.assertEqual(t["worker_pid"], 12345)
        self.assertEqual(t["heartbeat_at"], 3000000)


class LogTest(unittest.TestCase):
    def test_append_and_tail(self):
        server.append_task_log("lt1", "line one")
        server.append_task_log("lt1", "line two\nline three")
        out = server.read_task_log("lt1", 2)
        self.assertEqual(out["lines"], ["line two", "line three"])
        self.assertTrue(out["truncated"])
        self.assertGreater(out["size"], 0)

    def test_missing_log_empty(self):
        out = server.read_task_log("no-such-task")
        self.assertEqual(out["lines"], [])
        self.assertEqual(out["size"], 0)

    def test_log_path_confined(self):
        p = server.log_path("../../evil")
        self.assertEqual(p.parent, server.DATA / server.LOGS_DIRNAME)


class AliveTest(unittest.TestCase):
    def test_self_alive(self):
        self.assertTrue(server.is_process_alive(os.getpid()))

    def test_dead_and_bad_pids(self):
        self.assertFalse(server.is_process_alive(999999999))
        self.assertFalse(server.is_process_alive(None))
        self.assertFalse(server.is_process_alive("nope"))
        self.assertFalse(server.is_process_alive(-1))
        self.assertFalse(server.is_process_alive(0))


class DeathTest(unittest.TestCase):
    def test_record_death_fails_running_task(self):
        t = make_task(1, "running", worker="w1", worker_pid=999999999,
                      result="partial work")
        server.record_worker_death(t, 137, "last lines", now=5000000)
        self.assertEqual(t["status"], "failed")
        self.assertEqual(t["exit_code"], 137)
        self.assertEqual(t["death_at"], 5000000)
        self.assertIn("137", t["death_reason"])
        self.assertIn("partial work", t["result"])
        self.assertIn("last lines", t["result"])

    def test_record_death_keeps_terminal_status(self):
        t = make_task(1, "done", worker="w1")
        server.record_worker_death(t, 1, "x", now=5000000)
        self.assertEqual(t["status"], "done")
        self.assertEqual(t["exit_code"], 1)

    def test_sweep_detects_dead_process(self):
        # Stale heartbeat + expired lease + dead PID => death record.
        t = make_task(1, "running", worker="w1", worker_pid=999999999,
                      started_at=1000000, effort="low", heartbeat_at=1000000)
        changed = server.sweep_timeouts([t], now=1000000 + 1801)
        self.assertTrue(changed)
        self.assertEqual(t["status"], "failed")
        self.assertIn("exited", t["death_reason"])

    def test_sweep_live_process_gets_timeout_note(self):
        t = make_task(1, "running", worker="w1", worker_pid=os.getpid(),
                      started_at=1000000, effort="low", heartbeat_at=1000000)
        server.sweep_timeouts([t], now=1000000 + 1801)
        self.assertEqual(t["status"], "failed")
        self.assertEqual(t["death_reason"], "")


class WorkerStatusTest(unittest.TestCase):
    def test_status_current_task_and_alive(self):
        tasks = [make_task(1, "running", worker="w1",
                           worker_pid=os.getpid(), worker_cmd="muse exec",
                           heartbeat_at=5000, text="doing things")]
        workers = {}
        server.note_worker(workers, "w1", task_id="t1", pid=os.getpid(),
                           cmd="muse exec", now=5000)
        st = server.worker_status(workers, tasks, "w1", now=5000)
        self.assertTrue(st["alive"])
        self.assertEqual(st["last_heartbeat"], 5000)
        self.assertEqual(st["current_task"], "t1")
        self.assertFalse(st["heartbeat_stale"])

    def test_status_unknown_worker(self):
        self.assertIsNone(server.worker_status({}, [], "ghost"))

    def test_status_dead_reports_death(self):
        tasks = [make_task(1, "failed", worker="w9",
                           death_reason="worker process (pid 1) exited")]
        st = server.worker_status({}, tasks, "w9", now=9000)
        self.assertIsNotNone(st)
        self.assertIn("exited", st["death_reason"])
        self.assertIsNone(st["current_task"])

    def test_summarize_carries_pid_and_death(self):
        tasks = [make_task(1, "running", worker="w1",
                           worker_pid=os.getpid(), worker_cmd="muse exec",
                           heartbeat_at=5000)]
        out = server.summarize_workers(tasks)
        self.assertEqual(out[0]["pid"], os.getpid())
        self.assertTrue(out[0]["alive"])


if __name__ == "__main__":
    unittest.main()
