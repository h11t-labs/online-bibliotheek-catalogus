"""SQLite schema + FTS5 helpers for the catalog.

Design notes
------------
* ``editions`` holds one normalised row per PPN — the faithful per-item mirror of
  the library, and the record every work-level fact is derived *from*.
* ``works`` holds one row per book: an e-book and its audiobook are two editions
  of one work (see :mod:`obc.work` for how that identity is decided). It is the
  only read model for work-level facts; the web layer never reads a title,
  summary or genre off an edition.
* ``genres`` / ``work_genres`` model the many-to-many subjects for faceted
  filtering (one genre row per distinct subject string), hung off the work
  because a genre describes the book, not the file format.
* ``works_fts`` is a standalone FTS5 table (not external-content) so it can be
  populated in one pass, over the *pooled* text of a work's editions — a summary
  that only the audiobook edition carries still finds the book.
  ``unicode61 remove_diacritics 2`` folds Dutch diacritics so "espana"-style
  queries match "España", etc.
* Everything deterministically derivable from the catalog is derived here, at
  build time: work identity, facet counts, author sort keys, the series map and
  the genre taxonomy. The read path does indexed lookups only.

Writes happen as **full rebuilds**, never row-at-a-time: :func:`bulk_load`
(records held in RAM) or :func:`stream_rebuild` (constant-memory streaming) drop
and recreate every table. In production :mod:`obc.normalize` runs ``stream_rebuild``
into a temporary DB and atomically ``os.replace``\\ s it over the live file, so
readers keep seeing the old catalog until the swap (no downtime, no half-built
state).
"""

from __future__ import annotations

import collections
import json
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

# DEFAULT_DB honours OBC_DB so the CLI (scrape/normalize/sync) and the web app
# share one path (e.g. a Fly volume at /app/data/catalog.db). Defined once in
# obc.config; imported here so `db.DEFAULT_DB` keeps working for existing callers.
from . import work
from .config import DEFAULT_DB  # noqa: F401
from .textnorm import fold, slugify, surname_key

