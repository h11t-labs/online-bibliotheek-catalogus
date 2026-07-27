# Implementation spec: works + editions

This is the **executable companion** to `docs/works-and-editions.md` (the design
doc — read it first; it explains *why*). This document says *what to change,
where, in what order*, precisely enough that an agent can implement it without
re-deriving decisions. Where this spec and the design doc differ on a detail,
**this spec wins** (deviations are marked ⚠ and explained).

Deliverable: **one PR** on branch `claude/audiobook-ebook-unification-x0rebb`,
built as the sequence of commits below. Each step ends with a gate: the listed
commands must pass before the next step starts.

---

## 0. Ground rules for the executor

- Setup: `uv sync --extra recommend` (sklearn is needed so the similar tests run).
- Gates after every step: `uv run pytest`, `uv run ruff check .`, `uv run ty check`.
  All three must be green at every commit boundary, not just at the end.
- Commit messages: conventional commits, matching repo style (`feat(db): …`,
  `refactor(web): …`). The PR title is
  `feat(catalog)!: model works with editions (e-book + luisterboek = one book)`
  — the `!` is deliberate: the DB schema and several template contracts break.
- Match surrounding style: comment density like `db.py`/`queries.py` (comments say
  *why*, not *what*), line length 100, ruff rules as configured.
- Do not touch: `client.py`, `listing.py`, `lists/*`, `bio.py`, `htmlutil.py`,
  `scrape.py`'s browse/sync/reconcile machinery (only the additions named below),
  deploy files (`fly.toml`, `Dockerfile`, workflows).
- Terminology in code and comments: **work** = the book; **edition** = one PPN.
  Never reintroduce the word "book" for a table or variable that holds one grain
  or the other.
