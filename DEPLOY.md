# Deploying (Render, EU/Frankfurt)

The app is a single FastAPI web service backed by a SQLite file, run from the
`Dockerfile`, with the database on a **persistent disk**. Scheduled refresh runs
in-process (`OBC_SYNC_HOURS` / `OBC_LISTS_HOURS`) because the disk attaches to a
single service.

Render builds the Dockerfile straight from the **private** GitHub repo (via the
Render GitHub App), so there's no image registry or token to manage.

## Setup — `render.yaml`

1. **Render → New → Blueprint** → connect this repo. Render reads `render.yaml`
   (web service, Frankfurt, persistent disk at `/app/data`, health check `/healthz`).
2. Pick a paid instance (**Starter**+) — persistent disks aren't on the free tier.
3. Set the `NYT_API_KEY` env var in the dashboard (optional, enables the NYT lists).
4. Deploy. The site shows a friendly "wordt opgebouwd" page until the DB is on the
   disk (the `/healthz` endpoint stays 200, so health checks pass).

`autoDeploy: true` ships on every push to `main`.

## Seed / refresh the database

The disk starts empty. Open the service → **Shell** and run the harvest there
(runs in the container, writes to `/app/data` on the disk):

```bash
uv run obc scrape --full && uv run obc lists update && uv run obc normalize
```

`obc scrape --full` takes a while and is resumable. Alternatively upload a locally
built `data/catalog.db` to `/app/data` via the Render shell.

After the first build, the in-process scheduler keeps it fresh: `obc sync` every
`OBC_SYNC_HOURS` and `obc lists update` + `obc normalize` every `OBC_LISTS_HOURS`
(loguru lines prefixed `[cron]` in the logs).

## Environment variables

| Variable          | Example                | Purpose                                            |
|-------------------|------------------------|----------------------------------------------------|
| `OBC_DB`          | `/app/data/catalog.db` | DB path (set in `render.yaml` + Dockerfile)        |
| `NYT_API_KEY`     | `…`                    | Optional — enables the NYT bestseller lists        |
| `OBC_SYNC_HOURS`  | `24`                   | Run `obc sync` every N hours (0/unset = off)       |
| `OBC_LISTS_HOURS` | `168`                  | Run `obc lists update` + `obc normalize` every N h |

`PORT` is provided by Render automatically.

## Releasing a version

CI (`.github/workflows/deploy.yml`) on a version tag builds a versioned image to
GHCR (provenance) and creates a GitHub Release from `CHANGELOG.md`. To cut one:

```bash
scripts/release.sh 0.2.0          # bumps pyproject + CHANGELOG, commits, tags v0.2.0
git push origin main --follow-tags
```

The push to `main` is what Render deploys; the tag adds the changelog-backed
GitHub Release.
