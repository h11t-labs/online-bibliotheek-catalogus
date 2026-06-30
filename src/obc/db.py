"""SQLite schema + FTS5 helpers for the catalog.

Design notes
------------
* ``books`` holds one normalised row per PPN.
* ``genres`` / ``book_genres`` model the many-to-many subjects for faceted
  filtering (one genre row per distinct subject string).
* ``books_fts`` is a standalone FTS5 table (not external-content) so upserts are
  trivial: delete-by-ppn then insert. ``unicode61 remove_diacritics 2`` folds
  Dutch diacritics so "espana"-style queries match "España", etc.

All writes go through :func:`upsert_book`, which is idempotent on ``ppn`` — safe
to re-run after a fresh scrape.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .textnorm import fold

# Honour OBC_DB so the CLI (scrape/normalize/sync) and the web app share one path
# (e.g. a Railway volume at /app/data/catalog.db).
DEFAULT_DB = Path(os.environ.get("OBC_DB", "data/catalog.db"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
    ppn               TEXT PRIMARY KEY,
    slug              TEXT,
    url               TEXT,
    title             TEXT,
    author            TEXT,
    format            TEXT,            -- 'ebook' | 'audiobook'
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
    ereader           INTEGER,         -- 1 = available for e-reader (e-books)
    added_rank        INTEGER,         -- recency rank by license date (0 = newest)
    series            TEXT,
    series_no         INTEGER,
    age               TEXT,            -- reading age, e.g. "9-12 jaar" (detail page)
    keywords          TEXT,            -- free keyword tags (detail page)
    category          TEXT,            -- 'fictie' | 'nonfictie'
    raw_json          TEXT,
    scraped_at        TEXT
);

CREATE TABLE IF NOT EXISTS genres (
    id   INTEGER PRIMARY KEY,
    name TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS book_genres (
    book_ppn  TEXT NOT NULL REFERENCES books(ppn) ON DELETE CASCADE,
    genre_id  INTEGER NOT NULL REFERENCES genres(id) ON DELETE CASCADE,
    parent_id INTEGER,   -- the parent genre *for this book's audience* (NULL = top)
    PRIMARY KEY (book_ppn, genre_id)
);

CREATE TABLE IF NOT EXISTS authors (
    id        INTEGER PRIMARY KEY,
    name      TEXT UNIQUE,
    name_fold TEXT
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

CREATE TABLE IF NOT EXISTS book_authors (
    book_ppn  TEXT NOT NULL REFERENCES books(ppn) ON DELETE CASCADE,
    author_id INTEGER NOT NULL REFERENCES authors(id) ON DELETE CASCADE,
    position  INTEGER,
    PRIMARY KEY (book_ppn, author_id)
);

CREATE TABLE IF NOT EXISTS lists (
    id          INTEGER PRIMARY KEY,
    slug        TEXT UNIQUE,
    name        TEXT,
    url         TEXT,
    description TEXT,
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS book_lists (
    book_ppn TEXT NOT NULL REFERENCES books(ppn) ON DELETE CASCADE,
    list_id  INTEGER NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
    position INTEGER,
    year     INTEGER,           -- award year (prizes); NULL for ranked lists
    won      INTEGER,           -- 1 = won, 0 = nominated (prizes); NULL otherwise
    PRIMARY KEY (book_ppn, list_id)
);

-- full ordered list contents (incl. titles not in the library; ppn is NULL then)
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

CREATE INDEX IF NOT EXISTS idx_books_format   ON books(format);
CREATE INDEX IF NOT EXISTS idx_books_language ON books(language);
CREATE INDEX IF NOT EXISTS idx_books_year     ON books(year);
CREATE INDEX IF NOT EXISTS idx_books_ereader  ON books(ereader);
CREATE INDEX IF NOT EXISTS idx_books_title     ON books(title);
CREATE INDEX IF NOT EXISTS idx_books_added     ON books(added_rank);
CREATE INDEX IF NOT EXISTS idx_books_series     ON books(series);
-- case-insensitive (title, author) for the "other editions of this work" lookup on
-- every book page — without it that query full-scans all books (≈4s on Fly's shared CPU)
CREATE INDEX IF NOT EXISTS idx_books_title_author_lower
    ON books(lower(title), lower(COALESCE(author, '')));
CREATE INDEX IF NOT EXISTS idx_bg_genre       ON book_genres(genre_id);
CREATE INDEX IF NOT EXISTS idx_ba_author      ON book_authors(author_id);
CREATE INDEX IF NOT EXISTS idx_authors_fold   ON authors(name_fold);
CREATE INDEX IF NOT EXISTS idx_publishers_fold ON publishers(name_fold);
CREATE INDEX IF NOT EXISTS idx_bl_list        ON book_lists(list_id);
CREATE INDEX IF NOT EXISTS idx_li_list        ON list_items(list_id);

CREATE VIRTUAL TABLE IF NOT EXISTS books_fts USING fts5(
    ppn UNINDEXED,
    title,
    author,
    subjects,
    summary,
    tokenize = 'unicode61 remove_diacritics 2'
);
"""

