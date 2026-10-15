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
| **Tasks** | Dispatch headless coding tasks to `muse exec` sessions. Two modes: full-auto (runs and posts results) or approve (isolated git worktree, diff held for your Approve/Reject). |
| **Files** | Browse, view, and edit files under your workspace. Every save keeps a timestamped backup. Secret-looking paths are never served. |
| **Terminal** | Run shell commands with a 30s timeout. Every command is audit-logged. |
| **Diff** | Git status + diff for the current repo. |

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
| **Tasks** | Dispatch headless coding tasks to `muse exec` sessions. Two modes: full-auto (runs and posts results) or approve (isolated git worktree, diff held for your Approve/Reject). |
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

## Configuration

All via environment variables:

| Variable | Default | Purpose |
|----------|---------|---------|
| `MUSE_PWA_PORT` | `3091` | Port to bind (loopback only) |
| `MUSE_PWA_WORKSPACE` | `$HOME` | Filesystem root for the Files tab and task repos |
| `MUSE_PWA_TOKEN` | auto-generated → `data/token.txt` | Operator app token |
| `MUSE_PWA_ORCH_TOKEN` | auto-generated → `data/orchestrator-token.txt` | Scoped API token for automation |
| `MUSE_CLI_BIN` | `muse` | Muse Code CLI binary (override if not on PATH) |

### The orchestrator token

For programmatic access (scripts, other agents), use the orchestrator
token as a `Bearer` header. It can: read chat/tasks/goals, create and
update tasks and goals, and read files under the app directory. It
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
  `/api/tasks`, `/api/tasks/claim`, `/api/tasks/update`,
  `/api/files`, `/api/file`, `/api/exec`, `/api/git/status`,
  `/api/git/diff`, `/api/goals`). Health check: `GET /api/health`.
- **Task dispatch contract**: `POST /api/tasks` with
  `{"text", "repo", "model", "effort", "autonomy", "goal_id"}`.
  The worker claims via `/api/tasks/claim`, runs
  `muse exec --model … --reasoning-effort … --approval-mode never`,
  and posts back `status` / `result` / `diff`.
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
 (standard library only — no pip packages)
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

## Configuration

All via environment variables:

| Variable | Default | Purpose |
|----------|---------|---------|
| `MUSE_PWA_PORT` | `3091` | Port to bind (loopback only) |
| `MUSE_PWA_WORKSPACE` | `$HOME` | Filesystem root for the Files tab and task repos |
| `MUSE_PWA_TOKEN` | auto-generated → `data/token.txt` | Operator app token |
| `MUSE_PWA_ORCH_TOKEN` | auto-generated → `data/orchestrator-token.txt` | Scoped API token for automation |
| `MUSE_CLI_BIN` | `muse` | Muse Code CLI binary (override if not on PATH) |

### The orchestrator token

For programmatic access (scripts, other agents), use the orchestrator
token as a `Bearer` header. It can: read chat/tasks/goals, create and
update tasks and goals, and read files under the app directory. It
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
  `/api/tasks`, `/api/tasks/claim`, `/api/tasks/update`,
  `/api/files`, `/api/file`, `/api/exec`, `/api/git/status`,
  `/api/git/diff`, `/api/goals`). Health check: `GET /api/health`.
- **Task dispatch contract**: `POST /api/tasks` with
  `{"text", "repo", "model", "effort", "autonomy", "goal_id"}`.
  The worker claims via `/api/tasks/claim`, runs
  `muse exec --model … --reasoning-effort … --approval-mode never`,
  and posts back `status` / `result` / `diff`.
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
