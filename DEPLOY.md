# Deploying to Railway

The app is a single FastAPI web service backed by a SQLite file. On Railway we run
it from the `Dockerfile`, keep the database on a **persistent volume**, and let the
service refresh itself on a schedule (Railway volumes attach to one service, so the
"cron" runs inside the web service — see below).

## CI/CD: build in GitHub Actions, deploy to Railway

`.github/workflows/deploy.yml` runs on every push to `main`:

1. **test** — `uv sync` + `uv run pytest`.
2. **build** — builds the Docker image and pushes it to **GHCR**
   (`ghcr.io/<owner>/online-bibliotheek-catalogus:latest` and `:sha-…`).
3. **deploy** — runs `railway redeploy`, so Railway pulls the freshly built image.

### One-time setup

1. **Railway service from the image.** Create the service with source =
   **Docker image** → `ghcr.io/h11t-labs/online-bibliotheek-catalogus:latest`
   (Railway → New → Docker Image). Add the volume + env vars as below.
2. **Let Railway pull the image.** The repo is private, so the GHCR package is
   private too. Either:
   - make the package public (Repo → Packages → the image → *Package settings →
     Change visibility*), since the image bakes in no secrets; **or**
   - in the Railway service add registry credentials: variables
     `ghcr.io` username `<github-user>` + a PAT with `read:packages`.
3. **GitHub secrets/variables** (Repo → Settings → Secrets and variables → Actions):
   - secret **`RAILWAY_TOKEN`** — a Railway *project* token (Railway → project →
     Settings → Tokens).
   - variable **`RAILWAY_SERVICE`** — the exact Railway service name. The `deploy`
     job is skipped until this variable exists, so earlier pushes still build fine.

After that, every push to `main` ships automatically. You can also trigger it from
the Actions tab (**Run workflow**).

## Manual alternative — let Railway build from the repo

If you'd rather not use GHCR, skip the workflow and instead:

1. Push this repo to GitHub (private is fine).
2. Railway → **New Project → Deploy from GitHub repo** → pick this repo.
   Railway detects `railway.json` / `Dockerfile` and builds the image itself.

## 2. Add a volume (persistent SQLite)

1. On the service → **Variables/Settings → Volumes → New Volume**.
2. Mount path: **`/app/data`**.

Everything the app writes (`data/catalog.db`, `data/raw/…`) now lives on the volume
and survives deploys/restarts. `OBC_DB` is already set to `/app/data/catalog.db` in
the Dockerfile; both the web app and the `obc` CLI honour it.

## 3. Environment variables

| Variable          | Example                | Purpose                                             |
|-------------------|------------------------|-----------------------------------------------------|
| `OBC_DB`          | `/app/data/catalog.db` | DB path (already set in Dockerfile)                 |
| `NYT_API_KEY`     | `…`                    | Optional — enables the NYT bestseller lists         |
| `OBC_SYNC_HOURS`  | `24`                   | Run `obc sync` every N hours (0/unset = off)        |
| `OBC_LISTS_HOURS` | `168`                  | Run `obc lists update` + `obc normalize` every N h  |

`PORT` is provided by Railway automatically.

## 4. Seed the database

The volume starts empty, so populate it once. Two options:

**A. Build locally, upload.** Run the full pipeline on your own machine
(`uv run obc scrape --full && uv run obc lists update && uv run obc normalize`),
then copy `data/` to the volume with the Railway CLI:

```bash
railway link            # select the project/service
railway volume          # confirm the mount is /app/data
# copy the local DB (and raw data) up:
railway run --service <svc> 'mkdir -p /app/data'
# then use `railway ssh` or a one-off shell to scp/rsync, or:
cat data/catalog.db | railway run --service <svc> 'cat > /app/data/catalog.db'
```

**B. Scrape from Railway (recommended).** Railway's egress is not geo-gated the way
this dev sandbox is, so you can run the harvest there directly via a one-off command:

```bash
railway run --service <svc> 'uv run obc scrape --full && uv run obc lists update && uv run obc normalize'
```

(`obc scrape --full` ≈ a couple of hours; it's resumable — cached pages in
`data/raw/` on the volume are skipped on re-run.)

## 5. Scheduled refresh ("cron")

Set `OBC_SYNC_HOURS` (e.g. `24`) and `OBC_LISTS_HOURS` (e.g. `168`). On startup the
web service spawns daemon threads that shell out to `obc sync` and
`obc lists update && obc normalize` on those intervals, writing to the same volume.
Watch the deploy logs (loguru lines prefixed `[cron]`) to confirm runs.

> Prefer a separate Railway **Cron service**? You can, but a Railway volume can only
> attach to one service, so a separate cron service can't write the DB the web app
> serves. The in-service scheduler above avoids that limitation.

## 6. Verify

Open the generated URL → search, open a book, `/lists`, `/stats`. Done.
