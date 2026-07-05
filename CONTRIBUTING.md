# Contributing

How changes flow into this repo and out to production. Short version:

> **branch → PR (CI green) → squash-merge to `main` → merge the release PR → auto-deploy.**

`main` is always deployable. Nothing reaches production except by cutting a
version tag, so merging a PR is safe — it never deploys on its own. Versioning
and the changelog are handled automatically by
[release-please](https://github.com/googleapis/release-please): it reads the
Conventional Commit titles on `main` and keeps an open **release PR**; merging
that PR is what tags the version and ships it.

## 1. Branch

Never commit to `main` directly. Branch off the latest `main`:

```bash
git switch main && git pull
git switch -c fix/short-description      # or feat/… , chore/… , docs/…
```

## 2. Make the change

- Match the surrounding style; keep diffs focused (one logical change per PR).
- Add or update tests for any behaviour change — see `tests/`.
- **Do not** edit `CHANGELOG.md`, bump the version, or tag. release-please
  generates all of that from your commit. Just make sure the change is described
  by your Conventional Commit title (this becomes the changelog line): `fix:` →
  patch, `feat:` → minor, `feat!:` / `BREAKING CHANGE:` → major.

## 3. Check locally (same gates as CI)

All tooling runs through [uv](https://docs.astral.sh/uv/) — no manual venvs.

```bash
uv sync --frozen          # exact locked deps (regenerate uv.lock only if you change deps)
uv run pytest -q
uv run ruff check src tests
uv run ty check src
```

Green on all three ≈ green in CI. If you changed dependencies, commit the updated
`uv.lock`.

## 4. Open a PR

```bash
git push -u origin HEAD
gh pr create --fill --base main
```

The **CI / Release** workflow runs the `test` job (pytest + ruff + ty) on every
PR. Image build and Fly deploy are **skipped for PRs** — they only run on pushes
to `main` and on version tags. Merge only when CI is green.

## 5. Merge

**Squash-merge** into `main` so history stays one commit per change:

```bash
gh pr merge --squash --delete-branch
```

Use a [Conventional Commits](https://www.conventionalcommits.org/) title, matching
the existing history — e.g. `fix(normalize): …`, `feat(web): …`, `chore: …`.

Merging to `main` re-runs `test` and builds a `sha`-tagged image to GHCR. **It does
not deploy.**

## 6. Release & deploy

You don't cut releases by hand. release-please (`.github/workflows/release-please.yml`)
watches `main` and keeps an open **release PR** titled `chore: release X.Y.Z`. It
continuously updates that PR with the next version (derived from the Conventional
Commits since the last release) and the generated `CHANGELOG.md` section.

**To ship: merge the release PR.** That is the whole release step. Merging it:

1. commits the version bump (`pyproject.toml`) + changelog,
2. pushes the `vX.Y.Z` tag and creates the GitHub Release,
3. the tag fires `deploy.yml`'s `build → deploy` jobs: a versioned GHCR image,
   then `flyctl deploy` to Fly.io. The new machine self-refreshes the catalog on
   startup. Full details and the one-time Fly setup are in [`DEPLOY.md`](DEPLOY.md).

Batch several merged PRs into one release simply by waiting to merge the release
PR — it keeps accumulating until you do. Versions follow
[SemVer](https://semver.org/): patch for fixes, minor for backwards-compatible
features, major for breaking changes.

> The release PR only works once the `RELEASE_PLEASE_APP_ID` /
> `RELEASE_PLEASE_PRIVATE_KEY` secrets (the release-please GitHub App) are set —
> see [`DEPLOY.md`](DEPLOY.md).

## Quick reference

| Action | Command |
|---|---|
| Start work | `git switch -c fix/… main` |
| Local gates | `uv run pytest -q && uv run ruff check src tests && uv run ty check src` |
| Open PR | `gh pr create --fill --base main` |
| Merge | `gh pr merge --squash --delete-branch` |
| Ship it | merge the `chore: release X.Y.Z` PR that release-please opens |
