"""Minimal server-rendered search UI over the SQLite catalog.

Run with::

    uvicorn obc.web.app:app --reload

Search uses FTS5 ``bm25`` ranking (weighted toward title/author) and combines
with WHERE filters on format / language / genre / year. When the query is empty
it falls back to a plain browse ordered by the chosen sort.
"""

from __future__ import annotations

import os
import re
import sqlite3
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote, urlencode

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from .. import db
from ..textnorm import fold

DB_PATH = Path(os.environ.get("OBC_DB", db.DEFAULT_DB))
PAGE_SIZE = 24

_templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _coverw(url: str | None, width: int = 400) -> str:
    """Request a larger cover size. The leibniz signature stays valid when only
    the width changes."""
    if not url:
        return ""
    if "width=" in url:
        return re.sub(r"width=\d+", f"width={width}", url)
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}width={width}"


_templates.env.filters["coverw"] = _coverw


def _url_with(state: dict, **over) -> str:
    """Build a query string from ``state`` (values may be lists), applying
    overrides. Empty values are dropped. Used by templates for links."""
    merged = {**state, **over}
    pairs: list[tuple[str, str]] = []
    for key, val in merged.items():
        if val in (None, "", []):
            continue
        if isinstance(val, (list, tuple)):
            pairs += [(key, v) for v in val if v not in (None, "")]
        else:
            pairs.append((key, val))
    return "?" + urlencode(pairs) if pairs else "?"


def _url_without(state: dict, key: str, value: str) -> str:
    """Return a URL with ``value`` removed from the list-valued ``key``."""
    remaining = [v for v in (state.get(key) or []) if v != value]
    return _url_with({**state, key: remaining}, page=1)


_templates.env.globals["url_with"] = _url_with
_templates.env.globals["url_without"] = _url_without
app = FastAPI(title="online bibliotheek — eigen catalogus")

_STATIC = Path(__file__).parent / "static"


@app.on_event("startup")
def _start_scheduler() -> None:
    from . import scheduler
    scheduler.start()  # no-op unless OBC_SYNC_HOURS / OBC_LISTS_HOURS are set


@app.get("/favicon.svg", include_in_schema=False)
def favicon():
    return FileResponse(_STATIC / "favicon.svg", media_type="image/svg+xml")


@app.get("/favicon.ico", include_in_schema=False)
def favicon_ico():
    return FileResponse(_STATIC / "favicon.svg", media_type="image/svg+xml")

_SORTS = {
    "relevance": None,  # only meaningful with a query
    "added": "b.added_rank IS NULL, b.added_rank ASC",
    "year_desc": "b.year DESC",
    "year_asc": "b.year ASC",
    "title": "b.title COLLATE NOCASE ASC",
}


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    # diacritic/case-insensitive folding for LIKE matching (Klöpping ~ klopping)
    conn.create_function("fold", 1, lambda s: fold(s) if s else "", deterministic=True)
    return conn


def _as_year(value: str) -> int | None:
    """Lenient year parse: '' or junk -> None (avoids 422 on empty params)."""
    value = (value or "").strip()
    return int(value) if value.lstrip("-").isdigit() else None


def _fts_query(q: str) -> str:
    """Turn free text into a safe FTS5 MATCH expression (prefix, AND-ed)."""
    terms = re.findall(r"\w+", q, flags=re.UNICODE)
    return " ".join(f'"{t}"*' for t in terms)


def _formats_map(conn: sqlite3.Connection, rows) -> dict:
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


def _lists_map(conn: sqlite3.Connection, rows) -> dict:
    """ppn -> list of {name, slug, position} for the books on this page."""
    ppns = [r["ppn"] for r in rows]
    out: dict[str, list] = {}
    if ppns:
        qmarks = ",".join("?" * len(ppns))
        for r in conn.execute(
                f"SELECT bl.book_ppn, l.name, l.slug, bl.position, bl.year FROM book_lists bl "
                f"JOIN lists l ON l.id = bl.list_id WHERE bl.book_ppn IN ({qmarks}) "
                f"ORDER BY bl.position", ppns):
            out.setdefault(r["book_ppn"], []).append(
                {"name": r["name"], "slug": r["slug"], "position": r["position"],
                 "year": r["year"]})
    return out


_facets_cache: dict = {"key": None, "data": None}


