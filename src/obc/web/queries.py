"""Read-only data-access layer for the web UI.

Every SQL statement that backs the search interface lives here, so the FastAPI
routes in :mod:`obc.web.app` stay thin (HTTP parsing + presentation only).

Conventions
-----------
* Each function takes an open ``sqlite3.Connection`` and never mutates the
  catalog (the web app opens connections in ``mode=ro``).
* Functions return ``sqlite3.Row`` objects or plain Python containers — never
  rendered HTML. Cover-image sizing, URL building and templating stay in the
  route layer.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from ..textnorm import fold


# --------------------------------------------------------------------------- #
# connection
# --------------------------------------------------------------------------- #
def connect_ro(db_path: str | Path) -> sqlite3.Connection:
    """Open a read-only connection with a ``fold()`` SQL function for
    diacritic/case-insensitive ``LIKE`` matching (Klöpping ~ klopping)."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.create_function("fold", 1, lambda s: fold(s) if s else "", deterministic=True)
    return conn


# --------------------------------------------------------------------------- #
# small query helpers
# --------------------------------------------------------------------------- #
def parse_year(value: str) -> int | None:
    """Lenient year parse: '' or junk -> None (avoids 422 on empty params)."""
    value = (value or "").strip()
    return int(value) if value.lstrip("-").isdigit() else None


def fts_match(q: str) -> str:
    """Turn free text into a safe FTS5 MATCH expression (prefix, AND-ed)."""
    terms = re.findall(r"\w+", q, flags=re.UNICODE)
    return " ".join(f'"{t}"*' for t in terms)


def _in(col: str, values: list[str] | tuple[str, ...]) -> tuple[str, list]:
    marks = ",".join("?" * len(values))
    return f"{col} IN ({marks})", list(values)


# Sort keys -> ORDER BY fragment. ``relevance`` is only meaningful with a query
# (then it becomes a bm25 expression); otherwise it falls back to title order.
SORTS = {
    "relevance": None,
    "added": "b.added_rank IS NULL, b.added_rank ASC",
    "year_desc": "b.year DESC",
    "year_asc": "b.year ASC",
    "title": "b.title COLLATE NOCASE ASC",
}
LIST_SORTS = {
    "name": "l.name COLLATE NOCASE ASC",
    "available": "available DESC, l.name COLLATE NOCASE",
    "total": "total DESC, l.name COLLATE NOCASE",
    "pct": "(CASE WHEN total > 0 THEN available * 1.0 / total ELSE 0 END) DESC, l.name COLLATE NOCASE",
}


# --------------------------------------------------------------------------- #
# search
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SearchFilters:
    """A fully-resolved search request (params already cleaned by the route)."""
    q: str = ""
    format: str = ""
    languages: tuple[str, ...] = ()
    genres: tuple[str, ...] = ()
    publishers: tuple[str, ...] = ()
    authors: tuple[str, ...] = ()
    lists: tuple[str, ...] = ()
    ereader: bool = False
    year_from: int | None = None
    year_to: int | None = None
    sort: str = "relevance"


@dataclass
class SearchResult:
    rows: list[sqlite3.Row]
    total: int


def _build_where(f: SearchFilters) -> tuple[list[str], list]:
    """Translate filters into WHERE clauses + bound parameters."""
    where: list[str] = []
    params: list = []
    if f.format:
        where.append("b.format = ?")
        params.append(f.format)
    if f.languages:
        clause, vals = _in("b.language", f.languages)
        where.append(clause)
        params += vals
    if f.publishers:
        clause, vals = _in("b.publisher", f.publishers)
        where.append(clause)
        params += vals
    if f.authors:
        clause, vals = _in("a.name", f.authors)
        where.append("b.ppn IN (SELECT ba.book_ppn FROM book_authors ba "
                     f"JOIN authors a ON a.id = ba.author_id WHERE {clause})")
        params += vals
    if f.lists:
        clause, vals = _in("l.slug", f.lists)
        where.append("b.ppn IN (SELECT bl.book_ppn FROM book_lists bl "
                     f"JOIN lists l ON l.id = bl.list_id WHERE {clause})")
        params += vals
    if f.ereader:
        where.append("b.ereader = 1")
    if f.year_from is not None:
        where.append("b.year >= ?")
        params.append(f.year_from)
    if f.year_to is not None:
        where.append("b.year <= ?")
        params.append(f.year_to)
    if f.genres:
        clause, vals = _in("g.name", f.genres)
        where.append("b.ppn IN (SELECT bg.book_ppn FROM book_genres bg "
                     f"JOIN genres g ON g.id = bg.genre_id WHERE {clause})")
        params += vals
    return where, params


