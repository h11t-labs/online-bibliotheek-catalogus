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
# Target the web *process group* ("app"), NOT "${APP}.internal": the bare app name
# resolves to every machine in the app — including this cron machine itself, which
# has nothing on :8000 — so curl hits itself and fails with an instant "connection
# refused". "app.process.<app>.internal" only returns the web machine. Retries cover
# the brief window on a fresh scheduled boot before private DNS is ready.
URL="http://app.process.${APP}.internal:8000/admin/refresh"

fly machine run curlimages/curl:latest \
  --app "$APP" \
  --name catalog-cron \
  --schedule daily \
  --region "$REGION" \
  --entrypoint /bin/sh \
  -- -c "curl -sS --connect-timeout 15 --max-time 60 --retry 10 --retry-delay 6 --retry-connrefused --retry-all-errors -X POST -H \"Authorization: Bearer \$OBC_REFRESH_TOKEN\" $URL || true"

echo "Daily cron machine created. It will POST to $URL every day."