- The word `ppn` keeps meaning "an edition's id". A `work_id` *is* a PPN (the
  representative edition's), but name variables by role, not by type.
- PPN comparisons/orderings are **string** comparisons throughout (they are TEXT
  today, `set_primary_editions` orders them as text; keep that behaviour).

## 1. Target schema (single source of truth)

`normalize` rebuilds the whole DB into a temp file and swaps it in atomically, so
there is **no migration** — only this new `_SCHEMA` in `db.py`:

```sql
-- one row per PPN: the faithful per-item mirror of the library.
-- ⚠ Deviation from the design doc's "narrowed" editions table: editions KEEPS all
-- source columns (title, author, summary, keywords, audience, …). It is the raw
-- mirror + debugging record; `works` is the only READ model for work-level facts.
-- The web layer must never read a work-level fact (title, summary, genres, …)
-- from editions — only per-edition facts (format, pages, duration, narrator,
-- size, features, ereader, isbn, url/slug, its own year/publisher/cover).
CREATE TABLE IF NOT EXISTS editions (
    ppn               TEXT PRIMARY KEY,
    work_id           TEXT NOT NULL,    -- representative edition's ppn (see obc.work)
    slug              TEXT,
    url               TEXT,
    title             TEXT,
    author            TEXT,
    format            TEXT,             -- 'ebook' | 'audiobook'
    language          TEXT,
    publisher         TEXT,
    year              INTEGER,
    isbn              TEXT,
    pages             INTEGER,
    duration          TEXT,
    size              TEXT,
    features          TEXT,
    narrator          TEXT,
    audience          TEXT,
    summary           TEXT,
    cover_url         TEXT,
    also_available_as TEXT,
    note              TEXT,
    ereader           INTEGER,
    added_rank        INTEGER,
    series            TEXT,
    series_no         INTEGER,
    age               TEXT,
    keywords          TEXT,
    category          TEXT,
    raw_json          TEXT,
    scraped_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_editions_work ON editions(work_id);

-- one row per book: everything a reader searches/filters/sorts on. Derived from
-- editions by _build_works() on every rebuild — never written any other way.
CREATE TABLE IF NOT EXISTS works (
    work_id        TEXT PRIMARY KEY,    -- = the representative edition's ppn
    title          TEXT,                -- representative's
    author         TEXT,                -- representative's display string
    summary        TEXT,                -- longest non-empty across editions
    cover_url      TEXT,                -- representative's
    language       TEXT,                -- rep's, else any edition's (key ⇒ consistent)
    publisher      TEXT,                -- rep's (editions keep their own)
    year           INTEGER,             -- MIN(NULLIF(year,0)) across editions
    series         TEXT, series_no INTEGER,
    category       TEXT, audience TEXT, age TEXT,
    keywords       TEXT,                -- rep's, else any edition's
    has_ebook      INTEGER, has_audiobook INTEGER,
    ereader        INTEGER,             -- MAX over its e-book editions
    ebook_ppn      TEXT,                -- lowest ppn per format (string MIN)
    audiobook_ppn  TEXT,
    n_editions     INTEGER,
    added_rank     INTEGER              -- MIN(): a new audiobook resurfaces the book
);

-- genres / authors / publishers / languages / lists / list_items: same shape as
-- today, but the link tables hang off the WORK:
CREATE TABLE IF NOT EXISTS work_genres (
    work_id   TEXT NOT NULL REFERENCES works(work_id) ON DELETE CASCADE,
    genre_id  INTEGER NOT NULL REFERENCES genres(id) ON DELETE CASCADE,
    parent_id INTEGER,
    PRIMARY KEY (work_id, genre_id)
);
CREATE TABLE IF NOT EXISTS work_authors (
    work_id   TEXT NOT NULL REFERENCES works(work_id) ON DELETE CASCADE,
    author_id INTEGER NOT NULL REFERENCES authors(id) ON DELETE CASCADE,
    position  INTEGER,
    PRIMARY KEY (work_id, author_id)
);
CREATE TABLE IF NOT EXISTS work_lists (
    work_id  TEXT NOT NULL REFERENCES works(work_id) ON DELETE CASCADE,
    list_id  INTEGER NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
    position INTEGER, year INTEGER, won INTEGER,
    PRIMARY KEY (work_id, list_id)
);
-- lists / list_items / genres / authors / publishers / languages: unchanged
-- column-wise. list_items.ppn now HOLDS a work_id (a work_id is a ppn; keeping
-- the column name avoids touching the list templates/providers).

CREATE VIRTUAL TABLE IF NOT EXISTS works_fts USING fts5(
    work_id UNINDEXED, title, author, subjects, summary,
    tokenize = 'unicode61 remove_diacritics 2'
);

-- browse sort indexes: every works row is a book, so no boolean prefix column
-- (the whole idx_books_primary_* family and idx_books_title_author_lower are GONE)
CREATE INDEX IF NOT EXISTS idx_works_year   ON works(year DESC);
CREATE INDEX IF NOT EXISTS idx_works_added  ON works((added_rank IS NULL), added_rank);
CREATE INDEX IF NOT EXISTS idx_works_title  ON works(title COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_works_series ON works(series);
CREATE INDEX IF NOT EXISTS idx_wg_genre     ON work_genres(genre_id);
CREATE INDEX IF NOT EXISTS idx_wa_author    ON work_authors(author_id);
CREATE INDEX IF NOT EXISTS idx_wl_list      ON work_lists(list_id);
-- keep: idx_authors_fold, idx_publishers_fold, idx_li_list, languages/genres tables
```

`book_similar` becomes `work_similar(work_id, method, rank, other_work_id, score)`
— still created by `obc.similar`, not by `_SCHEMA`.

---

## Step 1 — `obc/work.py`: work identity (+ `related_ppns` in the detail parser)

**New file `src/obc/work.py`.** Public API (exact signatures):

```python
"""Work identity: group edition records (one per PPN) into works.

Evidence, strongest first:
1. the library's own "Ook beschikbaar als" cross-links (related_ppns),
2. a conservative normalised key (title stripped of format noise + first
   author's surname + language),
3. curated overrides (data/raw/work_overrides.json), applied last.
The representative edition — whose PPN doubles as the work_id — is the e-book
if the group has one, else the lowest PPN (string order, matching how the old
set_primary_editions chose).
"""

EditionMeta = tuple  # (title, author, language, format, related_ppns) — or a small dataclass

def strip_format_noise(title: str | None) -> str: ...
def work_key(title: str | None, author: str | None, language: str | None) -> tuple[str, str]:
    """(title_fold, author_surname) — language is handled by group_editions, not here."""
def group_editions(meta: dict[str, EditionMeta],
                   overrides: dict | None = None) -> dict[str, str]:
    """{ppn: work_id} for every ppn in meta."""
def stamp_work_ids(records: list[dict],
                   overrides: dict | None = None) -> list[dict]:
    """Convenience for bulk callers/tests: build meta from the records, group,
    set rec['work_id'] on each (only where missing), return the same list."""
def load_overrides(path: Path) -> dict:
    """{'merge': [[ppn, ppn], ...], 'split': [[ppn, ppn], ...]} or {} if absent."""
```

Implementation rules — follow exactly:

- `strip_format_noise`: strip **one** trailing format marker, case-insensitive,
  optionally wrapped in `()`/`[]` or preceded by `-`/`–`/`:`/`;`:
  `e-book`, `ebook`, `luisterboek`, `audioboek`, `digitaal luisterboek`,
  `luisterboek (digitaal)`, `digitaal`, `mp3`, `epub`, `epub2`, `epub3`,
  `gesproken versie`. If stripping empties the title, return the original.
  Regex, compiled once; write table-driven tests before tuning it.
- `work_key`: `(fold(strip_format_noise(title)), surname_key(split_authors(author)[0]) if split_authors(author) else "")`.
  Reuse `textnorm.fold`, `textnorm.surname_key`, `textnorm.split_authors` — do
  not re-implement any of them.
- `group_editions` algorithm:
  1. Union-find over PPNs (plain dict parent map, path compression; ~30 lines,
     local to the module).
  2. Bucket ppns by `work_key`. Inside one bucket, partition by `fold(language)`:
     editions sharing a non-empty language unify; an edition with **no** language
     joins the bucket's single non-empty language group if there is exactly one,
     otherwise it stays alone. Empty-key editions (no title fold) never unify by
     key.
  3. Union every `(ppn, r)` for `r` in that edition's `related_ppns` — **only if
     `r` is a key in `meta`** (the twin must exist in the catalog). This may
     bridge different keys/languages; the library's own link outranks the key.
  4. Overrides: for each `merge` pair, union. Then for each `split` pair `[a, b]`,
     if they ended up in one group, detach `b` into its own singleton work
     (document: the *second* element is the one forced out).
  5. Representative per group = `min(members, key=lambda p: (meta[p].format != "ebook", p))`.
  6. Return `{ppn: representative}` for all members.
- `load_overrides` reads `RAW_DIR / "work_overrides.json"` by default at the call
  site (normalize passes the path; the function itself takes an explicit path).

**`src/obc/detail.py`:** in the `dt/dd` loop, where the label maps to
`also_available_as`, additionally collect the hrefs:

```python
if field == "also_available_as":
    rec.setdefault(field, value)
    ppns = [m.group(1) for a in dd.find_all("a", href=True)
            if (m := _CANONICAL_RE.search(a["href"]))]
    if ppns:
        rec["related_ppns"] = ppns
    continue
```

(`_CANONICAL_RE` already matches `/catalogus/{ppn}/{slug}`.)

**Tests (this step):**

- New `tests/test_work.py`:
  - `strip_format_noise` table: `"De adoptie - luisterboek"` → `"De adoptie"`,
    `"Titel (e-book)"` → `"Titel"`, `"Luisterboek"` → unchanged (would empty),
    `"1984"` → unchanged, `"Deel 2: digitaal"` → `"Deel 2"`.
  - key grouping: same title+surname+language groups; different language splits;
    NULL language joins a single-language bucket but not an ambiguous one.
  - `related_ppns` bridges two different titles; a related ppn absent from meta
    is ignored.
  - overrides: merge joins, split detaches the second ppn.
  - representative: e-book wins over audiobook; lowest ppn breaks ties; group of
    one → itself.
- `tests/test_detail.py`: parsing `fixtures/ebook_460719149.html` yields
  `rec["related_ppns"] == ["460719130"]`; a fixture without the block yields no
  `related_ppns` key.

**Gate:** all three commands green. Commit:
`feat(work): work identity — cross-links, conservative key, overrides`.

---

## Step 2 — `db.py` + `normalize.py`: the new schema and build

**`src/obc/db.py`:**

1. Replace `_SCHEMA` with §1. Update `_ALL_TABLES` (FK-safe drop order):
   `("work_genres", "genres", "work_authors", "authors", "publishers",
   "languages", "work_lists", "list_items", "lists", "editions", "works",
   "works_fts")`.
2. `_BOOK_COLS` → `_EDITION_COLS` = today's list + `"work_id"` (insert right
   after `"ppn"`).
