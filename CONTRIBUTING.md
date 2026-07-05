# Contributing

How changes flow into this repo and out to production. Short version:

> **branch → PR (CI green) → squash-merge to `main` → tag a release → auto-deploy.**

`main` is always deployable. Nothing reaches production except by cutting a
version tag, so merging a PR is safe — it never deploys on its own.

## 1. Branch

Never commit to `main` directly. Branch off the latest `main`:

```bash
git switch main && git pull
git switch -c fix/short-description      # or feat/… , chore/… , docs/…
```

## 2. Make the change

- Match the surrounding style; keep diffs focused (one logical change per PR).
- Add or update tests for any behaviour change — see `tests/`.
- Add a bullet under `## [Unreleased]` in [`CHANGELOG.md`](CHANGELOG.md) for
  anything user-visible. **Do not** bump the version or tag — `scripts/release.sh`
  does that at release time.

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
PR. Image build, GitHub Release, and Fly deploy are **skipped for PRs** — they
only run on pushes to `main` and on version tags. Merge only when CI is green.

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

Deployment is driven by a **`vX.Y.Z` git tag**, not by merging. When you want the
merged changes live (batch several PRs into one release if you like):

```bash
git switch main && git pull
scripts/release.sh 0.3.29           # bumps pyproject + promotes [Unreleased] in CHANGELOG, commits, tags v0.3.29
git push origin main --follow-tags
```

The tag fires the `build → release → deploy` jobs: a versioned GHCR image, a GitHub
Release from the changelog section, then `flyctl deploy` to Fly.io. The new machine
self-refreshes the catalog on startup. Full details and the one-time Fly setup are
in [`DEPLOY.md`](DEPLOY.md).

Follow [SemVer](https://semver.org/): patch for fixes, minor for backwards-compatible
features, major for breaking changes.

## Quick reference

| Action | Command |
|---|---|
| Start work | `git switch -c fix/… main` |
| Local gates | `uv run pytest -q && uv run ruff check src tests && uv run ty check src` |
| Open PR | `gh pr create --fill --base main` |
| Merge | `gh pr merge --squash --delete-branch` |
| Ship it | `scripts/release.sh X.Y.Z && git push origin main --follow-tags` |
