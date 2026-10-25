#!/usr/bin/env python3
"""Muse Code CLI PWA — origin server.

A lightweight mobile-first web gateway for remote Muse Code CLI orchestration.
Python standard library only — no dependencies to install.

Architecture:
    Phone (PWA) → Cloudflare Tunnel → this server (127.0.0.1:3091)
                                           → headless `muse` CLI sessions

What it provides:
    - Chat: persistent operator chat log (single writer = this server)
    - Files: browse, view, and edit files under WORKSPACE (with backups)
    - Terminal: audited shell execution (30s cap, 64KB output cap)
    - Tasks: dispatch headless coding tasks to `muse` CLI sessions
    - Goals: lightweight tracked objectives

Security model:
    - Binds 127.0.0.1 only. The ONLY public path is Cloudflare Tunnel.
    - Two roles: `operator` (full access, via app token or session cookie)
      and `orchestrator` (scoped API token: tasks/goals/chat + read-only
      files under the app dir; no exec, no writes, no approvals).
    - Secret-looking paths (.secrets, .ssh, *.env, *.pem, *.key, *token*,
      *secret*) are never served.
    - Every terminal exec and file save is audit-logged.

Configuration (environment variables):
    MUSE_PWA_PORT        Port to bind (default: 3091)
    MUSE_PWA_WORKSPACE   Filesystem root exposed in the Files tab
                         (default: the user's home directory)
    MUSE_PWA_TOKEN       Operator app token (default: auto-generated and
                         stored in data/token.txt, mode 0600)
    MUSE_PWA_ORCH_TOKEN  Orchestrator API token (default: auto-generated
                         and stored in data/orchestrator-token.txt)

Authored by Muse (Meta's personal AI agent).
Released as open source — see LICENSE.
"""

import hashlib
import hmac
import json
import os
import secrets
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"          # PWA frontend (served as-is)
DATA = BASE / "data"              # runtime state: tokens, chat, tasks, audit

# Filesystem root exposed in the Files tab. Defaults to the user's home dir;
# override with MUSE_PWA_WORKSPACE to scope it to a project folder.
WORKSPACE = Path(os.environ.get("MUSE_PWA_WORKSPACE", str(Path.home())))

PORT = int(os.environ.get("MUSE_PWA_PORT", "3091"))

# Safety limits
MAX_BODY = 1_000_000      # max JSON request body (1 MB)
MAX_FILE = 512_000        # max file read/write via API (500 KB)
EXEC_TIMEOUT = 30         # terminal command timeout (seconds)
OUT_CAP = 64_000          # max captured output per command (64 KB)

# Paths that are never served, even to the operator
DENY_DIRS = {".secrets", ".ssh"}
DENY_SUFFIXES = (".env", ".pem", ".key")


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------

def _load_or_create_token(env_name: str, filename: str) -> str:
    """Return the token from env, else from data file, else generate one."""
    env = os.environ.get(env_name, "").strip()
    if env:
        return env
    f = DATA / filename
    if f.exists():
        return f.read_text().strip()
    DATA.mkdir(parents=True, exist_ok=True)
    tok = secrets.token_urlsafe(32)
    f.write_text(tok + "\n")
    try:
        f.chmod(0o600)
    except OSError:
        pass
    return tok


# Operator token: full access. First run generates it into data/token.txt —
# paste it once into the phone login; a 30-day HttpOnly cookie follows.
TOKEN = _load_or_create_token("MUSE_PWA_TOKEN", "token.txt")

# Session cookie value, derived from the operator token (never the token itself).
SESSION = hmac.new(TOKEN.encode(), b"muse-code-cli-pwa-session-v1",
                   hashlib.sha256).hexdigest()

# Orchestrator token: least-privilege API role for automation (see ORCH_PATHS).
ORCH_TOKEN = _load_or_create_token("MUSE_PWA_ORCH_TOKEN", "orchestrator-token.txt")

# Orchestrator scope: tasks/goals/chat + read-only file listing under BASE.
# No terminal exec, no file writes, no approve/reject.
ORCH_ROOT = BASE
ORCH_PATHS = (
    "/api/me", "/api/chat",
    "/api/tasks", "/api/tasks/claim", "/api/tasks/update",
    "/api/tasks/heartbeat", "/api/tasks/progress", "/api/tasks/retry",
    "/api/tasks/log", "/api/tasks/death",
    "/api/workers",
    "/api/orchestrator/heartbeat", "/api/orchestrator/status",
    "/api/goals", "/api/goals/update",
    "/api/files", "/api/file",
)
# Dynamic orchestrator-readable paths (prefix match in H.authed):
#   GET /api/tasks/<id>/log  (tail worker output)
#   GET /api/workers/<id>/status  (process + heartbeat + current task)
# /api/tasks/approve and /api/tasks/reject look like they match the
# prefix but stay operator-only (explicit role check in the handler).
ORCH_PREFIXES = ("/api/tasks/", "/api/workers/")
ORCH_PREFIX_DENY = ("/api/tasks/approve", "/api/tasks/reject")

# Worker observability limits
LOGS_DIRNAME = "logs"     # per-task worker output lives in data/logs/<id>.log
LOG_MAX_BYTES = 200_000   # per-task log cap (oldest output trimmed)
LOG_TAIL_MAX = 1000       # max lines served per tail request


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def audit(event: str, detail: str) -> None:
    """Append one JSON line to the audit log (exec, saves, task creates)."""
    DATA.mkdir(parents=True, exist_ok=True)
    line = json.dumps({
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "event": event,
        "detail": detail[:500],
    })
    with open(DATA / "audit.log", "a") as fh:
        fh.write(line + "\n")


def safe_path(rel: str) -> Path | None:
    """Resolve a user-supplied relative path, confined inside WORKSPACE."""
    p = (WORKSPACE / (rel or ".")).resolve()
    ok = str(p) == str(WORKSPACE) or str(p).startswith(str(WORKSPACE) + "/")
    return p if ok else None


def orch_allowed(p: Path) -> bool:
    """Orchestrator file reads are confined to the app directory."""
    s = str(p)  # callers pass safe_path() output, already resolved
    return s == str(ORCH_ROOT) or s.startswith(str(ORCH_ROOT) + "/")


def readable(p: Path) -> bool:
    """True unless the path looks secret (deny-listed dir/suffix/keyword)."""
    try:
        parts = set(p.relative_to(WORKSPACE).parts)
    except ValueError:
        parts = set()
    name = p.name.lower()
    return not (parts & DENY_DIRS
                or name.endswith(DENY_SUFFIXES)
                or "secret" in name
                or "token" in name)


def chat_read(limit: int = 200) -> list:
    """Return the most recent chat messages (oldest first)."""
    f = DATA / "chat.jsonl"
    if not f.exists():
        return []
    rows = [json.loads(x) for x in f.read_text(errors="replace").splitlines()
            if x.strip()]
    return rows[-limit:]


LOCK = threading.Lock()


def load_json(name: str, default):
    """Thread-safe JSON read from the data dir (returns default on error)."""
    f = DATA / name
    if not f.exists():
        return default
    try:
        return json.loads(f.read_text())
    except Exception:
        return default