_SCHEMA = """
-- one row per PPN: the faithful per-item mirror of the library. It keeps every
-- source column, because it is also the debugging record `works` is derived
-- from; the "narrowing" is a READ contract, not a column list. No FK on
-- work_id: editions are inserted before the works they are grouped into.
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
    ereader           INTEGER,          -- 1 = available for e-reader (e-books)
    added_rank        INTEGER,          -- recency rank by license date (0 = newest)
    series            TEXT,
    series_no         INTEGER,
    age               TEXT,             -- reading age, e.g. "9-12 jaar" (detail page)
    keywords          TEXT,             -- free keyword tags (detail page)
    category          TEXT,             -- 'fictie' | 'nonfictie'
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
    slug           TEXT,                -- '{title-slug}--{author-slug}'
    language       TEXT,                -- rep's, else any edition's (key => consistent)
    publisher      TEXT,                -- rep's (editions keep their own)
    year           INTEGER,             -- MIN(NULLIF(year,0)) across editions
    series         TEXT,
    series_no      INTEGER,
    series_slug    TEXT,                -- slugify(series): the /series/{slug} page
    category       TEXT,
    audience       TEXT,
    age            TEXT,
    keywords       TEXT,                -- rep's, else any edition's
    has_ebook      INTEGER,
    has_audiobook  INTEGER,
    ereader        INTEGER,             -- MAX over its e-book editions
    ebook_ppn      TEXT,                -- lowest ppn per format (string MIN)
    audiobook_ppn  TEXT,
    n_editions     INTEGER,
    added_rank     INTEGER              -- MIN(): a new audiobook resurfaces the book
);

CREATE TABLE IF NOT EXISTS genres (
    id      INTEGER PRIMARY KEY,
    name    TEXT UNIQUE,
    n_works INTEGER          -- stamped at build; the facet panel reads it as-is
);

CREATE TABLE IF NOT EXISTS work_genres (
    work_id   TEXT NOT NULL REFERENCES works(work_id) ON DELETE CASCADE,
    genre_id  INTEGER NOT NULL REFERENCES genres(id) ON DELETE CASCADE,
    parent_id INTEGER,   -- the parent genre *for this work's audience* (NULL = top)
    PRIMARY KEY (work_id, genre_id)
);

-- One row per PERSON, not per spelling. name_fold is the identity; name is the
-- most-common spelling across all credits, chosen at build time. Names that fold
-- to '' (non-Latin scripts) never merge — one row per spelling there. Display
-- strings on works/editions stay exactly as scraped; this table is the identity
-- underneath them. surname_sort / first_sort are the A-Z hub's two orderings,
-- stamped here so the hub is an indexed read instead of a per-process cache.
CREATE TABLE IF NOT EXISTS authors (
    id           INTEGER PRIMARY KEY,
    name         TEXT UNIQUE,
    name_fold    TEXT,
    n_works      INTEGER,
    surname_sort TEXT,
    first_sort   TEXT
);

-- distinct publishers with a folded form + count, for fast autocomplete
CREATE TABLE IF NOT EXISTS publishers (
    name      TEXT,
    name_fold TEXT,
    n         INTEGER
);

-- distinct languages with a folded form + count, for fast autocomplete
CREATE TABLE IF NOT EXISTS languages (
    name      TEXT,
    name_fold TEXT,
    n         INTEGER
);

-- The PK dedupes a work credited under two spellings of one person.
CREATE TABLE IF NOT EXISTS work_authors (
    work_id   TEXT NOT NULL REFERENCES works(work_id) ON DELETE CASCADE,
    author_id INTEGER NOT NULL REFERENCES authors(id) ON DELETE CASCADE,
    position  INTEGER,
    PRIMARY KEY (work_id, author_id)
);

CREATE TABLE IF NOT EXISTS lists (
    id          INTEGER PRIMARY KEY,
    slug        TEXT UNIQUE,
    name        TEXT,
    url         TEXT,
    description TEXT,
    updated_at  TEXT
);

-- a Bestseller-60 slot is a book, not an edition
CREATE TABLE IF NOT EXISTS work_lists (
    work_id  TEXT NOT NULL REFERENCES works(work_id) ON DELETE CASCADE,
    list_id  INTEGER NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
    position INTEGER,
    year     INTEGER,           -- award year (prizes); NULL for ranked lists
    won      INTEGER,           -- 1 = won, 0 = nominated (prizes); NULL otherwise
    PRIMARY KEY (work_id, list_id)
);

-- full ordered list contents (incl. titles not in the library; ppn is NULL then).
-- `ppn` holds a work_id — a work_id *is* a ppn, and keeping the column name means
-- the list templates and providers need no change.
CREATE TABLE IF NOT EXISTS list_items (
    list_id   INTEGER NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
    position  INTEGER,
    year      INTEGER,
    title     TEXT,
    author    TEXT,
    isbn      TEXT,
    cover_url TEXT,
    ppn       TEXT,
    won       INTEGER          -- 1 = won, 0 = nominated (prizes); NULL otherwise
);

-- One row per /series/{slug} page: spelling variants ("De Stad" / "De stad")
-- share a slug and therefore a page. Derived, like everything below.
CREATE TABLE IF NOT EXISTS series (
    slug   TEXT PRIMARY KEY,
    name   TEXT,               -- the spelling carrying the most works
    titles INTEGER             -- distinct works
);

-- One row per /genre/{slug} page: spelling variants merged, parent voted
-- catalog-wide. See build_genre_taxonomy.
CREATE TABLE IF NOT EXISTS genre_pages (
    slug        TEXT PRIMARY KEY,
    name        TEXT,                   -- longest spelling
    titles      INTEGER,                -- distinct works
    parent_slug TEXT                    -- catalog-wide parent ('' = top)
);

-- The hub's per-audience taxonomy: jeugd and volwassenen reuse genre names under
-- different parents, so each audience gets its own tree.
CREATE TABLE IF NOT EXISTS genre_tree (
    audience    TEXT NOT NULL,          -- 'volwassenen' | 'jeugd'
    slug        TEXT NOT NULL,
    parent_slug TEXT,                   -- '' = top level in this audience
    titles      INTEGER,                -- works in this audience
    PRIMARY KEY (audience, slug)
);

CREATE INDEX IF NOT EXISTS idx_editions_format ON editions(format);
-- Browse sort indexes: every works row is a book, so there is no boolean prefix
-- column to filter on first. The old (primary_edition, <sort key>) family — and
-- idx_books_title_author_lower, which existed only to make the (title, author)
-- workaround fast — are gone: a plain index on the sort key serves each ordering
-- straight from the index, with no "USE TEMP B-TREE FOR ORDER BY" over ~56k rows.
-- year_asc reuses the DESC index (SQLite scans it backwards). `added` needs the
-- IS NULL expression as a column because the ORDER BY puts NULLs last while an
-- ASC index stores them first. `title` must repeat COLLATE NOCASE to match.
CREATE INDEX IF NOT EXISTS idx_works_year     ON works(year DESC);
CREATE INDEX IF NOT EXISTS idx_works_added    ON works((added_rank IS NULL), added_rank);
CREATE INDEX IF NOT EXISTS idx_works_title    ON works(title COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_works_series   ON works(series_slug);
CREATE INDEX IF NOT EXISTS idx_works_language ON works(language);
CREATE INDEX IF NOT EXISTS idx_works_ereader  ON works(ereader);
CREATE INDEX IF NOT EXISTS idx_wg_genre       ON work_genres(genre_id);
CREATE INDEX IF NOT EXISTS idx_wa_author      ON work_authors(author_id);
CREATE INDEX IF NOT EXISTS idx_wl_list        ON work_lists(list_id);
CREATE INDEX IF NOT EXISTS idx_authors_fold   ON authors(name_fold);
CREATE INDEX IF NOT EXISTS idx_authors_nworks ON authors(n_works DESC);
CREATE INDEX IF NOT EXISTS idx_publishers_fold ON publishers(name_fold);
CREATE INDEX IF NOT EXISTS idx_li_list        ON list_items(list_id);

CREATE VIRTUAL TABLE IF NOT EXISTS works_fts USING fts5(
    work_id UNINDEXED,
    title,
    author,
    subjects,
    summary,
    tokenize = 'unicode61 remove_diacritics 2'
);
"""

