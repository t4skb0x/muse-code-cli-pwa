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
    "/api/workers",
    "/api/goals", "/api/goals/update",
    "/api/files", "/api/file",
)


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


def claim_task(tasks, worker="", now=None):
    """Claim the highest-priority queued task (FIFO within a priority)."""
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
    return t


def heartbeat_task(tasks, task_id, worker="", now=None):
    """Record a worker heartbeat. Returns the task, or None if missing."""
    now = int(now if now is not None else time.time())
    for t in tasks:
        if t["id"] == task_id:
            t["heartbeat_at"] = now
            if worker:
                t["worker"] = str(worker)[:80]
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
                "retried_from": t["id"],
            }
            tasks.append(nt)
            return nt, None
    return None, "no task"


def summarize_workers(tasks) -> list:
    """Connected workers: last heartbeat plus current running task, if any."""
    workers: dict = {}
    for t in tasks:
        w = (t.get("worker") or "").strip()
        if not w:
            continue
        e = workers.setdefault(w, {"worker": w, "last_heartbeat": None,
                                   "current_task": None,
                                   "current_task_text": ""})
        try:
            hb = int(t.get("heartbeat_at") or 0) or None
        except (TypeError, ValueError):
            hb = None
        if hb and (not e["last_heartbeat"] or hb > e["last_heartbeat"]):
            e["last_heartbeat"] = hb
        if t.get("status") == "running":
            e["current_task"] = t["id"]
            e["current_task_text"] = str(t.get("text", ""))[:120]
    return sorted(workers.values(), key=lambda e: e["worker"])


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
        return r == "orchestrator" and path in ORCH_PATHS

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

        if path == "/api/goals":
            with LOCK:
                goals = load_json("goals.json", [])
            return self.send_json({"goals": list(reversed(goals))[:50]})

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
                r = subprocess.run(["/bin/bash", "-lc", cmd], cwd=cwd,
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
            }
            with LOCK:
                tasks = load_json("tasks.json", [])
                tasks.append(task)
                save_json("tasks.json", tasks)
            audit("task-create", task["id"])
            return self.send_json({"ok": True, "task": enrich_task(task)})

        if u.path == "/api/tasks/claim":
            # Worker picks up the highest-priority queued task (FIFO inside
            # a priority). Records which worker claimed it (task.worker).
            with LOCK:
                tasks = load_json("tasks.json", [])
                t = claim_task(tasks, data.get("worker", ""))
                if t is None:
                    return self.send_json({"ok": True, "task": None})
                save_json("tasks.json", tasks)
                return self.send_json({"ok": True, "task": enrich_task(t)})

        if u.path == "/api/tasks/heartbeat":
            # Worker sign of life while running: {id, worker?}.
            tid = str(data.get("id", ""))
            if not tid:
                return self.send_json({"error": "missing id"}, 400)
            with LOCK:
                tasks = load_json("tasks.json", [])
                t = heartbeat_task(tasks, tid, data.get("worker", ""))
                if t is None:
                    return self.send_json({"error": "no task"}, 404)
                save_json("tasks.json", tasks)
                return self.send_json({"ok": True,
                                       "heartbeat_at": t["heartbeat_at"]})

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
                        if "heartbeat_at" in data:
                            try:
                                t["heartbeat_at"] = int(data["heartbeat_at"])
                            except (TypeError, ValueError):
                                pass
                        if t.get("status") in (TERMINAL_STATUSES
                                               + ("awaiting_approval",)):
                            t["finished_at"] = int(time.time())
                        save_json("tasks.json", tasks)
                        return self.send_json({"ok": True})
            return self.send_json({"error": "no task"}, 404)

        if u.path in ("/api/tasks/approve", "/api/tasks/reject"):
            # Operator only: apply or discard an approve-mode diff.
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
    print(f"Muse Code CLI PWA on http://127.0.0.1:{PORT} "
          f"workspace={WORKSPACE}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