def search(conn: sqlite3.Connection, f: SearchFilters, page: int,
           page_size: int) -> SearchResult:
    """Run a filtered + ranked search and return one page of rows plus the
    total match count. FTS5 ``bm25`` ranking is weighted toward title/author."""
    where, params = _build_where(f)

    joins = ""
    order = SORTS.get(f.sort) or "b.title COLLATE NOCASE ASC"
    match = fts_match(f.q) if f.q else ""
    if match:
        joins = "JOIN books_fts ft ON ft.ppn = b.ppn"
        where.append("books_fts MATCH ?")
        params.append(match)
        if f.sort == "relevance":
            order = "bm25(books_fts, 10.0, 6.0, 2.0, 1.0)"

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute(
        f"SELECT COUNT(*) FROM books b {joins} {where_sql}", params).fetchone()[0]
    offset = (page - 1) * page_size
    rows = conn.execute(
        f"SELECT b.* FROM books b {joins} {where_sql} "
        f"ORDER BY {order} LIMIT ? OFFSET ?",
        [*params, page_size, offset]).fetchall()
    return SearchResult(rows=rows, total=total)


def total_books(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]


# --------------------------------------------------------------------------- #
# per-result enrichment (formats of the same work; curated lists)
# --------------------------------------------------------------------------- #
def formats_map(conn: sqlite3.Connection, rows) -> dict[str, list[str]]:
    """Map ppn -> sorted list of formats the *work* exists in (a title may have
    both an e-book and an audiobook edition under different PPNs)."""
    titles = list({r["title"] for r in rows if r["title"]})
    by_work: dict[tuple, set] = {}
    if titles:
        qmarks = ",".join("?" * len(titles))
        for r in conn.execute(
                f"SELECT title, author, format FROM books WHERE title IN ({qmarks})",
                titles):
            key = ((r["title"] or "").lower(), (r["author"] or "").lower())
            by_work.setdefault(key, set()).add(r["format"])
    out = {}
    for r in rows:
        key = ((r["title"] or "").lower(), (r["author"] or "").lower())
        out[r["ppn"]] = sorted(f for f in by_work.get(key, {r["format"]}) if f)
    return out


def lists_map(conn: sqlite3.Connection, rows) -> dict[str, list[dict]]:
    """ppn -> list of {name, slug, position, year} for the books on this page."""
    ppns = [r["ppn"] for r in rows]
    out: dict[str, list] = {}
    if ppns:
        qmarks = ",".join("?" * len(ppns))
        for r in conn.execute(
                f"SELECT bl.book_ppn, l.name, l.slug, bl.position, bl.year, bl.won "
                f"FROM book_lists bl JOIN lists l ON l.id = bl.list_id "
                f"WHERE bl.book_ppn IN ({qmarks}) ORDER BY bl.position", ppns):
            out.setdefault(r["book_ppn"], []).append(
                {"name": r["name"], "slug": r["slug"], "position": r["position"],
                 "year": r["year"], "won": r["won"]})
    return out


