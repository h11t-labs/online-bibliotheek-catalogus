# Deploying

The app is a single FastAPI web service backed by a SQLite file, run from the
`Dockerfile`, with the database on a **persistent volume/disk**. The daily
refresh runs in the web machine (where the volume is), triggered by a scheduled
cron machine hitting a token-protected endpoint — see below.

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
gh secret set FLY_API_TOKEN --repo h11t-labs/online-bibliotheek-catalogus   # paste it
```

Then merging the release PR (see [Releasing a version](#releasing-a-version)) ships it.
(Without the secret the job skips, so releases stay green.)

Notes:
- One machine only (`min_machines_running = 1`) — SQLite is single-writer.
- Record loading itself streams (~190MB peak), but `normalize` also precomputes the
  "meer zoals dit" recommendations, whose Truncated-SVD peaks at ~1.1GB on the 64k
  catalog. The 512MB VM absorbs that with `swap_size_mb = 1024` (fly.toml) — it's a
  nightly batch step, so paging costs land on a job nobody waits for. Serving stays
  within RAM.
- The image installs the `recommend` extra (scikit-learn); without it `normalize`
  logs a warning and skips the recommendations, leaving the catalog fully usable.

## Seed / refresh the database

The volume starts empty. Open a shell on the running machine with `fly ssh
console` and run the harvest there (writes to `/app/data` on the volume):

```bash
fly ssh console
$ uv run obc scrape --full && uv run obc lists update && uv run obc normalize
```

`obc scrape --full` takes a while and is resumable. Alternatively upload a locally
built `data/catalog.db` to `/app/data` via `fly ssh sftp shell` (as in the seed
step above) — lighter than scraping on a small VM.

## Daily refresh (Fly cron → protected endpoint)

The refresh must run in the machine that owns the volume, so a stateless **Fly
scheduled (cron) machine** just triggers `POST /admin/refresh` over Fly's private
network; the app then runs `scrape --sync` + `lists update` + `normalize` in a
background thread (returns 202 immediately). The endpoint is protected by a bearer
token so only the cron can call it.

```bash
fly secrets set OBC_REFRESH_TOKEN=$(openssl rand -hex 32)   # shared secret
scripts/fly-cron.sh                                         # create the daily cron machine
```

Runs on the 512MB VM — no scaling. Loading records streams at ~190MB; the
recommendation build inside `normalize` peaks at ~1.1GB and leans on the machine's
1GB swap (see fly.toml). It builds into the temp DB, so the atomic swap publishes the
catalog and its recommendations together — readers never see one without the other.

## Environment variables

| Variable           | Example                | Purpose                                               |
|--------------------|------------------------|-------------------------------------------------------|
| `OBC_DB`           | `/app/data/catalog.db` | DB path (set in `fly.toml` + Dockerfile)              |
| `OBC_REFRESH_TOKEN`| `…` (secret)           | Bearer token guarding `POST /admin/refresh`           |
| `NYT_API_KEY`      | `…` (secret)           | Optional — enables the NYT bestseller lists           |

`PORT` is provided by the host automatically.

## Releasing a version

Releases are automated by [release-please](https://github.com/googleapis/release-please)
(`.github/workflows/release-please.yml`). It reads the Conventional Commit titles
on `main`, keeps an open **`chore: release X.Y.Z` PR** with the next version +
generated `CHANGELOG.md`, and — when you merge that PR — pushes the `vX.Y.Z` tag
and creates the GitHub Release. The tag then triggers the `build` + `deploy` jobs
in `.github/workflows/deploy.yml` (versioned GHCR image, then `flyctl deploy`).

So the release step is simply: **merge the release PR.** No manual version bump,
changelog edit, or tag.

### One-time setup: the release-please GitHub App

release-please authenticates with a **GitHub App token** (not the default
`GITHUB_TOKEN`) so the tag it pushes actually triggers `deploy.yml`. Create it once:

1. **`https://github.com/settings/apps` → New GitHub App** (owned by the
   `h11t-labs` account). Uncheck the webhook. Repository permissions:
   **Contents: Read and write** + **Pull requests: Read and write**. Install it on
   **only** the `online-bibliotheek-catalogus` repo.
2. Note the **App ID**; **Generate a private key** (downloads a `.pem`).
3. Add repo secrets:
   ```bash
   gh secret set RELEASE_PLEASE_APP_ID --body "<app-id>"
   gh secret set RELEASE_PLEASE_PRIVATE_KEY < path/to/app.private-key.pem
   ```

`.release-please-manifest.json` records the current released version; don't hand-edit
it except in the break-glass case below.

### Break-glass (release-please is broken and you must ship)

The deploy pipeline keys off the tag, not the bot, so you can ship by hand:

```bash
git tag vX.Y.Z && git push origin vX.Y.Z          # triggers build + Fly deploy
```

Then bump `.release-please-manifest.json` (and `pyproject.toml`) to `X.Y.Z` on
`main` so release-please stays in sync, and add the `CHANGELOG.md` entry yourself.
