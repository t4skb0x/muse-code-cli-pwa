#!/usr/bin/env python3
"""muse-pwa origin server (stdlib only). Binds 127.0.0.1:3091; Cloudflare
Tunnel is the only public path (see deploy/). Single writer for chat/audit."""
import hashlib, hmac, json, os, secrets, subprocess, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

BASE = Path(__file__).resolve().parent
STATIC, DATA = BASE / "static", BASE / "data"
WORKSPACE = Path("/home/algoprints")
PORT = int(os.environ.get("MUSE_PWA_PORT", "3091"))
MAX_BODY, MAX_FILE, EXEC_TIMEOUT, OUT_CAP = 1_000_000, 512_000, 30, 64_000
DENY_DIRS = {".secrets", ".ssh"}
DENY_SUFFIXES = (".env", ".pem", ".key")

def load_token() -> str:
    env = os.environ.get("MUSE_PWA_TOKEN", "").strip()
    if env:
        return env
    f = DATA / "token.txt"
    if f.exists():
        return f.read_text().strip()
    DATA.mkdir(parents=True, exist_ok=True)
    tok = secrets.token_urlsafe(32)
    f.write_text(tok + "\n")
    try: f.chmod(0o600)
    except OSError: pass
    return tok

TOKEN = load_token()
SESSION = hmac.new(TOKEN.encode(), b"muse-pwa-session-v1", hashlib.sha256).hexdigest()

def load_orch_token() -> str:
    env = os.environ.get("MUSE_PWA_ORCH_TOKEN", "").strip()
    if env: return env
    f = DATA / "orchestrator-token.txt"
    if f.exists(): return f.read_text().strip()
    DATA.mkdir(parents=True, exist_ok=True)
    tok = secrets.token_urlsafe(32); f.write_text(tok + "\n")
    try: f.chmod(0o600)
    except OSError: pass
    return tok

ORCH_TOKEN = load_orch_token()  # orchestrator role: tasks/goals/chat + read-only files under muse-pwa; no exec/writes/approvals
ORCH_ROOT = BASE  # ~/muse-pwa only
ORCH_PATHS = ("/api/me", "/api/chat", "/api/tasks", "/api/tasks/claim", "/api/tasks/update", "/api/goals", "/api/goals/update", "/api/files", "/api/file")

def audit(event: str, detail: str) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event, "detail": detail[:500]})
    with open(DATA / "audit.log", "a") as fh: fh.write(line + "\n")

def safe_path(rel: str) -> Path | None:
    p = (WORKSPACE / (rel or ".")).resolve()
    return p if str(p) == str(WORKSPACE) or str(p).startswith(str(WORKSPACE) + "/") else None

def orch_allowed(p: Path) -> bool:
    s = str(p)  # callers pass safe_path() output, already resolved
    return s == str(ORCH_ROOT) or s.startswith(str(ORCH_ROOT) + "/")

def readable(p: Path) -> bool:
    parts = set(p.relative_to(WORKSPACE).parts) if str(p).startswith(str(WORKSPACE)) else set()
    name = p.name.lower()
    return not (parts & DENY_DIRS or name.endswith(DENY_SUFFIXES) or "secret" in name or "token" in name)

def chat_read(limit: int = 200) -> list:
    f = DATA / "chat.jsonl"
    if not f.exists(): return []
    rows = [json.loads(x) for x in f.read_text(errors="replace").splitlines() if x.strip()]
    return rows[-limit:]

import threading
LOCK = threading.Lock()
def load_json(name: str, default):
    f = DATA / name
    if not f.exists(): return default
    try: return json.loads(f.read_text())
    except Exception: return default
def save_json(name: str, obj) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / name).write_text(json.dumps(obj))
def new_id() -> str:
    return str(int(time.time())) + "-" + secrets.token_hex(3)

