"""Minimal server-rendered search UI over the SQLite catalog.

Run with::

    uvicorn obc.web.app:app --reload

The routes here stay thin: they parse the request, delegate every database read
to :mod:`obc.web.queries`, then build presentation bits (cover sizing, filter
chips, URLs) and render a template. Search uses FTS5 ``bm25`` ranking (weighted
toward title/author) combined with WHERE filters; an empty query falls back to a
plain browse ordered by the chosen sort.
"""

from __future__ import annotations

import datetime
import os
import re
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Header, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from .. import db
from . import queries
from .bio import author_bio

DB_PATH = Path(os.environ.get("OBC_DB", db.DEFAULT_DB))
PAGE_SIZE = 24

_templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
_STATIC = Path(__file__).parent / "static"


# --------------------------------------------------------------------------- #
# Jinja helpers (presentation)
# --------------------------------------------------------------------------- #
def _coverw(url: str | None, width: int = 400) -> str:
    """Request a larger cover size. The leibniz signature stays valid when only
    the width changes."""
    if not url:
        return ""
    if "width=" in url:
        return re.sub(r"width=\d+", f"width={width}", url)
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}width={width}"


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


_NL_MONTHS = ("", "januari", "februari", "maart", "april", "mei", "juni", "juli",
              "augustus", "september", "oktober", "november", "december")


def _nldate(value) -> str:
    """Format an ISO datetime string or epoch seconds as a Dutch date
    ('27 juni 2026'). Returns '' for empty/unparseable input."""
    if not value:
        return ""
    try:
        if isinstance(value, (int, float)):
            dt = datetime.datetime.fromtimestamp(value)
        else:
            dt = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, OSError, OverflowError):
        return ""
    return f"{dt.day} {_NL_MONTHS[dt.month]} {dt.year}"


def _data_updated() -> float | None:
    """Epoch seconds the catalog was last (re)built — the DB file's mtime."""
    try:
        return DB_PATH.stat().st_mtime
    except OSError:
        return None


_templates.env.filters["coverw"] = _coverw
_templates.env.filters["nldate"] = _nldate
_templates.env.globals["url_with"] = _url_with
_templates.env.globals["url_without"] = _url_without
_templates.env.globals["data_updated"] = _data_updated

try:
    from importlib.metadata import version as _pkg_version
    APP_VERSION = _pkg_version("online-bibliotheek-catalogus")
except Exception:
    APP_VERSION = "dev"
_templates.env.globals["app_version"] = APP_VERSION


# --------------------------------------------------------------------------- #
# app + connection
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def _lifespan(app: FastAPI):
    # After every deploy/restart, kick off a catalog refresh so the DB is built
    # (fresh volume → full harvest) or kept fresh (→ incremental sync). It runs in
    # a background thread and is gated by an env flag so local `obc serve` and the
    # tests never scrape. The scheduled refresh is handled separately by the Fly
    # cron machine, which POSTs the token-protected /admin/refresh endpoint.
    if os.environ.get("OBC_REFRESH_ON_STARTUP") == "1":
        from ..log import logger
        from . import scheduler
        if scheduler.trigger_refresh():
            logger.info("[startup] catalog refresh triggered")
    yield


app = FastAPI(title="online bibliotheek — eigen catalogus", lifespan=_lifespan)


def _conn() -> sqlite3.Connection:
    return queries.connect_ro(DB_PATH)


@app.exception_handler(sqlite3.OperationalError)
async def _db_unavailable(request: Request, exc: sqlite3.OperationalError):
    """Friendly page when the catalog DB isn't present yet (e.g. fresh volume)."""
    return HTMLResponse(
        "<!doctype html><html lang='nl'><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>De catalogus wordt opgebouwd</title>"
        "<body style='font-family:system-ui,sans-serif;max-width:38rem;margin:16vh auto;"
        "padding:0 1.5rem;text-align:center;color:#3a2c20'>"
        "<div style='font-size:3rem'>📚</div>"
        "<h1 style='font-weight:800'>De catalogus wordt opgebouwd</h1>"
        "<p style='color:#7a6a5a;line-height:1.6'>De database is nog niet geladen. "
        "Kom over een moment terug.</p></body></html>",
        status_code=503)


@app.get("/healthz", include_in_schema=False)
def healthz():
    """Liveness probe — independent of the catalog DB (used by the host's health check)."""
    return {"status": "ok"}


