# Muse Code CLI PWA

A mobile-first web gateway for remote **Muse Code CLI** orchestration.
Authored by Muse (Meta's personal AI agent).

Open it on your phone, dispatch coding tasks to headless Muse sessions on
your machine, and review the results — from anywhere.

```
Phone (PWA) → Cloudflare Tunnel → this server (127.0.0.1:3091)
                                     → headless `muse` CLI sessions
```

## What it does

| Tab | Purpose |
|-----|---------|
| **Chat** | Persistent operator chat log. The terminal Muse session reads and replies here. |
| **Tasks** | Dispatch headless coding tasks to `muse exec` sessions. Two modes: full-auto (runs and posts results) or approve (isolated git worktree, diff held for your Approve/Reject). Tasks have priority (high/normal/low), live elapsed time, timeout indicators, and retry. |
| **Files** | Browse, view, and edit files under your workspace. Every save keeps a timestamped backup. Secret-looking paths are never served. |
| **Terminal** | Run shell commands with a 30s timeout. Every command is audit-logged. |
| **Diff** | Git status + diff for the current repo. |

## Requirements

- Python 3.10+ (standard library only — no pip packages)
- [Muse Code CLI](https://dev.meta.ai/products/muse-code) installed (`muse` on PATH)
- [Cloudflare](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) for the public tunnel
- A Cloudflare account (free tier works) for Tunnel + Access

## Quick start

```bash
# 1. Clone
git clone https://github.com/t4skb0x/muse-code-cli-pwa.git
cd muse-code-cli-pwa

# 2. Run (foreground, to verify)
python3 server.py
# → Muse Code CLI PWA on http://127.0.0.1:3091
# → First run generates data/token.txt (mode 0600). Save this token.

# 3. In another terminal, start the task worker
python3 worker.py
# → Polls for queued tasks and runs them via `muse exec`.
```

Open http://127.0.0.1:3091 in a browser, paste the token from
`data/token.txt`, and you're in. A 30-day HttpOnly session cookie follows,
so you only paste the token once per device.

### Run as services (durable)

```bash
cp deploy/muse-pwa.service deploy/muse-pwa-worker.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now muse-pwa.service muse-pwa-worker.service
# Enable lingering so they survive logout:
loginctl enable-linger $USER
```

Edit the `APP_DIR` paths in the service files if you cloned elsewhere.

### Expose via Cloudflare Tunnel

The server binds `127.0.0.1` only. The tunnel is the sole public path —
no ports opened on your machine, no dynamic DNS.

```bash
# 1. Create the tunnel (one time)
cloudflared tunnel create muse-pwa
# → note the tunnel UUID

# 2. Route your hostname (one time, free)
cloudflared tunnel route dns muse-pwa muse.example.com

# 3. Fill in deploy/cloudflared-muse.yml:
#    - tunnel UUID
#    - your hostname

# 4. Run the connector (or install deploy/cloudflared-muse.service)
cloudflared tunnel --config deploy/cloudflared-muse.yml run muse-pwa
```

### Lock it down with Cloudflare Access (recommended)

In the Cloudflare dashboard → Zero Trust → Access → Applications, add a
self-hosted app for your hostname and allow only your email (one-time PIN).
This gives you two gates: Cloudflare Access (identity) + the app token
(something you have). Both are free.

### Add to Home Screen (iPhone)

Open your public URL in Safari → Share → Add to Home Screen. It launches
as a standalone PWA with its own icon.

## Worker setup (task machine requirements)

A worker can run on the same machine as the server or on any other
machine that can reach the server's HTTP port. The worker machine needs:

- **Python 3.10+** — standard library only, no pip packages.
- **git** — the worker clones/updates task repos and, in approve mode,
  creates isolated git worktrees plus patch files.
- **Network access to the server** — HTTP(S) reachability to
  `http://<server>:3091` (same defaults; set `MUSE_PWA_PORT` if the
  server listens elsewhere). No inbound ports needed on the worker.
- **Muse Code CLI** with `muse exec` support — verify with
  `muse exec --help` (or set `MUSE_CLI_BIN` if `muse` is not on PATH).
- **A token** — one of, in order: the `MUSE_PWA_ORCH_TOKEN` /
  `MUSE_PWA_TOKEN` env vars, else `data/orchestrator-token.txt`, else
  `data/token.txt`. Copy the value over; never commit it.

While a task runs, the worker POSTs a heartbeat every 60s
(`MUSE_PWA_HEARTBEAT_INTERVAL`) and posts progress notes at start/finish.
The server's timeout sweep treats a task with a fresh heartbeat as alive
and fails only heartbeats-gone-silent tasks past their effort lease
(low 30m, medium 2h, high/unknown 4h). If the worker process itself is
killed, heartbeats stop and the sweep reaps the task — partial output
already posted is preserved. Identity defaults to the hostname; set
`WORKER_ID` when several workers share a host.

```bash
# worker on another machine:
MUSE_PWA_PORT=3091 WORKER_ID=vps-1 \
  MUSE_PWA_ORCH_TOKEN=<paste-orchestrator-token> \
  python3 worker.py
```

## Configuration

All via environment variables:

| Variable | Default | Purpose |
|----------|---------|---------|
| `MUSE_PWA_PORT` | `3091` | Port to bind (loopback only) |
| `MUSE_PWA_WORKSPACE` | `$HOME` | Filesystem root for the Files tab and task repos |
| `MUSE_PWA_TOKEN` | auto-generated → `data/token.txt` | Operator app token |
| `MUSE_PWA_ORCH_TOKEN` | auto-generated → `data/orchestrator-token.txt` | Scoped API token for automation |
| `MUSE_CLI_BIN` | `muse` | Muse Code CLI binary (override if not on PATH) |
| `WORKER_ID` | hostname | Worker identity (worker.py only) |
| `MUSE_PWA_HEARTBEAT_INTERVAL` | `60` | Seconds between worker heartbeats (worker.py only) |
| `MUSE_PWA_SWEEP_INTERVAL` | `60` | Seconds between background timeout sweeps (server.py only) |

### The orchestrator token

For programmatic access (scripts, other agents), use the orchestrator
token as a `Bearer` header. It can: read chat/tasks/goals/workers, create,
claim, heartbeat, progress, retry, and update tasks and goals, and read
files under the app directory. It
**cannot**: run terminal commands, save files, or approve/reject diffs.

```bash
curl -H "Authorization: Bearer $(cat data/orchestrator-token.txt)" \
  http://127.0.0.1:3091/api/tasks
```

Rotate it anytime: `bash deploy/rotate-credentials.sh`
(also rotates the Cloudflare Access service token — see script header).

## For agents

This repo is agent-ready:

- **API-first**: every UI action has a JSON endpoint (`/api/chat`,
  `/api/tasks` (+ `?status=&page=&per_page=`), `/api/tasks/claim`,
  `/api/tasks/update`, `/api/tasks/heartbeat`, `/api/tasks/progress`,
  `/api/tasks/retry`, `/api/workers`, `/api/files`, `/api/file`,
  `/api/exec`, `/api/git/status`, `/api/git/diff`, `/api/goals`).
  Health check: `GET /api/health`.
- **Task dispatch contract**: `POST /api/tasks` with
  `{"text", "repo", "model", "effort", "autonomy", "priority", "goal_id"}`.
  `GET /api/tasks` returns each task with `elapsed_seconds` and
  `timeout_at`, newest first, 20 per page by default (max 100).
  The worker claims via `/api/tasks/claim` (highest priority first,
  FIFO inside a priority), runs
  `muse exec --model … --reasoning-effort … --approval-mode never`,
  and posts back `status` / `result` / `diff`. Heartbeats and progress
  notes keep long tasks visibly alive; `POST /api/tasks/retry`
  re-dispatches a failed task with the same text.
- **No hidden state**: chat is `data/chat.jsonl` (append-only),
  tasks/goals are `data/tasks.json` / `data/goals.json`,
  every exec/save is in `data/audit.log`. The server is the single
  writer; workers and UIs only talk through the API.
- **Conventions**: paths are workspace-relative in the API;
  `safe_path()` confines everything inside `MUSE_PWA_WORKSPACE`;
  deny-listed paths (`.secrets`, `.ssh`, `*.env`, `*.pem`, `*.key`,
  `*token*`, `*secret*`) are never served.

## Project layout

```
server.py              Origin server (stdlib only) — chat, files, terminal, tasks API
worker.py              Task worker — polls /api/tasks/claim, runs `muse exec`
tests/
  test_tasks.py        Stdlib unittest suite for the task orchestration helpers
static/                PWA frontend (no build step, no framework)
  index.html           App shell + login
  app.js               Tab logic (chat, tasks, files, terminal, diff)
  style.css            Mobile-first styles
  manifest.webmanifest PWA manifest
  sw.js                Service worker (offline shell)
  icon.svg             App icon
deploy/
  muse-pwa.service          systemd unit: server
  muse-pwa-worker.service   systemd unit: worker
  cloudflared-muse.yml      Tunnel ingress template
  cloudflared-muse.service  systemd unit: tunnel connector
  rotate-credentials.sh     Rotate orchestrator + Cloudflare Access tokens
data/                  Runtime state (gitignored): tokens, chat, tasks, audit, backups
```

## Security notes

- The server never binds a public interface. If you can reach it without
  the tunnel, your setup is wrong.
- Tokens are 32-byte `secrets.token_urlsafe` values, stored mode 0600.
  The session cookie is an HMAC of the token, not the token itself.
- File reads/writes are confined to `MUSE_PWA_WORKSPACE` via
  path resolution + prefix check. Symlink escapes are resolved before
  the check.
- Terminal commands run as the server's user with a 30s timeout and
  64KB output cap. Treat the Terminal tab like SSH access — because
  it is.
- `data/` is gitignored. Never commit it.

## License

MIT — see LICENSE.

---

*Authored by Muse, Meta's personal AI agent — built to give Muse Code CLI
a phone-sized front door.*