def git(args: list, cwd: Path) -> str:
    r = subprocess.run(["git", "-C", str(cwd)] + args, capture_output=True, text=True, timeout=20)
    return (r.stdout + r.stderr)[:OUT_CAP]

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def send_json(self, obj, code=200, cookie=None):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if cookie: self.send_header("Set-Cookie", cookie)
        self.end_headers(); self.wfile.write(body)
    def role(self):
        auth = self.headers.get("Authorization", "")
        if auth == f"Bearer {TOKEN}": return "operator"
        if auth == f"Bearer {ORCH_TOKEN}": return "orchestrator"
        jar = {k.strip(): v.strip() for k, _, v in (c.partition("=") for c in self.headers.get("Cookie", "").split(";"))}
        return "operator" if hmac.compare_digest(jar.get("muse_session", ""), SESSION) else None
    def authed(self, path="") -> bool:
        r = self.role()
        if r == "operator": return True
        return r == "orchestrator" and path in ORCH_PATHS
    def body(self) -> dict:
        n = int(self.headers.get("Content-Length", "0"))
        if n > MAX_BODY: raise ValueError("body too large")
        return json.loads(self.rfile.read(n) or b"{}")
    def do_GET(self):
        u, q = urlparse(self.path), parse_qs(urlparse(self.path).query)
        if u.path == "/api/health": return self.send_json({"ok": True, "app": "muse-pwa"})
        if u.path.startswith("/api/") and not self.authed(u.path): return self.send_json({"error": "unauthorized"}, 401)
        try: self.route_get(u.path, q)
        except Exception as e: self.send_json({"error": str(e)}, 500)
    def route_get(self, path, q):
        if path == "/api/me": return self.send_json({"ok": True, "workspace": str(WORKSPACE)})
        if path == "/api/chat": return self.send_json({"messages": chat_read()})
        if path == "/api/files":
            p = safe_path(q.get("path", [""])[0])
            if self.role() == "orchestrator" and (not p or not orch_allowed(p)):
                return self.send_json({"error": "forbidden"}, 403)
            if not p or not p.is_dir(): return self.send_json({"error": "bad dir"}, 400)
            items = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
            out = [{"name": x.name, "dir": x.is_dir(), "size": (x.stat().st_size if x.is_file() else None)} for x in items[:500]]
            return self.send_json({"path": str(p.relative_to(WORKSPACE)), "items": out})
        if path == "/api/file":
            p = safe_path(q.get("path", [""])[0])
            if self.role() == "orchestrator" and (not p or not orch_allowed(p)):
                return self.send_json({"error": "forbidden"}, 403)
            if not p or not p.is_file() or not readable(p): return self.send_json({"error": "blocked or missing"}, 403)
            if p.stat().st_size > MAX_FILE: return self.send_json({"error": "file too large"}, 413)
            return self.send_json({"path": str(p.relative_to(WORKSPACE)), "content": p.read_text(errors="replace")})
        if path == "/api/git/status":
            p = safe_path(q.get("path", [""])[0]) or WORKSPACE
            return self.send_json({"output": git(["status", "--short", "--branch"], p)})
        if path == "/api/tasks":
            with LOCK: tasks = load_json("tasks.json", [])
            return self.send_json({"tasks": list(reversed(tasks))[:100]})
        if path == "/api/goals":
            with LOCK: goals = load_json("goals.json", [])
            return self.send_json({"goals": list(reversed(goals))[:50]})
        if path == "/api/git/diff":
            p = safe_path(q.get("path", [""])[0]) or WORKSPACE
            return self.send_json({"output": git(["diff", "--stat"], p) + "\n" + git(["diff"], p)})
        return self.serve_static(path)
    def do_POST(self):
        u = urlparse(self.path)
        try: data = self.body()
        except Exception as e: return self.send_json({"error": str(e)}, 400)
        if u.path == "/api/login":
            if not hmac.compare_digest(str(data.get("token", "")), TOKEN): return self.send_json({"error": "bad token"}, 401)
            secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
            return self.send_json({"ok": True}, cookie=f"muse_session={SESSION}; Path=/; HttpOnly; SameSite=Lax{secure}; Max-Age=2592000")
        if not self.authed(u.path): return self.send_json({"error": "unauthorized"}, 401)
        if u.path == "/api/logout": return self.send_json({"ok": True}, cookie="muse_session=; Path=/; Max-Age=0")
        if u.path == "/api/chat":
            text = str(data.get("text", "")).strip()
            if not text: return self.send_json({"error": "empty"}, 400)
            DATA.mkdir(parents=True, exist_ok=True)
            row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "from": data.get("from", "operator"), "text": text[:8000]}
            with open(DATA / "chat.jsonl", "a") as fh: fh.write(json.dumps(row) + "\n")
            audit("chat", row["from"]); return self.send_json({"ok": True, "message": row})
        if u.path == "/api/exec":
            cmd = str(data.get("cmd", "")).strip()
            cwd = safe_path(str(data.get("cwd", ""))) or WORKSPACE
            if not cmd: return self.send_json({"error": "empty cmd"}, 400)
            audit("exec", cmd)
            try:
                r = subprocess.run(["/bin/bash", "-lc", cmd], cwd=cwd, capture_output=True, text=True, timeout=EXEC_TIMEOUT)
                return self.send_json({"code": r.returncode, "output": (r.stdout + r.stderr)[:OUT_CAP]})
            except subprocess.TimeoutExpired: return self.send_json({"code": -1, "output": f"timeout after {EXEC_TIMEOUT}s"})
        if u.path == "/api/file/save":
            p = safe_path(str(data.get("path", "")))
            if not p or not readable(p): return self.send_json({"error": "blocked path"}, 403)
            content = str(data.get("content", ""))
            if len(content.encode()) > MAX_FILE: return self.send_json({"error": "too large"}, 413)
            DATA.joinpath("backups").mkdir(parents=True, exist_ok=True)
            if p.exists(): (DATA / "backups" / (p.name + "." + str(int(time.time())) + ".bak")).write_text(p.read_text(errors="replace"))
            p.parent.mkdir(parents=True, exist_ok=True); p.write_text(content)
            audit("save", str(p)); return self.send_json({"ok": True})
        if u.path == "/api/tasks":
            text = str(data.get("text", "")).strip()
            if not text: return self.send_json({"error": "empty task"}, 400)
            task = {"id": new_id(), "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "text": text[:8000],
                    "repo": str(data.get("repo", "muse-pwa"))[:200], "model": (str(data.get("model", "")).strip() or "muse-spark-1.3-contributor")[:80],
                    "effort": str(data.get("effort", "high"))[:20], "autonomy": "approve" if data.get("autonomy") == "approve" else "auto",
                    "goal_id": str(data.get("goal_id", ""))[:40], "status": "queued", "result": "", "diff": "", "wt": ""}
            with LOCK:
                tasks = load_json("tasks.json", []); tasks.append(task); save_json("tasks.json", tasks)
            audit("task-create", task["id"]); return self.send_json({"ok": True, "task": task})
        if u.path == "/api/tasks/claim":
            with LOCK:
                tasks = load_json("tasks.json", [])
                for t in tasks:
                    if t["status"] == "queued":
                        t["status"] = "running"; save_json("tasks.json", tasks)
                        return self.send_json({"ok": True, "task": t})
            return self.send_json({"ok": True, "task": None})
        if u.path == "/api/tasks/update":
            with LOCK:
                tasks = load_json("tasks.json", [])
                for t in tasks:
                    if t["id"] == str(data.get("id", "")):
                        for k in ("status", "result", "diff", "wt"):
                            if k in data: t[k] = str(data[k])[:20000]
                        save_json("tasks.json", tasks); return self.send_json({"ok": True})
            return self.send_json({"error": "no task"}, 404)
        if u.path in ("/api/tasks/approve", "/api/tasks/reject"):
            with LOCK:
                tasks = load_json("tasks.json", []); task = next((t for t in tasks if t["id"] == str(data.get("id", ""))), None)
                if not task or task["status"] != "awaiting_approval": return self.send_json({"error": "not awaiting"}, 400)
                repo = safe_path(task["repo"]) or WORKSPACE
                if u.path.endswith("approve"):
                    patch = DATA / "patches" / (task["id"] + ".patch")
                    if patch.exists() and patch.stat().st_size: git(["apply", str(patch)], repo)
                    task["status"] = "done"
                else: task["status"] = "rejected"
                if task.get("wt"): git(["worktree", "remove", "--force", task["wt"]], repo)
                save_json("tasks.json", tasks)
            audit(u.path, str(data.get("id", ""))); return self.send_json({"ok": True})
        if u.path == "/api/goals":
            obj_text = str(data.get("objective", "")).strip()
            if not obj_text: return self.send_json({"error": "empty goal"}, 400)
            goal = {"id": new_id(), "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "objective": obj_text[:2000], "status": "active", "percent": 0, "note": ""}
            with LOCK:
                goals = load_json("goals.json", []); goals.append(goal); save_json("goals.json", goals)
            return self.send_json({"ok": True, "goal": goal})
        if u.path == "/api/goals/update":
            with LOCK:
                goals = load_json("goals.json", [])
                for g in goals:
                    if g["id"] == str(data.get("id", "")):
                        if "percent" in data: g["percent"] = max(0, min(100, int(data["percent"])))
                        if data.get("status") in ("active", "complete", "blocked"): g["status"] = data["status"]
                        if "note" in data: g["note"] = str(data["note"])[:1000]
                        save_json("goals.json", goals); return self.send_json({"ok": True})
            return self.send_json({"error": "no goal"}, 404)
        return self.send_json({"error": "not found"}, 404)
    def serve_static(self, path):
        name = "index.html" if path in ("/", "") else path.lstrip("/")
        f = (STATIC / name).resolve()
        if not str(f).startswith(str(STATIC.resolve())) or not f.is_file(): f = STATIC / "index.html"
        mime = {".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".json": "application/json", ".webmanifest": "application/manifest+json", ".svg": "image/svg+xml"}[f.suffix] if f.suffix in (".html", ".js", ".css", ".json", ".webmanifest", ".svg") else "application/octet-stream"
        body = f.read_bytes()
        self.send_response(200); self.send_header("Content-Type", mime); self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache" if f.name in ("sw.js", "index.html") else "public, max-age=3600"); self.end_headers(); self.wfile.write(body)

if __name__ == "__main__":
    DATA.mkdir(parents=True, exist_ok=True)
    print(f"muse-pwa on http://127.0.0.1:{PORT} workspace={WORKSPACE}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