_BOOK_COLS = [
    "ppn", "slug", "url", "title", "author", "format", "language", "publisher",
    "year", "isbn", "pages", "duration", "size", "features", "narrator",
    "audience", "summary", "cover_url", "also_available_as", "note", "ereader",
    "added_rank", "series", "series_no", "age", "keywords", "category",
    "raw_json", "scraped_at",
]

# All tables, in FK-safe drop order, for a clean full rebuild in bulk_load.
_ALL_TABLES = ("book_genres", "genres", "book_authors", "authors", "publishers",
               "languages", "book_lists", "list_items", "lists", "books", "books_fts")


def _fts_values(rec: dict[str, Any]) -> tuple:
    """The 5-tuple inserted into ``books_fts`` for one record (subjects + keywords
    share the ``subjects`` column so both are searchable)."""
    kw = rec.get("keywords")
    kw = " ".join(kw) if isinstance(kw, list) else (kw or "")
    subjects = (" ".join(rec.get("subjects") or []) + " " + kw).strip()
    return (rec["ppn"], rec.get("title") or "", rec.get("author") or "",
            subjects, rec.get("summary") or "")


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


def _genre_id(conn: sqlite3.Connection, name: str) -> int:
    conn.execute("INSERT OR IGNORE INTO genres(name) VALUES (?)", (name,))
    row = conn.execute("SELECT id FROM genres WHERE name = ?", (name,)).fetchone()
    return row["id"]


def upsert_book(conn: sqlite3.Connection, rec: dict[str, Any]) -> None:
    """Insert or update one book record (idempotent on ``ppn``)."""
    ppn = rec.get("ppn")
    if not ppn:
        return
    subjects: list[str] = rec.get("subjects") or []

    values = {c: rec.get(c) for c in _BOOK_COLS}
    if values.get("raw_json") is None and "raw_json" not in rec:
        values["raw_json"] = json.dumps(rec, ensure_ascii=False)

    placeholders = ", ".join("?" for _ in _BOOK_COLS)
    updates = ", ".join(f"{c}=excluded.{c}" for c in _BOOK_COLS if c != "ppn")
    conn.execute(
        f"INSERT INTO books ({', '.join(_BOOK_COLS)}) VALUES ({placeholders}) "
        f"ON CONFLICT(ppn) DO UPDATE SET {updates}",
        [values[c] for c in _BOOK_COLS],
    )

    # refresh genres
    conn.execute("DELETE FROM book_genres WHERE book_ppn = ?", (ppn,))
    for name in dict.fromkeys(s for s in subjects if s):
        gid = _genre_id(conn, name)
        conn.execute(
            "INSERT OR IGNORE INTO book_genres(book_ppn, genre_id) VALUES (?, ?)",
            (ppn, gid),
        )

    # refresh FTS row
    conn.execute("DELETE FROM books_fts WHERE ppn = ?", (ppn,))
    conn.execute(
        "INSERT INTO books_fts(ppn, title, author, subjects, summary) "
        "VALUES (?, ?, ?, ?, ?)",
        _fts_values(rec),
    )


def upsert_many(conn: sqlite3.Connection, recs: Iterable[dict[str, Any]]) -> int:
    n = 0
    for rec in recs:
        upsert_book(conn, rec)
        n += 1
    conn.commit()
    return n


def _reset_schema(cur: sqlite3.Cursor) -> None:
    """Drop every table and recreate from ``_SCHEMA`` — a clean full rebuild
    (also picks up any schema changes since the last load)."""
    for t in _ALL_TABLES:
        cur.execute(f"DROP TABLE IF EXISTS {t}")
    cur.executescript(_SCHEMA)


def _insert_books(cur: sqlite3.Cursor, records: list[dict[str, Any]]) -> int:
    rows = []
    for r in records:
        rj = r.get("raw_json")
        if rj is None:
            rj = json.dumps(r, ensure_ascii=False)
        rows.append(tuple(rj if c == "raw_json" else r.get(c) for c in _BOOK_COLS))
    placeholders = ", ".join("?" for _ in _BOOK_COLS)
    cur.executemany(
        f"INSERT OR REPLACE INTO books ({', '.join(_BOOK_COLS)}) VALUES ({placeholders})",
        rows)
    return len(rows)