def _facets(conn: sqlite3.Connection) -> dict:
    """Facet values are the same for every request, so cache them and only
    recompute when the database file changes (i.e. after a normalize)."""
    try:
        key = DB_PATH.stat().st_mtime_ns
    except OSError:
        key = None
    if _facets_cache["key"] == key and _facets_cache["data"] is not None:
        return _facets_cache["data"]
    data = _compute_facets(conn)
    _facets_cache.update(key=key, data=data)
    return data


def _compute_facets(conn: sqlite3.Connection) -> dict:
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


def _in(col: str, values: list[str]) -> tuple[str, list]:
    marks = ",".join("?" * len(values))
    return f"{col} IN ({marks})", list(values)


@app.get("/", response_class=HTMLResponse)
def search(
    request: Request,
    q: str = "",
    format: str = "",
    language: list[str] = Query(default=[]),
    genre: list[str] = Query(default=[]),
    publisher: list[str] = Query(default=[]),
    author: list[str] = Query(default=[]),
    list: list[str] = Query(default=[]),
    ereader: str = "",
    year_from: str = "",
    year_to: str = "",
    sort: str = "",
    page: int = Query(1, ge=1),
):
    q = q.strip()
    yf, yt = _as_year(year_from), _as_year(year_to)
    # unset sort -> relevance for a search, newest-first when browsing
    if sort not in _SORTS:
        sort = "relevance" if q else "year_desc"
    # de-dupe / drop blanks
    language = [v for v in dict.fromkeys(language) if v]
    genre = [v for v in dict.fromkeys(genre) if v]
    publisher = [v for v in dict.fromkeys(publisher) if v]
    author = [v for v in dict.fromkeys(author) if v]
    list = [v for v in dict.fromkeys(list) if v]  # noqa: A001 (query param name)
    conn = _conn()

    where, params = [], []
    if format:
        where.append("b.format = ?"); params.append(format)
    if language:
        clause, vals = _in("b.language", language); where.append(clause); params += vals
    if publisher:
        clause, vals = _in("b.publisher", publisher); where.append(clause); params += vals
    if author:
        clause, vals = _in("a.name", author)
        where.append("b.ppn IN (SELECT ba.book_ppn FROM book_authors ba "
                     f"JOIN authors a ON a.id = ba.author_id WHERE {clause})")
        params += vals
    if list:
        clause, vals = _in("l.slug", list)
        where.append("b.ppn IN (SELECT bl.book_ppn FROM book_lists bl "
                     f"JOIN lists l ON l.id = bl.list_id WHERE {clause})")
        params += vals
    if ereader == "1":
        where.append("b.ereader = 1")
    if yf is not None:
        where.append("b.year >= ?"); params.append(yf)
    if yt is not None:
        where.append("b.year <= ?"); params.append(yt)
    if genre:
        clause, vals = _in("g.name", genre)
        where.append("b.ppn IN (SELECT bg.book_ppn FROM book_genres bg "
                     f"JOIN genres g ON g.id = bg.genre_id WHERE {clause})")
        params += vals

    joins, order = "", _SORTS[sort] or "b.title COLLATE NOCASE ASC"
    match = _fts_query(q) if q else ""
    if match:
        joins = "JOIN books_fts f ON f.ppn = b.ppn"
        where.append("books_fts MATCH ?"); params.append(match)
        if sort == "relevance":
            order = "bm25(books_fts, 10.0, 6.0, 2.0, 1.0)"

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute(
        f"SELECT COUNT(*) FROM books b {joins} {where_sql}", params).fetchone()[0]

    offset = (page - 1) * PAGE_SIZE
    rows = conn.execute(
        f"SELECT b.* FROM books b {joins} {where_sql} "
        f"ORDER BY {order} LIMIT ? OFFSET ?",
        [*params, PAGE_SIZE, offset]).fetchall()

    facets = _facets(conn)
    formats_map = _formats_map(conn, rows)
    lists_map = _lists_map(conn, rows)
    total_indexed = conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]
    conn.close()
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    list_names = {l["slug"]: l["name"] for l in facets["lists"]}

    state = {"q": q, "format": format, "language": language, "genre": genre,
             "publisher": publisher, "author": author, "list": list,
             "ereader": ereader if ereader == "1" else "", "sort": sort,
             "year_from": year_from if yf is not None else "",
             "year_to": year_to if yt is not None else ""}

    # active-filter chips (each with a remove URL + icon)
    chips = []
    if format:
        chips.append({"label": "E-book" if format == "ebook" else "Luisterboek",
                      "icon": "book" if format == "ebook" else "audio",
                      "url": _url_with(state, format="", page=1)})
    for key, icon in (("language", "lang"), ("genre", "genre"),
                      ("publisher", "publisher"), ("author", "author")):
        for v in state[key]:
            chips.append({"label": v, "icon": icon,
                          "url": _url_without(state, key, v)})
    for slug in state["list"]:
        chips.append({"label": list_names.get(slug, slug), "icon": "list",
                      "url": _url_without(state, "list", slug)})
    if yf is not None:
        chips.append({"label": f"vanaf {yf}", "icon": "cal",
                      "url": _url_with(state, year_from="", page=1)})
    if yt is not None:
        chips.append({"label": f"t/m {yt}", "icon": "cal",
                      "url": _url_with(state, year_to="", page=1)})
    if ereader == "1":
        chips.append({"label": "Voor e-reader", "icon": "ereader",
                      "url": _url_with(state, ereader="", page=1)})

    return _templates.TemplateResponse(request, "search.html", {
        "books": rows, "total": total, "total_indexed": total_indexed, "q": q,
        "format": format, "language": language, "genre": genre,
        "publisher": publisher, "author": author, "list": list, "ereader": ereader,
        "year_from": state["year_from"], "year_to": state["year_to"], "sort": sort,
        "page": page, "pages": pages, "facets": facets, "page_size": PAGE_SIZE,
        "chips": chips, "has_filters": bool(q or chips), "state": state,
        "formats_map": formats_map, "lists_map": lists_map,
        "list_options": [l["slug"] for l in facets["lists"]],
        "list_labels": {l["slug"]: l["name"] for l in facets["lists"]},
    })


