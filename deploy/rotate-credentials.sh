#!/bin/bash
# rotate-credentials.sh — muse-pwa credential rotation (operator-ordered 2026-10-15).
# Rotates BOTH: (1) Cloudflare Access service-token secret via API refresh
# (client_id unchanged, policy untouched), (2) PWA orchestrator Bearer
# (regen + restart server/worker, which load tokens once at startup).
# Fresh values print to STDOUT ONLY — never persisted. No secret literals in this file.
# Run: bash ~/muse-code-cli-pwa/deploy/rotate-credentials.sh
set -euo pipefail

MPWA=~/muse-code-cli-pwa
ENV_FILE=~/.cloudflared/muse-pwa-svctoken.env
ORCH_FILE=$MPWA/data/orchestrator-token.txt
# NOTE: tunnel is remotely-managed → no local credentials/AccountTag file exists.
# Account + token are discovered purely via API below (client_id match).

need() { command -v "$1" >/dev/null || { echo "MISSING: $1"; exit 1; }; }
need curl; need python3; need systemctl

# --- Step 0: refuse if a headless task is mid-run (restart would SIGTERM it) ---
RUNNING=$(python3 -c "import json;ts=json.load(open('$MPWA/data/tasks.json'));print(sum(1 for t in ts if t.get('status')=='running'))" 2>/dev/null || echo 0)
[ "$RUNNING" != "0" ] && { echo "ABORT: $RUNNING task(s) status=running. Wait or mark failed, then re-run."; exit 2; }
echo "step0: no running tasks; worker restart safe."

# --- Step 1: orchestrator Bearer regen (local; exact bytes, no trailing newline) ---
NEW_ORCH=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')
printf '%s' "$NEW_ORCH" > "$ORCH_FILE"
chmod 600 "$ORCH_FILE"
echo "step1: orchestrator file rewritten (0600, no trailing newline)."

# --- Step 2: CF rotation = CREATE new + swap policy include + DELETE old ---
# (POST .../refresh returns success but NO client_secret — proven 00416; it does not rotate secrets.)
CF_TOKEN="$(sed -n '1p' ~/.config/cloudflare/api-token.env | cut -d= -f2-)"
CLIENT_ID="$(sed -n '1p' "$ENV_FILE" | sed 's/^CF-Access-Client-Id: //')"
ACCTS=$(curl -s -H "Authorization: Bearer $CF_TOKEN" 'https://api.cloudflare.com/client/v4/accounts')
ACCT_IDS=$(echo "$ACCTS" | python3 -c "import json,sys; print(' '.join(a['id'] for a in json.load(sys.stdin).get('result',[])))")
[ -z "$ACCT_IDS" ] && { echo "CF ACCOUNTS LIST FAILED:"; echo "$ACCTS" | head -c 400; echo; exit 3; }
TOKEN_ID=""; ACCT=""
for AID in $ACCT_IDS; do
  L=$(curl -s -H "Authorization: Bearer $CF_TOKEN" "https://api.cloudflare.com/client/v4/accounts/$AID/access/service_tokens")
  TID=$(echo "$L" | python3 -c "import json,sys; print(' '.join(t['id'] for t in json.load(sys.stdin).get('result',[]) if t.get('client_id')=='$CLIENT_ID'))")
  if [ -n "$TID" ]; then ACCT="$AID"; TOKEN_ID="$TID"; break; fi
