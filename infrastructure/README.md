# Infrastructure (Railway)

Railway is provisioned by an **idempotent CLI script** — no provider, no state
file. It only creates what's missing and upserts variables, so it's safe to run
repeatedly. The CI/CD pipeline runs it automatically on every version tag (the
`infra` job, before `deploy`); you can also run it by hand.

## First-time / local run

```bash
railway login                       # once, opens the browser
cd infrastructure
CREATE_PROJECT=1 ./railway-setup.sh # first run: also creates + links the project
```

After the project exists, `railway link` it (or rely on `RAILWAY_TOKEN`) and just
run `./railway-setup.sh` to converge.

## In CI

On a version tag, the `infra` job runs `railway-setup.sh` with `RAILWAY_TOKEN` (a
project token scopes everything to the project), the minor image tag, and the
optional `NYT_API_KEY` secret. The service name is hardcoded to `web`. Both the
`infra` and `deploy` jobs skip cleanly if no `RAILWAY_TOKEN` is set, so releases
stay green before Railway is configured. Because it's idempotent, re-runs are
no-ops except for variable updates.

Override defaults with env vars:

```bash
NYT_API_KEY=xxx IMAGE=ghcr.io/h11t-labs/online-bibliotheek-catalogus:0.2 ./railway-setup.sh
```

What it sets up:

- project `online-bibliotheek-catalogus`
- service `web` from `ghcr.io/.../:0.1` with env vars (`OBC_DB`, `OBC_SYNC_HOURS`,
  `OBC_LISTS_HOURS`, optional `NYT_API_KEY`)
- a volume mounted at `/app/data` (persistent SQLite + raw data)
- a public `*.up.railway.app` domain

## Deploy settings as code

Per-deploy settings (builder, start command, restart policy) live in the repo's
[`railway.json`](../railway.json) — Railway reads it automatically. The in-app
scheduler handles periodic refresh via the `OBC_*_HOURS` env vars (no separate cron
service needed).

## Notes

- Private repo ⇒ private GHCR image: make the package public, or add registry
  credentials to the service in the dashboard so Railway can pull it.
- CI auto-deploy on version tags just needs the `RAILWAY_TOKEN` secret in GitHub
  (the service name is hardcoded to `web`); see `DEPLOY.md`.
- Prefer full infra-as-code with Terraform instead? An earlier Terraform version of
  this folder is in the git history.