_REFRESH_TOKEN = os.environ.get("OBC_REFRESH_TOKEN", "")


@app.post("/admin/refresh", include_in_schema=False)
def admin_refresh(authorization: str = Header(default="")):
    """Trigger a catalog refresh (sync + lists + normalize) in the background.

    Protected by a bearer token (``OBC_REFRESH_TOKEN``) so only the scheduled Fly
    cron can call it. Returns 202 immediately; the work runs in a thread in this
    machine (where the volume is mounted). 409 if a refresh is already running."""
    import hmac

    token = authorization.removeprefix("Bearer ").strip()
    if not _REFRESH_TOKEN or not hmac.compare_digest(token, _REFRESH_TOKEN):
        return Response(status_code=401)
    from . import scheduler
    if scheduler.trigger_refresh():
        return Response("refresh started", status_code=202)
    return Response("refresh already running", status_code=409)


@app.get("/favicon.svg", include_in_schema=False)
def favicon():
    return FileResponse(_STATIC / "favicon.svg", media_type="image/svg+xml")


@app.get("/favicon.ico", include_in_schema=False)
def favicon_ico():
    return FileResponse(_STATIC / "favicon.svg", media_type="image/svg+xml")


# Facet values are identical for every request, so cache them and only recompute
# when the database file changes (i.e. after a normalize).
_facets_cache: dict = {"key": None, "data": None}


def _facets(conn: sqlite3.Connection) -> dict:
    try:
        key = DB_PATH.stat().st_mtime_ns
    except OSError:
        key = None
    if _facets_cache["key"] == key and _facets_cache["data"] is not None:
        return _facets_cache["data"]
    data = queries.compute_facets(conn)
    _facets_cache.update(key=key, data=data)
    return data


# --------------------------------------------------------------------------- #
# search
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
def search(
    request: Request,
    q: str = "",
    format_: str = Query("", alias="format"),
    language: list[str] = Query(default=[]),
    genre: list[str] = Query(default=[]),
    publisher: list[str] = Query(default=[]),
    author: list[str] = Query(default=[]),
    list_: list[str] = Query(default=[], alias="list"),
    ereader: str = "",
    year_from: str = "",
    year_to: str = "",
    sort: str = "",
    page: int = Query(1, ge=1),
):
    q = q.strip()
    yf, yt = queries.parse_year(year_from), queries.parse_year(year_to)
    # unset sort -> relevance for a search, newest-first when browsing
    if sort not in queries.SORTS:
        sort = "relevance" if q else "year_desc"
    # de-dupe / drop blanks
    language = [v for v in dict.fromkeys(language) if v]
    genre = [v for v in dict.fromkeys(genre) if v]
    publisher = [v for v in dict.fromkeys(publisher) if v]
    author = [v for v in dict.fromkeys(author) if v]
    lists_ = [v for v in dict.fromkeys(list_) if v]

    filters = queries.SearchFilters(
        q=q, format=format_, languages=tuple(language), genres=tuple(genre),
        publishers=tuple(publisher), authors=tuple(author), lists=tuple(lists_),
        ereader=(ereader == "1"), year_from=yf, year_to=yt, sort=sort)

    conn = _conn()
    result = queries.search(conn, filters, page, PAGE_SIZE)
    rows = result.rows
    facets = _facets(conn)
    formats_map = queries.formats_map(conn, rows)
    lists_map = queries.lists_map(conn, rows)
    total_indexed = queries.total_books(conn)
    conn.close()

    total = result.total
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    list_names = {lst["slug"]: lst["name"] for lst in facets["lists"]}

    state = {"q": q, "format": format_, "language": language, "genre": genre,
             "publisher": publisher, "author": author, "list": lists_,
             "ereader": ereader if ereader == "1" else "", "sort": sort,
             "year_from": year_from if yf is not None else "",
             "year_to": year_to if yt is not None else ""}

    # active-filter chips (each with a remove URL + icon)
    chips = []
    if format_:
        chips.append({"label": "E-book" if format_ == "ebook" else "Luisterboek",
                      "icon": "book" if format_ == "ebook" else "audio",
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
        "format": format_, "language": language, "genre": genre,
        "publisher": publisher, "author": author, "list": lists_, "ereader": ereader,
        "year_from": state["year_from"], "year_to": state["year_to"], "sort": sort,
        "page": page, "pages": pages, "facets": facets, "page_size": PAGE_SIZE,
        "chips": chips, "has_filters": bool(q or chips), "state": state,
        "formats_map": formats_map, "lists_map": lists_map,
        "list_options": [lst["slug"] for lst in facets["lists"]],
        "list_labels": {lst["slug"]: lst["name"] for lst in facets["lists"]},
    })