def _insert_genres(cur: sqlite3.Cursor, records: list[dict[str, Any]]) -> None:
    pairs = [(r["ppn"], s) for r in records
             for s in dict.fromkeys(r.get("subjects") or []) if s]
    cur.executemany("INSERT OR IGNORE INTO genres(name) VALUES (?)",
                    [(n,) for n in sorted({s for _, s in pairs})])
    gid = {row["name"]: row["id"] for row in cur.execute("SELECT id, name FROM genres")}
    cur.executemany("INSERT OR IGNORE INTO book_genres(book_ppn, genre_id) VALUES (?, ?)",
                    [(ppn, gid[s]) for ppn, s in pairs if s in gid])


def _insert_authors(cur: sqlite3.Cursor, records: list[dict[str, Any]]) -> None:
    apairs = []  # (ppn, author_name, position)
    for r in records:
        names = r.get("authors") or ([r["author"]] if r.get("author") else [])
        for pos, name in enumerate(dict.fromkeys(n for n in names if n)):
            apairs.append((r["ppn"], name, pos))
    cur.executemany("INSERT OR IGNORE INTO authors(name, name_fold) VALUES (?, ?)",
                    [(n, fold(n)) for n in sorted({name for _, name, _ in apairs})])
    aid = {row["name"]: row["id"] for row in cur.execute("SELECT id, name FROM authors")}
    cur.executemany(
        "INSERT OR IGNORE INTO book_authors(book_ppn, author_id, position) VALUES (?, ?, ?)",
        [(ppn, aid[name], pos) for ppn, name, pos in apairs if name in aid])


def _distinct_counts(records: list[dict[str, Any]], field: str) -> dict[str, int]:
    """``{value: occurrence count}`` for a single book field (skips blanks)."""
    counts: dict[str, int] = {}
    for r in records:
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
    """Curated lists + full list_items (incl. unmatched) + book_lists (matched)."""
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
            "INSERT OR IGNORE INTO book_lists(book_ppn, list_id, position, year, won) "
            "VALUES (?, ?, ?, ?, ?)",
            [(it["ppn"], list_id, it.get("position"), it.get("year"), it.get("won"))
             for it in items if it.get("ppn")])


def _insert_fts(cur: sqlite3.Cursor, records: list[dict[str, Any]]) -> None:
    cur.executemany(
        "INSERT INTO books_fts(ppn, title, author, subjects, summary) VALUES (?, ?, ?, ?, ?)",
        [_fts_values(r) for r in records if r.get("ppn")])


def bulk_load(conn: sqlite3.Connection, records: Iterable[dict[str, Any]],
              lists: list[dict] | None = None) -> int:
    """Fast full rebuild: truncate then batch-insert everything.

    Much faster than per-row upserts (no per-record SELECT/DELETE, all
    ``executemany``). Use when loading the whole catalog from scratch.

    ``lists`` is an optional list of ``{slug,name,url,description,items}`` where
    each item is ``{"ppn":..., "position":...}`` (curated lists -> book_lists).
    """
    records = list(records)
    cur = conn.cursor()
    cur.execute("PRAGMA synchronous = OFF")
    cur.execute("PRAGMA temp_store = MEMORY")
    # No WAL/rollback journal during a full rebuild: peak disk stays ~the DB size
    # (no ~equal-size WAL beside it), so the rebuild fits a small volume. Safe here
    # because the rebuild is re-runnable from data/raw if the process is killed.
    cur.execute("PRAGMA journal_mode = OFF")
    _reset_schema(cur)
    n = _insert_books(cur, records)
    _insert_genres(cur, records)
    _insert_authors(cur, records)
    _insert_facets(cur, records)
    _insert_lists(cur, lists or [])
    _insert_fts(cur, records)
    conn.commit()
    return n


