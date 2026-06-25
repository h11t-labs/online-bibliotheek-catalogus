#!/usr/bin/env bash
# Provision the Railway app with the Railway CLI — simpler than Terraform: no
# provider, no state file. Run once on a fresh project after `railway login`.
#
# Override any default via env, e.g.:
#   NYT_API_KEY=... IMAGE=ghcr.io/mymix/online-bibliotheek-catalogus:0.2 ./railway-setup.sh
set -euo pipefail

PROJECT="${PROJECT_NAME:-online-bibliotheek-catalogus}"
SERVICE="${SERVICE_NAME:-web}"
IMAGE="${IMAGE:-ghcr.io/mymix/online-bibliotheek-catalogus:0.1}"
SYNC_HOURS="${OBC_SYNC_HOURS:-24}"
LISTS_HOURS="${OBC_LISTS_HOURS:-168}"

command -v railway >/dev/null || { echo "railway CLI not found — see https://docs.railway.com/develop/cli"; exit 1; }
railway whoami >/dev/null 2>&1 || { echo "Not logged in. Run: railway login"; exit 1; }

echo "▶ creating + linking project '$PROJECT'…"
railway init --name "$PROJECT" >/dev/null

echo "▶ adding service '$SERVICE' from $IMAGE…"
vars=(--variables "OBC_DB=/app/data/catalog.db"
      --variables "OBC_SYNC_HOURS=$SYNC_HOURS"
      --variables "OBC_LISTS_HOURS=$LISTS_HOURS")
[ -n "${NYT_API_KEY:-}" ] && vars+=(--variables "NYT_API_KEY=$NYT_API_KEY")
railway add --service "$SERVICE" --image "$IMAGE" "${vars[@]}"

echo "▶ attaching a 1 GB volume at /app/data…"
railway volume add --service "$SERVICE" --mount-path /app/data

echo "▶ generating a public domain…"
railway domain --service "$SERVICE" || true

cat <<EOF

✓ Done. Next:
  1. If the GHCR package is private, make it public OR add registry credentials
     to the service in the Railway dashboard so it can pull the image.
  2. Wire CI auto-deploy:
       gh variable set RAILWAY_SERVICE --repo mymix/online-bibliotheek-catalogus --body "$SERVICE"
       gh secret   set RAILWAY_TOKEN   --repo mymix/online-bibliotheek-catalogus   # paste a Railway token
EOF
