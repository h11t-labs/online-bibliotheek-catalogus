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
from pathlib import Path
from typing import Any, Iterable

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
    raw_json          TEXT,
    scraped_at        TEXT
);

CREATE TABLE IF NOT EXISTS genres (
    id   INTEGER PRIMARY KEY,
    name TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS book_genres (
    book_ppn TEXT NOT NULL REFERENCES books(ppn) ON DELETE CASCADE,
    genre_id INTEGER NOT NULL REFERENCES genres(id) ON DELETE CASCADE,
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
    ppn       TEXT
);

CREATE INDEX IF NOT EXISTS idx_books_format   ON books(format);
CREATE INDEX IF NOT EXISTS idx_books_language ON books(language);
CREATE INDEX IF NOT EXISTS idx_books_year     ON books(year);
CREATE INDEX IF NOT EXISTS idx_books_ereader  ON books(ereader);
CREATE INDEX IF NOT EXISTS idx_books_title     ON books(title);
CREATE INDEX IF NOT EXISTS idx_books_added     ON books(added_rank);
CREATE INDEX IF NOT EXISTS idx_books_series     ON books(series);
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
    "added_rank", "series", "series_no", "raw_json", "scraped_at",
]


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
        (ppn, rec.get("title") or "", rec.get("author") or "",
         " ".join(subjects), rec.get("summary") or ""),
    )


def upsert_many(conn: sqlite3.Connection, recs: Iterable[dict[str, Any]]) -> int:
    n = 0
    for rec in recs:
        upsert_book(conn, rec)
        n += 1
    conn.commit()
    return n


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
    # drop + recreate for a clean rebuild (also migrates any schema changes)
    for t in ("book_genres", "genres", "book_authors", "authors", "publishers",
              "languages", "book_lists", "list_items", "lists", "books", "books_fts"):
        cur.execute(f"DROP TABLE IF EXISTS {t}")
    cur.executescript(_SCHEMA)

    # books
    book_rows = []
    for r in records:
        rj = r.get("raw_json")
        if rj is None:
            rj = json.dumps(r, ensure_ascii=False)
        book_rows.append(tuple(
            rj if c == "raw_json" else r.get(c) for c in _BOOK_COLS))
    placeholders = ", ".join("?" for _ in _BOOK_COLS)
    cur.executemany(
        f"INSERT OR REPLACE INTO books ({', '.join(_BOOK_COLS)}) "
        f"VALUES ({placeholders})", book_rows)

    # genres (distinct) + book_genres
    pairs = [(r["ppn"], s) for r in records for s in dict.fromkeys(r.get("subjects") or []) if s]
    distinct = sorted({s for _, s in pairs})
    cur.executemany("INSERT OR IGNORE INTO genres(name) VALUES (?)",
                    [(n,) for n in distinct])
    gid = {row["name"]: row["id"] for row in cur.execute("SELECT id, name FROM genres")}
    cur.executemany("INSERT OR IGNORE INTO book_genres(book_ppn, genre_id) VALUES (?, ?)",
                    [(ppn, gid[s]) for ppn, s in pairs if s in gid])

    # authors (split multi-author) + book_authors
    apairs = []  # (ppn, author_name, position)
    for r in records:
        names = r.get("authors") or ([r["author"]] if r.get("author") else [])
        for pos, name in enumerate(dict.fromkeys(n for n in names if n)):
            apairs.append((r["ppn"], name, pos))
    a_distinct = sorted({name for _, name, _ in apairs})
    cur.executemany("INSERT OR IGNORE INTO authors(name, name_fold) VALUES (?, ?)",
                    [(n, fold(n)) for n in a_distinct])
    aid = {row["name"]: row["id"] for row in cur.execute("SELECT id, name FROM authors")}
    cur.executemany(
        "INSERT OR IGNORE INTO book_authors(book_ppn, author_id, position) VALUES (?, ?, ?)",
        [(ppn, aid[name], pos) for ppn, name, pos in apairs if name in aid])

    # distinct publishers (+folded) for fast autocomplete
    pub_counts: dict[str, int] = {}
    for r in records:
        p = r.get("publisher")
        if p:
            pub_counts[p] = pub_counts.get(p, 0) + 1
    cur.executemany("INSERT INTO publishers(name, name_fold, n) VALUES (?, ?, ?)",
                    [(p, fold(p), c) for p, c in pub_counts.items()])
    lang_counts: dict[str, int] = {}
    for r in records:
        lg = r.get("language")
        if lg:
            lang_counts[lg] = lang_counts.get(lg, 0) + 1
    cur.executemany("INSERT INTO languages(name, name_fold, n) VALUES (?, ?, ?)",
                    [(lg, fold(lg), c) for lg, c in lang_counts.items()])

    # curated lists: lists + full list_items (incl. unmatched) + book_lists (matched)
    for lst in (lists or []):
        cur.execute(
            "INSERT INTO lists(slug, name, url, description, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (lst["slug"], lst.get("name"), lst.get("url"),
             lst.get("description"), lst.get("updated_at")))
        list_id = cur.lastrowid
        items = lst.get("items", [])
        cur.executemany(
            "INSERT INTO list_items(list_id, position, year, title, author, isbn, cover_url, ppn) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(list_id, it.get("position"), it.get("year"), it.get("title"),
              it.get("author"), it.get("isbn"), it.get("cover_url"), it.get("ppn"))
             for it in items])
        cur.executemany(
            "INSERT OR IGNORE INTO book_lists(book_ppn, list_id, position, year) "
            "VALUES (?, ?, ?, ?)",
            [(it["ppn"], list_id, it.get("position"), it.get("year"))
             for it in items if it.get("ppn")])

    # FTS
    cur.executemany(
        "INSERT INTO books_fts(ppn, title, author, subjects, summary) VALUES (?, ?, ?, ?, ?)",
        [(r["ppn"], r.get("title") or "", r.get("author") or "",
          " ".join(r.get("subjects") or []), r.get("summary") or "")
         for r in records if r.get("ppn")])

    conn.commit()
    return len(book_rows)


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    g = lambda q, *a: conn.execute(q, a).fetchone()[0]
    return {
        "books": g("SELECT COUNT(*) FROM books"),
        "ebooks": g("SELECT COUNT(*) FROM books WHERE format='ebook'"),
        "audiobooks": g("SELECT COUNT(*) FROM books WHERE format='audiobook'"),
        "genres": g("SELECT COUNT(*) FROM genres"),
        "languages": g("SELECT COUNT(DISTINCT language) FROM books"),
    }
