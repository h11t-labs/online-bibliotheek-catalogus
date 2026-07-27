# online bibliotheek — eigen catalogus

A self-owned, fast, searchable catalog of the Dutch online library
([onlinebibliotheek.nl](https://www.onlinebibliotheek.nl/)), built because the
official search is poor.

Pipeline: **enumerate** every title → **fetch + parse** each detail page →
**normalize** into SQLite (+FTS5) → **search** in a minimal web UI.

## Stack

- Python 3.11+, all in `src/obc/`
- `httpx` + `beautifulsoup4`/`lxml` for fetching/parsing, `tenacity` for retries
- **SQLite + FTS5** storage & search (single file, no server)
- **FastAPI + Jinja2** server-rendered UI

## Setup

This project uses **[uv](https://docs.astral.sh/uv/)** for package management
(`uv.lock` is committed).

```bash
uv sync          # creates .venv and installs deps + the project
cp .env.example .env   # optional: configure secrets (see below)
```

### Configuration (`.env`)

The `obc` CLI auto-loads a `.env` file (copy from `.env.example`). Keys:

- `NYT_API_KEY` — to enable the New York Times Best Sellers lists. Get a **free**
  key at <https://developer.nytimes.com>: sign in → "Apps" → create an app →
  enable the **Books API** → copy the key into `.env`. Without it the NYT list
  provider simply skips (no error). After setting it:
  `uv run obc lists update && uv run obc normalize`.
- `OBC_DATA` — optional root directory for **all** catalog data (records, HTML
  cache, side files, checkpoint, and the DB). Defaults to `./data`; point it at a
  mounted volume to keep everything on persistent storage with one setting.
- `OBC_DB` — optional path to the SQLite catalog file specifically. Defaults to
  `$OBC_DATA/catalog.db`; set it only if you want the DB somewhere other than the
  data root.
`.env` is gitignored; never commit your key.

Search Console is verified with a **DNS TXT record on the apex domain**, so the
app renders no verification tag — a domain property covers every host and path,
which a per-URL HTML tag does not.

## Usage

Run commands with `uv run` (no manual venv activation needed):

```bash
uv run obc scrape --full      # complete catalog enumeration + e-reader flags (resumable)
uv run obc normalize          # load data/raw/records/*.json into data/catalog.db (~5s)
uv run obc serve              # http://127.0.0.1:8000

# keep it fresh without re-downloading everything
uv run obc scrape --sync      # daily: new/changed titles (newest-by-license first)
uv run obc scrape --reconcile # periodic: full scan; mark titles removed from catalog

# optional / periodic
uv run obc scrape --genres    # tag books with genres via subject facets (slow, ~1h)
uv run obc scrape --recent    # rank recently-added titles (for the 'Recent toegevoegd' sort)
uv run obc scrape --ereader   # refresh only the e-reader-available flag set
uv run obc scrape --enrich    # detail pages: ISBN, full genres, narrator, doelgroep,
                              # leeftijd, reeks, trefwoorden
uv run obc lists update       # refresh curated lists (Bestseller 60, NYT, prizes)
uv run obc similar            # rebuild only the "meer zoals dit" recommendations
uv run obc stats
```

`obc normalize` already precomputes the recommendations, so `obc similar` is only
for rebuilding them on their own (a different `-k` or `--lsa-dim`). Both need the
optional `recommend` extra — `uv sync --extra recommend` — and without it the
catalog is built exactly as usual, just with the "meer zoals dit" strip omitted.

`obc scrape --full` runs browse + ereader + genres + recent in one go. Cover images
are hotlinked straight from the library's CDN (`leibniz.zbkb.nl`); nothing is
cached locally. The search bar autocompletes titles, authors, publishers, genres,
languages and lists. Logs use loguru.

### Pages

| URL | What it is |
|---|---|
| `/` | search + faceted browse (grid or list, 12–96 per page) |
| `/book/{ppn}` | one title: editions, genres, curated lists, "meer zoals dit" |
| `/authors`, `/authors/{letter}` | A-Z author hub, sortable by surname or first name |
| `/author/{slug}` | everything by one author, with a Wikipedia blurb when there is one |
| `/series/{slug}` | one series, in reading order |
| `/genres`, `/genre/{slug}` | genre hub per audience (jeugd / volwassenen), and one genre |
| `/lists`, `/list/{slug}` | curated lists (Bestseller 60, NYT, prizes) |
| `/stats`, `/about` | catalog dashboard, and what this thing is |
| `/suggest`, `/facet` | JSON for the autocomplete and the searchable facets |
| `/robots.txt`, `/sitemap.xml` | crawler-facing; the sitemap is an index over paginated children |
| `/healthz`, `/admin/refresh` | liveness probe, and the token-protected refresh trigger |

Author, series and genre pages are addressed by **slug** (`/author/lisbeth-imbo`);
the older percent-encoded name URLs still resolve and 301 to the slug, and spelling
variants that fold together share one page. Unknown URLs get a real 404 page that
suggests close matches from the catalog rather than a bare "not found".

Add dependencies with `uv add <pkg>`; run tests with `uv run pytest`. Run
`obc normalize` after any scrape to refresh what the UI serves. Set `OBC_DB` to
point at a different DB.

### Data cleanup & curated lists

- **Publishers** are canonicalised at normalize time: spellings differing only in
  case/brackets/spacing collapse to the most-common variant. For imprints that
  share no words (e.g. *Prometheus* / *Bert Bakker*, or the many *Das Mag* forms)
  there's a curated alias list — `textnorm.PUBLISHER_ALIASES` — extend it as needed.
- **Authors** are split on `|` / `;` into individual people (`authors` /
  `book_authors` tables), so a co-author like *Ron Schröder* is searchable on their
  own. The joined string is still shown for display.
- **Curated lists** (`lists` / `list_items` / `book_lists`): pluggable *providers*
  under `src/obc/lists/` each return one or more lists → `data/raw/lists/{slug}.json`;
  `obc normalize` matches items to catalog PPNs (by ISBN, else title + author surname).
  Providers: **debestseller60.nl** (weekly Bestseller 60 + genre toplists Fictie,
  Non-fictie, Jeugd, Spannend, Koken) and the **New York Times** Best Sellers (all
  current lists via the official Books API — set a free `NYT_API_KEY` from
  developer.nytimes.com; without it the NYT provider just skips). The full list (incl. titles
  *not* in the library) is kept in `list_items`; matched ones go to `book_lists`.
  UI: a `/lists` overview, a `/list/{slug}` page showing every title in rank order
  (unavailable ones greyed out), a "Lijsten" filter, a cover ribbon, and detail badges.
  - **Add an automated list**: write a `fetch_all()` provider and append it to
    `lists/__init__.py:PROVIDERS`.
  - **Add a one-off / manual list** (e.g. a prize shortlist, or anything that can't be
    scraped): drop a JSON file in `data/raw/lists/` shaped like
    `{"slug","name","url","description","items":[{"position","title","author","isbn","cover_url"}]}`
    and run `obc normalize`. `obc lists update` only rewrites provider slugs, so manual
    files are preserved.
  - Note: bol.com's "Boeken Top 10" page is marketing banner *images* + category links
    (no structured ranking) and is bot-protected, so it isn't auto-scrapeable; use the
    manual-list route if you want a bol-derived list.

### Enumeration & refresh (how completeness is achieved)

The result pager *displays* only 50 pages but deep page URLs work to a hard
**10,000-result cap per query**. `--full` enumerates per **`type × taal`** (both
exhaustive — every title has a format and a language); foreign-language cells fit
under the cap and paginate fully, while Dutch (the only >10k language) is split by
**year** (Dutch is ~98% year-filled) with a maker-sort window to mop up the rest.
`leesvorm=ereader` is captured as the e-reader-availability flag.

Refresh: the catalog sorts by `licentie_datum`, so new/relicensed titles appear
first. `--sync` pages newest-first and stops after a long run of already-known
unchanged titles (usually a few pages). It can't see removals, so `--reconcile`
(a full scan) stamps `removed_at` on titles no longer present; the UI hides them.

## How it works

- **Enumeration** (`scrape.py --browse`): the catalog is walked via the faceted
  browse pages `zoekresultaten.catalogus[.N].html?type=…&jaar=…`. Each result
  `<li>` already carries rich metadata, so one request covers ~20 books
  (`listing.parse_listing`). The site **caps the pager at 50 pages (~1000
  results)**, so we partition by `type` (E-book / Digitaal_luisterboek) × `jaar`
  (original publication year) and, whenever a partition is still capped,
  recursively split it by `taal` → `nbcHoofdCategorie` → `doelgroep` → subject
  code until every piece fits. Records are de-duplicated by PPN and the run is
  resumable per `(format, year)` via `data/checkpoint.json`.
- **Listing vs detail metadata**: listing rows give title, author, summary,
  language, year, publisher, format, pages/duration, size, cover. `--enrich`
  then fetches **detail pages** (`/catalogus/{ppn}/{slug}.html`,
  `detail.parse_detail`) to add ISBN, the full subject/genre list, narrator,
  audience (doelgroep), age band, series and keywords. `client.Client` fetches
  politely (descriptive UA, configurable rate, backoff, on-disk HTML cache in
  `data/raw/html/`).
- **Storage** (`db.py`): `books` + `genres`/`book_genres` + an FTS5 table
  (`unicode61 remove_diacritics 2`) over title/author/subjects/summary. The DB is
  written by **full rebuild**, never per-row: `normalize` streams the records into
  a temporary DB and atomically swaps it over the live file, so readers keep
  seeing the old catalog until the swap.
- **Recommendations** (`similar.py`): "meer zoals dit" is content-based — an LSA
  (TF-IDF + truncated SVD) over each book's text, with the nearest neighbours
  stored in `book_similar`. Built inside `normalize`, into the temp DB, so the
  swap publishes a catalog and its recommendations together.
- **UI** (`web/app.py` + `web/queries.py`): FTS5 `bm25` ranking weighted toward
  title/author, plus facet filters (format, language, genre, year) and sorting.
  Routes stay thin — every SQL statement lives in `queries.py`.
- **Derived indexes** (`web/indexes.py`): the facet values, the A-Z author index,
  the series slug map and the genre tree are the same for every visitor and only
  change when a rebuild is swapped in, so they are memoised on the catalog file's
  mtime and rebuilt once per catalog, not per request (`web/cache.py` holds the
  small memoisation primitive they share).
- **SEO**: canonical slug URLs with 301s from the older ones, a paginated sitemap
  (`/sitemap.xml` → static / browse / book children), schema.org `Book` +
  `BreadcrumbList` + `WebSite` markup, `HEAD` on every route, and public
  `Cache-Control` on the pages that only change on a rebuild. Filtered search URLs
  are `noindex` and robots-disallowed — that URL space is effectively infinite.

### Notes & limits

- `jaar` filters by **original** publication year, not the edition year shown in
  listings — good for coverage (classic reprints are included), and dedup keeps
  it clean. Records with no original-year value (rare) won't be reached by year
  partitioning; widen `--year-from` for old classics (content starts ~1850).
- The full run is large (tens of thousands of titles at ~20/page). Keep the rate
  modest; the HTML cache + per-(format,year) checkpoint make it safe to stop and
  resume.

### Alternative: detail-only from a URL list

`obc scrape --from-file urls.txt` fetches + parses individual detail pages from a
list of catalog URLs / `ppn,slug` lines / a JSON array — handy for spot updates.

## Etiquette / legal

Personal-use harvesting of factual catalog metadata. `robots.txt` blocks named
bots and disallows `/*.do`; `/catalogus/` is allowed for normal browsers. Keep it
polite: low request rate, your own session, cache responses, no parallel
hammering. No login/borrow actions are automated.

## Layout

```
src/obc/
  client.py       polite fetcher + get_listing_html() + fetch_detail()
  listing.py      results-page HTML -> record dicts + pager size
  detail.py       detail-page HTML -> record dict (enrichment)
  scrape.py       browse/enrich enumerate -> data/raw/records/*.json (resumable)
  normalize.py    raw records -> SQLite (temp build + atomic swap)
  db.py           schema + FTS5 + bulk load + indexes
  similar.py      "meer zoals dit": LSA neighbours -> book_similar
  textnorm.py     folding, slugs, surname keys, publisher/author canonicalisation
  htmlutil.py     shared BeautifulSoup helpers
  config.py       data paths + user agent      log.py  loguru setup
  lists/          curated-list providers (bestseller60, nyt, wikiprize)
  web/app.py      page routes + presentation (+ templates/, static/)
  web/queries.py  every read-only SQL statement the UI runs
  web/indexes.py  the connection, and the indexes derived once per rebuild
  web/seo.py      canonical URLs, breadcrumbs, robots.txt, sitemaps
  web/cache.py    the per-catalog memoisation those indexes are built on
  web/bio.py      best-effort Wikipedia author blurb
  web/scheduler.py  the refresh POST /admin/refresh kicks off
  cli.py          `obc` entry point
tests/fixtures/   sample detail pages for parser tests
tests/sampledata.py  the hermetic catalog every db/queries/web test builds on
```
