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
            with LOCK:
                tasks = load_json("tasks.json", [])
            return self.send_json({"tasks": list(reversed(tasks))[:100]})

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
                "status": "queued", "result": "", "diff": "", "wt": "",
            }
            with LOCK:
                tasks = load_json("tasks.json", [])
                tasks.append(task)
                save_json("tasks.json", tasks)
            audit("task-create", task["id"])
            return self.send_json({"ok": True, "task": task})

        if u.path == "/api/tasks/claim":
            # Worker picks up the oldest queued task.
            with LOCK:
                tasks = load_json("tasks.json", [])
                for t in tasks:
                    if t["status"] == "queued":
                        t["status"] = "running"
                        save_json("tasks.json", tasks)
                        return self.send_json({"ok": True, "task": t})
            return self.send_json({"ok": True, "task": None})

        if u.path == "/api/tasks/update":
            with LOCK:
                tasks = load_json("tasks.json", [])
                for t in tasks:
                    if t["id"] == str(data.get("id", "")):
                        for k in ("status", "result", "diff", "wt"):
                            if k in data:
                                t[k] = str(data[k])[:20000]
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
    print(f"Muse Code CLI PWA on http://127.0.0.1:{PORT} "
          f"workspace={WORKSPACE}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
