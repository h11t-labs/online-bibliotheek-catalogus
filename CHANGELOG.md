# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/h11t-labs/online-bibliotheek-catalogus/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/h11t-labs/online-bibliotheek-catalogus/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/h11t-labs/online-bibliotheek-catalogus/releases/tag/v0.1.0