# --------------------------------------------------------------------------- #
# facets (values are identical for every request; the route caches them)
# --------------------------------------------------------------------------- #
def compute_facets(conn: sqlite3.Connection) -> dict:
    formats = [r["format"] for r in conn.execute(
        "SELECT DISTINCT format FROM books WHERE format IS NOT NULL ORDER BY format")]
    languages = [r["language"] for r in conn.execute(
        "SELECT language FROM books WHERE language IS NOT NULL AND length(language) <= 24 "
        "AND language NOT IN ('Fictie','Non-fictie','Nonfictie') "
        "GROUP BY language ORDER BY COUNT(*) DESC LIMIT 25")]
    genres = [r["name"] for r in conn.execute(
        "SELECT g.name FROM genres g JOIN book_genres bg ON bg.genre_id = g.id "
        "GROUP BY g.id ORDER BY COUNT(*) DESC LIMIT 40")]
    publishers = [r["publisher"] for r in conn.execute(
        "SELECT publisher FROM books WHERE publisher IS NOT NULL AND publisher <> '' "
        "GROUP BY publisher ORDER BY COUNT(*) DESC LIMIT 80")]
    authors = [r["name"] for r in conn.execute(
        "SELECT a.name FROM authors a JOIN book_authors ba ON ba.author_id = a.id "
        "GROUP BY a.id ORDER BY COUNT(*) DESC LIMIT 120")]
    lists = [{"slug": r["slug"], "name": r["name"]} for r in conn.execute(
        "SELECT slug, name FROM lists ORDER BY name")]
    return {"formats": formats, "languages": languages, "genres": genres,
            "publishers": publishers, "authors": authors, "lists": lists}


# --------------------------------------------------------------------------- #
# autocomplete + searchable facets
# --------------------------------------------------------------------------- #
def suggest(conn: sqlite3.Connection, q: str, limit: int = 7) -> dict | None:
    """Autocomplete data for the search bar. Returns ``None`` for an empty query.
    ``title_rows`` are raw rows; the route applies cover sizing + shaping."""
    terms = re.findall(r"\w+", q, flags=re.UNICODE)
    if not terms:
        return None
    # Unscoped (not title-only) so a match in subjects/keywords/summary/author also
    # surfaces a book here — e.g. a search term that's only in "Trefwoorden" used to
    # show nothing in the live dropdown even though the full search page found it.
    # Same bm25 weights as the main search, so title hits still rank first.
    title_rows = conn.execute(
        "SELECT b.ppn, b.title, b.author, b.cover_url, b.format "
        "FROM books_fts ft JOIN books b ON b.ppn = ft.ppn "
        "WHERE books_fts MATCH ? ORDER BY bm25(books_fts, 10.0, 6.0, 2.0, 1.0) LIMIT ?",
        (fts_match(q), limit)).fetchall()
    like = f"%{fold(q)}%"
    authors = [r["name"] for r in conn.execute(
        "SELECT a.name, COUNT(*) n FROM authors a JOIN book_authors ba "
        "ON ba.author_id = a.id WHERE a.name_fold LIKE ? GROUP BY a.id "
        "ORDER BY n DESC LIMIT 5", (like,))]
    publishers = [r["name"] for r in conn.execute(
        "SELECT name FROM publishers WHERE name_fold LIKE ? "
        "ORDER BY n DESC LIMIT 4", (like,))]
    genres = [r["name"] for r in conn.execute(
        "SELECT g.name, COUNT(*) n FROM genres g JOIN book_genres bg "
        "ON bg.genre_id = g.id WHERE fold(g.name) LIKE ? GROUP BY g.id "
        "ORDER BY n DESC LIMIT 4", (like,))]
    lists = [{"slug": r["slug"], "name": r["name"]} for r in conn.execute(
        "SELECT slug, name FROM lists WHERE fold(name) LIKE ? ORDER BY name LIMIT 4",
        (like,))]
    languages = [r["name"] for r in conn.execute(
        "SELECT name FROM languages WHERE name_fold LIKE ? ORDER BY n DESC LIMIT 3",
        (like,))]
    return {"title_rows": title_rows, "authors": authors, "publishers": publishers,
            "genres": genres, "languages": languages, "lists": lists}