_EDITION_COLS = [
    "ppn", "work_id", "slug", "url", "title", "author", "format", "language",
    "publisher", "year", "isbn", "pages", "duration", "size", "features", "narrator",
    "audience", "summary", "cover_url", "also_available_as", "note", "ereader",
    "added_rank", "series", "series_no", "age", "keywords", "category",
    "raw_json", "scraped_at",
]

# All tables, in FK-safe drop order, for a clean full rebuild in bulk_load.
_ALL_TABLES = ("work_genres", "genres", "work_authors", "authors", "publishers",
               "languages", "work_lists", "list_items", "lists", "editions", "works",
               "works_fts")

# works columns in the order _build_works selects them. Spelled out rather than
# relying on the DDL order, so adding a derived column (slug, series_slug — both
# filled by a later pass) can't silently shift every value one place over.
_WORKS_COLS = (
    "work_id", "title", "author", "summary", "cover_url", "language", "publisher",
    "year", "series", "series_no", "category", "audience", "age", "keywords",
    "has_ebook", "has_audiobook", "ereader", "ebook_ppn", "audiobook_ppn",
    "n_editions", "added_rank",
)


def connect(path: str | Path = DEFAULT_DB) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


def _rebuild_pragmas(cur: sqlite3.Cursor) -> None:
    """Session settings for a full rebuild.

    No WAL/rollback journal: peak disk stays ~the DB size (no ~equal-size WAL
    beside it), so the rebuild fits a small volume. Safe here because the rebuild
    is re-runnable from data/raw if the process is killed.

    Foreign keys off, because the build inserts children before their derived
    parent: ``work_genres`` / ``work_authors`` are keyed by work_id while
    ``works`` is still an ``INSERT … SELECT`` away. The declarations stay in the
    schema — they document the shape and drive ON DELETE CASCADE afterwards — but
    a rebuild is set-based and trusted, and the invariant tests are what actually
    hold it to the contract.
    """
    cur.execute("PRAGMA foreign_keys = OFF")
    cur.execute("PRAGMA synchronous = OFF")
    cur.execute("PRAGMA temp_store = MEMORY")
    cur.execute("PRAGMA journal_mode = OFF")


def _reset_schema(cur: sqlite3.Cursor) -> None:
    """Drop every table and recreate from ``_SCHEMA`` — a clean full rebuild
    (also picks up any schema changes since the last load)."""
    for t in _ALL_TABLES:
        cur.execute(f"DROP TABLE IF EXISTS {t}")
    cur.executescript(_SCHEMA)


def _edition_row(r: dict[str, Any]) -> tuple:
    rj = r.get("raw_json")
    if rj is None:
        rj = json.dumps(r, ensure_ascii=False)
    return tuple(rj if c == "raw_json" else r.get(c) for c in _EDITION_COLS)


def _insert_editions(cur: sqlite3.Cursor, records: list[dict[str, Any]]) -> int:
    rows = [_edition_row(r) for r in records]
    placeholders = ", ".join("?" for _ in _EDITION_COLS)
    cur.executemany(
        f"INSERT OR REPLACE INTO editions ({', '.join(_EDITION_COLS)}) "
        f"VALUES ({placeholders})",
        rows)
    return len(rows)


def _insert_genres(cur: sqlite3.Cursor, records: list[dict[str, Any]]) -> None:
    """Genre links, keyed by work: the union over a work's editions falls out of
    the primary key (two editions tagged the same subject give one link)."""
    pairs = [(r["work_id"], s) for r in records
             for s in dict.fromkeys(r.get("subjects") or []) if s]
    cur.executemany("INSERT OR IGNORE INTO genres(name) VALUES (?)",
                    [(n,) for n in sorted({s for _, s in pairs})])
    gid = {row["name"]: row["id"] for row in cur.execute("SELECT id, name FROM genres")}
    cur.executemany("INSERT OR IGNORE INTO work_genres(work_id, genre_id) VALUES (?, ?)",
                    [(wid, gid[s]) for wid, s in pairs if s in gid])


def _author_key(name: str) -> str:
    """Identity key for a person: the folded name.

    A name with no Latin characters at all folds to ``""`` and must never merge
    with another — several unrelated non-Latin names share that empty fold — so
    those fall back to the exact spelling, one row each, as before.
    """
    return fold(name) or "\x00" + name


def _insert_authors(cur: sqlite3.Cursor, records: list[dict[str, Any]]) -> None:
    """One ``authors`` row per person, linked to works.

    Keyed by fold, so "Ad Van Schaik" and "Ad van Schaik" are one row and the
    hub, the shelf and the filter all agree without a read-time merge. The
    display spelling is voted on afterwards (:func:`_stamp_author_names`).
    """
    apairs = []  # (work_id, author_key, position)
    spell_counts: dict[str, collections.Counter] = {}
    first_seen: dict[str, str] = {}
    for r in records:
        names = r.get("authors") or ([r["author"]] if r.get("author") else [])
        for pos, name in enumerate(dict.fromkeys(n for n in names if n)):
            key = _author_key(name)
            spell_counts.setdefault(key, collections.Counter())[name] += 1
            first_seen.setdefault(key, name)
            apairs.append((r["work_id"], key, pos))
    cur.executemany("INSERT OR IGNORE INTO authors(name, name_fold) VALUES (?, ?)",
                    [(n, fold(n)) for n in first_seen.values()])
    aid = {_author_key(row["name"]): row["id"]
           for row in cur.execute("SELECT id, name FROM authors")}
    cur.executemany(
        "INSERT OR IGNORE INTO work_authors(work_id, author_id, position) VALUES (?, ?, ?)",
        [(wid, aid[key], pos) for wid, key, pos in apairs if key in aid])
    _stamp_author_names(cur, spell_counts, aid)