3. **Delete** `set_primary_editions()`. Rename `set_book_genre_parents` →
   `set_work_genre_parents`; its UPDATE joins
   `works w ON w.work_id = work_genres.work_id` and reads `w.audience`.
4. `bulk_load(conn, records, lists=None)`:
   - first line of body: `records = work.stamp_work_ids(list(records))` (records
     that already carry `work_id` keep it — normalize stamps its own).
   - inserts into `editions`; genre/author links keyed by **work_id** with
     `INSERT OR IGNORE` (the union across editions falls out of the PK);
   - publisher/language counts: count a record **only when
     `rec["work_id"] == rec["ppn"]`** (one count per work, the representative's
     spelling);
   - then `_build_works(cur)`, `_build_works_fts(cur)`, `_insert_lists`,
     `analyze(cur)`.
5. `stream_rebuild(conn, records, lists=None, batch=2000)`: same changes. Records
   **must** arrive with `work_id` (normalize stamps them; fall back to
   `r.get("work_id") or ppn` defensively). No FTS rows during streaming — FTS is
   built set-based afterwards.
6. New `_build_works(cur)` — pure SQL, run after all editions are inserted:

```sql
INSERT INTO works
SELECT e.work_id, rep.title, rep.author,
       (SELECT x.summary FROM editions x WHERE x.work_id = e.work_id
          AND x.summary IS NOT NULL AND x.summary <> ''
          ORDER BY length(x.summary) DESC, x.ppn LIMIT 1),
       rep.cover_url,
       COALESCE(rep.language,  MAX(e.language)),
       rep.publisher,
       MIN(NULLIF(e.year, 0)),
       COALESCE(rep.series,    MAX(e.series)),
       COALESCE(rep.series_no, MAX(e.series_no)),
       COALESCE(rep.category,  MAX(e.category)),
       COALESCE(rep.audience,  MAX(e.audience)),
       COALESCE(rep.age,       MAX(e.age)),
       COALESCE(rep.keywords,  MAX(e.keywords)),
       MAX(e.format = 'ebook'), MAX(e.format = 'audiobook'),
       MAX(CASE WHEN e.format = 'ebook' THEN e.ereader END),
       MIN(CASE WHEN e.format = 'ebook' THEN e.ppn END),
       MIN(CASE WHEN e.format = 'audiobook' THEN e.ppn END),
       COUNT(*),
       MIN(e.added_rank)
FROM editions e JOIN editions rep ON rep.ppn = e.work_id
GROUP BY e.work_id;
```