@app.get("/series/{name}", response_class=HTMLResponse)
def series_page(request: Request, name: str):
    conn = _conn()
    rows = conn.execute(
        "SELECT b.* FROM books b WHERE b.series = ? "
        "ORDER BY b.series_no, b.year LIMIT 300", (name,)).fetchall()
    if not rows:
        conn.close()
        return HTMLResponse("<h1>Reeks niet gevonden</h1>", status_code=404)
    formats_map = _formats_map(conn, rows)
    conn.close()
    return _templates.TemplateResponse(request, "series.html", {
        "name": name, "books": rows, "total": len(rows), "formats_map": formats_map})


@app.get("/stats", response_class=HTMLResponse)
def stats_page(request: Request):
    conn = _conn()
    g = lambda q, *a: conn.execute(q, a).fetchone()[0]
    rows = lambda q, *a: conn.execute(q, a).fetchall()
    data = {
        "total": g("SELECT COUNT(*) FROM books"),
        "ebooks": g("SELECT COUNT(*) FROM books WHERE format='ebook'"),
        "audiobooks": g("SELECT COUNT(*) FROM books WHERE format='audiobook'"),
        "ereader": g("SELECT COUNT(*) FROM books WHERE ereader=1"),
        "authors": g("SELECT COUNT(*) FROM authors"),
        "publishers": g("SELECT COUNT(*) FROM publishers"),
        "lists": g("SELECT COUNT(*) FROM lists"),
        "languages": rows("SELECT name, n FROM languages ORDER BY n DESC LIMIT 8"),
        "genres": rows("SELECT g.name, COUNT(*) n FROM genres g "
                       "JOIN book_genres bg ON bg.genre_id=g.id GROUP BY g.id "
                       "ORDER BY n DESC LIMIT 12"),
        "years": rows("SELECT year, COUNT(*) n FROM books WHERE year >= 2000 "
                      "GROUP BY year ORDER BY year"),
        "top_authors": rows("SELECT a.name, COUNT(*) n FROM authors a "
                            "JOIN book_authors ba ON ba.author_id=a.id GROUP BY a.id "
                            "ORDER BY n DESC LIMIT 12"),
        "top_publishers": rows("SELECT name, n FROM publishers ORDER BY n DESC LIMIT 12"),
    }
    conn.close()
    return _templates.TemplateResponse(request, "stats.html", {"s": data})


_wiki_http = httpx.Client(timeout=6, follow_redirects=True,
                          headers={"User-Agent": "online-bibliotheek-catalogus/0.1"})
_AUTHOR_WORDS = ("schrijf", "schrijver", "schrijfster", "auteur", "dichter",
                 "romancier", "writer", "novelist", "poet", "journalist", "columnist",
                 "illustrator", "vertaler", "kinderboeken")