def _stamp_author_names(cur: sqlite3.Cursor, spell_counts: dict[str, collections.Counter],
                        aid: dict[str, int]) -> None:
    """Give every person their most-common spelling, plus the two A-Z sort keys.

    This is the read-time spelling vote (``author_display_name``'s old GROUP BY)
    moved to build time — the shelf, the hub and the sitemap then all read one
    indexed column. Ties break on the lexicographically smallest spelling:
    ``Counter.most_common`` is insertion-ordered among equal counts, which would
    make the display name depend on the order records happened to stream in.
    """
    rows = []
    for key, counter in spell_counts.items():
        if key not in aid:
            continue
        best = min(counter.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        rows.append((best, surname_key(best), slugify(best), aid[key]))
    cur.executemany(
        "UPDATE authors SET name = ?, surname_sort = ?, first_sort = ? WHERE id = ?",
        rows)


def _distinct_counts(records: list[dict[str, Any]], field: str) -> dict[str, int]:
    """``{value: occurrence count}`` for a single field, counted once per *work*.

    Only the representative edition contributes, so the autocomplete counts read
    as "books by this publisher" rather than "files".
    """
    counts: dict[str, int] = {}
    for r in records:
        if r.get("work_id") != r.get("ppn"):
            continue
        v = r.get(field)
        if v:
            counts[v] = counts.get(v, 0) + 1
    return counts


def _insert_facets(cur: sqlite3.Cursor, records: list[dict[str, Any]]) -> None:
    """Distinct publishers + languages (folded, with counts) for autocomplete."""
    for table, field in (("publishers", "publisher"), ("languages", "language")):
        cur.executemany(
            f"INSERT INTO {table}(name, name_fold, n) VALUES (?, ?, ?)",
            [(v, fold(v), c) for v, c in _distinct_counts(records, field).items()])


def _insert_lists(cur: sqlite3.Cursor, lists: list[dict]) -> None:
    """Curated lists + full list_items (incl. unmatched) + work_lists (matched).

    ``item["ppn"]`` already holds a work_id — :func:`obc.normalize.match_lists`
    maps the matched edition onto its work, so a list slot lands on the book
    whichever edition it was catalogued from.
    """
    for lst in lists:
        cur.execute(
            "INSERT INTO lists(slug, name, url, description, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (lst["slug"], lst.get("name"), lst.get("url"),
             lst.get("description"), lst.get("updated_at")))
        list_id = cur.lastrowid
        items = lst.get("items", [])
        cur.executemany(
            "INSERT INTO list_items(list_id, position, year, title, author, isbn, cover_url, ppn, won) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(list_id, it.get("position"), it.get("year"), it.get("title"),
              it.get("author"), it.get("isbn"), it.get("cover_url"), it.get("ppn"),
              it.get("won"))
             for it in items])
        cur.executemany(
            "INSERT OR IGNORE INTO work_lists(work_id, list_id, position, year, won) "
            "VALUES (?, ?, ?, ?, ?)",
            [(it["ppn"], list_id, it.get("position"), it.get("year"), it.get("won"))
             for it in items if it.get("ppn")])


def _build_works(cur: sqlite3.Cursor) -> None:
    """Derive ``works`` from the editions already inserted — one set-based pass.

    Every work-level fact is answered here once, so no reader ever re-derives it:
    the representative's title/author/cover, the *longest* summary across the
    editions (the audiobook often carries the fuller blurb), the oldest year (it
    is the book's publication year, not the reprint's), and the availability
    flags plus per-format PPNs that used to cost two extra queries per result
    page. ``added_rank`` is a MIN, so a newly licensed audiobook resurfaces the
    book in "Recent toegevoegd".
    """
    cur.execute(
        f"INSERT INTO works ({', '.join(_WORKS_COLS)}) "
        "SELECT e.work_id, rep.title, rep.author, "
        "       (SELECT x.summary FROM editions x WHERE x.work_id = e.work_id "
        "          AND x.summary IS NOT NULL AND x.summary <> '' "
        "          ORDER BY length(x.summary) DESC, x.ppn LIMIT 1), "
        "       rep.cover_url, "
        "       COALESCE(rep.language,  MAX(e.language)), "
        "       rep.publisher, "
        "       MIN(NULLIF(e.year, 0)), "
        "       COALESCE(rep.series,    MAX(e.series)), "
        "       COALESCE(rep.series_no, MAX(e.series_no)), "
        "       COALESCE(rep.category,  MAX(e.category)), "
        "       COALESCE(rep.audience,  MAX(e.audience)), "
        "       COALESCE(rep.age,       MAX(e.age)), "
        "       COALESCE(rep.keywords,  MAX(e.keywords)), "
        "       MAX(e.format = 'ebook'), MAX(e.format = 'audiobook'), "
        "       MAX(CASE WHEN e.format = 'ebook' THEN e.ereader END), "
        "       MIN(CASE WHEN e.format = 'ebook' THEN e.ppn END), "
        "       MIN(CASE WHEN e.format = 'audiobook' THEN e.ppn END), "
        "       COUNT(*), "
        "       MIN(e.added_rank) "
        "FROM editions e JOIN editions rep ON rep.ppn = e.work_id "
        "GROUP BY e.work_id")