7. New `_build_works_fts(cur)` — pooled text, so a summary that lives only on the
   audiobook edition still finds the work (design doc §2.3):

```sql
INSERT INTO works_fts(work_id, title, author, subjects, summary)
SELECT w.work_id,
       (SELECT group_concat(DISTINCT e.title)  FROM editions e WHERE e.work_id = w.work_id),
       (SELECT group_concat(DISTINCT e.author) FROM editions e WHERE e.work_id = w.work_id),
       COALESCE((SELECT group_concat(g.name, ' ') FROM work_genres wg
                 JOIN genres g ON g.id = wg.genre_id WHERE wg.work_id = w.work_id), '')
         || ' ' ||
       COALESCE((SELECT group_concat(DISTINCT e.keywords) FROM editions e
                 WHERE e.work_id = w.work_id AND e.keywords IS NOT NULL), ''),
       (SELECT group_concat(DISTINCT e.summary) FROM editions e
        WHERE e.work_id = w.work_id AND e.summary IS NOT NULL)
FROM works w;
```

   (Delete `_fts_values` and `_insert_fts`; subjects come from `work_genres` now,
   which is the union the old per-record path approximated.)
8. `load_prior_ereader`: query `editions`, and on `sqlite3.Error` **retry with
   `books`** — during the first deploy the *live* DB it reads still has the old
   schema, and losing the flag would blank the facet (the exact failure this
   function exists to prevent).
9. `stats(conn)` returns
   `{"works", "editions", "ebooks" (works w/ has_ebook), "audiobooks" (works w/
   has_audiobook), "genres", "languages"}`.

**`src/obc/normalize.py`:**

1. `_prepass` additionally collects
   `meta[ppn] = (title, author, language, format, related_ppns)` for every
   non-removed record, and returns
   `canon, by_isbn, by_key, genre_info, work_of` where
   `work_of = work.group_editions(meta, work.load_overrides(RAW_DIR / "work_overrides.json"))`.
2. `_transform(...)` gains a `work_of` argument and stamps
   `r["work_id"] = work_of.get(ppn, ppn)` (pass it through `iter_records`/aux).
3. `match_lists(by_isbn, by_key, work_of)`: after resolving a ppn, map it
   `ppn = work_of.get(ppn, ppn)`; the `seen` dedupe then de-dupes **per work**
   (two list slots can no longer land on two editions of one book).
4. Module docstring bullet list: add the grouping step.

**Tests (this step):**

- `tests/sampledata.py` — add three records (docstring updated):
  - `"007"`: audiobook, title `"De Ontdekking"`, author `"Anna Vrij"`, year 2023,
    narrator `"Piet Stem"`, summary `"Volledige walvisexpeditie editie."` — a
    **second audiobook** of work 001 whose summary word `walvisexpeditie` exists
    nowhere else.
  - `"008"`: audiobook, title `"Koken met Liefde - luisterboek"`, author
    `"Dirk Kok"` — merges with 005 **only** via `strip_format_noise`.
  - `"009"`: audiobook, title `"Het grote mysterie, tweede deel"`, author
    `"Bob de Wit"`, `"related_ppns": ["004"]` — merges with 004 **only** via the
    explicit link.
  - Resulting truth: **9 editions, 5 works**; work `001` = {001,002,007}
    (`audiobook_ppn` = `"002"`), work `003` = {003}, work `004` = {004,009},
    work `005` = {005,008}, work `006` = {006}.