@lru_cache(maxsize=4096)
def _author_bio(name: str):
    """Best-effort short bio + photo from the Dutch Wikipedia. Returns None when
    there is no clear author page (cached, incl. misses)."""
    try:
        r = _wiki_http.get(
            "https://nl.wikipedia.org/api/rest_v1/page/summary/" + quote(name, safe=""))
        if r.status_code != 200:
            return None
        d = r.json()
        if d.get("type") == "disambiguation" or not d.get("extract"):
            return None
        blob = f"{d.get('description', '')} {d['extract']}".lower()
        if not any(w in blob for w in _AUTHOR_WORDS):
            return None  # likely a same-named non-author; skip
        return {"extract": d["extract"],
                "thumb": (d.get("thumbnail") or {}).get("source"),
                "url": (d.get("content_urls", {}).get("desktop", {}) or {}).get("page")}
    except (httpx.HTTPError, ValueError):
        return None


@app.get("/author/{name}", response_class=HTMLResponse)
def author_page(request: Request, name: str):
    conn = _conn()
    rows = conn.execute(
        "SELECT b.* FROM books b JOIN book_authors ba ON ba.book_ppn = b.ppn "
        "JOIN authors a ON a.id = ba.author_id WHERE a.name = ? "
        "ORDER BY b.year DESC, b.title COLLATE NOCASE LIMIT 300", (name,)).fetchall()
    if not rows:
        conn.close()
        return HTMLResponse("<h1>Auteur niet gevonden</h1>", status_code=404)
    formats_map = _formats_map(conn, rows)
    lists_map = _lists_map(conn, rows)
    conn.close()
    # distinct lists/awards across this author's books (newest year first)
    seen, author_lists = set(), []
    for entries in lists_map.values():
        for e in entries:
            if e["slug"] not in seen:
                seen.add(e["slug"])
                author_lists.append(e)
    author_lists.sort(key=lambda e: -(e.get("year") or 0))
    return _templates.TemplateResponse(request, "author.html", {
        "name": name, "books": rows, "total": len(rows),
        "formats_map": formats_map, "lists_map": lists_map,
        "author_lists": author_lists, "bio": _author_bio(name)})


_LIST_SORTS = {
    "name": "l.name COLLATE NOCASE ASC",
    "available": "available DESC, l.name COLLATE NOCASE",
    "total": "total DESC, l.name COLLATE NOCASE",
    "pct": "(CASE WHEN total > 0 THEN available * 1.0 / total ELSE 0 END) DESC, l.name COLLATE NOCASE",
}


@app.get("/lists", response_class=HTMLResponse)
def lists_overview(request: Request, sort: str = "name"):
    if sort not in _LIST_SORTS:
        sort = "name"
    conn = _conn()
    rows = conn.execute(
        "SELECT l.slug, l.name, l.description, l.url, l.updated_at, "
        "COUNT(li.rowid) AS total, "
        "SUM(CASE WHEN li.ppn IS NOT NULL THEN 1 ELSE 0 END) AS available "
        "FROM lists l LEFT JOIN list_items li ON li.list_id = l.id "
        "GROUP BY l.id ORDER BY " + _LIST_SORTS[sort]).fetchall()
    conn.close()
    return _templates.TemplateResponse(request, "lists.html", {"lists": rows, "sort": sort})


@app.get("/list/{slug}", response_class=HTMLResponse)
def list_detail(request: Request, slug: str, show: str = ""):
    conn = _conn()
    lst = conn.execute("SELECT * FROM lists WHERE slug = ?", (slug,)).fetchone()
    if lst is None:
        conn.close()
        return HTMLResponse("<h1>Lijst niet gevonden</h1>", status_code=404)
    rows = conn.execute(
        "SELECT li.position, li.year, li.title, li.author, li.cover_url, li.ppn, "
        "b.cover_url AS bcover, b.format AS bformat "
        "FROM list_items li LEFT JOIN books b ON b.ppn = li.ppn "
        "WHERE li.list_id = ? ORDER BY li.position", (lst["id"],)).fetchall()
    conn.close()
    total = len(rows)
    available = sum(1 for i in rows if i["ppn"])
    if show == "available":
        items = [i for i in rows if i["ppn"]]
    elif show == "unavailable":
        items = [i for i in rows if not i["ppn"]]
    else:
        show, items = "", rows
    return _templates.TemplateResponse(request, "list_detail.html", {
        "lst": lst, "items": items, "available": available,
        "total": total, "show": show})