done
[ -z "$TOKEN_ID" ] && { echo "CF LOOKUP FAILED: no service token with client_id $CLIENT_ID."; exit 3; }
echo "step2a: found old token $TOKEN_ID in account $ACCT."
CRT=$(curl -s -X POST -H "Authorization: Bearer $CF_TOKEN" -H 'Content-Type: application/json' -d '{"name":"muse-pwa-orchestrator-20261015"}' "https://api.cloudflare.com/client/v4/accounts/$ACCT/access/service_tokens")
COK=$(echo "$CRT" | python3 -c "import json,sys;print(json.load(sys.stdin).get('success'))")
[ "$COK" != "True" ] && { echo "CF CREATE FAILED:"; echo "$CRT" | head -c 600; echo; exit 3; }
NEW_TID=$(echo "$CRT" | python3 -c "import json,sys;print(json.load(sys.stdin)['result']['id'])")
NEW_ID=$(echo "$CRT" | python3 -c "import json,sys;print(json.load(sys.stdin)['result']['client_id'])")
NEW_SECRET=$(echo "$CRT" | python3 -c "import json,sys;print(json.load(sys.stdin)['result']['client_secret'])")
[ -z "$NEW_SECRET" ] && { echo "CF CREATE returned empty secret; new token $NEW_TID orphaned — delete manually."; exit 3; }
echo "step2b: new token $NEW_TID created."
APPS=$(curl -s -H "Authorization: Bearer $CF_TOKEN" "https://api.cloudflare.com/client/v4/accounts/$ACCT/access/apps")
APP_ID=$(echo "$APPS" | python3 -c "import json,sys; print(' '.join(a['id'] for a in json.load(sys.stdin).get('result',[]) if a.get('name')=='Muse Remote'))")
[ -z "$APP_ID" ] && { echo "CF APP LOOKUP FAILED. New token $NEW_TID orphaned — delete manually."; exit 3; }
POLS=$(curl -s -H "Authorization: Bearer $CF_TOKEN" "https://api.cloudflare.com/client/v4/accounts/$ACCT/access/apps/$APP_ID/policies")
POL_ID=$(echo "$POLS" | python3 -c "import json,sys; o='$TOKEN_ID'; print(' '.join(p['id'] for p in json.load(sys.stdin).get('result',[]) for inc in p.get('include',[]) if (inc.get('service_token') or {}).get('token_id')==o))")
[ -z "$POL_ID" ] && { echo "CF POLICY LOOKUP FAILED. New token $NEW_TID orphaned — delete manually."; exit 3; }
case "$POL_ID" in *\ *) echo "MULTIPLE policies match old token; aborting. New token $NEW_TID orphaned — delete manually."; exit 3;; esac
PGET=$(curl -s -H "Authorization: Bearer $CF_TOKEN" "https://api.cloudflare.com/client/v4/accounts/$ACCT/access/apps/$APP_ID/policies/$POL_ID")
PBODY=$(echo "$PGET" | python3 -c "import json,sys; p=json.load(sys.stdin)['result']; p={k:v for k,v in p.items() if k not in ('id','created_at','updated_at')}; print(json.dumps(p).replace('$TOKEN_ID','$NEW_TID'))")
PUT=$(curl -s -X PUT -H "Authorization: Bearer $CF_TOKEN" -H 'Content-Type: application/json' -d "$PBODY" "https://api.cloudflare.com/client/v4/accounts/$ACCT/access/apps/$APP_ID/policies/$POL_ID")
POK=$(echo "$PUT" | python3 -c "import json,sys;print(json.load(sys.stdin).get('success'))")
[ "$POK" != "True" ] && { echo "CF POLICY SWAP FAILED:"; echo "$PUT" | head -c 600; echo "New token $NEW_TID orphaned — delete manually."; exit 3; }
echo "step2c: policy $POL_ID swapped to new token; polling edge for propagation (want 200 on /api/health):"
H=""
for i in $(seq 1 12); do
  H=$(curl -s -o /dev/null -w '%{http_code}' -H "CF-Access-Client-Id: $NEW_ID" -H "CF-Access-Client-Secret: $NEW_SECRET" https://${PWA_HOSTNAME:?set PWA_HOSTNAME}/api/health)
  echo "  poll $i: $H"
  [ "$H" = "200" ] && break
  sleep 10
done
[ "$H" != "200" ] && { echo "EDGE PROPAGATION FAILED after 2min. Policy swapped; old token kept — investigate."; exit 3; }
DEL=$(curl -s -X DELETE -H "Authorization: Bearer $CF_TOKEN" "https://api.cloudflare.com/client/v4/accounts/$ACCT/access/service_tokens/$TOKEN_ID")
DOK=$(echo "$DEL" | python3 -c "import json,sys;print(json.load(sys.stdin).get('success'))")
[ "$DOK" != "True" ] && echo "WARN: old token delete failed (dead secret anyway; manual cleanup): $(echo "$DEL" | head -c 200)"
CLIENT_ID="$NEW_ID"
{ echo "CF-Access-Client-Id: $CLIENT_ID"; echo "CF-Access-Client-Secret: $NEW_SECRET"; } > "$ENV_FILE"
chmod 600 "$ENV_FILE"
echo "step2: CF token rotated (new client_id); env rewritten 0600; old token deleted."

# --- Step 3: restart server+worker + local scope matrix ---
systemctl --user restart muse-pwa.service muse-pwa-worker.service
sleep 3
systemctl --user is-active muse-pwa.service muse-pwa-worker.service
OPTOK=$(cat $MPWA/data/token.txt)
code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }
echo "local /api/health: $(code http://127.0.0.1:3091/api/health) (want 200)"
echo "local orch GET /api/tasks: $(code -H "Authorization: Bearer $NEW_ORCH" http://127.0.0.1:3091/api/tasks) (want 200)"
echo "local orch POST /api/exec: $(code -X POST -H "Authorization: Bearer $NEW_ORCH" -H 'Content-Type: application/json' -d '{"cmd":"true"}' http://127.0.0.1:3091/api/exec) (want 401)"
echo "local oper GET /api/me: $(code -H "Authorization: Bearer $OPTOK" http://127.0.0.1:3091/api/me) (want 200)"

# --- Step 4: edge chain (fresh vs wrong-secret control; both carry the orch Bearer) ---
EDGE=https://${PWA_HOSTNAME:?set PWA_HOSTNAME}
GOOD=$(code -H "CF-Access-Client-Id: $CLIENT_ID" -H "CF-Access-Client-Secret: $NEW_SECRET" -H "Authorization: Bearer $NEW_ORCH" "$EDGE/api/me")
BAD=$(code -H "CF-Access-Client-Id: $CLIENT_ID" -H "CF-Access-Client-Secret: WRONGSECRET00000000000000000000000000000000" -H "Authorization: Bearer $NEW_ORCH" "$EDGE/api/me")
GATE=$(code "$EDGE/api/me")
echo "edge fresh-secret: $GOOD (want 200) | edge wrong-secret control: $BAD | edge no-creds: $GATE (want 302)"
if [ "$GOOD" = "200" ] && [ "$BAD" != "200" ]; then echo "EDGE VERDICT: service token PROVEN (discriminating).";
elif [ "$GOOD" = "200" ]; then echo "EDGE VERDICT: 200 but control also 200 — IP-bypass covers this host; token unproven from here, operator verifies remotely.";
else echo "EDGE VERDICT: fresh secret NOT 200 — investigate before relay."; exit 4; fi

# --- Step 5: relay (stdout only) ---
echo '--- RELAY (copy now; never persisted) ---'
echo "CF-Access-Client-Id: $CLIENT_ID"
echo "CF-Access-Client-Secret: $NEW_SECRET"
echo "ORCHESTRATOR_BEARER: $NEW_ORCH"