- `tests/test_db.py`:
  - round-trip: `stats()["editions"] == 9`, `["works"] == 5`,
    `["ebooks"] == 5`, `["audiobooks"] == 3`.
  - works aggregates: work 001 has `has_ebook=1, has_audiobook=1,
    ebook_ppn="001", audiobook_ppn="002", n_editions=3, year=2020` (MIN);
    `works_fts MATCH 'walvisexpeditie'` finds work 001.
  - `test_editions_lookup_uses_index_not_scan` → replace with: the editions-of-a-
    work lookup (`WHERE work_id = ?`) uses `idx_editions_work`.
  - `test_browse_sorts_are_indexed` → assert the `idx_works_*` family exists and
    covers every `queries.SORTS` key (bar relevance/year_asc).
  - stream/bulk equivalence: stamp first
    (`work.stamp_work_ids(sampledata.records())`) before `stream_rebuild`;
    snapshot works, editions, work_authors, works_fts counts.
  - genre-parent tests: rename to the `work_*` names; semantics unchanged.
- `tests/test_normalize.py`: extend the `raw` fixture with an e-book/audiobook
  pair sharing a title; assert both records get the same `work_id`, the
  representative is the e-book, and a matched list item lands on the work id.
  Add an overrides file case (split forces two works).

**Gate + commit:** `feat(db)!: works + editions schema, works built per rebuild`.

---

## Step 3 — `queries.py`: read the work model

Full inventory — every public name, disposition:

| Function | Action |
| --- | --- |
| `connect_ro`, `parse_year`, `fts_match`, `_limit`, `_in` | keep unchanged |
| `SORTS` | alias `b.` → `w.` (`"added": "w.added_rank IS NULL, w.added_rank ASC"`, …) |
| `SearchFilters`, `SearchResult` | keep (same fields; `format` still `""/"ebook"/"audiobook"`) |
| `_build_where` | rewrite: `format` → `w.has_ebook = 1` / `w.has_audiobook = 1`; `ereader` → `w.ereader = 1`; languages/publishers/year on `w.`; authors → `w.work_id IN (SELECT wa.work_id FROM work_authors wa JOIN authors a …)`; lists → `work_lists`; genres → `work_genres` |
| `search` | `FROM works w [JOIN works_fts ft ON ft.work_id = w.work_id]`; **delete** the `primary_edition` block and the format-filter exception; bm25 weights unchanged |
| `_has_primary_edition`, `_collapse_editions` | **delete** (and the two tests that exercised the pre-column fallback) |
| `total_books` | rename `total_works`; `COUNT(*) FROM works` (fix the two call sites in `app.py`) |
| `formats_map`, `editions_map` | **delete** — flags/ppns ride on the work row |
| `lists_map` | keyed by `work_id`, reads `work_lists`; callers pass work rows |
| `compute_facets` | formats: `[f for f, flag in (("audiobook","has_audiobook"),("ebook","has_ebook")) if conn.execute(f"SELECT EXISTS(SELECT 1 FROM works WHERE {flag}=1)").fetchone()[0]]`; other facets over `works` + `work_*` joins |
| `suggest` | over `works_fts`/`works`; select `w.work_id AS ppn, w.title, w.author, w.cover_url, w.ebook_ppn, w.audiobook_ppn, CASE WHEN w.has_ebook THEN 'ebook' ELSE 'audiobook' END AS format`; the `prim` filter is gone (one row per work by construction) |
| `facet_values` | joins switch to `work_authors`; publisher table unchanged |
| `book_detail(conn, ppn)` | new contract, below |
| `author_books`, `author_books_by_fold`, `author_display_name`, `author_index`, `author_title_counts` | join `works w` + `work_authors`; no collapse suffix; counts are naturally per work |
| `series_books` | `FROM works w WHERE w.series IN (…) ORDER BY w.series_no, w.year` |
| `genre_books`, `genre_index` | over `work_genres`/`works` (returns `work_id`, keep the dict key name `ppn` in the row alias to leave `app._genre_data` untouched: `bg.work_id AS ppn`); **drop** the `parent_id` try/except fallbacks — the works-absence 503 covers the deploy window now |
| `browse_summary` | drop the mirror-the-collapse branch and the `has_edition` EXISTS pair; `SUM(w.has_ebook) AS ebooks, SUM(w.has_audiobook) AS audiobooks, SUM(w.ereader) AS ereader`; author breakdown joins `work_authors` |
| `similar_books` | reads `work_similar s JOIN works w ON w.work_id = s.other_work_id`; select `w.work_id AS ppn, w.title, w.author, w.cover_url, w.has_ebook, w.has_audiobook, s.score` |
| `lists_overview`, `list_row`, `list_items` | `list_items` joins `works w ON w.work_id = li.ppn`; expose `w.cover_url AS bcover, w.has_ebook AS bebook, w.has_audiobook AS baudio` (template updated in step 4) |
| `web_stats` | totals from `works`/`editions`; genre bars over `work_genres`; drop the pre-`parent_id` fallback |

`book_detail` new contract:

