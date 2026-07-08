#!/usr/bin/env bash
# Manual fallback — normally the deploy pipeline provisions this (see the
# "(Re)provision daily refresh cron machine" step in .github/workflows/deploy.yml).
#
# Creates the daily Fly scheduled (cron) machine that triggers the catalog
# refresh. It's stateless — it just POSTs to /admin/refresh on the web app over
# Fly's private network; the actual work runs in the web machine (where the
# volume is). The shared token comes from the app secret OBC_REFRESH_TOKEN.
#
# The curl must NOT use -f and must exit 0, or a 409 ("already running") / brief
# connection error makes the machine restart-loop.
#
# One-time prerequisite:
#   fly secrets set OBC_REFRESH_TOKEN=$(openssl rand -hex 32)
set -euo pipefail

APP="${FLY_APP:-online-bibliotheek-catalogus}"
REGION="${FLY_REGION:-ams}"
URL="http://${APP}.internal:8000/admin/refresh"

fly machine run curlimages/curl:latest \
  --app "$APP" \
  --name catalog-cron \
  --schedule daily \
  --region "$REGION" \
  --entrypoint /bin/sh \
  -- -c "curl -sS -m 60 -X POST -H \"Authorization: Bearer \$OBC_REFRESH_TOKEN\" $URL || true"

echo "Daily cron machine created. It will POST to $URL every day."
