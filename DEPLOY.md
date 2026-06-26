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

## Weekly refresh (Fly cron → protected endpoint)

The refresh must run in the machine that owns the volume, so a stateless **Fly
scheduled (cron) machine** just triggers `POST /admin/refresh` over Fly's private
network; the app then runs `scrape --sync` + `lists update` + `normalize` in a
background thread (returns 202 immediately). The endpoint is protected by a bearer
token so only the cron can call it.

```bash
fly secrets set OBC_REFRESH_TOKEN=$(openssl rand -hex 32)   # shared secret
scripts/fly-cron.sh                                         # create the weekly cron machine
```

`normalize` streams at ~190MB, so this runs on the 512MB VM — no scaling. (For
hosts without scheduled machines you can instead set `OBC_SYNC_HOURS` /
`OBC_LISTS_HOURS` to run the same work on an in-process interval.)

## Environment variables

| Variable           | Example                | Purpose                                               |
|--------------------|------------------------|-------------------------------------------------------|
| `OBC_DB`           | `/app/data/catalog.db` | DB path (set in `fly.toml`/`render.yaml` + Dockerfile)|
| `OBC_REFRESH_TOKEN`| `…` (secret)           | Bearer token guarding `POST /admin/refresh`           |
| `NYT_API_KEY`      | `…` (secret)           | Optional — enables the NYT bestseller lists           |
| `OBC_SYNC_HOURS`   | `0`                    | Optional fallback: in-process interval (0 = off)      |
| `OBC_LISTS_HOURS`  | `0`                    | Optional fallback: in-process interval (0 = off)      |

`PORT` is provided by the host automatically.

## Releasing a version

CI (`.github/workflows/deploy.yml`) on a version tag builds a versioned image to
GHCR (provenance) and creates a GitHub Release from `CHANGELOG.md`. To cut one:

```bash
scripts/release.sh 0.2.0          # bumps pyproject + CHANGELOG, commits, tags v0.2.0
git push origin main --follow-tags
```

The push to `main` is what Render deploys; the tag adds the changelog-backed
GitHub Release.