```python
def book_detail(conn: sqlite3.Connection, ppn: str) -> dict | None:
    """Everything the book page needs.

    ``ppn`` may be any edition's PPN. For a non-representative edition returns
    {"redirect": work_id} so the route can 301 — old audiobook URLs keep working.
    Otherwise: {"work": row, "editions": [edition rows, e-book first then ppn],
    "genres": [...], "authors": [...], "work_lists": [...]}.
    None if the PPN is unknown at either grain.
    """
```

Lookup order: `SELECT * FROM works WHERE work_id = ?`; miss →
`SELECT work_id FROM editions WHERE ppn = ?` → `{"redirect": …}`; miss → `None`.
Genres/authors/lists queries as today but over the `work_*` tables. The
genre-chip "hide a parent shown via its child" logic moves over unchanged.

**Tests:** rewrite `tests/test_queries.py` expectations to the 9-edition/5-work
fixture. Key new assertions:

- `format="audiobook"` → `{"001", "004", "005"}` (works *available as*, not
  audiobook rows) — this is the §2.1 fix pinned.
- `q="walvisexpeditie"` finds work `001` — §2.3 pinned (fails on the old model).
- `book_detail("002") == {"redirect": "001"}`; `book_detail("001")["editions"]`
  has ppns `["001", "002", "007"]`; `book_detail("nope") is None`.
- `browse_summary` on no filters: `ebooks == 5, audiobooks == 3`.
- `author_books_by_fold("anna vrij")` → 1 row; `("bob de wit")` → 2 works.
- suggest for `"ontdek"`: one row, `ppn == "001"`, carries both `*_ppn`s.
- delete `test_formats_map_links_both_editions`, the two
  `*_tolerates_pre_hierarchy_schema` tests; keep+update the relevance-weight test
  (works path, distinct authors so the two stay separate works — it already does).

**Gate + commit:** `refactor(queries): read works, not editions`.

---

## Step 4 — `app.py` + templates: one URL per book, per-edition blocks

**`src/obc/web/app.py`:**

1. `/book/{ppn}` route:
   - `detail.get("redirect")` → `RedirectResponse(f"/book/{detail['redirect']}", status_code=301)`.
   - Context: `b` = the **work** row, `editions` = list of edition rows,
     plus `genres/authors/book_lists/similar` as today (`book_lists` name kept for
     template compatibility, filled from `work_lists`).
   - JSON-LD: one `Book` with
     `workExample: [{"@type": "Book", "bookFormat": …, "isbn": …,
     "numberOfPages"/"duration"(ISO-8601 not required — omit rather than guess),
     "url": edition["url"]} for each edition]`; top-level `isbn` dropped in favour
     of per-example; `bookFormat` at top level removed.
2. `/suggest`: build `"editions": {…}` from `ebook_ppn`/`audiobook_ppn` (drop the
   `editions_map` call); JSON shape stays identical so `base.html`'s dropdown JS
   is untouched.