# --------------------------------------------------------------------------- #
# detail / browse pages
# --------------------------------------------------------------------------- #
@app.get("/series/{name}", response_class=HTMLResponse)
def series_page(request: Request, name: str):
    conn = _conn()
    rows = queries.series_books(conn, name)
    if not rows:
        conn.close()
        return HTMLResponse("<h1>Reeks niet gevonden</h1>", status_code=404)
    formats_map = queries.formats_map(conn, rows)
    conn.close()
    return _templates.TemplateResponse(request, "series.html", {
        "name": name, "books": rows, "total": len(rows), "formats_map": formats_map})


@app.get("/stats", response_class=HTMLResponse)
def stats_page(request: Request):
    conn = _conn()
    data = queries.web_stats(conn)
    conn.close()
    return _templates.TemplateResponse(request, "stats.html", {"s": data})


@app.get("/over", response_class=HTMLResponse)
def about(request: Request):
    """Static 'about' page — independent of the catalog DB so it always renders."""
    return _templates.TemplateResponse(request, "over.html", {})


@app.get("/author/{name}", response_class=HTMLResponse)
def author_page(request: Request, name: str):
    conn = _conn()
    rows = queries.author_books(conn, name)
    if not rows:
        conn.close()
        return HTMLResponse("<h1>Auteur niet gevonden</h1>", status_code=404)
    formats_map = queries.formats_map(conn, rows)
    lists_map = queries.lists_map(conn, rows)
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
        "author_lists": author_lists, "bio": author_bio(name)})


@app.get("/lists", response_class=HTMLResponse)
def lists_overview(request: Request, sort: str = "name"):
    if sort not in queries.LIST_SORTS:
        sort = "name"
    conn = _conn()
    rows = queries.lists_overview(conn, sort)
    conn.close()
    return _templates.TemplateResponse(request, "lists.html", {"lists": rows, "sort": sort})


@app.get("/list/{slug}", response_class=HTMLResponse)
def list_detail(request: Request, slug: str, show: str = ""):
    conn = _conn()
    lst = queries.list_row(conn, slug)
    if lst is None:
        conn.close()
        return HTMLResponse("<h1>Lijst niet gevonden</h1>", status_code=404)
    rows = queries.list_items(conn, lst["id"])
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


@app.get("/book/{ppn}", response_class=HTMLResponse)
def book(request: Request, ppn: str):
    conn = _conn()
    detail = queries.book_detail(conn, ppn)
    conn.close()
    if detail is None:
        return HTMLResponse("<h1>Niet gevonden</h1>", status_code=404)
    return _templates.TemplateResponse(request, "book.html", {
        "b": detail["row"], "genres": detail["genres"], "editions": detail["editions"],
        "authors": detail["authors"], "book_lists": detail["book_lists"]})


# --------------------------------------------------------------------------- #
# JSON endpoints (autocomplete + searchable facets)
# --------------------------------------------------------------------------- #
@app.get("/suggest")
def suggest(q: str = "", limit: int = 7):
    """Autocomplete: matching titles (-> book) and authors/publishers/… (-> search)."""
    conn = _conn()
    data = queries.suggest(conn, q.strip(), limit)
    conn.close()
    if data is None:
        return {"titles": [], "authors": []}
    titles = [
        {"ppn": r["ppn"], "title": r["title"], "author": r["author"],
         "cover_url": _coverw(r["cover_url"], 80), "format": r["format"]}
        for r in data["title_rows"]
    ]
    return {"titles": titles, "authors": data["authors"],
            "publishers": data["publishers"], "genres": data["genres"],
            "languages": data["languages"], "lists": data["lists"]}


@app.get("/facet")
def facet(kind: str = Query("", alias="type"), q: str = "", limit: int = 30):
    """Searchable facet values (for large facets like author/publisher)."""
    conn = _conn()
    values = queries.facet_values(conn, kind, q, limit)
    conn.close()
    return {"values": values}
