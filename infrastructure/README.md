# Infrastructure (Railway)

The simplest way to provision Railway is the **CLI script** here — it uses your
logged-in Railway session, so there's no provider, no state file, and no token to
manage for setup.

## One-time provisioning

```bash
railway login            # once, opens the browser
cd infrastructure
./railway-setup.sh       # creates project + service (from the GHCR image) + volume + domain
```

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
- CI auto-deploy on version tags needs the `RAILWAY_SERVICE` variable and
  `RAILWAY_TOKEN` secret in GitHub (see the end of `railway-setup.sh` and `DEPLOY.md`).
- Prefer full infra-as-code with Terraform instead? An earlier Terraform version of
  this folder is in the git history.