def save_json(name: str, obj) -> None:
    """Thread-safe JSON write to the data dir."""
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / name).write_text(json.dumps(obj))


def new_id() -> str:
    """Unique id for tasks and goals: <epoch>-<random hex>."""
    return str(int(time.time())) + "-" + secrets.token_hex(3)


# ---------------------------------------------------------------------------
# Task orchestration helpers (pure list ops — unit-tested in tests/)
# ---------------------------------------------------------------------------

TASK_MAX_RUNTIME = {"low": 1800, "medium": 7200, "high": 14400}  # seconds
HEARTBEAT_STALE = 300  # a heartbeat newer than this means the worker is alive
SWEEP_INTERVAL = int(os.environ.get("MUSE_PWA_SWEEP_INTERVAL", "60"))
TERMINAL_STATUSES = ("done", "failed", "rejected")
PRIORITY_RANK = {"high": 0, "normal": 1, "low": 2}


def task_max_runtime(effort) -> int:
    """Max run-seconds for an effort level (unknown efforts get 'high')."""
    return TASK_MAX_RUNTIME.get(str(effort or "high").lower(), 14400)


def normalize_priority(value) -> str:
    """Coerce a priority to high/normal/low (default: normal)."""
    v = str(value or "normal").lower()
    return v if v in PRIORITY_RANK else "normal"


def sweep_timeouts(tasks, now=None) -> bool:
    """Fail 'running' tasks past their effort lease. True if changed.

    A task with a fresh worker heartbeat is alive by definition, so the
    sweep skips it. Partial output already posted is preserved: the
    timeout note is appended, never replacing it.
    """
    now = int(now if now is not None else time.time())
    changed = False
    for t in tasks:
        if t.get("status") != "running":
            continue
        try:
            sa = int(t.get("started_at"))
            fresh_lease = False
        except (TypeError, ValueError):
            try:  # legacy task: backfill from display ts (local wall time)
                sa = int(time.mktime(time.strptime(
                    str(t.get("ts", ""))[:19], "%Y-%m-%dT%H:%M:%S")))
                fresh_lease = False
            except (ValueError, OverflowError):
                sa = now  # unparseable ts: grant a full lease, don't fail now
                fresh_lease = True
            t["started_at"] = sa
            changed = True
            if fresh_lease:
                continue
        try:
            hb = int(t.get("heartbeat_at") or 0)
        except (TypeError, ValueError):
            hb = 0
        if hb and now - hb < HEARTBEAT_STALE:
            continue  # worker is alive; its own timeout will finish the task
        limit = task_max_runtime(t.get("effort"))
        if now - sa > limit:
            try:
                pid = int(t.get("worker_pid")) \
                    if t.get("worker_pid") is not None else None
            except (TypeError, ValueError):
                pid = None
            if pid and not is_process_alive(pid):
                # Death detection: the worker process is gone and never
                # completed the task — record exit state, don't just guess.
                tail = read_task_log(t.get("id", ""), 20)["lines"]
                record_worker_death(
                    t, t.get("exit_code"),
                    "\n".join(tail[-20:]), now)
                changed = True
                continue
            note = (f"Timeout: worker produced no result after "
                    f"{limit / 3600:g}h (effort={t.get('effort')}). "
                    f"Likely died silently. Safe to re-dispatch as fresh task.")
            prev = (t.get("result") or "").strip()
            t["result"] = (((prev + "\n\n") if prev else "") + note)[:20000]
            t["status"] = "failed"
            t["finished_at"] = now
            changed = True
    return changed


def sweep_forever() -> None:
    """Background timeout sweep (daemon thread): no request path runs it."""
    while True:
        time.sleep(SWEEP_INTERVAL)
        try:
            with LOCK:
                tasks = load_json("tasks.json", [])
                if sweep_timeouts(tasks):
                    save_json("tasks.json", tasks)
        except Exception:
            pass  # never take down the server over a sweep


def enrich_task(t, now=None) -> dict:
    """Copy of a task plus elapsed_seconds and timeout_at (epoch or None)."""
    now = int(now if now is not None else time.time())
    out = dict(t)
    try:
        sa = int(t.get("started_at"))
    except (TypeError, ValueError):
        sa = None
    try:
        fa = int(t.get("finished_at"))
    except (TypeError, ValueError):
        fa = None
    if t.get("status") == "running" and sa:
        out["elapsed_seconds"] = max(0, now - sa)
        out["timeout_at"] = sa + task_max_runtime(t.get("effort"))
    else:
        out["elapsed_seconds"] = max(0, (fa - sa) if (sa and fa) else 0)
        out["timeout_at"] = None
    return out


def claim_task(tasks, worker="", now=None, pid=None, cmd=""):
    """Claim the highest-priority queued task (FIFO within a priority).

    Records worker process tracking: PID, claim (start) time, and the
    command the worker is about to run. pid/cmd are optional so old
    workers that only send a name keep working.
    """
    now = int(now if now is not None else time.time())
    ranked = sorted(
        (t for t in tasks if t.get("status") == "queued"),
        key=lambda t: (PRIORITY_RANK.get(
            str(t.get("priority") or "normal").lower(), 1),
            str(t.get("ts", "")), str(t.get("id", ""))),
    )
    if not ranked:
        return None
    t = ranked[0]
    t["status"] = "running"
    t["started_at"] = now
    t["finished_at"] = None
    t["worker"] = str(worker or "")[:80]
    t["heartbeat_at"] = None
    try:
        t["worker_pid"] = int(pid) if pid is not None else None
    except (TypeError, ValueError):
        t["worker_pid"] = None
    t["worker_cmd"] = str(cmd or "")[:500]
    t["worker_started_at"] = now
    t["exit_code"] = None
    t["death_at"] = None
    t["death_reason"] = ""
    return t


def heartbeat_task(tasks, task_id, worker="", now=None, pid=None):
    """Record a worker heartbeat. Returns the task, or None if missing."""
    now = int(now if now is not None else time.time())
    for t in tasks:
        if t["id"] == task_id:
            t["heartbeat_at"] = now
            if worker:
                t["worker"] = str(worker)[:80]
            if pid is not None:
                try:
                    t["worker_pid"] = int(pid)
                except (TypeError, ValueError):
                    pass
            return t
    return None


def add_progress(tasks, task_id, message, now=None):
    """Append a progress note (also a heartbeat). Returns (task, error)."""
    now = int(now if now is not None else time.time())
    msg = str(message or "").strip()
    if not msg:
        return None, "empty message"
    for t in tasks:
        if t["id"] == task_id:
            if t.get("status") not in ("queued", "running"):
                return None, "task not active"
            log = t.get("progress") or []
            log.append({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                        "message": msg[:500]})
            t["progress"] = log[-50:]
            t["heartbeat_at"] = now
            return t, None
    return None, "no task"


