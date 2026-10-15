# muse-pwa — iPhone full-remote for Muse (option C)

Public URL (after tunnel step): https://muse.ridgestate.com — open in iPhone Safari → Share → Add to Home Screen.
Origin: `server.py` (Python stdlib only) on `127.0.0.1:3091`, vivobook. Cloudflare Tunnel carries it; no Worker runs in the path, so Workers quota/cost = $0 (Tunnel + DNS are free; Workers Free 100k req/day is untouched).

Tabs: Chat (`data/chat.jsonl`, single writer = this server) · Files (browse/view/edit with backup) · Terminal (30s cap, audited to `data/audit.log`) · Diff (git status+diff).

## Start (exact)
```bash
python3 /home/algoprints/muse-pwa/server.py   # foreground check
# durable:
cp /home/algoprints/muse-pwa/deploy/muse-pwa.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now muse-pwa.service
```
Token: first run writes `data/token.txt` (0600). Paste it once into the phone login; a 30-day HttpOnly cookie follows.

## Tunnel + protection (exact, one-time)
```bash
cloudflared tunnel create muse-pwa
cloudflared tunnel route dns muse-pwa muse.ridgestate.com
# fill UUID into deploy/cloudflared-muse.yml, then:
cloudflared tunnel --config /home/algoprints/muse-pwa/deploy/cloudflared-muse.yml run muse-pwa
```
Then in Cloudflare Zero Trust → Access, add a self-hosted app for `muse.ridgestate.com` allowing only the operator email (free tier). Two gates total: Access + app token. Origin binds loopback only — `muse.ridgestate.com` is the only entry.

Status 2026-10-15: LIVE — systemd `muse-pwa.service` + `cloudflared-muse.service` (enabled, linger on), Tunnel healthy, Access-gated. First use: open the URL, sign in with the operator Gmail (emailed PIN), then paste the app token once.