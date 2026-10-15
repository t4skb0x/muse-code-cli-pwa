# muse-pwa — silo laws
1. Origin binds 127.0.0.1:3091 only; public path = Cloudflare Tunnel `muse.ridgestate.com` + Access (operator email only) + app token. Never bind LAN/WAN directly.
2. `server.py` is the single writer for `data/chat.jsonl` + `data/audit.log`. No second writer.
3. File API confined to /home/algoprints; secret paths (.secrets/.ssh/*.env/*.pem/*.key/*secret*/*token*) are never served.
4. Every terminal exec + file save is audit-logged. Exec cap 30s / 64KB out.
5. Canon: ~/LAWS.md + farm map ~/AGENTS.md. Port 3091 registered in service-registry.json.