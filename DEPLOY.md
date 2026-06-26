# Deploying

The app is a single FastAPI web service backed by a SQLite file, run from the
`Dockerfile`, with the database on a **persistent volume/disk**. Scheduled refresh
runs in-process (`OBC_SYNC_HOURS` / `OBC_LISTS_HOURS`) because the volume attaches
to a single instance.

## Fly.io (recommended: cheap, private, EU/Amsterdam) — `fly.toml`

`fly deploy` builds the Dockerfile and stores the image in your **private** Fly
registry (nothing public), with SQLite on a Fly Volume. ~€2–3.5/month. Fly requires
a payment method on the account.

```bash
brew install flyctl            # or: curl -L https://fly.io/install.sh | sh
fly auth login
fly apps create online-bibliotheek-catalogus      # pick a unique name (match fly.toml)
fly volumes create catalog_data --region ams --size 1
fly secrets set NYT_API_KEY=...                    # optional (NYT lists)
fly deploy
```

First deploy shows the friendly "wordt opgebouwd" page until the DB is on the volume.
Seed it by uploading your locally built DB (avoids a memory-heavy scrape on a small
VM):

```bash
fly ssh sftp shell
> put data/catalog.db /app/data/catalog.db
```

(or run `fly ssh console -C "..."` to scrape on the box, but bump the VM memory first).

### Auto-deploy via CI

The `deploy` job in `.github/workflows/deploy.yml` runs `flyctl deploy` on every
version tag. Add a deploy token once:

```bash
fly tokens create deploy           # copy the token
gh secret set FLY_API_TOKEN --repo mymix/online-bibliotheek-catalogus   # paste it
```

Then `scripts/release.sh X.Y.Z && git push origin main --follow-tags` ships it.
(Without the secret the job skips, so releases stay green.)

Notes:
- One machine only (`min_machines_running = 1`) — SQLite is single-writer.
- `OBC_LISTS_HOURS=0` in `fly.toml`: a full `normalize` is memory-heavy for a 512MB
  VM. Refresh lists locally and re-upload, or temporarily `fly scale memory 1024`.

---

## Render (alternative, EU/Frankfurt) — `render.yaml`

Render builds the Dockerfile straight from the **private** GitHub repo (via the
Render GitHub App), so there's no image registry or token to manage. ~€6.70/month
(Starter + 1GB disk).

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