@app.get("/suggest")
def suggest(q: str = "", limit: int = 7):
    """Autocomplete: matching titles (-> book) and authors (-> search)."""
    q = q.strip()
    terms = re.findall(r"\w+", q, flags=re.UNICODE)
    if not terms:
        return {"titles": [], "authors": []}
    conn = _conn()
    title_m = " ".join(f'title:"{t}"*' for t in terms)
    titles = [
        {"ppn": r["ppn"], "title": r["title"], "author": r["author"],
         "cover_url": _coverw(r["cover_url"], 80), "format": r["format"]}
        for r in conn.execute(
            "SELECT b.ppn, b.title, b.author, b.cover_url, b.format "
            "FROM books_fts f JOIN books b ON b.ppn = f.ppn "
            "WHERE books_fts MATCH ? ORDER BY bm25(books_fts) LIMIT ?",
            (title_m, limit))
    ]
    fq = f"%{fold(q)}%"
    authors = [r["name"] for r in conn.execute(
        "SELECT a.name, COUNT(*) n FROM authors a JOIN book_authors ba "
        "ON ba.author_id = a.id WHERE a.name_fold LIKE ? GROUP BY a.id "
        "ORDER BY n DESC LIMIT 5", (fq,))]
    publishers = [r["name"] for r in conn.execute(
        "SELECT name FROM publishers WHERE name_fold LIKE ? "
        "ORDER BY n DESC LIMIT 4", (fq,))]
    genres = [r["name"] for r in conn.execute(
        "SELECT g.name, COUNT(*) n FROM genres g JOIN book_genres bg "
        "ON bg.genre_id = g.id WHERE fold(g.name) LIKE ? GROUP BY g.id "
        "ORDER BY n DESC LIMIT 4", (fq,))]
    lists = [{"slug": r["slug"], "name": r["name"]} for r in conn.execute(
        "SELECT slug, name FROM lists WHERE fold(name) LIKE ? ORDER BY name LIMIT 4",
        (fq,))]
    languages = [r["name"] for r in conn.execute(
        "SELECT name FROM languages WHERE name_fold LIKE ? ORDER BY n DESC LIMIT 3",
        (fq,))]
    conn.close()
    return {"titles": titles, "authors": authors, "publishers": publishers,
            "genres": genres, "languages": languages, "lists": lists}


@app.get("/facet")
def facet(type: str = "", q: str = "", limit: int = 30):
    """Searchable facet values (for large facets like author/publisher)."""
    conn = _conn()
    qq = q.strip()
    if type == "author":
        base = ("SELECT a.name v, COUNT(*) n FROM authors a "
                "JOIN book_authors ba ON ba.author_id = a.id ")
        if qq:
            rows = conn.execute(base + "WHERE a.name_fold LIKE ? GROUP BY a.id "
                                "ORDER BY n DESC LIMIT ?", (f"%{fold(qq)}%", limit))
        else:
            rows = conn.execute(base + "GROUP BY a.id ORDER BY n DESC LIMIT ?", (limit,))
    elif type == "publisher":
        if qq:
            rows = conn.execute(
                "SELECT name v, n FROM publishers WHERE name_fold LIKE ? "
                "ORDER BY n DESC LIMIT ?", (f"%{fold(qq)}%", limit))
        else:
            rows = conn.execute(
                "SELECT name v, n FROM publishers ORDER BY n DESC LIMIT ?", (limit,))
    else:
        conn.close()
        return {"values": []}
    values = [r["v"] for r in rows]
    conn.close()
    return {"values": values}


@app.get("/book/{ppn}", response_class=HTMLResponse)
def book(request: Request, ppn: str):
    conn = _conn()
    row = conn.execute("SELECT * FROM books WHERE ppn = ?", (ppn,)).fetchone()
    if row is None:
        conn.close()
        return HTMLResponse("<h1>Niet gevonden</h1>", status_code=404)
    genres = [r["name"] for r in conn.execute(
        "SELECT g.name FROM genres g JOIN book_genres bg ON bg.genre_id = g.id "
        "WHERE bg.book_ppn = ? ORDER BY g.name", (ppn,))]
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
                   "year": r["year"]} for r in conn.execute(
        "SELECT l.name, l.slug, bl.position, bl.year FROM book_lists bl "
        "JOIN lists l ON l.id = bl.list_id WHERE bl.book_ppn = ? ORDER BY bl.position",
        (ppn,))]
    conn.close()
    return _templates.TemplateResponse(
        request, "book.html",
        {"b": row, "genres": genres, "editions": editions,
         "authors": authors, "book_lists": book_lists})
