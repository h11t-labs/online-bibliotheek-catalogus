#!/usr/bin/env bash
# Converge the Railway app to the desired state — idempotent, so it's safe to run
# on every release from CI. Only missing resources are created; variables are
# upserted. Simpler than Terraform: no provider, no state file.
#
# Auth:
#   - CI: set RAILWAY_TOKEN (a project token scopes all commands to the project).
#   - Local: run `railway login` + `railway link` first (or set CREATE_PROJECT=1
#     on the very first run to create the project).
#
# Override defaults via env: PROJECT_NAME, SERVICE_NAME, IMAGE, OBC_SYNC_HOURS,
# OBC_LISTS_HOURS, NYT_API_KEY.
set -euo pipefail

PROJECT="${PROJECT_NAME:-online-bibliotheek-catalogus}"
SERVICE="${SERVICE_NAME:-web}"
IMAGE="${IMAGE:-ghcr.io/mymix/online-bibliotheek-catalogus:0.1}"
SYNC_HOURS="${OBC_SYNC_HOURS:-24}"
LISTS_HOURS="${OBC_LISTS_HOURS:-168}"

command -v railway >/dev/null || { echo "railway CLI not found — https://docs.railway.com/develop/cli"; exit 1; }

# Exit 0 if a "name" equal to $1 exists anywhere in the JSON on stdin.
# (Uses python3 -c so the piped JSON stays on stdin.)
json_has_name() {
  python3 -c '
import sys, json
target = sys.argv[1]
def names(o):
    if isinstance(o, dict):
        for k, v in o.items():
            if k == "name" and isinstance(v, str):
                yield v
            yield from names(v)
    elif isinstance(o, list):
        for i in o:
            yield from names(i)
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(1)
sys.exit(0 if target in set(names(data)) else 1)
' "$1"
}

# 0. Project context.
if ! railway status >/dev/null 2>&1; then
  if [ "${CREATE_PROJECT:-0}" = "1" ]; then
    echo "▶ creating project '$PROJECT'…"
    railway init --name "$PROJECT" >/dev/null
  else
    echo "No linked Railway project. Run 'railway link' (local) or set CREATE_PROJECT=1." >&2
    exit 1
  fi
fi

# 1. Service — create only if absent.
if railway service list --json 2>/dev/null | json_has_name "$SERVICE"; then
  echo "✓ service '$SERVICE' already exists"
else
  echo "▶ creating service '$SERVICE' from $IMAGE…"
  railway add --service "$SERVICE" --image "$IMAGE"
fi

# 2. Variables — idempotent upsert (no deploy trigger; the deploy job handles that).
echo "▶ ensuring variables…"
railway variable set "OBC_DB=/app/data/catalog.db" --service "$SERVICE" --skip-deploys >/dev/null
railway variable set "OBC_SYNC_HOURS=$SYNC_HOURS"   --service "$SERVICE" --skip-deploys >/dev/null
railway variable set "OBC_LISTS_HOURS=$LISTS_HOURS" --service "$SERVICE" --skip-deploys >/dev/null
if [ -n "${NYT_API_KEY:-}" ]; then
  railway variable set "NYT_API_KEY=$NYT_API_KEY" --service "$SERVICE" --skip-deploys >/dev/null
fi

# 3. Volume at /app/data — create only if absent.
if railway volume list --json 2>/dev/null | grep -q "/app/data"; then
  echo "✓ volume at /app/data already exists"
else
  echo "▶ adding volume at /app/data…"
  railway volume add --service "$SERVICE" --mount-path /app/data
fi

# 4. Public domain — generate only if none.
if railway domain list --service "$SERVICE" --json 2>/dev/null | grep -q "railway.app"; then
  echo "✓ public domain already exists"
else
  echo "▶ generating public domain…"
  railway domain --service "$SERVICE" || true
fi

echo "✓ infra converged."