3. Search route: drop `editions_map`; `total_indexed = queries.total_works(conn)`.
4. Format landing pages (now honest — the reason #27 removed them is gone):

```python
@app.get("/e-books", response_class=HTMLResponse)
def ebooks_page(request, conn=Depends(get_conn)):
    return _browse_page(request, conn, heading="E-books",
        lead="Alle e-books uit de collectie van de online Bibliotheek.",
        filters=queries.SearchFilters(format="ebook", sort="year_desc"),
        search_url="/?format=ebook", crumb=None)
# idem /luisterboeken with format="audiobook"
```

   Add `"/e-books", "/luisterboeken"` to `_CACHE_PREFIXES` and to
   `sitemap_browse` paths. (`_browse_page` needs no change — `browse_summary` is
   already honest per step 3. The `split` sentence hides itself on format pages
   because one of the two counts equals the total; leave as-is.)
5. `sitemap_books`: `SELECT work_id FROM works ORDER BY work_id`.
6. `_db_unavailable` bootstrap list: add `"no such table: works"` and
   `"no such table: works_fts"` (keep the `books` entries — harmless, and they
   cover a pre-rename DB during the window).
7. `/stats` context unchanged; data via updated `web_stats`.

**`src/obc/web/scheduler.py`:** in `_default_cmds()`, before building `cmds`:

```python
def _schema_stale() -> bool:
    """True when the live DB predates the works schema — then a normalize from
    the records already on disk fixes the shape in minutes, instead of the site
    503ing behind a full sync pipeline."""
    from .. import db
    try:
        conn = sqlite3.connect(f"file:{db.DEFAULT_DB}?mode=ro", uri=True)
        try:
            return not conn.execute("SELECT 1 FROM sqlite_master "
                                    "WHERE type='table' AND name='works'").fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return False   # no DB at all -> the full-harvest path handles it
```

If `_seeded() and _schema_stale()`: prepend `["normalize"]` to `cmds`.

**Templates:**

- `_macros.html` — new signature; update the three call sites
  (browse/author/series):

```jinja
{% macro fmt_icons(w) -%}
{# availability badges for a WORK card: solid badge per format the book has #}
<div class="fmt-icons">
  {% if w['has_ebook'] %}<span class="fmt-ic ebook" title="E-book">…#ic-book…</span>{% endif %}
  {% if w['has_audiobook'] %}<span class="fmt-ic audio" title="Luisterboek">…#ic-audio…</span>{% endif %}
</div>
{%- endmacro %}
```

  (The dim "alt" badge dies with the per-edition cards; remove its CSS if it is
  now unreferenced — check `.fmt-ic.alt` in `base.html`.)
- `browse.html` / `author.html` / `series.html`: cards link
  `/book/{{ b['work_id'] }}`; `fmt_icons(b)`; `formats_map` context variable and
  its query call removed from the three routes. Every
  `lists_map.get(b['ppn'])` becomes `lists_map.get(b['work_id'])` (and
  `queries.lists_map` itself keys on `r["work_id"]`).
- `search.html`: `{% set ed = … %}` / `primary_ppn` block →
  card link `/book/{{ b['work_id'] }}`; `fmt_ic(fmt, ppn)` macro becomes
  `fmt_ic(fmt, available, work_id)` rendering
  `href="/book/{{ work_id }}#{{ 'luisterboek' if audio else 'e-book' }}"` when
  available, the greyed placeholder otherwise (list view keeps its two aligned
  columns). Grid overlay: only available formats.
- `book.html`: restructure the info column —
  - keep: format badges (from `has_*`), h1, authors, series/list badges, genre
    chips, summary (= the work's pooled-longest);
  - the shared `.meta` block keeps: Taal, Verschenen (work year), Doelgroep,
    Leeftijd, Trefwoorden;
  - then per edition (e-book first) a block `id="e-book"` / `id="luisterboek"`,
    heading "E-book" / "Luisterboek", its own `.meta` rows — ISBN, Aantal
    pagina's / Speelduur, Verteller, Omvang, Bestandstype, Voor e-reader,
    Uitgever + jaar when they differ from the work's — and its own
    `Lenen op onlinebibliotheek.nl ↗` button (edition `url`);
  - the poster keeps ONE cover (the work's) and loses its single borrow button
    (buttons live in the edition blocks now);
  - `Ook beschikbaar als` meta row: drop (superseded by the blocks);
  - the old cross-edition `linkbadge` ("Ook als luisterboek →") becomes an anchor
    `href="#luisterboek"`;
  - the `simcard` macro ("meer zoals dit") renders its format icons from
    `s['has_ebook']`/`s['has_audiobook']` instead of the single `s['format']`,
    and links `/book/{{ s['ppn'] }}` (the aliased work_id from `similar_books`).
- `list_detail.html`: the availability icon reads `bebook`/`baudio` (render both
  icons when both) instead of single `bformat`.
- `stats.html`: add an "edities" stat card (`s.editions`); "titels totaal" now
  means works — relabel to "boeken".
- `base.html`: nothing structural (dropdown JS shape unchanged). Optional, only
  if trivial: header nav gains E-books/Luisterboeken links — **skip if it
  disturbs the responsive nav width comments** (`base.html` warns the nav width
  is load-bearing; leave nav alone in this PR).

**Tests:** update `tests/test_web.py` throughout; new/changed assertions at
minimum:

- `GET /book/002` → 301 → `location: /book/001`; `GET /book/001` contains both
  `id="e-book"` and `id="luisterboek"` blocks, two borrow links (`/460…` not
  required — assert on `b['url']` values of 001 and 002), narrator only in the
  luisterboek block.
- `GET /?format=audiobook` shows 3 works, body contains `/book/001` (the work
  URL), **not** `/book/002` as a card link.
- `GET /e-books` and `/luisterboeken` 200, counts = 5 and 3, both in
  `/sitemap-browse.xml`.
- `/sitemap-books-1.xml` lists exactly the 5 work URLs.
- `/suggest?q=ontdek` JSON: one title, `editions == {"ebook": "001",
  "audiobook": "002"}`.
- JSON-LD on `/book/001` parses and has two `workExample` entries.
- 503-window test: point `DB_PATH` at a DB built with the **old** schema (build
  it inline with raw SQL: a minimal `books` table) → `/` returns 503 with the
  "wordt opgebouwd" page.
- `tests/test_scheduler.py`: `_default_cmds()` prepends `["normalize"]` when the
  DB lacks `works` (monkeypatch `db.DEFAULT_DB` at a stale file), and doesn't
  when it has it / doesn't exist.

**Gate + commit:** `feat(web)!: one URL per book, per-edition detail blocks,
format pages`.

---

## Step 5 — `similar.py` over works + curated lists

**`src/obc/similar.py`:**

- `_load_docs`: read `works` (`work_id, title, author, summary, keywords`) and
  `work_genres`; **delete** `_norm_key`, the `keys`/`used_keys` machinery, and
  the over-fetch `pool` logic (no edition twins exist at this grain — keep a
  small over-fetch of `k + 5` only to survive the `min_score` cut, or simplify to
  exact `k`; pick one and say so in a comment).
- `_ensure_table`: create `work_similar` as in §1; also
  `DROP TABLE IF EXISTS book_similar` (stale table from a pre-works build of the
  same temp DB cannot exist, but the local dev DB path can — cheap insurance).
- `build_similar` writes `work_similar`; `main()` cleanup query unchanged in
  spirit.
- `normalize._build_similar` needs no change (it calls `build_similar`).

**Curated lists** were finished in step 2 (`match_lists` maps to works); verify
`test_lists`/`test_normalize` cover: one list slot whose ISBN is the *audiobook*
edition's still lands on the work.

**Tests:** `tests/test_similar.py` — table/columns renamed; the "editions of one
work never both appear" test becomes "a recommended work_id appears at most
once and never equals the source" (structurally guaranteed; keep the assertion).

**Gate + commit:** `refactor(similar): recommend works`.

---

## Step 6 — tooling, docs, PR

1. **`obc works --report`** — new module function `work.report(db_path)` +
   CLI subcommand (`cli.py`): prints
   - works/editions totals, group-size histogram
     (`SELECT n_editions, COUNT(*) FROM works GROUP BY 1 ORDER BY 1`);
   - **false splits**: editions whose `also_available_as` mentions
     `luisterboek` while their work has `has_audiobook = 0`, and vice versa for
     `e-book` — count + first 20 `(ppn, title)`;
   - **suspicious merges**: works whose editions disagree on language, or span
     `MAX(year)-MIN(year) > 5`, or have >1 distinct publisher_key — count + first
     20;
   - exit code 0 always (it is a report, not a check).
2. **`obc scrape --relink`** — in `scrape.py`, sibling of `enrich()`: select
   records where `also_available_as` is truthy **and** `related_ppns` is absent,
   refetch the detail page (`cache=False`), `_merge` and write. Add the argparse
   flag + dispatch. Log the todo-count up front.

   Why targeted and not a full re-enrich: an absent `also_available_as` label
   means the page had no twin block when it was enriched, so refetching it
   yields nothing — the label is a free oracle for which pages carry a link.
   Twins licensed *after* a record was enriched are covered anyway: the newer
   edition's own enrich captures the link, and `group_editions` unions in both
   directions. So `--relink` reaches the same grouping evidence as re-scraping
   all ~68k detail pages, at roughly a third of the requests, and it is
   self-resuming (a merged record drops out of the selector).
3. **README.md**: usage section gains `obc works --report` and
   `scrape --relink`; "How it works" storage paragraph rewritten (works +
   editions, one URL per book); pages list gains `/e-books`, `/luisterboeken`.
4. **`docs/works-and-editions.md`**: mark the plan as implemented; note the ⚠
   deviation (editions keeps raw columns; the narrowing is the read contract).
5. PR: use `.github/pull_request_template.md` structure. Body must include the
   test-suite result and a placeholder table for the `obc works --report` numbers
   with a note: **the repo owner runs the report against a production DB copy and
   fills it in before deploying** (the executor has no production data — that
   check moves from "before merge" to "before deploy", and the report command in
   this PR is what makes it a one-liner).

**Gate:** full suite + lint + types green; `uv run obc --help`,
`uv run obc scrape --help` show the new flags. Commit:
`feat(cli): works report + targeted relink pass`.

---

## 7. Global invariants (check before opening the PR)

1. `grep -rn "primary_edition\|formats_map\|editions_map\|book_genres\|book_authors\|book_lists\|book_similar\|books_fts\|FROM books\b" src/` →
   only hits allowed: the `load_prior_ereader` books-fallback and the
   scheduler/bootstrap compatibility strings.
2. Every `/book/…` href in templates points at a `work_id` (or an edition anchor
   `#e-book`/`#luisterboek` on the work URL). No template reads a work-level fact
   from an edition row.
3. `queries.py` has **no** `(title, author)` string-matching join left.
4. The suggest JSON contract (`titles[].editions`) is byte-shape-identical to
   before (dropdown JS untouched).
5. Fixture truth used everywhere: **9 editions / 5 works**; work 001 =
   {001, 002, 007}; 004 = {004, 009} (via `related_ppns`); 005 = {005, 008} (via
   `strip_format_noise`).
6. `pytest`, `ruff check .`, `ty check` green; no new dependencies added.
