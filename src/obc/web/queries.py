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
* Reads go to ``works`` — one row per book — for every work-level fact, and to
  ``editions`` only for what describes the file you borrow (format, pages,
  duration, narrator, ISBN, borrow link). Nothing here re-derives "these two
  rows are the same book": :mod:`obc.work` decided that at build time.
* Nothing here counts, groups or sorts what the build already stamped
  (``n_works``, ``surname_sort``, ``series_slug``, ``genre_pages``): the read
  path is indexed lookups only.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from ..textnorm import fold, slugify

# Genre slugs are computed inside SQL (see the genre statements below), which calls
# this once per scanned row — hundreds of thousands of times over a few hundred
# distinct names. Memoised, all but the first few hundred calls are a dict lookup.
_slug = lru_cache(maxsize=4096)(slugify)


# --------------------------------------------------------------------------- #
# connection
# --------------------------------------------------------------------------- #
def connect_ro(db_path: str | Path) -> sqlite3.Connection:
    """Open a read-only connection with ``fold()`` and ``slug()`` SQL functions.

    ``fold()`` gives diacritic/case-insensitive ``LIKE`` matching (Klöpping ~
    klopping); ``slug()`` is the same slug the URLs use, so grouping by the thing
    a page is addressed by can happen in SQL instead of in Python.

    ``check_same_thread=False``: FastAPI can run a ``yield`` dependency's setup and
    the route handler on different threadpool threads, so a connection opened in
    ``get_conn`` and used in the handler would otherwise raise a
    ``sqlite3.ProgrammingError`` intermittently (only when the two land on
    different threads, i.e. under load). The connection is per-request and only
    ever touched sequentially — never by two threads at once — so disabling the
    same-thread guard is safe."""
    conn = sqlite3.connect(
        f"file:{db_path}?mode=ro", uri=True, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.create_function("fold", 1, lambda s: fold(s) if s else "", deterministic=True)
    conn.create_function("slug", 1, lambda s: _slug(s) if s else "", deterministic=True)
    # Read the database through the OS page cache instead of copying pages into
    # this connection's own. Connections are per request, so a private cache is
    # thrown away as soon as it is warm; a mapping is shared by every request and
    # every thread, and costs evictable file-backed pages rather than heap.
    conn.execute("PRAGMA mmap_size = 268435456")
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


def _limit(value: int, default: int, maximum: int) -> int:
    """Clamp caller-supplied LIMIT values; SQLite treats negative LIMIT as unlimited."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, maximum))


def _in(col: str, values: list[str] | tuple[str, ...]) -> tuple[str, list]:
    marks = ",".join("?" * len(values))
    return f"{col} IN ({marks})", list(values)


# Sort keys -> ORDER BY fragment. ``relevance`` is only meaningful with a query
# (then it becomes a bm25 expression); otherwise it falls back to title order.
SORTS = {
    "relevance": None,
    "added": "w.added_rank IS NULL, w.added_rank ASC",
    "year_desc": "w.year DESC",
    "year_asc": "w.year ASC",
    "title": "w.title COLLATE NOCASE ASC",
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
        # "available as", not "this row is": a work-level flag is what makes the
        # format facet mean the same thing as the format landing pages, and it is
        # the whole reason the collapse-unless-format-filter branch is gone.
        where.append("w.has_audiobook = 1" if f.format == "audiobook"
                     else "w.has_ebook = 1")
    if f.languages:
        clause, vals = _in("w.language", f.languages)
        where.append(clause)
        params += vals
    if f.publishers:
        clause, vals = _in("w.publisher", f.publishers)
        where.append(clause)
        params += vals
    if f.authors:
        # Match on the fold, not the display name: only the canonical spelling
        # survives as authors.name now, so an ?author= URL carrying a variant
        # ("Bob De Wit") has to keep working. Folded here rather than with the
        # SQL fold() function, which only exists on connect_ro connections.
        clause, vals = _in("a.name_fold", [fold(v) for v in f.authors])
        where.append("w.work_id IN (SELECT wa.work_id FROM work_authors wa "
                     f"JOIN authors a ON a.id = wa.author_id WHERE {clause})")
        params += vals
    if f.lists:
        clause, vals = _in("l.slug", f.lists)
        where.append("w.work_id IN (SELECT wl.work_id FROM work_lists wl "
                     f"JOIN lists l ON l.id = wl.list_id WHERE {clause})")
        params += vals
    if f.ereader:
        where.append("w.ereader = 1")
    if f.year_from is not None:
        where.append("w.year >= ?")
        params.append(f.year_from)
    if f.year_to is not None:
        where.append("w.year <= ?")
        params.append(f.year_to)
    if f.genres:
        clause, vals = _in("g.name", f.genres)
        where.append("w.work_id IN (SELECT wg.work_id FROM work_genres wg "
                     f"JOIN genres g ON g.id = wg.genre_id WHERE {clause})")
        params += vals
    return where, params


def search(conn: sqlite3.Connection, f: SearchFilters, page: int,
           page_size: int) -> SearchResult:
    """Run a filtered + ranked search and return one page of rows plus the
    total match count. FTS5 ``bm25`` ranking is weighted toward title/author.

    One row per book by construction — ``works`` *is* the grain the reader is
    searching — so there is no collapse to apply, and none to skip when a format
    filter is set. That exception is what made ``?format=audiobook`` count
    editions as titles and show one work four times.
    """
    where, params = _build_where(f)

    joins = ""
    order = SORTS.get(f.sort) or "w.title COLLATE NOCASE ASC"
    match = fts_match(f.q) if f.q else ""
    if match:
        joins = "JOIN works_fts ft ON ft.work_id = w.work_id"
        where.append("works_fts MATCH ?")
        params.append(match)
        if f.sort == "relevance":
            # first weight = the UNINDEXED work_id column (bm25 weights are
            # positional over ALL declared columns): work_id, title, author,
            # subjects, summary.
            order = "bm25(works_fts, 0.0, 10.0, 6.0, 2.0, 1.0)"

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    if match and len(where) == 1:
        # A bare full-text search: FTS5 can count its own matches, so the join to
        # `works` — 51,980 row lookups for "de", none of which the count uses —
        # is skipped. Same number, and it never touches the 593MB table: 231ms to
        # 37ms here, and far more than that on a machine whose page cache holds a
        # fifth of the catalog. Any other filter needs the join to apply it.
        total = conn.execute(
            "SELECT COUNT(*) FROM works_fts WHERE works_fts MATCH ?",
            [match]).fetchone()[0]
    else:
        total = conn.execute(
            f"SELECT COUNT(*) FROM works w {joins} {where_sql}", params).fetchone()[0]
    offset = (page - 1) * page_size
    rows = conn.execute(
        f"SELECT w.* FROM works w {joins} {where_sql} "
        f"ORDER BY {order} LIMIT ? OFFSET ?",
        [*params, page_size, offset]).fetchall()
    return SearchResult(rows=rows, total=total)


def total_works(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM works").fetchone()[0]


# --------------------------------------------------------------------------- #
# per-result enrichment (curated lists)
# --------------------------------------------------------------------------- #
def lists_map(conn: sqlite3.Connection, rows) -> dict[str, list[dict]]:
    """work_id -> list of {name, slug, position, year} for the books on this page."""
    ids = [r["work_id"] for r in rows]
    out: dict[str, list] = {}
    if ids:
        qmarks = ",".join("?" * len(ids))
        for r in conn.execute(
                f"SELECT wl.work_id, l.name, l.slug, wl.position, wl.year, wl.won "
                f"FROM work_lists wl JOIN lists l ON l.id = wl.list_id "
                f"WHERE wl.work_id IN ({qmarks}) ORDER BY wl.position", ids):
            out.setdefault(r["work_id"], []).append(
                {"name": r["name"], "slug": r["slug"], "position": r["position"],
                 "year": r["year"], "won": r["won"]})
    return out


# --------------------------------------------------------------------------- #
# facets
# --------------------------------------------------------------------------- #
def compute_facets(conn: sqlite3.Connection) -> dict:
    """The filter panel's values — all of them LIMITed index reads.

    The genre and author lists used to be a GROUP BY over the link tables per
    request, which is why the route cached them behind a lock; the counts are
    stamped at build time now, so every list below is cheap enough to just read.
    """
    formats = [fmt for fmt, flag in (("audiobook", "has_audiobook"),
                                     ("ebook", "has_ebook"))
               if conn.execute(
                   f"SELECT EXISTS(SELECT 1 FROM works WHERE {flag} = 1)").fetchone()[0]]
    languages = [r["language"] for r in conn.execute(
        "SELECT language FROM works WHERE language IS NOT NULL AND length(language) <= 24 "
        "AND language NOT IN ('Fictie','Non-fictie','Nonfictie') "
        "GROUP BY language ORDER BY COUNT(*) DESC LIMIT 25")]
    genres = [r["name"] for r in conn.execute(
        "SELECT name FROM genres WHERE n_works > 0 ORDER BY n_works DESC LIMIT 40")]
    publishers = [r["name"] for r in conn.execute(
        "SELECT name FROM publishers ORDER BY n DESC LIMIT 80")]
    authors = [r["name"] for r in conn.execute(
        "SELECT name FROM authors WHERE n_works > 0 ORDER BY n_works DESC LIMIT 120")]
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
    limit = _limit(limit, 7, 20)
    # Unscoped (not title-only) so a match in subjects/keywords/summary/author also
    # surfaces a book here — e.g. a search term that's only in "Trefwoorden" used to
    # show nothing in the live dropdown even though the full search page found it.
    # Same bm25 weights as the main search, so title hits still rank first. One row
    # per work by construction, so no collapse filter (a title cannot show up twice).
    # first weight = the UNINDEXED work_id column (work_id, title, author, subjects,
    # summary).
    title_rows = conn.execute(
        "SELECT w.work_id, w.title, w.author, w.slug, w.cover_url, "
        "       w.ebook_ppn, w.audiobook_ppn, "
        "       CASE WHEN w.has_ebook THEN 'ebook' ELSE 'audiobook' END AS format "
        "FROM works_fts ft JOIN works w ON w.work_id = ft.work_id "
        "WHERE works_fts MATCH ? "
        "ORDER BY bm25(works_fts, 0.0, 10.0, 6.0, 2.0, 1.0) LIMIT ?",
        (fts_match(q), limit)).fetchall()
    like = f"%{fold(q)}%"
    authors = [r["name"] for r in conn.execute(
        "SELECT name FROM authors WHERE name_fold LIKE ? "
        "ORDER BY n_works DESC LIMIT 5", (like,))]
    publishers = [r["name"] for r in conn.execute(
        "SELECT name FROM publishers WHERE name_fold LIKE ? "
        "ORDER BY n DESC LIMIT 4", (like,))]
    genres = [r["name"] for r in conn.execute(
        "SELECT name FROM genres WHERE fold(name) LIKE ? "
        "ORDER BY n_works DESC LIMIT 4", (like,))]
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
    limit = _limit(limit, 30, 50)
    like = f"%{fold(qq)}%"
    if kind == "author":
        base = "SELECT name v FROM authors WHERE n_works > 0 "
        rows = (conn.execute(base + "AND name_fold LIKE ? "
                             "ORDER BY n_works DESC LIMIT ?", (like, limit)) if qq
                else conn.execute(base + "ORDER BY n_works DESC LIMIT ?", (limit,)))
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
    """Everything the book page needs.

    ``ppn`` may be any edition's PPN. For a non-representative edition returns
    {"redirect": work_id} so the route can 301 — old audiobook URLs keep working.
    Otherwise: {"work": row, "editions": [edition rows, e-book first then ppn],
    "genres": [...], "authors": [...], "work_lists": [...]}.
    None if the PPN is unknown at either grain.
    """
    work = conn.execute("SELECT * FROM works WHERE work_id = ?", (ppn,)).fetchone()
    if work is None:
        row = conn.execute("SELECT work_id FROM editions WHERE ppn = ?", (ppn,)).fetchone()
        return {"redirect": row["work_id"]} if row else None
    # genres with their parent (resolved per this work's audience). The slug is
    # what the chip links to: /genre/<slug> is the indexable page, while the
    # ?genre= variant it used to point at is noindex and robots-disallowed. Names
    # that fold to nothing have no page, so their chip renders unlinked.
    genres = [{"name": r["name"], "parent": r["parent_name"],
               "slug": _slug(r["name"])} for r in conn.execute(
        "SELECT g.name, p.name AS parent_name "
        "FROM work_genres wg JOIN genres g ON g.id = wg.genre_id "
        "LEFT JOIN genres p ON p.id = wg.parent_id "
        "WHERE wg.work_id = ? ORDER BY COALESCE(p.name, g.name), g.name", (ppn,))]
    # Drop a top-level genre's own chip when a "parent › child" chip already shows it —
    # e.g. skip standalone "Literatuur & Romans" when "Literatuur & Romans › Sociale
    # romans" is also on this book; that chip already conveys the top-level genre.
    shown_as_parent = {g["parent"] for g in genres if g["parent"]}
    genres = [g for g in genres if not (g["parent"] is None and g["name"] in shown_as_parent)]
    # the editions you can actually borrow, e-book first — one indexed lookup on
    # editions.work_id instead of a case-insensitive (title, author) re-derivation
    editions = conn.execute(
        "SELECT * FROM editions WHERE work_id = ? "
        "ORDER BY (CASE WHEN format = 'ebook' THEN 0 ELSE 1 END), ppn", (ppn,)).fetchall()
    authors = [r["name"] for r in conn.execute(
        "SELECT a.name FROM authors a JOIN work_authors wa ON wa.author_id = a.id "
        "WHERE wa.work_id = ? ORDER BY wa.position", (ppn,))]
    work_lists = [{"name": r["name"], "slug": r["slug"], "position": r["position"],
                   "year": r["year"], "won": r["won"]} for r in conn.execute(
        "SELECT l.name, l.slug, wl.position, wl.year, wl.won FROM work_lists wl "
        "JOIN lists l ON l.id = wl.list_id WHERE wl.work_id = ? ORDER BY wl.position",
        (ppn,))]
    return {"work": work, "editions": editions, "genres": genres,
            "authors": authors, "work_lists": work_lists}


def work_ref(conn: sqlite3.Connection, ppn: str) -> tuple[str, str] | None:
    """``(work_id, slug)`` for a work id *or* any of its editions' PPNs.

    All a redirect needs: ``/book/{ppn}`` is redirect-only now, and a canonical
    ``/boek/…`` URL carrying a stale slug has to find the current one without
    fetching the whole page's data.
    """
    row = conn.execute(
        "SELECT work_id, slug FROM works WHERE work_id = ?", (ppn,)).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT w.work_id, w.slug FROM editions e "
            "JOIN works w ON w.work_id = e.work_id WHERE e.ppn = ?", (ppn,)).fetchone()
    return (row["work_id"], row["slug"] or "") if row else None


def author_books(conn: sqlite3.Connection, name: str) -> list[sqlite3.Row]:
    """Books by the exact author name — the fallback for names that don't slug."""
    return conn.execute(
        "SELECT w.* FROM works w JOIN work_authors wa ON wa.work_id = w.work_id "
        "JOIN authors a ON a.id = wa.author_id WHERE a.name = ? "
        "ORDER BY w.year DESC, w.title COLLATE NOCASE LIMIT 300", (name,)).fetchall()


# A slug round-trips into a name_fold by swapping dashes for spaces, so the two
# functions below look up an indexed column rather than a computed one. The catalog
# holds the same person under several spellings ("Ad Van Schaik" / "Ad van Schaik",
# "Agnès" / "Agnes") — 359 of them — and ``authors`` is keyed by that fold, so one
# person is one row, one shelf and one page.
def author_display_name(conn: sqlite3.Connection, fold_key: str) -> str | None:
    """The spelling to show for a folded author key. ``None`` when no author folds
    to this key (which is also the author page's 404 signal).

    A single-row read: the vote between spellings happens at build time now.
    """
    row = conn.execute(
        "SELECT name FROM authors WHERE name_fold = ? LIMIT 1", (fold_key,)).fetchone()
    return row["name"] if row else None


def author_books_by_fold(conn: sqlite3.Connection, fold_key: str) -> list[sqlite3.Row]:
    """Books by the person whose name folds to ``fold_key``.

    A plain join: ``work_authors``' primary key already collapsed a work credited
    under two spellings into one link, so there is nothing left to GROUP BY.
    """
    return conn.execute(
        "SELECT w.* FROM works w JOIN work_authors wa ON wa.work_id = w.work_id "
        "JOIN authors a ON a.id = wa.author_id WHERE a.name_fold = ? "
        "ORDER BY w.year DESC, w.title COLLATE NOCASE LIMIT 300",
        (fold_key,)).fetchall()


def series_books(conn: sqlite3.Connection, slug: str) -> list[sqlite3.Row]:
    """The parts of a series, in order — one indexed lookup on ``works.series_slug``.

    Several spellings of one series ("De Stad" / "De stad") share a slug, and
    therefore a page; the slug lives on the work now, so the read side no longer
    needs the spellings map the web layer used to cache.
    """
    return conn.execute(
        "SELECT w.* FROM works w WHERE w.series_slug = ? "
        "ORDER BY w.series_no, w.year LIMIT 300", (slug,)).fetchall()


def series_row(conn: sqlite3.Connection, slug: str) -> sqlite3.Row | None:
    """The series' display spelling + part count, for the page heading."""
    return conn.execute("SELECT * FROM series WHERE slug = ?", (slug,)).fetchone()


def series_index(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every series page: slug, display spelling and part count, most parts first."""
    return conn.execute(
        "SELECT slug, name, titles FROM series "
        "ORDER BY titles DESC, name COLLATE NOCASE").fetchall()


# An author or series page only earns a place in the *sitemap* once it actually
# aggregates something: with a single title it is a weaker copy of that title's
# own page, and 13k of those would dilute the pages that do add value. It stays
# reachable and listed in the A-Z hub — this rule is about what gets nominated to
# search engines, not about what a reader can browse to.
MIN_INDEXABLE_TITLES = 2


def author_letter_counts(conn: sqlite3.Connection,
                         by_surname: bool = True) -> list[tuple[str, int]]:
    """``[(letter, n)]`` for the A-Z hub — the only thing that page renders.

    The hub shows 27 counts and no authors at all, so reading every author to
    length the buckets was 22,383 rows for 27 numbers. This groups inside the
    letter index instead.
    """
    col = "surname_letter" if by_surname else "first_letter"
    return [(r[0], r[1]) for r in conn.execute(
        f"SELECT {col}, COUNT(*) FROM authors "
        f"WHERE n_works > 0 AND first_sort <> '' AND {col} IS NOT NULL "
        f"GROUP BY {col}")]


def authors_in_letter(conn: sqlite3.Connection, letter: str,
                      by_surname: bool = True) -> list[sqlite3.Row]:
    """One letter's authors, in the order the page shows them.

    Proportional to the letter, which the bucket-the-world approach was not: the
    catch-all holds three people and used to cost the same as the 2,562 under B.
    """
    col, order = ("surname_letter", "surname_sort, first_sort") if by_surname \
        else ("first_letter", "first_sort")
    return conn.execute(
        f"SELECT name, n_works AS titles FROM authors "
        f"WHERE {col} = ? AND n_works > 0 AND first_sort <> '' ORDER BY {order}",
        (letter,)).fetchall()


def author_index(conn: sqlite3.Connection,
                 by_surname: bool = True) -> list[sqlite3.Row]:
    """Every person with both A-Z sort keys and their work count, in hub order.

    One row per person rather than per spelling, and every column already
    stamped: the hub's fold-merge loop and its per-request counting are gone.

    Ordered here rather than by the caller. ``surname_sort`` and ``first_sort``
    are exactly ``surname_key(name)`` and ``slugify(name)``, stamped at build time
    (see ``db._stamp_author_names``) — and the hub was calling both functions
    again, per row, to rebuild a key it already had. SQLite sorts the stamped
    columns instead.
    """
    order = "surname_sort, first_sort" if by_surname else "first_sort"
    return conn.execute(
        "SELECT name, name_fold, surname_sort, first_sort, n_works AS titles "
        f"FROM authors WHERE n_works > 0 ORDER BY {order}").fetchall()


# --------------------------------------------------------------------------- #
# publisher pages
# --------------------------------------------------------------------------- #
def publisher_page(conn: sqlite3.Connection, slug: str) -> dict | None:
    """One publisher page: display name, every spelling of it, and its title count.

    No new column and no new table: ``publishers`` already stores ``name_fold``
    under an index, and a slug round-trips into a fold by swapping dashes for
    spaces — the same lookup the author pages do.

    A publisher reaches the catalog under more than one spelling ("Ambo|Anthos
    Uitgevers, Amsterdam" and "Ambo|Anthos uitgevers, Amsterdam"; five variants of
    "De Crime Compagnie, Laren NH"), and those all fold together. The page filters
    on every one of them, or its heading would promise more titles than it shows.
    The display name is the spelling the catalog actually uses most — ties go to
    the longest, which is the fuller form rather than a truncation.
    """
    rows = conn.execute(
        "SELECT name, n FROM publishers WHERE name_fold = ?",
        (slug.replace("-", " "),)).fetchall()
    if not rows:
        return None
    best = max(rows, key=lambda r: (r["n"], len(r["name"])))
    return {"name": best["name"], "titles": sum(r["n"] for r in rows),
            "spellings": tuple(r["name"] for r in rows)}


def publisher_pages(conn: sqlite3.Connection) -> list[dict]:
    """Every publisher page, folded spellings already merged — for the sitemap.

    Grouped in SQL on the indexed ``name_fold`` so the read path stays a scan of
    one small table rather than 1.5k per-publisher lookups.
    """
    return [{"slug": r["name_fold"].replace(" ", "-"), "titles": r["titles"]}
            for r in conn.execute(
                "SELECT name_fold, SUM(n) AS titles FROM publishers "
                "WHERE name_fold != '' GROUP BY name_fold ORDER BY name_fold")]


# --------------------------------------------------------------------------- #
# genre pages (taxonomy built by obc.db.build_genre_taxonomy)
# --------------------------------------------------------------------------- #
def genre_page(conn: sqlite3.Connection, slug: str) -> sqlite3.Row | None:
    """One genre page: display name, work count, catalog-wide parent slug."""
    return conn.execute("SELECT * FROM genre_pages WHERE slug = ?", (slug,)).fetchone()


def genre_spellings(conn: sqlite3.Connection, slug: str) -> list[str]:
    """Every genre spelling that shares this page's slug.

    The catalog holds "Biografieën" twice — precomposed and with a combining
    diaeresis — and both fold to ``biografieen``, so the page has to filter on all
    of them or it would show fewer titles than its own heading claims. A slug
    round-trips into a fold by swapping dashes for spaces (that is how
    :func:`obc.textnorm.slugify` is built), so this is a lookup, not a re-derivation.
    """
    return [r["name"] for r in conn.execute(
        "SELECT name FROM genres WHERE fold(name) = ?", (slug.replace("-", " "),))]


def genre_children(conn: sqlite3.Connection, slug: str) -> list[sqlite3.Row]:
    """The subgenres filed under ``slug``, largest first."""
    return conn.execute(
        "SELECT slug, name, titles FROM genre_pages WHERE parent_slug = ? "
        "ORDER BY titles DESC, slug", (slug,)).fetchall()


def genre_pages(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every genre page, for the hub total and the sitemap's threshold check."""
    return conn.execute(
        "SELECT slug, name, titles, parent_slug FROM genre_pages ORDER BY slug"
    ).fetchall()


def genre_tree(conn: sqlite3.Connection, audience: str) -> list[dict]:
    """One audience's genre tree: top genres carrying their own children.

    Jeugd and volwassenen are separate taxonomies — 67 of 213 subgenres sit under
    a different parent depending on which shelf you are standing at — so each has
    its own rows. The nesting below is presentation shaping over a few hundred
    rows; the parent votes and the counts were settled at build time.
    """
    rows = conn.execute(
        "SELECT t.slug, t.parent_slug, t.titles, gp.name "
        "FROM genre_tree t JOIN genre_pages gp ON gp.slug = t.slug "
        "WHERE t.audience = ?", (audience,)).fetchall()
    kids: dict[str, list[dict]] = {}
    for r in rows:
        if r["parent_slug"]:
            kids.setdefault(r["parent_slug"], []).append(
                {"name": r["name"], "slug": r["slug"], "titles": r["titles"]})
    for group in kids.values():
        group.sort(key=lambda g: -g["titles"])
    tops = [{"name": r["name"], "slug": r["slug"], "titles": r["titles"],
             "children": kids.get(r["slug"], [])}
            for r in rows if not r["parent_slug"]]
    return sorted(tops, key=lambda g: (-g["titles"], g["name"].lower()))


def browse_summary(conn: sqlite3.Connection, f: SearchFilters,
                   top_authors: int = 8) -> dict:
    """Aggregate facts about a filtered slice: format split, year span, top authors.

    This is what keeps a browse landing page from being a bare wall of covers —
    every genre gets a different, factual paragraph, and the author names double
    as internal links into the author pages.

    Availability, not representation: ``SUM(has_ebook)`` counts the books in this
    slice you can read, which is the same question the shelf below badges. It used
    to be two correlated EXISTS subqueries over a (title, author) key, plus a
    dedicated functional index, plus a branch mirroring search()'s collapse rule —
    all of it to work around counting works through whichever edition happened to
    represent them.
    """
    where, params = _build_where(f)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    row = conn.execute(
        "SELECT SUM(w.has_ebook) AS ebooks, SUM(w.has_audiobook) AS audiobooks, "
        "       SUM(w.ereader) AS ereader, "
        "       MIN(NULLIF(w.year, 0)) AS year_min, MAX(w.year) AS year_max "
        f"FROM works w {where_sql}", params).fetchone()
    # Grouped by author_id: persons keep unrelated non-Latin names in separate
    # rows, so the old "name_fold <> ''" exclusion (which existed because
    # fold-grouping fused them into one person with a summed count) can go.
    authors = conn.execute(
        "SELECT a.name AS name, COUNT(*) AS titles FROM works w "
        "JOIN work_authors wa ON wa.work_id = w.work_id "
        "JOIN authors a ON a.id = wa.author_id "
        f"{where_sql} GROUP BY wa.author_id "
        "ORDER BY titles DESC, a.name_fold LIMIT ?",
        [*params, top_authors]).fetchall()
    return {"ebooks": row["ebooks"] or 0, "audiobooks": row["audiobooks"] or 0,
            "ereader": row["ereader"] or 0, "year_min": row["year_min"],
            "year_max": row["year_max"], "authors": authors}


def similar_books(conn: sqlite3.Connection, work_id: str, method: str = "lsa",
                  limit: int = 20) -> list[sqlite3.Row]:
    """"Meer zoals dit": precomputed LSA neighbours for a book (see obc.similar).

    Returns display rows ordered by similarity, or an empty list if the table isn't
    built yet — the page just omits the strip.
    """
    limit = _limit(limit, 20, 30)
    try:
        return conn.execute(
            "SELECT w.work_id, w.title, w.author, w.slug, w.cover_url, "
            "       w.has_ebook, w.has_audiobook, s.score "
            "FROM work_similar s JOIN works w ON w.work_id = s.other_work_id "
            "WHERE s.work_id = ? AND s.method = ? ORDER BY s.rank LIMIT ?",
            (work_id, method, limit)).fetchall()
    except sqlite3.OperationalError as exc:  # table absent -> feature not built yet
        if "work_similar" not in str(exc) and "method" not in str(exc):
            raise
        return []


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
    """A list's full contents. ``li.ppn`` holds a work_id (a work_id *is* a ppn),
    so the row carries the work's slug and both availability flags — the item
    links to one book page showing every edition it exists in."""
    return conn.execute(
        "SELECT li.position, li.year, li.title, li.author, li.cover_url, li.ppn, li.won, "
        "w.work_id AS work_id, w.slug AS slug, w.cover_url AS bcover, "
        "w.has_ebook AS bebook, w.has_audiobook AS baudio "
        "FROM list_items li LEFT JOIN works w ON w.work_id = li.ppn "
        "WHERE li.list_id = ? ORDER BY li.position", (list_id,)).fetchall()


# --------------------------------------------------------------------------- #
# stats dashboard
# --------------------------------------------------------------------------- #
def web_stats(conn: sqlite3.Connection) -> dict:
    def one(q: str, *a):
        return conn.execute(q, a).fetchone()[0]

    def many(q: str, *a):
        return conn.execute(q, a).fetchall()

    # top-level genres (parent_id IS NULL for that link) and sub-genres carry
    # their parent's name, so the stats page can show "Parent › Kind" like the
    # book page does. A genre used both ways (rare cross-audience overlap) gets
    # its own row per role, so counts stay honest.
    genres = many(
        "SELECT g.name, p.name AS parent, COUNT(*) n "
        "FROM work_genres wg JOIN genres g ON g.id = wg.genre_id "
        "LEFT JOIN genres p ON p.id = wg.parent_id "
        "GROUP BY g.id, p.id ORDER BY n DESC LIMIT 12")

    return {
        # both grains, separately: a book and the files you can borrow it as are
        # different numbers, and this page used to quietly report the second one
        "total": one("SELECT COUNT(*) FROM works"),
        "editions": one("SELECT COUNT(*) FROM editions"),
        "ebooks": one("SELECT COUNT(*) FROM works WHERE has_ebook = 1"),
        "audiobooks": one("SELECT COUNT(*) FROM works WHERE has_audiobook = 1"),
        "ereader": one("SELECT COUNT(*) FROM works WHERE ereader = 1"),
        "authors": one("SELECT COUNT(*) FROM authors"),
        "publishers": one("SELECT COUNT(*) FROM publishers"),
        "lists": one("SELECT COUNT(*) FROM lists"),
        "languages": many("SELECT name, n FROM languages ORDER BY n DESC LIMIT 8"),
        "genres": genres,
        "years": many("SELECT year, COUNT(*) n FROM works WHERE year >= 2000 "
                      "GROUP BY year ORDER BY year"),
        "top_authors": many("SELECT name, n_works AS n FROM authors "
                            "ORDER BY n_works DESC LIMIT 12"),
        "top_publishers": many("SELECT name, n FROM publishers ORDER BY n DESC LIMIT 12"),
    }
