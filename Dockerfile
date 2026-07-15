# syntax=docker/dockerfile:1
FROM python:3.14-slim

# uv for fast, reproducible installs (uses the committed uv.lock)
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /uvx /bin/

WORKDIR /app

# Install dependencies first (better layer caching)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Then the source + editable install of the project
COPY . .
RUN uv sync --frozen --no-dev

# Catalog DB lives on the mounted volume (see DEPLOY.md). The web app and CLI
# both read OBC_DB.
ENV OBC_DB=/app/data/catalog.db

# Fly provides $PORT (fly.toml sets it explicitly).
#
# --host '' (empty) is deliberate: it must listen on BOTH stacks.
#   * Fly's edge proxy reaches the app over IPv4  -> the public site + healthz.
#   * Fly's private network (.internal / 6PN) is IPv6-only -> the daily refresh
#     cron POSTing to app.process.<app>.internal:8000.
# An empty host makes asyncio bind one socket per family (documented behaviour),
# which is the only value that serves both. Measured in this image:
#   --host 0.0.0.0 -> IPv4 only (6PN cron gets "connection refused"; the refresh
#                     silently never runs while the site looks healthy)
#   --host ::      -> IPv6 only (asyncio forces IPV6_V6ONLY=1, so it is NOT
#                     dual-stack despite bindv6only=0; this breaks the edge proxy)
# Keep this single-process: with --workers/--reload uvicorn binds the socket
# itself and an empty host would collapse back to IPv4-only.
CMD ["sh", "-c", "uv run uvicorn obc.web.app:app --host '' --port ${PORT:-8000}"]
