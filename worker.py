#!/usr/bin/env python3
"""muse-pwa task worker: claims queued tasks, runs `muse exec` headless.
auto = direct in repo (results posted after); approve = isolated git worktree,
diff held for phone Approve/Reject. Server stays single writer via API."""
import json, subprocess, time, urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA, WORKSPACE = BASE / "data", Path("/home/algoprints")
API = "http://127.0.0.1:3091"
TOKEN = (DATA / ("orchestrator-token.txt" if (DATA / "orchestrator-token.txt").exists() else "token.txt")).read_text().strip()  # least-privilege role
EXEC_TIMEOUT = 900

def api(path, body=None):
    r = urllib.request.Request(API + path, method="POST" if body is not None else "GET",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=30) as x: return json.loads(x.read())

def sh(cmd, cwd, timeout=EXEC_TIMEOUT):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout + r.stderr)

def run_task(t):
    repo = (WORKSPACE / t["repo"]).resolve()
    goal_line = ""
    if t.get("goal_id"):
        goals = api("/api/goals")["goals"]
        g = next((x for x in goals if x["id"] == t["goal_id"]), None)
        if g: goal_line = f"Active goal: {g['objective']}\n"
    prompt = ("HALT ON SUBAGENTS: work first-hand only, no delegation. "
              "You are a headless coding task from the operator's phone. " + goal_line +
              "Task: " + t["text"] + "\nFinish with a 3-line summary of what you changed and how you verified it.")
    cmd = ["muse", "exec", "--model", t["model"], "--reasoning-effort", t["effort"],
           "--approval-mode", "never", "--trust-workspace", prompt]
    if t["autonomy"] == "approve":
        wt = DATA / "worktrees" / t["id"]
        wt.parent.mkdir(parents=True, exist_ok=True)
        code, out = sh(["git", "worktree", "add", str(wt), "HEAD"], repo, 60)
        if code != 0: return api("/api/tasks/update", {"id": t["id"], "status": "failed", "result": "worktree failed: " + out[-2000:]})
        api("/api/tasks/update", {"id": t["id"], "wt": str(wt)})
        code, out = sh(cmd, wt)
        sh(["git", "add", "-A"], wt, 60)
        _, stat = sh(["git", "diff", "--cached", "--stat"], wt, 60)
        patch_dir = DATA / "patches"; patch_dir.mkdir(parents=True, exist_ok=True)
        with open(patch_dir / (t["id"] + ".patch"), "w") as fh:
            fh.write(subprocess.run(["git", "diff", "--cached"], cwd=wt, capture_output=True, text=True, timeout=60).stdout)
        return api("/api/tasks/update", {"id": t["id"], "status": "awaiting_approval", "result": out[-8000:], "diff": stat})
    code, out = sh(cmd, repo)
    _, stat = sh(["git", "diff", "--stat"], repo, 60) if (repo / ".git").exists() else (0, "")
    return api("/api/tasks/update", {"id": t["id"], "status": "done" if code == 0 else "failed", "result": out[-8000:], "diff": stat})

print("muse-pwa worker polling", flush=True)
while True:
    try:
        t = api("/api/tasks/claim", {})["task"]
        if t: run_task(t)
        else: time.sleep(5)
    except Exception as e:
        print("worker error:", e, flush=True); time.sleep(10)