def retry_task(tasks, task_id, now=None):
    """Re-dispatch a failed task with the same text. Returns (task, error)."""
    now = int(now if now is not None else time.time())
    for t in tasks:
        if t["id"] == task_id:
            if t.get("status") != "failed":
                return None, "only failed tasks can be retried"
            nt = {
                "id": new_id(),
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "started_at": now,
                "text": t.get("text", ""),
                "repo": t.get("repo", "muse-pwa"),
                "model": t.get("model", "muse-spark-1.3-contributor"),
                "effort": t.get("effort", "high"),
                "autonomy": t.get("autonomy", "auto"),
                "goal_id": t.get("goal_id", ""),
                "priority": normalize_priority(t.get("priority")),
                "status": "queued", "result": "", "diff": "", "wt": "",
                "worker": "", "heartbeat_at": None, "progress": [],
                "worker_pid": None, "worker_cmd": "",
                "worker_started_at": None, "exit_code": None,
                "death_at": None, "death_reason": "",
                "retried_from": t["id"],
            }
            tasks.append(nt)
            return nt, None
    return None, "no task"


def summarize_workers(tasks) -> list:
    """Connected workers: last heartbeat plus current running task, if any.

    Each entry also carries process tracking (pid/cmd/alive) taken from
    the running task's claim record, plus any recorded death reason.
    """
    workers: dict = {}
    for t in tasks:
        w = (t.get("worker") or "").strip()
        if not w:
            continue
        e = workers.setdefault(w, {"worker": w, "last_heartbeat": None,
                                   "current_task": None,
                                   "current_task_text": "",
                                   "pid": None, "cmd": "",
                                   "alive": None, "death_reason": ""})
        try:
            hb = int(t.get("heartbeat_at") or 0) or None
        except (TypeError, ValueError):
            hb = None
        if hb and (not e["last_heartbeat"] or hb > e["last_heartbeat"]):
            e["last_heartbeat"] = hb
        if t.get("status") == "running":
            e["current_task"] = t["id"]
            e["current_task_text"] = str(t.get("text", ""))[:120]
            try:
                pid = int(t.get("worker_pid")) \
                    if t.get("worker_pid") is not None else None
            except (TypeError, ValueError):
                pid = None
            e["pid"] = pid
            e["cmd"] = str(t.get("worker_cmd") or "")[:500]
            e["alive"] = is_process_alive(pid) if pid else None
            e["death_reason"] = str(t.get("death_reason") or "")
    return sorted(workers.values(), key=lambda e: e["worker"])


# ---------------------------------------------------------------------------
# Worker observability: per-task output logs + process death detection
# ---------------------------------------------------------------------------

def log_path(task_id: str) -> Path:
    """Log file for a task's worker output (server is the only writer)."""
    safe = "".join(c for c in str(task_id or "") if c.isalnum() or c in "-_")
    return DATA / LOGS_DIRNAME / ((safe or "unknown") + ".log")