def _build_works_fts(cur: sqlite3.Cursor) -> None:
    """One FTS row per work, over the *pooled* text of its editions.

    Per-edition FTS rows plus a collapse-after-MATCH is how a book became
    unfindable: genres, keywords and summaries come from the per-PPN detail pass,
    so a term living only on the audiobook edition matched only that row — which
    the collapse then threw away. Pooling the text answers the query at the grain
    the reader is asking about. Subjects come from ``work_genres``, which is
    already the union the old per-record path only approximated.
    """
    cur.execute(
        "INSERT INTO works_fts(work_id, title, author, subjects, summary) "
        "SELECT w.work_id, "
        "       (SELECT group_concat(DISTINCT e.title)  FROM editions e "
        "        WHERE e.work_id = w.work_id), "
        "       (SELECT group_concat(DISTINCT e.author) FROM editions e "
        "        WHERE e.work_id = w.work_id), "
        "       COALESCE((SELECT group_concat(g.name, ' ') FROM work_genres wg "
        "                 JOIN genres g ON g.id = wg.genre_id "
        "                 WHERE wg.work_id = w.work_id), '') "
        "         || ' ' || "
        "       COALESCE((SELECT group_concat(DISTINCT e.keywords) FROM editions e "
        "                 WHERE e.work_id = w.work_id AND e.keywords IS NOT NULL), ''), "
        "       (SELECT group_concat(DISTINCT e.summary) FROM editions e "
        "        WHERE e.work_id = w.work_id AND e.summary IS NOT NULL) "
        "FROM works w")


def _stamp_counts(cur: sqlite3.Cursor) -> None:
    """Stamp ``authors.n_works`` / ``genres.n_works``.

    The facet panel and the autocomplete used to GROUP BY over the link tables on
    every request — eight concurrent cold /genres hits walked 157k rows each. The
    counts are a property of the rebuild, so the rebuild writes them and the read
    path is ``ORDER BY n_works DESC LIMIT n`` on an index. No COUNT(DISTINCT
    work_id) needed: the link tables' primary keys already dedupe.
    """
    cur.execute("UPDATE authors SET n_works = "
                "(SELECT COUNT(*) FROM work_authors WHERE author_id = authors.id)")
    cur.execute("UPDATE genres SET n_works = "
                "(SELECT COUNT(*) FROM work_genres WHERE genre_id = genres.id)")


def _cap_slug(value: str, limit: int) -> str:
    """A slug cut to ``limit`` characters on a dash boundary, never mid-word."""
    if len(value) <= limit:
        return value
    head = value[:limit]
    cut = head.rfind("-")
    return (head[:cut] if cut > 0 else head).strip("-")


def _stamp_work_slugs(cur: sqlite3.Cursor) -> None:
    """Stamp ``works.slug`` = ``'{title-slug}--{author-slug}'``.

    Built from our own :func:`obc.textnorm.slugify`, never the library's edition
    slug: those can themselves end in ``--``, ours never contain a double hyphen,
    which is what makes ``--`` an unambiguous separator from the work_id in the
    canonical ``/boek/{title}--{author}--{id}`` path. An empty piece drops *with*
    its separator, so a work with no author gets a title-only slug and a work with
    neither gets ``''`` (the route then serves the bare id).

    The first author comes from ``work_authors`` rather than by splitting the
    display string, which can hold a comma inside a single name ("Buren, van").
    The length caps may change later without breaking anything: a stale slug 301s
    to the current one, because the id is the truth and the slug is cosmetic.
    """
    rows = cur.execute(
        "SELECT w.work_id, w.title, "
        "       (SELECT a.name FROM work_authors wa JOIN authors a ON a.id = wa.author_id "
        "        WHERE wa.work_id = w.work_id ORDER BY wa.position LIMIT 1) AS author "
        "FROM works w").fetchall()
    updates = []
    for work_id, title, author in rows:
        parts = [p for p in (_cap_slug(slugify(title), 60),
                             _cap_slug(slugify(author), 40)) if p]
        updates.append(("--".join(parts), work_id))
    cur.executemany("UPDATE works SET slug = ? WHERE work_id = ?", updates)