def stream_rebuild(conn: sqlite3.Connection, records: Iterable[dict[str, Any]],
                   lists: list[dict] | None = None, batch: int = 2000) -> int:
    """Low-memory full rebuild: stream records in batches instead of holding the
    whole catalog in RAM. Same result as :func:`bulk_load`, but peak memory is
    ~constant (small id caches + one batch), so it runs on a tiny box."""
    cur = conn.cursor()
    cur.execute("PRAGMA synchronous = OFF")
    cur.execute("PRAGMA temp_store = MEMORY")
    # No WAL/rollback journal during a full rebuild: peak disk stays ~the DB size
    # (no ~equal-size WAL beside it), so the rebuild fits a small volume. Safe here
    # because the rebuild is re-runnable from data/raw if the process is killed.
    cur.execute("PRAGMA journal_mode = OFF")
    for t in _ALL_TABLES:
        cur.execute(f"DROP TABLE IF EXISTS {t}")
    cur.executescript(_SCHEMA)

    book_sql = (f"INSERT OR REPLACE INTO books ({', '.join(_BOOK_COLS)}) "
                f"VALUES ({', '.join('?' for _ in _BOOK_COLS)})")
    gid_cache: dict[str, int] = {}
    aid_cache: dict[str, int] = {}
    pub_counts: dict[str, int] = {}
    lang_counts: dict[str, int] = {}
    book_rows: list = []
    bg_rows: list = []
    ba_rows: list = []
    fts_rows: list = []
    n = 0

    def _id(cache, table, name):
        i = cache.get(name)
        if i is None:
            if table == "authors":
                cur.execute("INSERT OR IGNORE INTO authors(name, name_fold) VALUES (?, ?)",
                            (name, fold(name)))
            else:
                cur.execute(f"INSERT OR IGNORE INTO {table}(name) VALUES (?)", (name,))
            i = cur.execute(f"SELECT id FROM {table} WHERE name = ?", (name,)).fetchone()["id"]
            cache[name] = i
        return i

    def flush():
        if book_rows:
            cur.executemany(book_sql, book_rows)
            book_rows.clear()
        if bg_rows:
            cur.executemany("INSERT OR IGNORE INTO book_genres(book_ppn, genre_id) "
                            "VALUES (?, ?)", bg_rows)
            bg_rows.clear()
        if ba_rows:
            cur.executemany("INSERT OR IGNORE INTO book_authors(book_ppn, author_id, "
                            "position) VALUES (?, ?, ?)", ba_rows)
            ba_rows.clear()
        if fts_rows:
            cur.executemany("INSERT INTO books_fts(ppn, title, author, subjects, "
                            "summary) VALUES (?, ?, ?, ?, ?)", fts_rows)
            fts_rows.clear()

    for r in records:
        ppn = r.get("ppn")
        if not ppn:
            continue
        rj = r.get("raw_json")
        if rj is None:
            rj = json.dumps(r, ensure_ascii=False)
        book_rows.append(tuple(rj if c == "raw_json" else r.get(c) for c in _BOOK_COLS))
        for s in dict.fromkeys(s for s in (r.get("subjects") or []) if s):
            bg_rows.append((ppn, _id(gid_cache, "genres", s)))
        names = r.get("authors") or ([r["author"]] if r.get("author") else [])
        for pos, name in enumerate(dict.fromkeys(nm for nm in names if nm)):
            ba_rows.append((ppn, _id(aid_cache, "authors", name), pos))
        if r.get("publisher"):
            pub_counts[r["publisher"]] = pub_counts.get(r["publisher"], 0) + 1
        if r.get("language"):
            lang_counts[r["language"]] = lang_counts.get(r["language"], 0) + 1
        fts_rows.append(_fts_values(r))
        n += 1
        if len(book_rows) >= batch:
            flush()
    flush()

    cur.executemany("INSERT INTO publishers(name, name_fold, n) VALUES (?, ?, ?)",
                    [(p, fold(p), c) for p, c in pub_counts.items()])
    cur.executemany("INSERT INTO languages(name, name_fold, n) VALUES (?, ?, ?)",
                    [(lg, fold(lg), c) for lg, c in lang_counts.items()])
    _insert_lists(cur, lists or [])
    conn.commit()
    return n


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    def g(q: str, *a: Any) -> Any:
        return conn.execute(q, a).fetchone()[0]
    return {
        "books": g("SELECT COUNT(*) FROM books"),
        "ebooks": g("SELECT COUNT(*) FROM books WHERE format='ebook'"),
        "audiobooks": g("SELECT COUNT(*) FROM books WHERE format='audiobook'"),
        "genres": g("SELECT COUNT(*) FROM genres"),
        "languages": g("SELECT COUNT(DISTINCT language) FROM books"),
    }


def set_book_genre_parents(conn: sqlite3.Connection, genre_info: tuple) -> None:
    """Stamp ``book_genres.parent_id`` with the parent genre *resolved within each
    book's own audience*.

    ``genre_info`` is ``(genre_code, genre_count)`` where ``genre_code`` maps
    ``(audience, name) -> 'major.minor' facet code`` and ``genre_count`` how many
    books carry each ``(audience, name)``. Jeugd and volwassenen reuse the same
    numbers but mean different genres, so the same name can have a different parent
    per audience — hence the parent lives on the per-book link, not the (name-keyed)
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
        "UPDATE book_genres SET parent_id = ("
        "  SELECT gp.parent_id FROM _gpa gp JOIN books b ON b.ppn = book_genres.book_ppn "
        "  WHERE gp.genre_id = book_genres.genre_id "
        "    AND gp.audience = lower(COALESCE(b.audience, '')))")
    cur.execute("DROP TABLE _gpa")
    conn.commit()