def facet_values(conn: sqlite3.Connection, kind: str, q: str = "",
                 limit: int = 30) -> list[str]:
    """Searchable facet values (for large facets like author/publisher)."""
    qq = q.strip()
    like = f"%{fold(qq)}%"
    if kind == "author":
        base = ("SELECT a.name v, COUNT(*) n FROM authors a "
                "JOIN book_authors ba ON ba.author_id = a.id ")
        rows = (conn.execute(base + "WHERE a.name_fold LIKE ? GROUP BY a.id "
                             "ORDER BY n DESC LIMIT ?", (like, limit)) if qq
                else conn.execute(base + "GROUP BY a.id ORDER BY n DESC LIMIT ?", (limit,)))
    elif kind == "publisher":
        rows = (conn.execute("SELECT name v, n FROM publishers WHERE name_fold LIKE ? "
                             "ORDER BY n DESC LIMIT ?", (like, limit)) if qq
                else conn.execute("SELECT name v, n FROM publishers ORDER BY n DESC LIMIT ?",
                                  (limit,)))
    else:
        return []
    return [r["v"] for r in rows]


# --------------------------------------------------------------------------- #
# detail pages
# --------------------------------------------------------------------------- #
def book_detail(conn: sqlite3.Connection, ppn: str) -> dict | None:
    """Everything the book page needs, or ``None`` if the PPN is unknown."""
    row = conn.execute("SELECT * FROM books WHERE ppn = ?", (ppn,)).fetchone()
    if row is None:
        return None
    # genres with their parent (resolved per this book's audience). Tolerate a catalog
    # DB built before the book_genres.parent_id column — i.e. the window after a
    # schema-changing deploy but before the next rebuild — by falling back to flat.
    try:
        genres = [{"name": r["name"], "parent": r["parent_name"]} for r in conn.execute(
            "SELECT g.name, p.name AS parent_name "
            "FROM book_genres bg JOIN genres g ON g.id = bg.genre_id "
            "LEFT JOIN genres p ON p.id = bg.parent_id "
            "WHERE bg.book_ppn = ? ORDER BY COALESCE(p.name, g.name), g.name", (ppn,))]
    except sqlite3.OperationalError:  # DB built before the book_genres.parent_id column
        genres = [{"name": r["name"], "parent": None} for r in conn.execute(
            "SELECT g.name FROM genres g JOIN book_genres bg ON bg.genre_id = g.id "
            "WHERE bg.book_ppn = ? ORDER BY g.name", (ppn,))]
    # Drop a top-level genre's own chip when a "parent › child" chip already shows it —
    # e.g. skip standalone "Literatuur & Romans" when "Literatuur & Romans › Sociale
    # romans" is also on this book; that chip already conveys the top-level genre.
    shown_as_parent = {g["parent"] for g in genres if g["parent"]}
    genres = [g for g in genres if not (g["parent"] is None and g["name"] in shown_as_parent)]
    # other editions of the same work (e.g. the audiobook of this e-book)
    editions = {row["format"]: ppn}
    for r in conn.execute(
            "SELECT ppn, format FROM books WHERE lower(title)=lower(?) "
            "AND lower(COALESCE(author,''))=lower(COALESCE(?,'')) AND format IS NOT NULL",
            (row["title"], row["author"])):
        editions.setdefault(r["format"], r["ppn"])
    authors = [r["name"] for r in conn.execute(
        "SELECT a.name FROM authors a JOIN book_authors ba ON ba.author_id = a.id "
        "WHERE ba.book_ppn = ? ORDER BY ba.position", (ppn,))]
    book_lists = [{"name": r["name"], "slug": r["slug"], "position": r["position"],
                   "year": r["year"], "won": r["won"]} for r in conn.execute(
        "SELECT l.name, l.slug, bl.position, bl.year, bl.won FROM book_lists bl "
        "JOIN lists l ON l.id = bl.list_id WHERE bl.book_ppn = ? ORDER BY bl.position",
        (ppn,))]
    return {"row": row, "genres": genres, "editions": editions,
            "authors": authors, "book_lists": book_lists}


