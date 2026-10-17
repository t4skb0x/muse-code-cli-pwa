#!/usr/bin/env python3
"""Muse Code CLI PWA — task worker.

Polls the server for queued tasks and runs them headless via `muse exec`.

Two autonomy modes:
    auto     Run directly in the repo; post results when done.
    approve  Run in an isolated git worktree; hold the diff for the
             operator to Approve/Reject from the phone.

The server stays the single writer for task state — the worker only talks
to it through the API, using the least-privilege orchestrator token.

Configuration (environment variables):
    MUSE_PWA_PORT        Server port (default: 3091)
    MUSE_PWA_WORKSPACE   Filesystem root for task repos
                         (default: the user's home directory)
    MUSE_PWA_ORCH_TOKEN  Orchestrator API token (default: read from
                         data/orchestrator-token.txt, else data/token.txt)
    MUSE_CLI_BIN         Muse Code CLI binary (default: "muse").
                         Override if `muse` is not on PATH.
    WORKER_ID            Identity reported on claim/heartbeat
                         (default: the machine hostname).
    MUSE_PWA_HEARTBEAT_INTERVAL
                         Seconds between heartbeats while a task runs
                         (default: 60).

Prerequisite: the Muse Code CLI must support `muse exec`. Verify with:
    muse exec --help
If your CLI version lacks `muse exec`, upgrade at
https://dev.meta.ai/products/muse-code.

Authored by Muse (Meta's personal AI agent).
Released as open source — see LICENSE.
"""

import json
import os
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"

WORKSPACE = Path(os.environ.get("MUSE_PWA_WORKSPACE", str(Path.home())))

# Muse Code CLI binary — override with MUSE_CLI_BIN if not on PATH.
MUSE_BIN = os.environ.get("MUSE_CLI_BIN", "muse")

PORT = os.environ.get("MUSE_PWA_PORT", "3091")
API = f"http://127.0.0.1:{PORT}"

EXEC_TIMEOUT = 900  # max seconds per headless task run (15 min)

# Worker identity, reported on claim/heartbeat so the phone UI can show
# which machine runs each task. Override with WORKER_ID when several
# workers share a hostname (e.g. multiple systemd instances).
WORKER_ID = os.environ.get("WORKER_ID", "").strip() or socket.gethostname()

# Seconds between heartbeat POSTs while a task runs (also refreshable via
# MUSE_PWA_HEARTBEAT_INTERVAL). The server treats a task with a fresh
# heartbeat as alive and skips it in the timeout sweep.
HEARTBEAT_INTERVAL = int(os.environ.get("MUSE_PWA_HEARTBEAT_INTERVAL", "60"))


def _load_token() -> str:
    """Orchestrator token first (least privilege), else the operator token."""
    env = os.environ.get("MUSE_PWA_ORCH_TOKEN", "").strip()
    if env:
        return env
    for name in ("orchestrator-token.txt", "token.txt"):
        f = DATA / name
        if f.exists():
            return f.read_text().strip()
    raise SystemExit("no token found: start server.py first so it generates one")


TOKEN = _load_token()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def api(path, body=None):
    """Call the PWA server API (GET when body is None, else POST)."""
    req = urllib.request.Request(
        API + path,
        method="POST" if body is not None else "GET",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {TOKEN}",
                 "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def sh(cmd, cwd, timeout=EXEC_TIMEOUT):
    """Run a shell command, returning (returncode, combined output)."""
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                       timeout=timeout)
    return r.returncode, (r.stdout + r.stderr)


def _goal_context(task) -> str:
    """Fetch the linked goal's objective, if any, for the task prompt."""
    if not task.get("goal_id"):
        return ""
    goals = api("/api/goals")["goals"]
    g = next((x for x in goals if x["id"] == task["goal_id"]), None)
    return f"Active goal: {g['objective']}\n" if g else ""


# ---------------------------------------------------------------------------
# Task execution
# ---------------------------------------------------------------------------

def heartbeat_forever(task_id, stop):
    """POST a heartbeat every HEARTBEAT_INTERVAL until stop is set."""
    while not stop.wait(HEARTBEAT_INTERVAL):
        try:
            api("/api/tasks/heartbeat", {"id": task_id, "worker": WORKER_ID})
        except Exception as e:
            print("heartbeat error:", e, flush=True)


def progress(task_id, message):
    """Report incremental progress (also refreshes the heartbeat)."""
    try:
        api("/api/tasks/progress", {"id": task_id, "message": message})
    except Exception as e:
        print("progress error:", e, flush=True)


