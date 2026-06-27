# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.6] - 2026-06-27

### Added
- The NYT bestseller lists now work on Fly: the deploy pipeline syncs `NYT_API_KEY`
  (from CI secrets) to the app before deploy, so the on-box refresh fetches them.

### Changed
- The deployed image is tagged with the release version (e.g. `v0.3.6`) so it shows in
  `fly image show` / the Fly dashboard. (Fly's own `v1, v2, …` release counter in
  `fly status` is internal and can't be set to our semver.)

## [0.3.5] - 2026-06-27

### Added
- "Last updated" dates: the footer shows when the catalogus was last (re)built, and
  each list (on `/lists` and the list page) shows its `Bijgewerkt op` date.
- The Bestseller 60 lists now show the **week + date range** they cover
  (e.g. "week 26 · 22 t/m 28 juni 2026").
- Award lists now distinguish **winners from nominees** — entries show
  "· gewonnen" / "· genomineerd" on book, author and list pages (parsed from the
  Wikipedia section structure).

### Changed
- The **About page** (`/over`) is rewritten for visitors: plain language about what
  the site is and how to borrow, without the technical jargon.

## [0.3.4] - 2026-06-27

### Fixed
- Root cause of the persistent `ENOSPC`: the ~64k per-book record files exhausted the
  **inode table** of the 1GB ext4 volume (plenty of free blocks, but no free inodes),
  so any new file write failed. The deploy pipeline now ensures the catalog volume is
  **≥ 2GB** (idempotent — extend only when smaller, before deploy so the machine
  remounts the grown filesystem), and `fly.toml` sizes a fresh volume at 2GB.

## [0.3.3] - 2026-06-27

### Fixed
- Refresh still hit `ENOSPC` on the 1GB volume because the old `catalog.db` (~DB-sized)
  sat next to the rebuild. The reclaim step now also drops the DB file itself up front
  (it is rebuilt from `data/raw`), and logs free space + record count so a stubborn
  full volume is diagnosable.

## [0.3.2] - 2026-06-27

### Fixed
- The Fly volume stayed full across refreshes (so even the incremental sync + lists
  writes hit `ENOSPC`): earlier interrupted WAL-mode rebuilds had left a ~DB-sized
  `catalog.db-wal` behind. Every refresh now **reclaims disk first** — dropping stale
  WAL/journal sidecars and the on-disk HTML cache — so the sync and the journal-less
  rebuild fit the 1GB volume without growing it.

## [0.3.1] - 2026-06-27

### Fixed
- The on-box catalog rebuild ran out of disk on the 1GB Fly volume (`ENOSPC`): a
  full rebuild now runs with `PRAGMA journal_mode = OFF`, so peak disk is ~the DB
  size instead of the DB **plus** an equal-size WAL. It fits without growing (or
  paying for) a bigger volume. Safe because the rebuild is re-runnable from `data/raw`.
- The weekly cron machine restart-looped: its `curl -f` exited non-zero on a 409
  ("refresh already running") or a brief connection error, so Fly kept restarting it.
  It now uses `curl -sS … || true`, and the deploy pipeline destroy+recreates the
  `catalog-cron` machine so command changes land.

## [0.3.0] - 2026-06-27

### Added
- Header **theme switcher** cycling System / Light / Dark, persisted in
  localStorage. The dark palette now applies via an explicit `data-theme` override
  too (not only `prefers-color-scheme`), so a theme can be forced.
- **`/over`** — a static About page (project, data source, how it works), rendered
  independently of the catalog DB.

### Changed
- The catalog now **builds/refreshes itself** — no manual seeding:
  - **After every deploy**: the new machine triggers a refresh on startup
    (`OBC_REFRESH_ON_STARTUP=1`), self-seeding — a full harvest on a fresh volume,
    otherwise an incremental sync, then lists + normalize, in a background thread.
  - **On a schedule**: a weekly Fly **cron machine** (`catalog-cron`) POSTs the
    token-protected `/admin/refresh` over Fly's private network. It is provisioned
    **by the deploy pipeline** (idempotently), not by hand.

## [0.2.0] - 2026-06-26

### Added
- Fly.io deploy config (`fly.toml`): private image via Fly's registry, SQLite on a
  Fly Volume, region Amsterdam (EU). DEPLOY.md documents it as the primary target.
- Token-protected `POST /admin/refresh` endpoint + `scripts/fly-cron.sh`: a stateless
  Fly scheduled (cron) machine triggers the weekly refresh, which runs in-process
  (where the volume is). Guarded by `OBC_REFRESH_TOKEN`.
- `ruff` lint/format config and a **hermetic** test suite: a tiny fixture catalog is
  built in-memory via `db.bulk_load` (see `tests/conftest.py`/`tests/sampledata.py`),
  so the db, normalize, query and web tests run anywhere without the real
  `catalog.db`. New `test_db`, `test_normalize`, `test_queries`, `test_lists`.

### Changed
- `normalize` now streams records in batches (`db.stream_rebuild`) instead of loading
  the whole catalog into memory — peak RSS ~190MB instead of ~600MB, so the weekly
  refresh runs on a 512MB box. Output is identical.
- Web layer restructured for single-responsibility: all SQL moved to `obc.web.queries`
  (a read-only repository), the Wikipedia author-bio to `obc.web.bio`, leaving
  `obc.web.app` as thin routes. FastAPI `on_event` → `lifespan`. Shared helpers
  `obc.htmlutil.node_text` and `obc.util.read_json`/`write_json` de-duplicate the
  listing/detail parsers and the harvest/load pipeline. No behaviour change.

### Fixed
- List detail pages: a generic `.row` CSS rule leaked the card border/background onto
  the site header; scoped it under `.booklist`.
- Author pages now show the curated-list / prize ribbon on book covers, matching the
  search overview (the route already provided the data; only the template lacked it).

### Removed
- In-process interval scheduler — the weekly refresh is cron-triggered only (a Fly
  scheduled machine → `POST /admin/refresh`), so the web process holds no timers.

## [0.1.2] - 2026-06-25

### Added
- Render (EU/Frankfurt) deployment via `render.yaml` Blueprint, with a persistent
  disk for the catalog and an in-process refresh scheduler.
- `/healthz` liveness endpoint and a friendly "wordt opgebouwd" page shown while the
  catalog database isn't present yet.

### Removed
- Railway deployment (CI jobs, `railway.json`, `infrastructure/`) — replaced by Render.

## [0.1.1] - 2026-06-25

### Added
- Declarative, idempotent Railway provisioning via `infrastructure/railway-setup.sh`
  (creates only what's missing; upserts variables). Runs as the CI `infra` step.

### Changed
- CI/CD: versioned image tags only (no `latest`); GitHub Releases generated from the
  changelog; `infra`/`deploy` run on version tags only and skip cleanly when
  `RAILWAY_TOKEN` is unset; service name hardcoded to `web`.
- Bump GitHub Actions runner actions to Node 24 (`checkout@v5`, `setup-uv@v6`).

## [0.1.0] - 2026-06-25

First tagged release.

### Added
- Catalog harvester (`obc scrape`) enumerating onlinebibliotheek.nl via faceted
  browse pages, with resumable raw-JSON caching and an incremental `obc sync`.
- SQLite + FTS5 store (`obc normalize`) with diacritics-folded search.
- FastAPI + Jinja server-rendered UI: faceted search, book/author/series/list/stats
  pages, and search-bar autocomplete across every facet (titles, authors,
  publishers, genres, languages, lists).
- Curated lists: Bestseller 60, NYT bestsellers (needs `NYT_API_KEY`), and Dutch
  literary prizes via Wikipedia (Libris, Boekenbon, NS Publieksprijs). Award lists
  show the year; the `/lists` overview is sortable (name, availability, coverage).
- "Recently added" sort, conservative series detection, genre tagging via subject
  facets, publisher canonicalisation, and author-alias merging (e.g. Bernlef →
  J. Bernlef).
- Author pages with a Wikipedia bio block and the lists/awards the author appears on.
- loguru logging, web integration tests, and Railway deploy config (Dockerfile,
  volume, in-app scheduler for periodic refresh).
- CI/CD via GitHub Actions: test, build the Docker image, push to GHCR, deploy to
  Railway.

### Fixed
- `book_genres` was never populated (inverted id map), leaving genres empty.
- Non-language values ("Fictie", "Verzameld werk", …) polluting the language facet.

[Unreleased]: https://github.com/h11t-labs/online-bibliotheek-catalogus/compare/v0.1.2...HEAD
[0.1.2]: https://github.com/h11t-labs/online-bibliotheek-catalogus/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/h11t-labs/online-bibliotheek-catalogus/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/h11t-labs/online-bibliotheek-catalogus/releases/tag/v0.1.0