def append_task_log(task_id: str, text: str) -> int:
    """Append worker output to the per-task log. Returns file size.

    Trims oldest output past LOG_MAX_BYTES so one chatty worker cannot
    fill the disk.
    """
    d = DATA / LOGS_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    f = log_path(task_id)
    chunk = str(text or "")
    with open(f, "a") as fh:
        fh.write(chunk if chunk.endswith("\n") else chunk + "\n")
    try:
        if f.stat().st_size > LOG_MAX_BYTES:
            lines = f.read_text(errors="replace").splitlines()
            keep = lines[-(LOG_MAX_BYTES // 100):]
            f.write_text("\n".join(keep) + "\n")
    except OSError:
        pass
    try:
        return f.stat().st_size
    except OSError:
        return 0


def read_task_log(task_id: str, lines: int = 100) -> dict:
    """Tail a task's worker output. Returns {lines, truncated, size}."""
    try:
        lines = max(1, min(LOG_TAIL_MAX, int(lines)))
    except (TypeError, ValueError):
        lines = 100
    f = log_path(task_id)
    try:
        size = f.stat().st_size
    except OSError:
        return {"task_id": str(task_id), "lines": [],
                "truncated": False, "size": 0}
    with open(f, errors="replace") as fh:
        all_lines = fh.read().splitlines()
    tail = all_lines[-lines:]
    return {"task_id": str(task_id), "lines": tail,
            "truncated": len(all_lines) > len(tail), "size": size}


def is_process_alive(pid) -> bool:
    """True if a local process with this PID exists (kill(pid, 0))."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not ours
    except OSError:
        return False
    return True


def record_worker_death(task, exit_code=None, tail="", now=None) -> dict:
    """Record a worker death on a running task: exit code, last output,
    and timestamp. Marks the task failed (it produced no result) while
    preserving any partial output already posted."""
    now = int(now if now is not None else time.time())
    try:
        code = int(exit_code) if exit_code is not None else None
    except (TypeError, ValueError):
        code = None
    last = str(tail or "").strip()[-2000:]
    reason = (f"worker process (pid {task.get('worker_pid')}) exited "
              f"with code {code} at "
              f"{time.strftime('%Y-%m-%dT%H:%M:%S%z', time.localtime(now))} "
              f"without completing the task.")
    task["exit_code"] = code
    task["death_at"] = now
    task["death_reason"] = reason
    if task.get("status") == "running":
        note = reason + (f"\nLast output:\n{last}" if last else
                         "\nNo output captured.")
        prev = (task.get("result") or "").strip()
        task["result"] = (((prev + "\n\n") if prev else "") + note)[:20000]
        task["status"] = "failed"
        task["finished_at"] = now
    return task


def worker_status(workers: dict, tasks, worker_id: str, now=None) -> dict | None:
    """Status for one worker: process alive?, last heartbeat, current task,
    plus death reason when its process exited. None if never seen."""
    now = int(now if now is not None else time.time())
    w = str(worker_id or "").strip()
    reg = (workers or {}).get(w)
    cur, cur_text, death, pid, cmd = None, "", "", None, ""
    for t in tasks or []:
        if (t.get("worker") or "").strip() != w:
            continue
        if t.get("status") == "running":
            cur = t["id"]
            cur_text = str(t.get("text", ""))[:120]
            try:
                pid = int(t.get("worker_pid")) \
                    if t.get("worker_pid") is not None else None
            except (TypeError, ValueError):
                pid = None
            cmd = str(t.get("worker_cmd") or "")[:500]
            death = str(t.get("death_reason") or "")
        elif str(t.get("death_reason") or "") and not death:
            death = str(t.get("death_reason"))
    if reg is None and cur is None:
        # Fall back to any historical task row for this worker name.
        seen = any((t.get("worker") or "").strip() == w for t in tasks or [])
        if not seen:
            return None
    try:
        hb = int((reg or {}).get("last_heartbeat") or 0) or None
    except (TypeError, ValueError):
        hb = None
    if pid is None and reg is not None:
        try:
            pid = int(reg.get("pid")) if reg.get("pid") is not None else None
        except (TypeError, ValueError):
            pid = None
        cmd = cmd or str(reg.get("cmd") or "")[:500]
    alive = is_process_alive(pid) if pid else None
    if reg is not None and not death:
        death = str(reg.get("death_reason") or "")
    return {"worker": w, "pid": pid, "cmd": cmd,
            "alive": alive, "last_heartbeat": hb,
            "heartbeat_stale": (hb is None or now - hb >= HEARTBEAT_STALE),
            "current_task": cur, "current_task_text": cur_text,
            "death_reason": death, "now": now}


def note_worker(workers: dict, worker: str, task_id=None, pid=None,
                cmd="", now=None, exit_code=None, death_reason="") -> dict:
    """Upsert the server-side worker registry entry (data/workers.json)."""
    now = int(now if now is not None else time.time())
    w = str(worker or "").strip()[:80]
    if not w:
        return {}
    e = workers.setdefault(w, {"worker": w, "pid": None, "cmd": "",
                               "started_at": now, "last_heartbeat": now,
                               "current_task": None,
                               "exit_code": None, "death_reason": "",
                               "death_at": None})
    if pid is not None:
        try:
            e["pid"] = int(pid)
        except (TypeError, ValueError):
            pass
    if cmd:
        e["cmd"] = str(cmd)[:500]
    e["last_heartbeat"] = now
    if task_id is not None:
        e["current_task"] = task_id
        if task_id is not None and exit_code is None:
            e["exit_code"] = None
            e["death_reason"] = ""
            e["death_at"] = None
    if exit_code is not None:
        try:
            e["exit_code"] = int(exit_code)
        except (TypeError, ValueError):
            e["exit_code"] = None
    if death_reason:
        e["death_reason"] = str(death_reason)[:1000]
        e["death_at"] = now
    return e


# ---------------------------------------------------------------------------
# Orchestrator (Spark) heartbeat — "who watches the watcher"
# ---------------------------------------------------------------------------

ORCH_HEARTBEAT_DEAD = 360  # no heartbeat for this long => orchestrator is dead


def record_orchestrator_heartbeat(timestamp=None, status="", now=None) -> dict:
    """Build the orchestrator heartbeat record for data/orchestrator.json."""
    now = int(now if now is not None else time.time())
    try:
        ts = int(timestamp) if timestamp is not None else now
    except (TypeError, ValueError):
        ts = now
    if ts <= 0:
        ts = now
    return {"last_heartbeat": ts,
            "status": str(status or "").strip()[:80] or "ok",
            "updated_at": now}


def orchestrator_status(record=None, now=None) -> dict:
    """Status view: last heartbeat, age, alive/dead.

    Dead when never seen or when the last heartbeat is older than
    ORCH_HEARTBEAT_DEAD seconds (6 minutes).
    """
    now = int(now if now is not None else time.time())
    try:
        hb = int((record or {}).get("last_heartbeat") or 0) or None
    except (TypeError, ValueError):
        hb = None
    age = (now - hb) if hb is not None else None
    alive = hb is not None and age <= ORCH_HEARTBEAT_DEAD
    return {"last_heartbeat": hb,
            "seconds_since": age,
            "status": "alive" if alive else "dead",
            "reported_status": str((record or {}).get("status") or ""),
            "now": now}


def filter_tasks(tasks, status="") -> list:
    """Keep only tasks with the given status ("" keeps everything)."""
    s = str(status or "").strip().lower()
    if not s:
        return list(tasks)
    return [t for t in tasks if str(t.get("status", "")).lower() == s]


def paginate(items, page=1, per_page=20):
    """Slice newest-first items into a page. Returns (items, page, pp, total)."""
    try:
        page = max(1, int(page))
    except (TypeError, ValueError):
        page = 1
    try:
        per_page = min(100, max(1, int(per_page)))
    except (TypeError, ValueError):
        per_page = 20
    total = len(items)
    start = (page - 1) * per_page
    return items[start:start + per_page], page, per_page, total


# ---------------------------------------------------------------------------
# Persistent monitors (never auto-disable — explicit removal only)
# ---------------------------------------------------------------------------

MONITOR_TYPES = ("task-liveness", "stall-detection")
MONITOR_MIN_INTERVAL = 5   # seconds; shorter intervals are clamped, not refused
MONITOR_TICK = 5           # seconds between due-checks in monitors_forever()


def monitor_log(event: str, detail: str = "") -> None:
    """Append one JSON line to the persistent monitor log (data/monitors.log).

    Monitor history lives on disk, not just in memory, so it survives
    restarts. This log is append-only; nothing here ever truncates it.
    """
    DATA.mkdir(parents=True, exist_ok=True)
    line = json.dumps({
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "event": event,
        "detail": str(detail)[:1000],
    })
    with open(DATA / "monitors.log", "a") as fh:
        fh.write(line + "\n")


def load_monitors():
    """Read the monitor registry (list of dicts)."""
    return load_json("monitors.json", [])


def save_monitors(monitors) -> None:
    """Persist the monitor registry."""
    save_json("monitors.json", monitors)


def normalize_monitor_type(value) -> str:
    """Coerce a monitor type, defaulting to task-liveness."""
    v = str(value or "task-liveness").strip().lower()
    return v if v in MONITOR_TYPES else ""


def create_monitor(monitors, name, mtype, interval, target="", now=None):
    """Add a monitor to the registry. Returns (monitor, error).

    Monitors start active and stay active: nothing in this codebase
    deactivates or deletes them except remove_monitor (explicit DELETE).
    """
    now = int(now if now is not None else time.time())
    name = str(name or "").strip()[:80]
    if not name:
        return None, "missing name"
    if any(m.get("name") == name for m in monitors):
        return None, "monitor already exists"
    mtype = normalize_monitor_type(mtype)
    if not mtype:
        return None, "unknown type (want task-liveness or stall-detection)"
    try:
        interval = int(interval)
    except (TypeError, ValueError):
        interval = 60
    interval = max(MONITOR_MIN_INTERVAL, min(86400, interval))
    mon = {
        "name": name,
        "type": mtype,
        "interval": interval,
        "target": str(target or "").strip()[:80],
        "active": True,
        "created_at": now,
        "last_check": None,
        "next_check": now + interval,
        "last_result": "",
        "check_count": 0,
    }
    monitors.append(mon)
    return mon, None


def remove_monitor(monitors, name) -> bool:
    """Delete a monitor by name. The ONLY way a monitor goes away."""
    for i, m in enumerate(monitors):
        if m.get("name") == name:
            del monitors[i]
            return True
    return False


def enrich_monitor(m, now=None) -> dict:
    """Copy of a monitor plus guaranteed active/last/next check fields."""
    now = int(now if now is not None else time.time())
    out = dict(m)
    out["active"] = True  # persistent monitors are always active while listed
    out["last_check"] = m.get("last_check")
    nxt = m.get("next_check")
    try:
        out["next_check"] = int(nxt) if nxt is not None else None
    except (TypeError, ValueError):
        out["next_check"] = None
    if out["next_check"] is None:
        try:
            out["next_check"] = int(m.get("created_at", now)) + int(
                m.get("interval", 60))
        except (TypeError, ValueError):
            out["next_check"] = now + 60
    return out


def _monitor_scope(tasks, target):
    """Running tasks watched by a monitor (target id, or all when blank)."""
    running = [t for t in tasks if t.get("status") == "running"]
    tgt = str(target or "").strip()
    if not tgt or tgt.lower() == "all":
        return running
    return [t for t in running if t.get("id") == tgt]


def check_task_liveness(tasks, target="", now=None) -> dict:
    """Task-liveness: every running task in scope must show heartbeat/output.

    Alive = fresh worker heartbeat, recent progress note, or posted output.
    Returns {"ok", "checked", "alive", "stale", "summary"}.
    """
    now = int(now if now is not None else time.time())
    scope = _monitor_scope(tasks, target)
    alive, stale = [], []
    for t in scope:
        try:
            hb = int(t.get("heartbeat_at") or 0)
        except (TypeError, ValueError):
            hb = 0
        has_signal = ((hb and now - hb < HEARTBEAT_STALE)
                      or bool((t.get("progress") or []))
                      or bool((t.get("result") or "").strip()))
        (alive if has_signal else stale).append(t.get("id"))
    ok = not stale
    return {
        "ok": ok,
        "checked": len(scope),
        "alive": alive,
        "stale": stale,
        "summary": ("all %d running task(s) alive" % len(scope)
                    if ok else
                    "%d of %d running task(s) stale: %s"
                    % (len(stale), len(scope), ", ".join(stale))),
    }


def check_stall(tasks, target="", interval=60, now=None) -> dict:
    """Stall-detection with the 2x rule: a running task is stalled when its
    time since last activity exceeds 2x the monitor interval.

    Last activity = latest of heartbeat_at (progress notes refresh it) and
    started_at. Returns {"ok", "checked", "stalled", "active", "summary"}.
    """
    now = int(now if now is not None else time.time())
    try:
        interval = max(MONITOR_MIN_INTERVAL, int(interval))
    except (TypeError, ValueError):
        interval = 60
    scope = _monitor_scope(tasks, target)
    stalled, active = [], []
    for t in scope:
        try:
            hb = int(t.get("heartbeat_at") or 0)
        except (TypeError, ValueError):
            hb = 0
        try:
            sa = int(t.get("started_at") or 0)
        except (TypeError, ValueError):
            sa = 0
        last = max(hb, sa)
        (stalled if now - last > 2 * interval else active).append(t.get("id"))
    ok = not stalled
    return {
        "ok": ok,
        "checked": len(scope),
        "stalled": stalled,
        "active": active,
        "summary": ("no stalls (%d running task(s) within 2x interval)"
                    % len(scope) if ok else
                    "stalled: %s (silent >2x interval %ds)"
                    % (", ".join(stalled), interval)),
    }


def run_monitor_check(mon, tasks, now=None) -> dict:
    """Run one monitor's check against a task list. Pure: no I/O."""
    now = int(now if now is not None else time.time())
    if mon.get("type") == "stall-detection":
        return check_stall(tasks, mon.get("target", ""),
                           mon.get("interval", 60), now)
    return check_task_liveness(tasks, mon.get("target", ""), now)


def monitors_tick(monitors, tasks, now=None) -> bool:
    """Run every due monitor, log results, reschedule. Returns True if the
    registry changed (caller should save it).

    NEVER disables or removes a monitor — not on failure, not on empty
    scope, not on check errors. Errors are logged and the monitor stays
    active with its next check rescheduled.
    """
    now = int(now if now is not None else time.time())
    changed = False
    for mon in monitors:
        try:
            nxt = mon.get("next_check")
            nxt = int(nxt) if nxt is not None else now
        except (TypeError, ValueError):
            nxt = now
        if now < nxt:
            continue
        try:
            res = run_monitor_check(mon, tasks, now)
            mon["last_result"] = res.get("summary", "")[:500]
            monitor_log("check",
                        f"{mon.get('name')} [{mon.get('type')}] {res.get('summary', '')}")
        except Exception as e:
            mon["last_result"] = f"check error (stays active): {e}"[:500]
            monitor_log("check-error", f"{mon.get('name')}: {e}")
        mon["active"] = True
        mon["last_check"] = now
        try:
            step = max(MONITOR_MIN_INTERVAL, int(mon.get("interval", 60)))
        except (TypeError, ValueError):
            step = 60
        mon["next_check"] = now + step
        mon["check_count"] = int(mon.get("check_count") or 0) + 1
        changed = True
    return changed


def monitors_forever() -> None:
    """Background monitor loop (daemon thread): checks never auto-disable."""
    while True:
        time.sleep(MONITOR_TICK)
        try:
            with LOCK:
                monitors = load_monitors()
                if not monitors:
                    continue
                tasks = load_json("tasks.json", [])
                if monitors_tick(monitors, tasks):
                    save_monitors(monitors)
        except Exception:
            pass  # never take down the server over a monitor check


def git(args: list, cwd: Path) -> str:
    """Run a git subcommand, capturing output (truncated to OUT_CAP)."""
    r = subprocess.run(["git", "-C", str(cwd)] + args,
                       capture_output=True, text=True, timeout=20)
    return (r.stdout + r.stderr)[:OUT_CAP]


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass  # quiet; use the audit log instead

    # -- response helpers -------------------------------------------------

    def send_json(self, obj, code=200, cookie=None):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    # -- auth --------------------------------------------------------------

    def role(self):
        """Return 'operator', 'orchestrator', or None."""
        auth = self.headers.get("Authorization", "")
        if auth == f"Bearer {TOKEN}":
            return "operator"
        if auth == f"Bearer {ORCH_TOKEN}":
            return "orchestrator"
        jar = {k.strip(): v.strip() for k, _, v in
               (c.partition("=") for c in
                self.headers.get("Cookie", "").split(";"))}
        if hmac.compare_digest(jar.get("muse_session", ""), SESSION):
            return "operator"
        return None

    def authed(self, path="") -> bool:
        r = self.role()
        if r == "operator":
            return True
        if r != "orchestrator":
            return False
        if path in ORCH_PATHS:
            return True
        # Dynamic per-task / per-worker reads (log tail, worker status).
        # Approve/reject stay operator-only despite the /api/tasks/ prefix.
        return (path.startswith(ORCH_PREFIXES)
                and path not in ORCH_PREFIX_DENY)

    def body(self) -> dict:
        n = int(self.headers.get("Content-Length", "0"))
        if n > MAX_BODY:
            raise ValueError("body too large")
        return json.loads(self.rfile.read(n) or b"{}")

    # -- routing ------------------------------------------------------------

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/api/health":
            return self.send_json({"ok": True, "app": "muse-code-cli-pwa"})
        if u.path.startswith("/api/") and not self.authed(u.path):
            return self.send_json({"error": "unauthorized"}, 401)
        try:
            self.route_get(u.path, q)
        except Exception as e:
            self.send_json({"error": str(e)}, 500)

    def route_get(self, path, q):
        if path == "/api/me":
            return self.send_json({"ok": True, "workspace": str(WORKSPACE)})

        if path == "/api/chat":
            return self.send_json({"messages": chat_read()})

        if path == "/api/files":
            p = safe_path(q.get("path", [""])[0])
            if self.role() == "orchestrator" and (not p or not orch_allowed(p)):
                return self.send_json({"error": "forbidden"}, 403)
            if not p or not p.is_dir():
                return self.send_json({"error": "bad dir"}, 400)
            items = sorted(p.iterdir(),
                           key=lambda x: (not x.is_dir(), x.name.lower()))
            out = [{"name": x.name, "dir": x.is_dir(),
                    "size": x.stat().st_size if x.is_file() else None}
                   for x in items[:500]]
            return self.send_json(
                {"path": str(p.relative_to(WORKSPACE)), "items": out})

        if path == "/api/file":
            p = safe_path(q.get("path", [""])[0])
            if self.role() == "orchestrator" and (not p or not orch_allowed(p)):
                return self.send_json({"error": "forbidden"}, 403)
            if not p or not p.is_file() or not readable(p):
                return self.send_json({"error": "blocked or missing"}, 403)
            if p.stat().st_size > MAX_FILE:
                return self.send_json({"error": "file too large"}, 413)
            return self.send_json({"path": str(p.relative_to(WORKSPACE)),
                                   "content": p.read_text(errors="replace")})

        if path == "/api/tasks":
            # ?status=queued|running|done|… filters; ?page=&per_page= paginates.
            # Timeout sweep runs on a background interval, not per request.
            with LOCK:
                tasks = load_json("tasks.json", [])
            now = int(time.time())
            visible = filter_tasks(tasks, q.get("status", [""])[0])
            ordered = list(reversed(visible))
            page_items, page, per_page, total = paginate(
                ordered, q.get("page", ["1"])[0],
                q.get("per_page", ["20"])[0])
            return self.send_json({
                "tasks": [enrich_task(t, now) for t in page_items],
                "page": page, "per_page": per_page, "total": total,
            })

        if path == "/api/workers":
            with LOCK:
                tasks = load_json("tasks.json", [])
            return self.send_json({"workers": summarize_workers(tasks),
                                   "now": int(time.time())})

        if path.startswith("/api/workers/") and path.endswith("/status"):
            # GET /api/workers/<id>/status: process alive?, last
            # heartbeat, current task, death reason if exited.
            from urllib.parse import unquote
            wid = unquote(path[len("/api/workers/"):-len("/status")]).strip()
            if not wid:
                return self.send_json({"error": "missing worker"}, 400)
            with LOCK:
                tasks = load_json("tasks.json", [])
                workers = load_json("workers.json", {})
            st = worker_status(workers, tasks, wid)
            if st is None:
                return self.send_json({"error": "unknown worker"}, 404)
            return self.send_json(st)

        if path == "/api/orchestrator/status":
            # GET /api/orchestrator/status: last Spark heartbeat, age,
            # alive/dead. Reads data/orchestrator.json (persistent).
            with LOCK:
                record = load_json("orchestrator.json", {})
            return self.send_json(orchestrator_status(record))

        if path.startswith("/api/tasks/") and path.endswith("/log"):
            # GET /api/tasks/<id>/log?lines=N: tail worker output.
            from urllib.parse import unquote
            tid = unquote(path[len("/api/tasks/"):-len("/log")]).strip()
            if not tid:
                return self.send_json({"error": "missing id"}, 400)
            try:
                n = int(q.get("lines", ["100"])[0])
            except (TypeError, ValueError):
                n = 100
            return self.send_json(read_task_log(tid, n))

        if path == "/api/goals":
            with LOCK:
                goals = load_json("goals.json", [])
            return self.send_json({"goals": list(reversed(goals))[:50]})

        if path == "/api/monitors":
            # Persistent monitors: always active while listed. Operator only.
            if self.role() != "operator":
                return self.send_json({"error": "forbidden"}, 403)
            with LOCK:
                monitors = load_monitors()
            now = int(time.time())
            return self.send_json(
                {"monitors": [enrich_monitor(m, now) for m in monitors]})

        if path == "/api/git/status":
            p = safe_path(q.get("path", [""])[0]) or WORKSPACE
            return self.send_json(
                {"output": git(["status", "--short", "--branch"], p)})

        if path == "/api/git/diff":
            p = safe_path(q.get("path", [""])[0]) or WORKSPACE
            return self.send_json({"output": git(["diff", "--stat"], p)
                                   + "\n" + git(["diff"], p)})

        return self.serve_static(path)

    def do_POST(self):
        u = urlparse(self.path)
        try:
            data = self.body()
        except Exception as e:
            return self.send_json({"error": str(e)}, 400)

        # Login is the only unauthenticated POST: token -> session cookie.
        if u.path == "/api/login":
            if not hmac.compare_digest(str(data.get("token", "")), TOKEN):
                return self.send_json({"error": "bad token"}, 401)
            secure = ("; Secure"
                      if self.headers.get("X-Forwarded-Proto") == "https"
                      else "")
            cookie = (f"muse_session={SESSION}; Path=/; HttpOnly; "
                      f"SameSite=Lax{secure}; Max-Age=2592000")
            return self.send_json({"ok": True}, cookie=cookie)

        if not self.authed(u.path):
            return self.send_json({"error": "unauthorized"}, 401)

        if u.path == "/api/logout":
            return self.send_json(
                {"ok": True}, cookie="muse_session=; Path=/; Max-Age=0")

        if u.path == "/api/chat":
            text = str(data.get("text", "")).strip()
            if not text:
                return self.send_json({"error": "empty"}, 400)
            DATA.mkdir(parents=True, exist_ok=True)
            row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                   "from": data.get("from", "operator"),
                   "text": text[:8000]}
            with open(DATA / "chat.jsonl", "a") as fh:
                fh.write(json.dumps(row) + "\n")
            audit("chat", row["from"])
            return self.send_json({"ok": True, "message": row})

        if u.path == "/api/exec":
            # Operator only (not in ORCH_PATHS).
            cmd = str(data.get("cmd", "")).strip()
            cwd = safe_path(str(data.get("cwd", ""))) or WORKSPACE
            if not cmd:
                return self.send_json({"error": "empty cmd"}, 400)
            audit("exec", cmd)
            try:
                if os.name == "nt":  # Field fix (Windows): platform shell
                    shell = ["powershell", "-NoProfile", "-Command", cmd]
                else:
                    shell = ["/bin/bash", "-lc", cmd]
                r = subprocess.run(shell, cwd=cwd,
                                   capture_output=True, text=True,
                                   timeout=EXEC_TIMEOUT)
                return self.send_json(
                    {"code": r.returncode,
                     "output": (r.stdout + r.stderr)[:OUT_CAP]})
            except subprocess.TimeoutExpired:
                return self.send_json(
                    {"code": -1, "output": f"timeout after {EXEC_TIMEOUT}s"})

        if u.path == "/api/file/save":
            # Operator only (not in ORCH_PATHS).
            p = safe_path(str(data.get("path", "")))
            if not p or not readable(p):
                return self.send_json({"error": "blocked path"}, 403)
            content = str(data.get("content", ""))
            if len(content.encode()) > MAX_FILE:
                return self.send_json({"error": "too large"}, 413)
            # Keep a timestamped backup of the previous version.
            DATA.joinpath("backups").mkdir(parents=True, exist_ok=True)
            if p.exists():
                bak = DATA / "backups" / (p.name + "." + str(int(time.time()))
                                          + ".bak")
                bak.write_text(p.read_text(errors="replace"))
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
            audit("save", str(p))
            return self.send_json({"ok": True})

        if u.path == "/api/tasks":
            text = str(data.get("text", "")).strip()
            if not text:
                return self.send_json({"error": "empty task"}, 400)
            task = {
                "id": new_id(),
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "text": text[:8000],
                "repo": str(data.get("repo", "muse-pwa"))[:200],
                "model": (str(data.get("model", "")).strip()
                          or "muse-spark-1.3-contributor")[:80],
                "effort": str(data.get("effort", "high"))[:20],
                "autonomy": ("approve" if data.get("autonomy") == "approve"
                             else "auto"),
                "goal_id": str(data.get("goal_id", ""))[:40],
                "priority": normalize_priority(data.get("priority")),
                "status": "queued", "result": "", "diff": "", "wt": "",
                "worker": "", "heartbeat_at": None, "progress": [],
                "worker_pid": None, "worker_cmd": "",
                "worker_started_at": None, "exit_code": None,
                "death_at": None, "death_reason": "",
            }
            with LOCK:
                tasks = load_json("tasks.json", [])
                tasks.append(task)
                save_json("tasks.json", tasks)
            audit("task-create", task["id"])
            return self.send_json({"ok": True, "task": enrich_task(task)})

        if u.path == "/api/tasks/claim":
            # Worker picks up the highest-priority queued task (FIFO inside
            # a priority). Records worker identity + process tracking
            # (PID, start time, command) on the task and in workers.json,
            # and opens the per-task output log.
            with LOCK:
                tasks = load_json("tasks.json", [])
                t = claim_task(tasks, data.get("worker", ""),
                               pid=data.get("pid"), cmd=data.get("cmd", ""))
                if t is None:
                    return self.send_json({"ok": True, "task": None})
                workers = load_json("workers.json", {})
                note_worker(workers, t.get("worker"), task_id=t["id"],
                            pid=t.get("worker_pid"), cmd=t.get("worker_cmd"))
                save_json("workers.json", workers)
                save_json("tasks.json", tasks)
            append_task_log(
                t["id"],
                f"worker {t.get('worker')} claimed task "
                f"(pid {t.get('worker_pid')}) cmd: {t.get('worker_cmd')}")
            return self.send_json({"ok": True, "task": enrich_task(t)})

        if u.path == "/api/tasks/heartbeat":
            # Worker sign of life while running: {id, worker?, pid?}.
            tid = str(data.get("id", ""))
            if not tid:
                return self.send_json({"error": "missing id"}, 400)
            with LOCK:
                tasks = load_json("tasks.json", [])
                t = heartbeat_task(tasks, tid, data.get("worker", ""),
                                   pid=data.get("pid"))
                if t is None:
                    return self.send_json({"error": "no task"}, 404)
                workers = load_json("workers.json", {})
                note_worker(workers, t.get("worker"), task_id=tid,
                            pid=t.get("worker_pid"))
                save_json("workers.json", workers)
                save_json("tasks.json", tasks)
                return self.send_json({"ok": True,
                                       "heartbeat_at": t["heartbeat_at"]})

        if u.path == "/api/orchestrator/heartbeat":
            # Spark sign of life: {timestamp?, status?}. Persisted to
            # data/orchestrator.json (survives restarts), not just memory.
            # No audit line: like worker heartbeats, this fires every
            # few minutes and would flood the audit log.
            with LOCK:
                record = record_orchestrator_heartbeat(
                    data.get("timestamp"), data.get("status", ""))
                save_json("orchestrator.json", record)
                return self.send_json({"ok": True,
                                       **orchestrator_status(record)})

        if u.path == "/api/tasks/log":
            # Worker output streaming: {id, text}. Appends to the
            # per-task log file (server is the only writer) and counts
            # as a heartbeat so streaming workers look alive.
            tid = str(data.get("id", ""))
            text = str(data.get("text", ""))
            if not tid:
                return self.send_json({"error": "missing id"}, 400)
            if not text.strip():
                return self.send_json({"error": "empty text"}, 400)
            with LOCK:
                tasks = load_json("tasks.json", [])
                t = heartbeat_task(tasks, tid, data.get("worker", ""))
                if t is None:
                    return self.send_json({"error": "no task"}, 404)
                save_json("tasks.json", tasks)
            size = append_task_log(tid, text[:10000])
            return self.send_json({"ok": True,
                                   "heartbeat_at": t["heartbeat_at"],
                                   "size": size})

        if u.path == "/api/tasks/death":
            # Death report: {id, exit_code?, tail?}. The worker's
            # supervisor posts this when the worker process exits
            # without completing the task.
            tid = str(data.get("id", ""))
            if not tid:
                return self.send_json({"error": "missing id"}, 400)
            tail = str(data.get("tail", ""))
            if not tail:
                tail = "\n".join(read_task_log(tid, 20)["lines"])
            with LOCK:
                tasks = load_json("tasks.json", [])
                t = next((x for x in tasks if x["id"] == tid), None)
                if t is None:
                    return self.send_json({"error": "no task"}, 404)
                if t.get("status") not in ("queued", "running"):
                    return self.send_json({"error": "task not active"}, 400)
                record_worker_death(t, data.get("exit_code"), tail)
                workers = load_json("workers.json", {})
                note_worker(workers, t.get("worker"),
                            exit_code=t.get("exit_code"),
                            death_reason=t.get("death_reason"))
                save_json("workers.json", workers)
                save_json("tasks.json", tasks)
            audit("worker-death", f"{tid} pid={t.get('worker_pid')} "
                                 f"code={t.get('exit_code')}")
            return self.send_json({"ok": True, "task": enrich_task(t)})

        if u.path == "/api/tasks/progress":
            # Incremental progress on a long task: {id, message}.
            tid = str(data.get("id", ""))
            if not tid:
                return self.send_json({"error": "missing id"}, 400)
            with LOCK:
                tasks = load_json("tasks.json", [])
                t, err = add_progress(tasks, tid, data.get("message", ""))
                if err == "no task":
                    return self.send_json({"error": "no task"}, 404)
                if err:
                    return self.send_json({"error": err}, 400)
                save_json("tasks.json", tasks)
                return self.send_json({"ok": True,
                                       "heartbeat_at": t["heartbeat_at"]})

        if u.path == "/api/tasks/retry":
            # Re-dispatch a failed task with the same text: {id}.
            tid = str(data.get("id", ""))
            if not tid:
                return self.send_json({"error": "missing id"}, 400)
            with LOCK:
                tasks = load_json("tasks.json", [])
                nt, err = retry_task(tasks, tid)
                if err == "no task":
                    return self.send_json({"error": "no task"}, 404)
                if err:
                    return self.send_json({"error": err}, 400)
                save_json("tasks.json", tasks)
            audit("/api/tasks/retry", tid)
            return self.send_json({"ok": True, "task": enrich_task(nt)})

        if u.path == "/api/tasks/update":
            with LOCK:
                tasks = load_json("tasks.json", [])
                for t in tasks:
                    if t["id"] == str(data.get("id", "")):
                        for k in ("status", "result", "diff", "wt", "worker"):
                            if k in data:
                                t[k] = str(data[k])[:20000]
                        if "exit_code" in data:
                            try:
                                t["exit_code"] = int(data["exit_code"])
                            except (TypeError, ValueError):
                                t["exit_code"] = None
                        if "heartbeat_at" in data:
                            try:
                                t["heartbeat_at"] = int(data["heartbeat_at"])
                            except (TypeError, ValueError):
                                pass
                        if t.get("status") in (TERMINAL_STATUSES
                                               + ("awaiting_approval",)):
                            t["finished_at"] = int(time.time())
                            if (t.get("exit_code") not in (None, 0)
                                    and not t.get("death_reason")):
                                t["death_reason"] = (
                                    f"worker process (pid "
                                    f"{t.get('worker_pid')}) exited with "
                                    f"code {t.get('exit_code')}.")
                                t["death_at"] = t["finished_at"]
                        if t.get("status") in TERMINAL_STATUSES:
                            workers = load_json("workers.json", {})
                            e = note_worker(
                                workers, t.get("worker"),
                                exit_code=t.get("exit_code"))
                            if t.get("death_reason"):
                                e["death_reason"] = t["death_reason"][:1000]
                                e["death_at"] = t.get("death_at")
                            save_json("workers.json", workers)
                        save_json("tasks.json", tasks)
                        return self.send_json({"ok": True})
            return self.send_json({"error": "no task"}, 404)

        if u.path in ("/api/tasks/approve", "/api/tasks/reject"):
            # Operator only: apply or discard an approve-mode diff.
            # (Explicit check: the /api/tasks/ orchestrator prefix must
            # never open these two.)
            if self.role() != "operator":
                return self.send_json({"error": "forbidden"}, 403)
            with LOCK:
                tasks = load_json("tasks.json", [])
                task = next((t for t in tasks
                             if t["id"] == str(data.get("id", ""))), None)
                if not task or task["status"] != "awaiting_approval":
                    return self.send_json({"error": "not awaiting"}, 400)
                repo = safe_path(task["repo"]) or WORKSPACE
                if u.path.endswith("approve"):
                    patch = DATA / "patches" / (task["id"] + ".patch")
                    if patch.exists() and patch.stat().st_size:
                        git(["apply", str(patch)], repo)
                    task["status"] = "done"
                else:
                    task["status"] = "rejected"
                if task.get("wt"):
                    git(["worktree", "remove", "--force", task["wt"]], repo)
                task["finished_at"] = int(time.time())
                save_json("tasks.json", tasks)
            audit(u.path, str(data.get("id", "")))
            return self.send_json({"ok": True})

        if u.path == "/api/goals":
            obj_text = str(data.get("objective", "")).strip()
            if not obj_text:
                return self.send_json({"error": "empty goal"}, 400)
            goal = {"id": new_id(),
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "objective": obj_text[:2000],
                    "status": "active", "percent": 0, "note": ""}
            with LOCK:
                goals = load_json("goals.json", [])
                goals.append(goal)
                save_json("goals.json", goals)
            return self.send_json({"ok": True, "goal": goal})

        if u.path == "/api/monitors":
            # Operator only: create a persistent monitor.
            # Body: {name, type, interval, target}.
            if self.role() != "operator":
                return self.send_json({"error": "forbidden"}, 403)
            with LOCK:
                monitors = load_monitors()
                mon, err = create_monitor(
                    monitors, data.get("name"), data.get("type"),
                    data.get("interval", 60), data.get("target", ""))
                if err == "monitor already exists":
                    return self.send_json({"error": err}, 409)
                if err:
                    return self.send_json({"error": err}, 400)
                save_monitors(monitors)
            monitor_log("create",
                        f"{mon['name']} [{mon['type']}] every {mon['interval']}s"
                        f" target={mon['target'] or 'all'}")
            audit("monitor-create", mon["name"])
            return self.send_json({"ok": True,
                                   "monitor": enrich_monitor(mon)})

        if u.path == "/api/goals/update":
            with LOCK:
                goals = load_json("goals.json", [])
                for g in goals:
                    if g["id"] == str(data.get("id", "")):
                        if "percent" in data:
                            g["percent"] = max(0, min(100, int(data["percent"])))
                        if data.get("status") in ("active", "complete",
                                                  "blocked"):
                            g["status"] = data["status"]
                        if "note" in data:
                            g["note"] = str(data["note"])[:1000]
                        save_json("goals.json", goals)
                        return self.send_json({"ok": True})
            return self.send_json({"error": "no goal"}, 404)

        return self.send_json({"error": "not found"}, 404)

    def do_DELETE(self):
        u = urlparse(self.path)
        if not self.authed("/api/monitors"):
            return self.send_json({"error": "unauthorized"}, 401)
        # Explicit removal is the ONLY way a monitor goes away.
        if u.path == "/api/monitors" or u.path == "/api/monitors/":
            return self.send_json({"error": "missing monitor name"}, 400)
        if u.path.startswith("/api/monitors/"):
            if self.role() != "operator":
                return self.send_json({"error": "forbidden"}, 403)
            from urllib.parse import unquote
            name = unquote(u.path[len("/api/monitors/"):]).strip()
            if not name:
                return self.send_json({"error": "missing monitor name"}, 400)
            with LOCK:
                monitors = load_monitors()
                if not remove_monitor(monitors, name):
                    return self.send_json({"error": "no monitor"}, 404)
                save_monitors(monitors)
            monitor_log("delete", name)
            audit("monitor-delete", name)
            return self.send_json({"ok": True, "deleted": name})
        return self.send_json({"error": "not found"}, 404)

    def serve_static(self, path):
        """Serve the PWA frontend; fall back to index.html (SPA)."""
        name = "index.html" if path in ("/", "") else path.lstrip("/")
        f = (STATIC / name).resolve()
        if not str(f).startswith(str(STATIC.resolve())) or not f.is_file():
            f = STATIC / "index.html"
        mime = {
            ".html": "text/html",
            ".js": "text/javascript",
            ".css": "text/css",
            ".json": "application/json",
            ".webmanifest": "application/manifest+json",
            ".svg": "image/svg+xml",
        }.get(f.suffix, "application/octet-stream")
        body = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        # Never cache the shell or service worker; cache assets for an hour.
        self.send_header(
            "Cache-Control",
            "no-cache" if f.name in ("sw.js", "index.html")
            else "public, max-age=3600")
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    DATA.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=sweep_forever, daemon=True).start()
    threading.Thread(target=monitors_forever, daemon=True).start()
    print(f"Muse Code CLI PWA on http://127.0.0.1:{PORT} "
          f"workspace={WORKSPACE}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