def _stamp_series(cur: sqlite3.Cursor) -> None:
    """Stamp ``works.series_slug`` and fill the ``series`` table.

    ``works.series`` is free text with no folded column, so the slug -> spellings
    map used to be rebuilt per web process. Now the slug lives on the work (one
    indexed lookup per series page) and the display spelling + part count live in
    their own table.
    """
    counts = cur.execute(
        "SELECT series, COUNT(*) AS n FROM works "
        "WHERE COALESCE(series, '') <> '' GROUP BY series").fetchall()
    per_slug: dict[str, collections.Counter] = {}
    spellings = []
    for name, n in counts:
        slug = slugify(name)
        spellings.append((slug, name))
        if slug:
            per_slug.setdefault(slug, collections.Counter())[name] += n
    cur.executemany("UPDATE works SET series_slug = ? WHERE series = ?", spellings)
    cur.execute("DELETE FROM series")
    cur.executemany(
        "INSERT INTO series(slug, name, titles) VALUES (?, ?, ?)",
        [(slug, min(ctr.items(), key=lambda kv: (-kv[1], kv[0]))[0], sum(ctr.values()))
         for slug, ctr in per_slug.items()])


# The catalog carries two genre taxonomies, not one: jeugd and volwassenen reuse
# genre names under different parents, and 67 of 213 subgenres sit somewhere
# different depending on which shelf you are standing at.
_AUDIENCES = ("volwassenen", "jeugd")


def build_genre_taxonomy(conn: sqlite3.Connection) -> None:
    """Fill ``genre_pages`` + ``genre_tree``.

    This is the web layer's old ``_genre_data`` cache, moved to where it belongs:
    the slug merge (the catalog holds "Biografieën" twice, precomposed and with a
    combining diaeresis), the longest-spelling display name, the catalog-wide
    parent vote and the per-audience trees are all deterministic functions of the
    catalog, so they are computed once per rebuild instead of once per process.
    The rules are unchanged — only where they run.

    Must run *after* :func:`set_work_genre_parents`, which fills the parent_id it
    votes on.
    """
    cur = conn.cursor()
    flat: dict[str, dict] = {}
    per_aud: dict[str, dict[str, dict]] = {a: {} for a in _AUDIENCES}
    for row in cur.execute(
            "SELECT g.name AS name, p.name AS parent, wg.work_id AS work_id, "
            "       lower(COALESCE(w.audience, '')) AS audience "
            "FROM genres g JOIN work_genres wg ON wg.genre_id = g.id "
            "LEFT JOIN genres p ON p.id = wg.parent_id "
            "JOIN works w ON w.work_id = wg.work_id").fetchall():
        slug = slugify(row["name"])
        if not slug:
            continue
        entry = flat.setdefault(slug, {"name": row["name"], "names": [], "works": set(),
                                       "parents": collections.Counter()})
        if row["name"] not in entry["names"]:
            entry["names"].append(row["name"])
        entry["works"].add(row["work_id"])
        pslug = slugify(row["parent"] or "")
        entry["parents"][pslug] += 1
        # 2.567 books carry no audience, and a catalog built without the detail
        # pass has none at all — those land on the default shelf rather than
        # falling out of the hub entirely. No genre in the live catalog is
        # reachable *only* that way, so this shifts counts, never visibility.
        aud = per_aud[row["audience"] if row["audience"] in per_aud else _AUDIENCES[0]]
        a = aud.setdefault(slug, {"works": set(), "parents": collections.Counter()})
        a["works"].add(row["work_id"])
        a["parents"][pslug] += 1

    for entry in flat.values():
        parent = entry["parents"].most_common(1)[0][0]
        entry["parent"] = parent if parent and parent != slugify(entry["name"]) else ""
        entry["titles"] = len(entry.pop("works"))
        entry["name"] = max(entry["names"], key=len)
        del entry["parents"]

    cur.execute("DELETE FROM genre_pages")
    cur.executemany(
        "INSERT INTO genre_pages(slug, name, titles, parent_slug) VALUES (?, ?, ?, ?)",
        [(slug, e["name"], e["titles"], e["parent"]) for slug, e in flat.items()])

    cur.execute("DELETE FROM genre_tree")
    rows = []
    for aud in _AUDIENCES:
        for slug, a in per_aud[aud].items():
            # A tree may only state what its own audience files. Borrowing the
            # catalog-wide parent when this audience names none put "Film" under
            # Kunst & Cultuur for adults, where all three adult records carry no
            # parent at all — a relationship the data never asserts. An explicit
            # parent still outranks a bare "top level" *within* the audience.
            named = [(pslug, n) for pslug, n in a["parents"].items()
                     if pslug and pslug != slug and pslug in per_aud[aud]]
            parent = max(named, key=lambda kv: kv[1])[0] if named else ""
            rows.append((aud, slug, parent, len(a["works"])))
    cur.executemany(
        "INSERT INTO genre_tree(audience, slug, parent_slug, titles) VALUES (?, ?, ?, ?)",
        rows)
    conn.commit()


def analyze(cur: sqlite3.Cursor) -> None:
    """Collect index statistics into ``sqlite_stat1`` (~0.3s on the full catalog).

    Without them SQLite plans the facet queries blind: a genre or list filter is a
    subquery over a link table holding a few thousand entries for that value, but
    the planner can't know that and may drive off the works side instead, walking
    every row per request. With statistics it drives off the selective side
    (``idx_wg_genre``, ``idx_wl_list``): ``?genre=...`` stays single-digit
    milliseconds for both the COUNT and the row fetch.

    Run at the end of every rebuild, not from ``_SCHEMA``: statistics describe the data,
    so they go stale as the catalog changes and are only worth what the last refresh
    measured."""
    cur.execute("ANALYZE")


