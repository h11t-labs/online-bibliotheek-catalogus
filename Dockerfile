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

# Railway provides $PORT
CMD ["sh", "-c", "uv run uvicorn obc.web.app:app --host 0.0.0.0 --port ${PORT:-8000}"]