def _partial_output(exc) -> str:
    """Best-effort decode of TimeoutExpired output (bytes on timeout)."""
    out = []
    for chunk in (getattr(exc, "stdout", None), getattr(exc, "stderr", None)):
        if isinstance(chunk, bytes):
            chunk = chunk.decode(errors="replace")
        out.append(chunk or "")
    return "".join(out)


def timed_out(task_id, elapsed, partial):
    """Report a task timeout, preserving any partial output produced."""
    note = (f"Timeout after {elapsed}s: worker produced no final result. "
            f"Partial output preserved below.\n")
    return api("/api/tasks/update", {
        "id": task_id,
        "status": "failed",
        "result": (note + (partial or ""))[-8000:],
    })


def run_task(t):
    """Run one claimed task headless and report the result to the server."""
    stop = threading.Event()
    hb = threading.Thread(target=heartbeat_forever, args=(t["id"], stop),
                          daemon=True)
    hb.start()
    try:
        progress(t["id"], f"worker {WORKER_ID} started ({t['autonomy']} mode)")
        return _run_task_inner(t)
    finally:
        stop.set()
        hb.join(timeout=5)


def _run_task_inner(t):
    """Task body: runs under the heartbeat thread started by run_task."""
    repo = (WORKSPACE / t["repo"]).resolve()

    prompt = (
        "HALT ON SUBAGENTS: work first-hand only, no delegation. "
        "You are a headless coding task from the operator's phone. "
        + _goal_context(t) +
        "Task: " + t["text"] + "\n"
        "Finish with a 3-line summary of what you changed "
        "and how you verified it."
    )
    cmd = [MUSE_BIN, "exec",
           "--model", t["model"],
           "--reasoning-effort", t["effort"],
           "--approval-mode", "never",
           "--trust-workspace", prompt]

    if t["autonomy"] == "approve":
        return _run_approve_mode(t, repo, cmd)

    # auto mode: run directly in the repo, post results when done.
    try:
        code, out = sh(cmd, repo)
    except subprocess.TimeoutExpired as e:
        return timed_out(t["id"], EXEC_TIMEOUT, _partial_output(e))
    if (repo / ".git").exists():
        _, stat = sh(["git", "diff", "--stat"], repo, 60)
    else:
        stat = ""
    progress(t["id"], f"finished with exit code {code}")
    return api("/api/tasks/update", {
        "id": t["id"],
        "status": "done" if code == 0 else "failed",
        "result": out[-8000:],
        "diff": stat,
    })


def _run_approve_mode(t, repo, cmd):
    """Run in an isolated git worktree; hold the diff for phone approval."""
    wt = DATA / "worktrees" / t["id"]
    wt.parent.mkdir(parents=True, exist_ok=True)

    code, out = sh(["git", "worktree", "add", str(wt), "HEAD"], repo, 60)
    if code != 0:
        return api("/api/tasks/update", {
            "id": t["id"], "status": "failed",
            "result": "worktree failed: " + out[-2000:]})

    api("/api/tasks/update", {"id": t["id"], "wt": str(wt)})

    progress(t["id"], f"running in isolated worktree {wt.name}")
    try:
        code, out = sh(cmd, wt)
    except subprocess.TimeoutExpired as e:
        return timed_out(t["id"], EXEC_TIMEOUT, _partial_output(e))

    # Capture the diff as a patch file for the approve/reject step.
    sh(["git", "add", "-A"], wt, 60)
    _, stat = sh(["git", "diff", "--cached", "--stat"], wt, 60)
    patch_dir = DATA / "patches"
    patch_dir.mkdir(parents=True, exist_ok=True)
    diff = subprocess.run(["git", "diff", "--cached"], cwd=wt,
                          capture_output=True, text=True, timeout=60).stdout
    (patch_dir / (t["id"] + ".patch")).write_text(diff)

    return api("/api/tasks/update", {
        "id": t["id"],
        "status": "awaiting_approval",
        "result": out[-8000:],
        "diff": stat,
    })


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    """Poll for queued tasks and run them (import-safe: see tests/)."""
    print(f"Muse Code CLI PWA worker polling as {WORKER_ID}", flush=True)
    while True:
        try:
            t = api("/api/tasks/claim", {"worker": WORKER_ID})["task"]
            if t:
                run_task(t)
            else:
                time.sleep(5)
        except Exception as e:
            print("worker error:", e, flush=True)
            time.sleep(10)


if __name__ == "__main__":
    main()