def _build_derived(cur: sqlite3.Cursor) -> None:
    """Every derived read table/column, in dependency order.

    ``works`` first (everything hangs off it), then the FTS text (which reads
    work_genres), then the columns the read path treats as given. The genre
    taxonomy is *not* here: it needs work_genres.parent_id, which normalize
    stamps after the rebuild — see :func:`build_genre_taxonomy`.
    """
    _build_works(cur)
    _build_works_fts(cur)
    _stamp_counts(cur)
    _stamp_work_slugs(cur)
    _stamp_series(cur)


def bulk_load(conn: sqlite3.Connection, records: Iterable[dict[str, Any]],
              lists: list[dict] | None = None) -> int:
    """Fast full rebuild: truncate then batch-insert everything.

    Much faster than row-at-a-time writes (no per-record SELECT/DELETE, all
    ``executemany``). Use when loading the whole catalog from scratch.

    ``lists`` is an optional list of ``{slug,name,url,description,items}`` where
    each item is ``{"ppn":..., "position":...}`` (curated lists -> work_lists).
    """
    # Records that already carry a work_id keep it (normalize stamps its own,
    # from the same grouping, during its streaming prepass).
    records = work.stamp_work_ids(list(records))
    cur = conn.cursor()
    _rebuild_pragmas(cur)
    _reset_schema(cur)
    n = _insert_editions(cur, records)
    _insert_genres(cur, records)
    _insert_authors(cur, records)
    _insert_facets(cur, records)
    _build_derived(cur)
    _insert_lists(cur, lists or [])
    analyze(cur)
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")
    return n


def stream_rebuild(conn: sqlite3.Connection, records: Iterable[dict[str, Any]],
                   lists: list[dict] | None = None, batch: int = 2000) -> int:
    """Low-memory full rebuild: stream records in batches instead of holding the
    whole catalog in RAM. Same result as :func:`bulk_load`, but peak memory is
    ~constant (small id caches + one batch), so it runs on a tiny box.

    Records are expected to arrive already carrying ``work_id`` (normalize stamps
    them in its prepass, which reads every record file anyway); a record without
    one falls back to being its own work rather than failing the rebuild. No FTS
    rows are written while streaming — ``works_fts`` is built set-based from the
    finished ``works``.
    """
    cur = conn.cursor()
    _rebuild_pragmas(cur)
    for t in _ALL_TABLES:
        cur.execute(f"DROP TABLE IF EXISTS {t}")
    cur.executescript(_SCHEMA)

    edition_sql = (f"INSERT OR REPLACE INTO editions ({', '.join(_EDITION_COLS)}) "
                   f"VALUES ({', '.join('?' for _ in _EDITION_COLS)})")
    gid_cache: dict[str, int] = {}
    aid_cache: dict[str, int] = {}
    spell_counts: dict[str, collections.Counter] = {}
    pub_counts: dict[str, int] = {}
    lang_counts: dict[str, int] = {}
    edition_rows: list = []
    wg_rows: list = []
    wa_rows: list = []
    n = 0

    def _gid(name: str) -> int:
        i = gid_cache.get(name)
        if i is None:
            cur.execute("INSERT OR IGNORE INTO genres(name) VALUES (?)", (name,))
            i = cur.execute("SELECT id FROM genres WHERE name = ?", (name,)).fetchone()["id"]
            gid_cache[name] = i
        return i

    def _aid(name: str) -> int:
        """The person id for a spelling, inserting the first spelling seen as the
        display name; the majority spelling replaces it after streaming."""
        key = _author_key(name)
        spell_counts.setdefault(key, collections.Counter())[name] += 1
        i = aid_cache.get(key)
        if i is None:
            cur.execute("INSERT OR IGNORE INTO authors(name, name_fold) VALUES (?, ?)",
                        (name, fold(name)))
            i = cur.execute("SELECT id FROM authors WHERE name = ?",
                            (name,)).fetchone()["id"]
            aid_cache[key] = i
        return i

    def flush():
        if edition_rows:
            cur.executemany(edition_sql, edition_rows)
            edition_rows.clear()
        if wg_rows:
            cur.executemany("INSERT OR IGNORE INTO work_genres(work_id, genre_id) "
                            "VALUES (?, ?)", wg_rows)
            wg_rows.clear()
        if wa_rows:
            cur.executemany("INSERT OR IGNORE INTO work_authors(work_id, author_id, "
                            "position) VALUES (?, ?, ?)", wa_rows)
            wa_rows.clear()

    for r in records:
        ppn = r.get("ppn")
        if not ppn:
            continue
        work_id = r.get("work_id") or ppn
        r["work_id"] = work_id
        edition_rows.append(_edition_row(r))
        for s in dict.fromkeys(s for s in (r.get("subjects") or []) if s):
            wg_rows.append((work_id, _gid(s)))
        names = r.get("authors") or ([r["author"]] if r.get("author") else [])
        for pos, name in enumerate(dict.fromkeys(nm for nm in names if nm)):
            wa_rows.append((work_id, _aid(name), pos))
        # one count per work — the representative's spelling, so the autocomplete
        # counts books rather than files
        if work_id == ppn:
            if r.get("publisher"):
                pub_counts[r["publisher"]] = pub_counts.get(r["publisher"], 0) + 1
            if r.get("language"):
                lang_counts[r["language"]] = lang_counts.get(r["language"], 0) + 1
        n += 1
        if len(edition_rows) >= batch:
            flush()
    flush()

    cur.executemany("INSERT INTO publishers(name, name_fold, n) VALUES (?, ?, ?)",
                    [(p, fold(p), c) for p, c in pub_counts.items()])
    cur.executemany("INSERT INTO languages(name, name_fold, n) VALUES (?, ?, ?)",
                    [(lg, fold(lg), c) for lg, c in lang_counts.items()])
    _stamp_author_names(cur, spell_counts, aid_cache)
    _build_derived(cur)
    _insert_lists(cur, lists or [])
    analyze(cur)
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")
    return n


