# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.19] - 2026-06-30

### Fixed
- Genre hierarchy parent is now resolved **per book's audience**. A genre name can exist
  in both jeugd and volwassenen with a *different* parent (e.g. *Misdaad & Mysterie* is a
  jeugd sub of *Spanning & Avontuur* but a volwassenen sub of *Spanning & Thrillers*), so
  a single name-keyed parent was always wrong for one audience. The parent moved off the
  genre row onto the per-book `book_genres.parent_id`, resolved within that book's
  audience. Supersedes the v0.3.18 code-namespacing. Takes effect on the next normalize.

## [0.3.18] - 2026-06-30

### Fixed
- Genre hierarchy showed the **wrong parent** (and duplicate chips): the jeugd and
  volwassenen subject facets reuse the same numbers (2.0 = jeugd *Natuur & Dieren* vs
  volwassenen *Literatuur & Romans*), so a sub-genre's parent code matched both
  audiences' top genres — e.g. jeugd *Wilde dieren* showed under *Literatuur & Romans*,
  and *Misdaad & Mysterie* appeared twice. Genre facet codes are now namespaced by
  audience, so a parent resolves within its own audience. Takes effect on the next
  `normalize` (no re-scrape needed — records already carry the audience).

## [0.3.17] - 2026-06-30

### Fixed
- Award lists marked **every** book as "gewonnen" (e.g. the Boekenbon Literatuurprijs):
  the Wikipedia parser only recognised a separate "Genomineerden" *section* as nominees,
  but that prize lists winner + nominees together per year in a table — recent years even
  spread the cells over separate lines. Now the first title each year is the winner and
  the rest are nominees, and trailing prose (Trivia) is skipped, so each year has exactly
  one winner. Takes effect on the next `lists update` + `normalize`.

## [0.3.16] - 2026-06-29

### Added
- Lighter load on the single small Fly VM (it was CPU-saturated by a bot crawling the
  68k-page sitemap): `Crawl-delay: 10` in `robots.txt` throttles well-behaved crawlers,
  and `Cache-Control: public` is set on the stable content pages (book/author/series/
  list/stats/over for an hour, the browse home for 10 min) so repeat + crawler hits are
  served from cache instead of recomputed each time.

## [0.3.15] - 2026-06-29

### Fixed
- Book pages were slow (~3–4s) on Fly while snappy locally: the "other editions of this
  work" lookup ran `lower(title)=… AND lower(author)=…`, which **full-scans the whole
  books table** on every page load — negligible on a fast local CPU, ~4s on Fly's
  shared-cpu-1x. Added a case-insensitive `(title, author)` expression index, turning it
  into an index lookup (~3 ms, ~150× faster). Applied on the next catalog rebuild.

## [0.3.14] - 2026-06-27

### Added
- Custom domain wired into the deploy pipeline: with the `OBC_DOMAIN` repo variable set,
  the deploy stages `OBC_SITE_URL=https://<domain>` (canonical/sitemap/OG) and
  idempotently requests Fly TLS certificates for the apex + `www`. DNS (A/AAAA on the
  apex, CNAME on `www`) is set once at the registrar.

## [0.3.13] - 2026-06-27

### Fixed
- Book detail pages showed "de catalogus wordt opgebouwd" (503) on a catalog DB built
  before the v0.3.12 `genres.code/parent` columns: the genre query referenced columns
  the not-yet-rebuilt DB lacked, raising `OperationalError`. `book_detail` now falls
  back to a flat genre list on the old schema, so detail pages keep working during the
  window between a schema-changing deploy and the next rebuild.

## [0.3.12] - 2026-06-27

### Added
- **Genre hierarchy**: the curated genres form a tree (e.g. *Natuur & Dieren* →
  *Dolfijnen & Walvissen* / *Wilde dieren*), derived from the detail-page facet codes
  (`major.minor` — `X.0` is a top-level genre, `X.Y` a sub-genre of `X.0`). Genres now
  carry a `code` + `parent`; book pages show the parent (*Natuur & Dieren › Wilde
  dieren*). The previously empty genre facet also fills itself via the enrich pass
  (the detail pages carry the genre names).

## [0.3.11] - 2026-06-27

### Added
- **Detail-page enrichment**: books now carry **Leeftijd** (reading age), an explicit
  **Serie** (name + number, more reliable than guessing it from the title),
  **keywords**, and fiction/non-fiction — parsed from the detail pages. New
  `age` / `keywords` / `category` columns; keywords are searchable. The refresh runs
  `scrape --enrich` when `OBC_ENRICH=1` (a one-time pass over every detail page —
  hours, resumable, zero-downtime, no HTML cache kept; then only new titles).

## [0.3.10] - 2026-06-27

### Fixed
- No more "de catalogus wordt opgebouwd" page during a routine refresh/deploy:
  `normalize` now builds a **temp DB and swaps it in atomically** (`os.replace`), so
  the web app keeps serving the old, complete catalog throughout the ~2–3 min rebuild
  instead of 503-ing while the tables are dropped/recreated. (The very first build on
  an empty volume still shows the page until there's data to serve.)

## [0.3.9] - 2026-06-27

### Changed
- GoatCounter now points to its own site (`obc.goatcounter.com`); dropped the
  host-prefix `path` config (a dedicated GoatCounter site per project is cleaner).

## [0.3.8] - 2026-06-27

### Changed
- GoatCounter is now wired to the `h11t-labs` counter with the host-prefixed `path`
  config (so one account can tell sites apart by host), replacing the env-driven
  placeholder. count.js skips localhost, so dev stays out of the stats.

## [0.3.7] - 2026-06-27

### Added
- **Privacy-friendly analytics**: an optional GoatCounter snippet, emitted only when
  `OBC_GOATCOUNTER` (the counter URL) is set — so nothing loads in dev or tests, and
  there are no cookies / consent banner.
- **SEO**: per-page `<meta name="description">`, `<link rel="canonical">`, Open Graph
  + Twitter cards (book covers as `og:image`), `schema.org/Book` JSON-LD on book
  pages, a `robots.txt`, and a paginated `sitemap.xml` (index → static + book
  sitemaps). Filtered search URLs are `noindex,follow` to keep the infinite facet
  space out of the index. `OBC_SITE_URL` sets the absolute origin for these URLs.

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