def author_books(conn: sqlite3.Connection, name: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT b.* FROM books b JOIN book_authors ba ON ba.book_ppn = b.ppn "
        "JOIN authors a ON a.id = ba.author_id WHERE a.name = ? "
        "ORDER BY b.year DESC, b.title COLLATE NOCASE LIMIT 300", (name,)).fetchall()


def series_books(conn: sqlite3.Connection, name: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT b.* FROM books b WHERE b.series = ? "
        "ORDER BY b.series_no, b.year LIMIT 300", (name,)).fetchall()


# --------------------------------------------------------------------------- #
# curated lists
# --------------------------------------------------------------------------- #
def lists_overview(conn: sqlite3.Connection, sort: str) -> list[sqlite3.Row]:
    order = LIST_SORTS.get(sort, LIST_SORTS["name"])
    return conn.execute(
        "SELECT l.slug, l.name, l.description, l.url, l.updated_at, "
        "COUNT(li.rowid) AS total, "
        "SUM(CASE WHEN li.ppn IS NOT NULL THEN 1 ELSE 0 END) AS available "
        "FROM lists l LEFT JOIN list_items li ON li.list_id = l.id "
        "GROUP BY l.id ORDER BY " + order).fetchall()


def list_row(conn: sqlite3.Connection, slug: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM lists WHERE slug = ?", (slug,)).fetchone()


def list_items(conn: sqlite3.Connection, list_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT li.position, li.year, li.title, li.author, li.cover_url, li.ppn, li.won, "
        "b.cover_url AS bcover, b.format AS bformat "
        "FROM list_items li LEFT JOIN books b ON b.ppn = li.ppn "
        "WHERE li.list_id = ? ORDER BY li.position", (list_id,)).fetchall()


# --------------------------------------------------------------------------- #
# stats dashboard
# --------------------------------------------------------------------------- #
def web_stats(conn: sqlite3.Connection) -> dict:
    def one(q: str, *a):
        return conn.execute(q, a).fetchone()[0]

    def many(q: str, *a):
        return conn.execute(q, a).fetchall()

    return {
        "total": one("SELECT COUNT(*) FROM books"),
        "ebooks": one("SELECT COUNT(*) FROM books WHERE format='ebook'"),
        "audiobooks": one("SELECT COUNT(*) FROM books WHERE format='audiobook'"),
        "ereader": one("SELECT COUNT(*) FROM books WHERE ereader=1"),
        "authors": one("SELECT COUNT(*) FROM authors"),
        "publishers": one("SELECT COUNT(*) FROM publishers"),
        "lists": one("SELECT COUNT(*) FROM lists"),
        "languages": many("SELECT name, n FROM languages ORDER BY n DESC LIMIT 8"),
        # top-level genres (parent_id IS NULL for that link) and sub-genres carry
        # their parent's name, so the stats page can show "Parent › Kind" like the
        # book page does. A genre used both ways (rare cross-audience overlap) gets
        # its own row per role, so counts stay honest.
        "genres": many(
            "SELECT g.name, p.name AS parent, COUNT(*) n "
            "FROM book_genres bg JOIN genres g ON g.id = bg.genre_id "
            "LEFT JOIN genres p ON p.id = bg.parent_id "
            "GROUP BY g.id, p.id ORDER BY n DESC LIMIT 12"),
        "years": many("SELECT year, COUNT(*) n FROM books WHERE year >= 2000 "
                      "GROUP BY year ORDER BY year"),
        "top_authors": many("SELECT a.name, COUNT(*) n FROM authors a "
                            "JOIN book_authors ba ON ba.author_id=a.id GROUP BY a.id "
                            "ORDER BY n DESC LIMIT 12"),
        "top_publishers": many("SELECT name, n FROM publishers ORDER BY n DESC LIMIT 12"),
    }