def load_prior_ereader(path: str | Path | None) -> dict[str, int]:
    """``{ppn: ereader}`` for editions whose e-reader flag is already known in the
    live DB. :mod:`obc.normalize` uses it to keep the flag when the ereader
    side-file is missing, so a lost side-file can't silently blank the whole
    facet on the next rebuild. Empty ``{}`` if the DB / table is absent (fresh
    volume) — nothing to preserve then.

    Falls back to the old ``books`` table: on the deploy that introduces this
    schema the *live* DB it reads still has the old shape, and losing the flag
    then is exactly the failure this function exists to prevent.
    """
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        return {}
    try:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        try:
            try:
                rows = conn.execute(
                    "SELECT ppn, ereader FROM editions "
                    "WHERE ereader IS NOT NULL").fetchall()
            except sqlite3.Error:
                rows = conn.execute(
                    "SELECT ppn, ereader FROM books WHERE ereader IS NOT NULL").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
    return dict(rows)


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    """Catalog counts at both grains — a book and the files you borrow it as are
    different numbers, and /stats used to quietly report editions as titles."""
    def g(q: str, *a: Any) -> Any:
        return conn.execute(q, a).fetchone()[0]
    return {
        "works": g("SELECT COUNT(*) FROM works"),
        "editions": g("SELECT COUNT(*) FROM editions"),
        "ebooks": g("SELECT COUNT(*) FROM works WHERE has_ebook = 1"),
        "audiobooks": g("SELECT COUNT(*) FROM works WHERE has_audiobook = 1"),
        "genres": g("SELECT COUNT(*) FROM genres"),
        "languages": g("SELECT COUNT(DISTINCT language) FROM works"),
    }


def set_work_genre_parents(conn: sqlite3.Connection, genre_info: tuple) -> None:
    """Stamp ``work_genres.parent_id`` with the parent genre *resolved within each
    work's own audience*.

    ``genre_info`` is ``(genre_code, genre_count)`` where ``genre_code`` maps
    ``(audience, name) -> 'major.minor' facet code`` and ``genre_count`` how many
    books carry each ``(audience, name)``. Jeugd and volwassenen reuse the same
    numbers but mean different genres, so the same name can have a different parent
    per audience — hence the parent lives on the per-work link, not the (name-keyed)
    genre row. The top genre for an ``(audience, code)`` is the **most common** name
    there, so a name that leaked into the wrong audience's data can't hijack the
    parent. A small ``(audience, genre_id) -> parent_id`` table then drives one
    set-based UPDATE, cheap in memory even with hundreds of thousands of links."""
    genre_code, genre_count = genre_info
    gid_of = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM genres")}
    # most common genre name per (audience, code) -> its id
    best: dict[tuple[str, str], tuple[int, str]] = {}
    for (aud, name), code in genre_code.items():
        c = genre_count.get((aud, name), 0)
        if (aud, code) not in best or c > best[(aud, code)][0]:
            best[(aud, code)] = (c, name)
    gid_by_aud_code = {ac: gid_of[nm] for ac, (_, nm) in best.items() if nm in gid_of}
    parents: dict[tuple[str, int], int] = {}  # (audience, genre_id) -> parent_id
    for (aud, name), code in genre_code.items():
        if name not in gid_of:
            continue
        major, _, minor = code.partition(".")
        if minor in ("", "0"):
            continue  # top-level genre — no parent
        pid = gid_by_aud_code.get((aud, f"{major}.0"))
        if pid and pid != gid_of[name]:
            parents[(aud, gid_of[name])] = pid
    cur = conn.cursor()
    cur.execute("CREATE TEMP TABLE _gpa (audience TEXT, genre_id INTEGER, parent_id INTEGER)")
    cur.executemany("INSERT INTO _gpa VALUES (?, ?, ?)",
                    [(aud, gid, pid) for (aud, gid), pid in parents.items()])
    cur.execute("CREATE INDEX _gpa_idx ON _gpa (genre_id, audience)")
    cur.execute(
        "UPDATE work_genres SET parent_id = ("
        "  SELECT gp.parent_id FROM _gpa gp JOIN works w ON w.work_id = work_genres.work_id "
        "  WHERE gp.genre_id = work_genres.genre_id "
        "    AND gp.audience = lower(COALESCE(w.audience, '')))")
    cur.execute("DROP TABLE _gpa")
    conn.commit()
