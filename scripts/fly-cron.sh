#!/usr/bin/env bash
# Create the weekly Fly scheduled (cron) machine that triggers the catalog
# refresh. It's stateless — it just POSTs to /admin/refresh on the web app over
# Fly's private network; the actual work runs in the web machine (where the
# volume is). The shared token comes from the app secret OBC_REFRESH_TOKEN.
#
# One-time prerequisite:
#   fly secrets set OBC_REFRESH_TOKEN=$(openssl rand -hex 32)
set -euo pipefail

APP="${FLY_APP:-online-bibliotheek-catalogus}"
REGION="${FLY_REGION:-ams}"
URL="http://${APP}.internal:8000/admin/refresh"

fly machine run curlimages/curl:latest \
  --app "$APP" \
  --schedule weekly \
  --region "$REGION" \
  --entrypoint /bin/sh \
  -- -c "curl -fsS -m 30 -X POST -H \"Authorization: Bearer \$OBC_REFRESH_TOKEN\" $URL"

echo "Weekly cron machine created. It will POST to $URL every week."
