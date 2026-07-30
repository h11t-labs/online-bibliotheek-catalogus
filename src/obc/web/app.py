"""Minimal server-rendered search UI over the SQLite catalog.

Run with::

    uvicorn obc.web.app:app --reload

The routes here stay thin: they parse the request, delegate every database read
to :mod:`obc.web.queries`, then build presentation bits (cover sizing, filter
chips, URLs) and render a template. Search uses FTS5 ``bm25`` ranking (weighted
toward title/author) combined with WHERE filters; an empty query falls back to a
plain browse ordered by the chosen sort.

Three modules sit under this one and none of them import it back:
:mod:`obc.web.queries` (every SQL statement), :mod:`obc.web.indexes` (the
connection, and the indexes derived once per catalog rebuild) and
:mod:`obc.web.seo` (canonical URLs, breadcrumbs, robots.txt, sitemaps).
"""

from __future__ import annotations

import datetime
import os
import re
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, unquote, urlencode

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.routing import APIRoute
from fastapi.templating import Jinja2Templates

from ..textnorm import language_code, slugify
from . import indexes, queries, seo
from .bio import author_bio
from .indexes import AUDIENCES, AUTHOR_SORTS, BY_SURNAME, get_conn

PAGE_SIZE = 24
PER_PAGE_OPTIONS = (12, 24, 48, 96)  # selectable items-per-page (PAGE_SIZE is the default)
# How deep pagination goes. `LIMIT n OFFSET m` makes SQLite walk and discard m
# rows, so the cost is linear in the depth: measured over "de" (51,980 matches),
# page 1 is 373ms, page 100 657ms, page 200 1.3s and the last page 4.2s — and the
# pager links straight to that last page, so the worst case was one click from
# every search. 100 pages is 2,400 titles deep, well past anything a reader walks
# to: past that they refine the search instead. The *total* stays exact and is
# still shown; only the walk is bounded.
MAX_PAGES = 100

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


def _rounded(n: int) -> str:
    """``65.530`` -> ``'65.000+'``. Titles and meta descriptions quote the catalog
    size, and the exact count shifts with every refresh — rounding keeps the
    <title> stable between crawls instead of churning on each rebuild."""
    return f"{n // 1000}.000+" if n >= 1000 else str(n)


def _snippet(text: str, limit: int = 155) -> str:
    """Turn a raw catalog summary into a meta description.

    Publisher blurbs arrive wrapped in quote marks and padded with newlines, which
    reads as broken text in a result snippet. Collapse the whitespace, drop the
    wrapping quotes, and cut on a word boundary at roughly the width Search shows.
    """
    text = re.sub(r"\s+", " ", (text or "")).strip().strip('"“”\'')
    if len(text) <= limit:
        return text
    head = text[:limit]
    return head[:head.rfind(" ")].rstrip(" ,;:-–—") + "…" if " " in head else head + "…"


def _nlnum(n: int) -> str:
    """9803 -> '9.803' (Dutch thousands separator)."""
    return f"{n:,}".replace(",", ".")


_DUR_CLOCK = re.compile(r"^(\d+):([0-5]\d)(?::[0-5]\d)?$")
_DUR_WORDS = re.compile(r"(?:(\d+)\s*uur)?(?:\D*?(\d+)\s*minu)?", re.I)


def _dur_short(value) -> str:
    """Speelduur in one shape: '3:17:19' and '9 uur 1 minuut' both -> '3 u 17 min'.

    The catalog stores both spellings, which the meta rows can carry unnoticed but
    the borrow button cannot: there it sits one line below the e-book's page count,
    where a raw 'h:mm:ss' reads as a timestamp rather than a length. Anything
    matching neither shape passes through untouched.
    """
    s = str(value or "").strip()
    if not s:
        return ""
    if m := _DUR_CLOCK.match(s):
        hours, minutes = int(m[1]), int(m[2])
    else:
        m = _DUR_WORDS.match(s)
        if not m or not (m[1] or m[2]):
            return s
        hours, minutes = int(m[1] or 0), int(m[2] or 0)
    if hours and minutes:
        return f"{hours} u {minutes} min"
    return f"{hours} u" if hours else f"{minutes} min"


_templates.env.filters["coverw"] = _coverw
_templates.env.filters["nldate"] = _nldate
_templates.env.filters["author_path"] = seo.author_path
_templates.env.filters["series_path"] = seo.series_path
_templates.env.filters["genre_path"] = seo.genre_path
_templates.env.filters["nlnum"] = _nlnum
_templates.env.filters["dur_short"] = _dur_short
_templates.env.globals["url_with"] = _url_with
_templates.env.globals["url_without"] = _url_without
_templates.env.globals["book_path"] = seo.book_path
_templates.env.globals["data_updated"] = indexes.data_updated
_templates.env.globals["site_url"] = seo.SITE_URL
_templates.env.globals["site_name"] = seo.SITE_NAME

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

# Pages with no per-user state and a catalog that only changes on the daily rebuild,
# so they're safe to cache publicly — this offloads repeat hits and crawler traffic
# from the single small VM. Detail pages cache an hour; the browse home a few minutes.
# "/book/" stays: those are permanent redirects now, and a redirect is cacheable too.
_CACHE_PREFIXES = ("/boek/", "/book/", "/author", "/series/", "/list", "/stats",
                   "/about", "/genre", "/e-books", "/luisterboeken")


# Templates use inline <script>/<style> blocks (base.html), GoatCounter loads its
# counter from gc.zgo.at and beacons to obc.goatcounter.com, and covers hotlink
# leibniz.zbkb.nl plus several list providers' CDNs — hence 'unsafe-inline' and a
# broad img-src https:. Kept as one string so the middleware sets it verbatim.
_CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline' https://gc.zgo.at; "
    "connect-src 'self' https://obc.goatcounter.com; img-src 'self' https: data:; "
    "style-src 'self' 'unsafe-inline'; frame-ancestors 'self'; base-uri 'self'"
)


# The site answers on three hostnames: the apex, its www., and the fly.dev origin.
# All three served 200, so crawlers spent budget fetching the same 64k pages up to
# three times over. Canonical tags already named the apex; a 301 makes that cheap
# as well as unambiguous.
#
# Only the two *known* aliases are bounced, never "any host that isn't SITE_URL".
# OBC_SITE_URL is set as a Fly secret and only shadows the [env] default, so a
# blanket rule would silently 301 the live domain onto whatever a stale config
# said — deindexing the site. An alias that no longer matches just serves the page.
def _hostname(value: str) -> str:
    """Bare hostname from an origin or a Host header — no scheme, no port, folded."""
    return value.split("//", 1)[-1].split("/", 1)[0].split(":", 1)[0].lower()


# Compared port-less on both sides: a SITE_URL carrying a port (local runs, a
# staging origin) would otherwise match nothing and quietly disable the redirect.
_CANONICAL_HOST = _hostname(seo.SITE_URL) if seo.SITE_URL else ""
# Health checks and the cron machine reach the app over the private network under
# the machine's own Host; those paths are never redirected.
_NO_REDIRECT = ("/healthz", "/admin/")


def _is_alias(host: str) -> bool:
    """Is ``host`` a known alias of the canonical one (so safe to 301 away)?"""
    host = _hostname(host)
    if not _CANONICAL_HOST or not host or host == _CANONICAL_HOST:
        return False
    return host == f"www.{_CANONICAL_HOST}" or host.endswith(".fly.dev")


# One middleware, not two: each `@app.middleware("http")` wraps the whole app in
# another BaseHTTPMiddleware, and that layer is not free — it runs the downstream
# app in an anyio task group and streams the response back through a memory
# stream, per request. The alias redirect and the response headers have nothing to
# do with each other, but they do belong in the same pass.
@app.middleware("http")
async def _headers_and_canonical_host(request: Request, call_next):
    # The alias 301 skips the routing pass but still gets the headers below — it is
    # a response the site sends, and it used to get them by virtue of running in
    # the inner of the two middlewares.
    if (_is_alias(request.headers.get("host", ""))
            and not request.url.path.startswith(_NO_REDIRECT)):
        query = f"?{request.url.query}" if request.url.query else ""
        response: Response = RedirectResponse(
            seo.SITE_URL + request.url.path + query, status_code=301)
    else:
        response = await call_next(request)
    # Security headers on every response (cheap, no per-user state).
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = _CSP
    if (request.method in ("GET", "HEAD") and response.status_code == 200
            and "cache-control" not in response.headers):
        path = request.url.path
        if path == "/" and not request.url.query:
            response.headers["Cache-Control"] = "public, max-age=600"
        elif path.startswith(_CACHE_PREFIXES):
            response.headers["Cache-Control"] = "public, max-age=3600"
    return response


@app.exception_handler(sqlite3.OperationalError)
async def _db_unavailable(request: Request, exc: sqlite3.OperationalError):
    """Friendly page when the catalog DB isn't present yet (e.g. fresh volume).

    Only bootstrap-state errors (missing DB file / core tables not built yet) get
    the friendly 503 page; genuine SQL bugs re-raise so they surface as 500s in
    development and monitoring instead of hiding behind "catalogus wordt opgebouwd".
    """
    msg = str(exc).lower()
    # The `books`/`books_fts` entries stay alongside the new names: on the deploy
    # that renames the tables the volume still holds the old DB until the refresh
    # completes, and either shape's absence is the same bootstrap state.
    bootstrap_errors = (
        "unable to open database file",
        "no such table: works",
        "no such table: works_fts",
        "no such table: books",
        "no such table: books_fts",
    )
    if not any(e in msg for e in bootstrap_errors):
        raise exc
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


# --------------------------------------------------------------------------- #
# 404
# --------------------------------------------------------------------------- #
# Heading + explanation per kind of miss. A dead /book/… link is a different
# situation from a mistyped URL, and saying which one it is beats one generic
# "niet gevonden" for every case.
_NOT_FOUND_COPY = {
    "page": ("Deze pagina bestaat niet",
             "De link klopt niet (meer), of er is een typefout in het adres geslopen."),
    "book": ("Dit boek staat niet in de catalogus",
             "Mogelijk is de titel uit de collectie gehaald, of klopt de link niet meer."),
    "author": ("Auteur niet gevonden",
               "Deze auteur staat niet in de catalogus — of de naam wordt net iets "
               "anders geschreven."),
    "genre": ("Genre niet gevonden",
              "Dit genre staat niet in de catalogus — kijk in het overzicht welke er "
              "wel zijn."),
    "series": ("Reeks niet gevonden",
               "Deze reeks staat niet in de catalogus, of de delen staan er onder een "
               "andere reeksnaam."),
    "list": ("Lijst niet gevonden",
             "Deze lijst bestaat niet (meer). In het lijstenoverzicht staan ze allemaal."),
    "letter": ("Geen auteurs onder deze letter",
               "Onder deze letter staat niemand in de catalogus. Kies een andere letter "
               "in het auteursoverzicht."),
}
# A path segment worth feeding to the suggester: a plain word slug. Keeps the
# random URLs bots probe (``/wp-login.php``, ``/.env``) from each costing an
# FTS query for nothing.
_WORDY_SLUG = re.compile(r"[a-z][a-z0-9]{2,}(?:-[a-z0-9]+)*$", re.IGNORECASE)


def _slug_words(value: str) -> str:
    """A slug or encoded path segment as a plain search phrase.

    ``annie-mg-schmidt`` -> ``annie mg schmidt``.
    """
    return re.sub(r"[\s\-_]+", " ", unquote(value)).strip()


def _near_matches(term: str, limit: int = 6, titles: bool = True) -> list[dict]:
    """Best-effort "bedoelde je" links for ``term``.

    Runs on a page that is itself an error, so it never raises: no catalog, a
    half-built one or a term FTS can't parse simply yields no suggestions.

    ``titles=False`` for a mistyped *site* URL: an author or list whose name
    matches is a real answer, but AND-ing the words of "/veelgestelde-vragen"
    over the full text index just dredges up unrelated books.
    """
    if not term:
        return []
    try:
        conn = queries.connect_ro(indexes.DB_PATH)
    except sqlite3.Error:
        return []
    try:
        data = queries.suggest(conn, term, limit)
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    if not data:
        return []
    # Authors and lists first: they are whole destinations, while a title row is
    # one book out of many that matched the same words.
    out = [{"kind": "author", "icon": "author", "label": name, "url": seo.author_path(name)}
           for name in data["authors"]]
    out += [{"kind": "list", "icon": "list", "label": lst["name"],
             "url": f"/list/{lst['slug']}"} for lst in data["lists"]]
    # A genre has no page of its own; it is a filter on the browse view.
    out += [{"kind": "genre", "icon": "genre", "label": name,
             "url": f"/?genre={quote(name, safe='')}"} for name in data["genres"]]
    if titles:
        out += [{"kind": "book", "icon": "book", "label": row["title"] or "—",
                 "sub": row["author"] or "", "url": seo.book_path(row)}
                for row in data["title_rows"]]
    return out[:limit]


def _not_found(request: Request, kind: str = "page", term: str = "") -> Response:
    """The site's 404 page: what was missed, what to do next, close matches.

    ``term`` is the words behind the failed URL (an author slug, a list slug);
    it prefills the search box and drives the suggestions. Pass "" where the URL
    holds no readable words — a PPN, say — so the box opens empty instead of
    seeded with catalog gibberish.
    """
    head, lead = _NOT_FOUND_COPY.get(kind, _NOT_FOUND_COPY["page"])
    return _templates.TemplateResponse(request, "404.html", {
        "kind": kind, "head": head, "lead": lead, "term": term,
        "matches": _near_matches(term, titles=kind != "page"),
        # Error pages are never worth indexing, but their links are worth crawling.
        "robots": "noindex,follow",
        "meta_description": f"{head} — zoek in de collectie van de online Bibliotheek."},
        status_code=404)


@app.exception_handler(404)
async def _route_not_found(request: Request, exc):
    """Unmatched URLs. Without this they get FastAPI's ``{"detail":"Not Found"}``.

    The last path segment is often a readable slug (an old or mistyped link), so
    it seeds the search — but only when it looks like words rather than the
    random paths scanners probe.
    """
    tail = request.url.path.rstrip("/").rsplit("/", 1)[-1]
    term = _slug_words(tail) if _WORDY_SLUG.match(tail) else ""
    return _not_found(request, "page", term)


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


# How many covers a browse landing page shows. It is an entry point, not a
# replacement for the search UI — the "filter in zoeken" link opens the rest.
BROWSE_PREVIEW = 120


def _browse_page(request: Request, conn: sqlite3.Connection, *, heading: str,
                 lead: str, filters: queries.SearchFilters, search_url: str,
                 crumb: tuple[str, str] | None = None,
                 parent: dict | None = None,
                 children: list[dict] | None = None) -> Response:
    """Render a browse landing page (a genre or a format) from ``filters``."""
    result = queries.search(conn, filters, page=1, page_size=BROWSE_PREVIEW)
    summary = queries.browse_summary(conn, filters)
    # Only mention the split when there is one — a format page would otherwise
    # advertise "50.398 e-books en 0 luisterboeken".
    split = (f" {_nlnum(summary['ebooks'])} e-books en "
             f"{_nlnum(summary['audiobooks'])} luisterboeken."
             if summary["ebooks"] and summary["audiobooks"] else "")
    return _templates.TemplateResponse(request, "browse.html", {
        "heading": heading, "lead": lead, "books": result.rows,
        "total": result.total, "summary": summary, "search_url": search_url,
        "shown": len(result.rows),
        "lists_map": queries.lists_map(conn, result.rows),
        "parent": parent, "children": children or [],
        "breadcrumbs": seo.breadcrumbs(
            request, *([crumb] if crumb else []),
            *([(parent["name"], f"/genre/{parent['slug']}")] if parent else []),
            (heading, "")),
        "meta_description": f"{_nlnum(result.total)} titels — {lead}{split}"[:300],
    })


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
    view: str = "",
    page: int = Query(1, ge=1),
    per_page: int = Query(PAGE_SIZE, alias="per_page"),
    conn: sqlite3.Connection = Depends(get_conn),
):
    q = q.strip()
    view = view if view in ("grid", "list") else "grid"
    page_size = per_page if per_page in PER_PAGE_OPTIONS else PAGE_SIZE
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

    page = min(page, MAX_PAGES)   # before the query: the offset is the cost
    result = queries.search(conn, filters, page, page_size)
    rows = result.rows
    facets = queries.compute_facets(conn)
    lists_map = queries.lists_map(conn, rows)
    total_indexed = queries.total_works(conn)

    total = result.total
    pages = min(max(1, (total + page_size - 1) // page_size), MAX_PAGES)
    list_labels = {lst["slug"]: lst["name"] for lst in facets["lists"]}

    state = {"q": q, "format": format_, "language": language, "genre": genre,
             "publisher": publisher, "author": author, "list": lists_,
             "ereader": ereader if ereader == "1" else "", "sort": sort,
             "year_from": year_from if yf is not None else "",
             "year_to": year_to if yt is not None else "",
             "view": view if view == "list" else "",   # empty = default grid (clean URLs)
             "per_page": str(page_size) if page_size != PAGE_SIZE else ""}

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
        chips.append({"label": list_labels.get(slug, slug), "icon": "list",
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

    # WebSite structured data belongs on the home page only, and only on the bare
    # browse: every ?-carrying variant is noindex and robots-disallowed, so marking
    # those up as "the site" too would just hand Search conflicting copies. Matches
    # the rule the cache middleware already uses for the home page.
    is_home = not (q or chips) and page == 1 and not request.url.query
    return _templates.TemplateResponse(request, "search.html", {
        "books": rows, "total": total, "total_indexed": total_indexed, "q": q,
        "total_rounded": _rounded(total_indexed),
        "meta_description": (
            f"Doorzoek {_rounded(total_indexed)} e-books en luisterboeken van de online "
            "Bibliotheek. Filter op genre, auteur, taal en jaar, en zie meteen of een "
            "titel op je e-reader past."),
        "jsonld": ({"@context": "https://schema.org", "@type": "WebSite",
                    "name": seo.SITE_NAME,
                    "alternateName": ["Online Bibliotheek Zoekgids",
                                      "Catalogus online Bibliotheek"],
                    "url": seo.origin(request) + "/",
                    "inLanguage": "nl-NL"} if is_home else None),
        "format": format_, "language": language, "genre": genre,
        "publisher": publisher, "author": author, "list": lists_, "ereader": ereader,
        "year_from": state["year_from"], "year_to": state["year_to"], "sort": sort,
        "page": page, "pages": pages, "facets": facets, "page_size": page_size,
        "view": view, "per_page_options": list(PER_PAGE_OPTIONS),
        "chips": chips, "has_filters": bool(q or chips), "state": state,
        "robots": "noindex,follow" if (q or chips) else "index,follow",
        "lists_map": lists_map,
        "list_options": [lst["slug"] for lst in facets["lists"]],
        "list_labels": list_labels,
    })


# --------------------------------------------------------------------------- #
# detail / browse pages
# --------------------------------------------------------------------------- #
@app.get("/series/{name:path}", response_class=HTMLResponse)
def series_page(request: Request, name: str, conn: sqlite3.Connection = Depends(get_conn)):
    """Series page, addressed by slug: /series/het-mysterie.

    Like the author pages, the encoded-name URLs keep working and redirect.
    """
    slug = slugify(name)
    entry = queries.series_row(conn, slug)
    if entry:
        if name != slug:
            return RedirectResponse(f"/series/{slug}", status_code=301)
        name = entry["name"]
    rows = queries.series_books(conn, slug)
    if not rows:
        return _not_found(request, "series", _slug_words(name))
    return _templates.TemplateResponse(request, "series.html", {
        "name": name, "books": rows, "total": len(rows),
        "breadcrumbs": seo.breadcrumbs(request, (f"Reeks {name}", "")),
        "meta_description": f"Alle {len(rows)} delen van de reeks {name} in de online "
                            f"Bibliotheek — op volgorde, met e-book en luisterboek."})


@app.get("/stats", response_class=HTMLResponse)
def stats_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    data = queries.web_stats(conn)
    return _templates.TemplateResponse(request, "stats.html", {
        "s": data, "breadcrumbs": seo.breadcrumbs(request, ("Statistieken", ""))})


@app.get("/about", response_class=HTMLResponse)
def about(request: Request):
    """Static 'about' page — independent of the catalog DB so it always renders."""
    return _templates.TemplateResponse(request, "about.html", {
        "breadcrumbs": seo.breadcrumbs(request, ("Over deze catalogus", ""))})


# /over shipped in v1.1.2 and is in the live sitemap, so unlike the other URLs
# renamed alongside it this one owes a permanent redirect.
# Both spellings, so /over/ lands on /about in one hop instead of taking
# Starlette's trailing-slash 307 first.
@app.get("/over", include_in_schema=False)
@app.get("/over/", include_in_schema=False)
def about_legacy():
    return RedirectResponse("/about", status_code=301)


@app.get("/genres", response_class=HTMLResponse)
def genres_index(request: Request, publiek: str = AUDIENCES[0][0],
                 conn: sqlite3.Connection = Depends(get_conn)):
    """Hub over the genre pages, one audience at a time.

    Jeugd and volwassenen are separate taxonomies, and stacking both made a page
    of 364 genres — a toggle shows the one you are actually browsing. Same shape
    as the author hub's sort toggle: the clean path stays canonical, and the
    ?-variant is robots-disallowed, so it adds no crawlable duplicate.
    """
    trees = {key: queries.genre_tree(conn, key) for key, _ in AUDIENCES}
    total = len(queries.genre_pages(conn))
    # One section per audience: a subgenre sits under the parent that is right for
    # *that* shelf, so "Avontuur" appears under Spanning & Avontuur for jeugd and
    # under Spanning & Thrillers for volwassenen — as the catalog actually files it.
    audiences = [{"key": key, "label": label, "tops": len(trees[key]),
                  "total": sum(1 + len(g["children"]) for g in trees[key])}
                 for key, label in AUDIENCES if trees[key]]
    keys = [a["key"] for a in audiences]
    publiek = publiek if publiek in keys else (keys[0] if keys else "")
    shown = next((a for a in audiences if a["key"] == publiek), None)
    return _templates.TemplateResponse(request, "genres.html", {
        "audiences": audiences, "publiek": publiek, "shown": shown,
        "genres": trees.get(publiek, []), "total": total,
        "breadcrumbs": seo.breadcrumbs(request, ("Genres", "")),
        "meta_description": f"Alle {_nlnum(total)} genres in de online "
                            f"Bibliotheek, apart voor volwassenen en jeugd — met "
                            f"het aantal e-books en luisterboeken per genre."})


@app.get("/genre/{slug}", response_class=HTMLResponse)
def genre_page(request: Request, slug: str,
               conn: sqlite3.Connection = Depends(get_conn)):
    entry = queries.genre_page(conn, slugify(slug))
    if entry is None:
        return _not_found(request, "genre", _slug_words(slug))
    if slug != slugify(slug):   # one canonical spelling per genre
        return RedirectResponse(f"/genre/{slugify(slug)}", status_code=301)
    name = entry["name"]
    parent = queries.genre_page(conn, entry["parent_slug"]) if entry["parent_slug"] else None
    return _browse_page(
        request, conn, heading=name,
        lead=f"Alle titels in het genre {name} uit de collectie van de online "
             f"Bibliotheek.",
        filters=queries.SearchFilters(
            genres=tuple(queries.genre_spellings(conn, slugify(slug))), sort="year_desc"),
        search_url=f"/?genre={quote(name, safe='')}",
        crumb=("Genres", "/genres"),
        parent=({"name": parent["name"], "slug": entry["parent_slug"]} if parent else None),
        children=[{"name": c["name"], "slug": c["slug"], "titles": c["titles"]}
                  for c in queries.genre_children(conn, slugify(slug))])


# The format landing pages, back and honest this time. They were removed in #27
# because they counted editions as titles and showed the same work up to four
# times; ``?format=`` is a work-level flag now ("available as"), so the count on
# the page and the cards below it are the same books.
@app.get("/e-books", response_class=HTMLResponse)
def ebooks_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return _browse_page(
        request, conn, heading="E-books",
        lead="Alle e-books uit de collectie van de online Bibliotheek.",
        filters=queries.SearchFilters(format="ebook", sort="year_desc"),
        search_url="/?format=ebook", crumb=None)


@app.get("/luisterboeken", response_class=HTMLResponse)
def audiobooks_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return _browse_page(
        request, conn, heading="Luisterboeken",
        lead="Alle luisterboeken uit de collectie van de online Bibliotheek.",
        filters=queries.SearchFilters(format="audiobook", sort="year_desc"),
        search_url="/?format=audiobook", crumb=None)


@app.get("/authors", response_class=HTMLResponse)
def authors_index(request: Request, sort: str = BY_SURNAME,
                  conn: sqlite3.Connection = Depends(get_conn)):
    """A-Z hub over the author pages.

    Author pages used to hang off individual book pages only, which left ~10k of
    the site's most search-worthy pages ("boeken van X") several clicks deep and
    out of every sitemap.
    """
    sort = sort if sort in AUTHOR_SORTS else BY_SURNAME
    # The hub renders 27 counts and no authors, so it reads 27 counts.
    counts = indexes.letter_counts(conn, sort)
    letters = indexes.letter_order(counts)
    return _templates.TemplateResponse(request, "authors.html", {
        "letters": letters, "letter": "", "authors": [],
        "total": (total := sum(counts.values())), "sort": sort,
        "counts": counts,
        "meta_description": f"Blader alfabetisch door alle {_nlnum(total)} auteurs "
                            f"in de online Bibliotheek — e-books en luisterboeken."})


@app.get("/authors/{letter}", response_class=HTMLResponse)
def authors_letter(request: Request, letter: str, sort: str = BY_SURNAME,
                   conn: sqlite3.Connection = Depends(get_conn)):
    sort = sort if sort in AUTHOR_SORTS else BY_SURNAME
    counts = indexes.letter_counts(conn, sort)
    key = letter.upper() if len(letter) == 1 else letter.lower()
    if key not in counts:
        return _not_found(request, "letter")
    # One canonical spelling per letter, so /authors/A and /authors/a don't become
    # two URLs with the same content.
    canonical = key.lower()
    if letter != canonical:
        return RedirectResponse(f"/authors/{canonical}" +
                                (f"?sort={sort}" if sort != BY_SURNAME else ""),
                                status_code=301)
    rows = indexes.authors_in_letter(conn, key, sort)
    label = key.upper() if key != indexes.OTHER_LETTER else "Overig"
    return _templates.TemplateResponse(request, "authors.html", {
        "letters": indexes.letter_order(counts), "letter": key, "label": label,
        "authors": rows, "total": len(rows), "sort": sort,
        "counts": counts,
        "meta_description": f"{_nlnum(len(rows))} auteurs waarvan de naam met {label} "
                            f"begint, met al hun e-books en luisterboeken in de online "
                            f"Bibliotheek."})


# ``:path`` because two catalog authors carry a slash in their name ("Elizabeth
# August/Dreamshield"). With a plain ``{name}`` their page 404s under either
# spelling — both the link on the book page and the breadcrumb item URL would
# have pointed at a dead URL.
@app.get("/author/{name:path}", response_class=HTMLResponse)
def author_page(request: Request, name: str, conn: sqlite3.Connection = Depends(get_conn)):
    """Author page, addressed by slug: /author/lisbeth-imbo.

    Anything that folds to the same key lands here, so the old percent-encoded
    ``/author/Lisbeth%20Imbo`` links keep working and redirect to the slug.
    """
    slug = slugify(name)
    if slug:
        display = queries.author_display_name(conn, slug.replace("-", " "))
        rows = queries.author_books_by_fold(conn, slug.replace("-", " ")) if display else []
        if rows and name != slug:
            return RedirectResponse(f"/author/{slug}", status_code=301)
        name = display or name
    else:
        # Names with no Latin characters (Greek script, a stray "|" row) fold to
        # nothing, so they have no slug and stay on their encoded URL.
        rows = queries.author_books(conn, name)
    if not rows:
        return _not_found(request, "author", _slug_words(name))
    lists_map = queries.lists_map(conn, rows)
    # distinct lists/awards across this author's books (newest year first)
    seen, author_lists = set(), []
    for entries in lists_map.values():
        for e in entries:
            if e["slug"] not in seen:
                seen.add(e["slug"])
                author_lists.append(e)
    author_lists.sort(key=lambda e: -(e.get("year") or 0))
    return _templates.TemplateResponse(request, "author.html", {
        "name": name, "books": rows, "total": len(rows), "lists_map": lists_map,
        "author_lists": author_lists, "bio": author_bio(name),
        "breadcrumbs": seo.breadcrumbs(request, (name, "")),
        "meta_description": f"Alle {len(rows)} titels van {name} in de online "
                            f"Bibliotheek — e-books en luisterboeken."})


@app.get("/lists", response_class=HTMLResponse)
def lists_overview(request: Request, sort: str = "name",
                   conn: sqlite3.Connection = Depends(get_conn)):
    if sort not in queries.LIST_SORTS:
        sort = "name"
    rows = queries.lists_overview(conn, sort)
    return _templates.TemplateResponse(request, "lists.html", {
        "lists": rows, "sort": sort,
        "breadcrumbs": seo.breadcrumbs(request, ("Lijsten", ""))})


@app.get("/list/{slug}", response_class=HTMLResponse)
def list_detail(request: Request, slug: str, show: str = "",
                conn: sqlite3.Connection = Depends(get_conn)):
    lst = queries.list_row(conn, slug)
    if lst is None:
        return _not_found(request, "list", _slug_words(slug))
    rows = queries.list_items(conn, lst["id"])
    total = len(rows)
    available = sum(1 for i in rows if i["ppn"])
    if show == "available":
        items = [i for i in rows if i["ppn"]]
    elif show == "unavailable":
        items = [i for i in rows if not i["ppn"]]
    else:
        show, items = "", rows
    return _templates.TemplateResponse(request, "list_detail.html", {
        "lst": lst, "items": items, "available": available, "total": total, "show": show,
        "breadcrumbs": seo.breadcrumbs(request, ("Lijsten", "/lists"), (lst["name"], "")),
        "meta_description": (lst["description"] or f"De lijst {lst['name']}")
                            + f" — {available} van {total} titels in de bibliotheek."})


# One canonical URL per book: /boek/{titel}--{auteur}--{id}. Both old
# /book/{ppn} URLs — the e-book's and the audiobook's — 301 here, and a stale slug
# 301s to the current one, because the id is the truth and the slug is cosmetic.
@app.get("/boek/{rest:path}", response_class=HTMLResponse)
def book_page(request: Request, rest: str, conn: sqlite3.Connection = Depends(get_conn)):
    """The book page. ``rest`` is ``{title}--{author}--{id}``; the id is everything
    after the **last** ``--`` (a bare id has none), which is unambiguous because our
    own slugs never contain a double hyphen."""
    work_id = rest.rsplit("--", 1)[-1]
    detail = queries.book_detail(conn, work_id)
    if detail is None:
        # A PPN carries no readable words, so nothing seeds the search box here.
        return _not_found(request, "book")
    if "redirect" in detail:   # an edition's PPN used as the id
        ref = queries.work_ref(conn, detail["redirect"])
        if ref is None:
            return _not_found(request, "book")
        return RedirectResponse(seo.book_href(ref[1], ref[0]), status_code=301)
    b = detail["work"]
    canonical = seo.book_path(b)
    if rest != canonical.removeprefix("/boek/"):   # wrong or stale slug
        return RedirectResponse(canonical, status_code=301)

    editions = detail["editions"]
    summary = (b["summary"] or "").strip()
    cover = _coverw(b["cover_url"], 400)
    # schema.org/Book structured data. Google itself only reads Book markup from
    # an onboarded feed, but Bing and LLM crawlers parse this, so it's worth
    # keeping correct: a BCP 47 code rather than the Dutch language name, and a
    # tidied blurb instead of the raw quote-wrapped, line-broken catalog text.
    #
    # One Book for the work with a workExample per edition — the pattern
    # schema.org documents for exactly this — replacing the two competing Book
    # entities that each claimed the same title. No duration: the catalog stores it
    # as "6 uur", schema.org wants ISO-8601, and a guessed value is worse than none.
    jsonld = {"@context": "https://schema.org", "@type": "Book", "name": b["title"],
              "author": [{"@type": "Person", "name": a} for a in detail["authors"]] or None,
              "inLanguage": language_code(b["language"]),
              "publisher": b["publisher"],
              "datePublished": str(b["year"]) if b["year"] else None,
              "image": cover or None, "description": _snippet(summary, 1000) or None,
              "url": seo.origin(request) + canonical,
              "workExample": [_edition_jsonld(e) for e in editions]}
    jsonld = {k: v for k, v in jsonld.items() if v}
    # "meer zoals dit": LSA content-based recommendations (see obc.similar), shown as
    # a horizontal scroll strip on the book page.
    similar = queries.similar_books(conn, b["work_id"], limit=20)
    return _templates.TemplateResponse(request, "book.html", {
        "b": b, "editions": editions, "genres": detail["genres"],
        "authors": detail["authors"], "work_lists": detail["work_lists"],
        "similar": similar,
        "meta_description": _snippet(summary) or f"{b['title']} in de online Bibliotheek.",
        "og_image": cover, "jsonld": jsonld,
        "breadcrumbs": seo.breadcrumbs(
            request,
            *([(detail["authors"][0], seo.author_path(detail["authors"][0]))]
              if detail["authors"] else []),
            (b["title"], ""))})


def _edition_jsonld(edition) -> dict:
    """One ``workExample``: what distinguishes this edition from its twin."""
    data = {
        "@type": "Book",
        "bookFormat": ("https://schema.org/AudiobookFormat"
                       if edition["format"] == "audiobook" else "https://schema.org/EBook"),
        "isbn": edition["isbn"],
        "numberOfPages": edition["pages"],
        "url": edition["url"],
    }
    return {k: v for k, v in data.items() if v}


# Kept forever: ~68k of these URLs are indexed, and both editions of a twinned
# title point at one of them.
@app.get("/book/{ppn}", include_in_schema=False)
def book_legacy(request: Request, ppn: str, conn: sqlite3.Connection = Depends(get_conn)):
    ref = queries.work_ref(conn, ppn)
    if ref is None:
        return _not_found(request, "book")
    work_id, slug = ref
    return RedirectResponse(seo.book_href(slug, work_id), status_code=301)


# --------------------------------------------------------------------------- #
# JSON endpoints (autocomplete + searchable facets)
# --------------------------------------------------------------------------- #
@app.get("/suggest")
def suggest(q: str = "", limit: int = Query(7, ge=1, le=20),
            conn: sqlite3.Connection = Depends(get_conn)):
    """Autocomplete: matching titles (-> book) and authors/publishers/… (-> search)."""
    data = queries.suggest(conn, q.strip(), limit)
    if data is None:
        return {"titles": [], "authors": []}
    # editions per suggested work, so the dropdown can show an e-book / audiobook
    # badge per format the book exists in — the flags ride on the work row now, so
    # no extra query. The JSON shape is unchanged (base.html's dropdown JS is
    # untouched); ``url`` is new, so a dropdown click lands on the canonical path
    # instead of eating a 301 on the way.
    titles = [
        {"ppn": r["work_id"], "title": r["title"], "author": r["author"],
         "cover_url": _coverw(r["cover_url"], 80), "format": r["format"],
         "url": seo.book_path(r),
         "editions": {fmt: ppn for fmt, ppn in (("ebook", r["ebook_ppn"]),
                                                ("audiobook", r["audiobook_ppn"])) if ppn}}
        for r in data["title_rows"]
    ]
    return {"titles": titles, "authors": data["authors"],
            "publishers": data["publishers"], "genres": data["genres"],
            "languages": data["languages"], "lists": data["lists"]}


@app.get("/facet")
def facet(kind: str = Query("", alias="type"), q: str = "",
          limit: int = Query(30, ge=1, le=50),
          conn: sqlite3.Connection = Depends(get_conn)):
    """Searchable facet values (for large facets like author/publisher)."""
    values = queries.facet_values(conn, kind, q, limit)
    return {"values": values}


# --------------------------------------------------------------------------- #
# HEAD
# --------------------------------------------------------------------------- #
# Starlette's plain Route adds HEAD alongside GET; FastAPI's APIRoute does not, so
# every page answered `HEAD` with 405. Link checkers, uptime monitors and some
# crawlers probe with HEAD first and read that as a broken URL.
#
# Each GET gets a *twin* HEAD route rather than an extra method on the original.
# APIRoute freezes its `unique_id` at construction, so widening `methods`
# afterwards would publish a second OpenAPI operation carrying the GET's
# operationId — ten duplicate IDs in /openapi.json. The twins are kept out of the
# schema entirely, which is also the truthful description: HEAD is transport
# plumbing here, not a distinct operation. The handler still runs; the server
# drops the body off the response.
#
# The crawler routes are copied onto the app one by one rather than with
# `include_router`, and before the twinning below. FastAPI keeps an included
# router in `app.routes` as a single opaque container, so the pass below cannot
# see into it — robots.txt and the sitemaps would go on answering HEAD with 405,
# and those are the first URLs a checker probes.
for _route in list(seo.router.routes):
    if isinstance(_route, APIRoute):
        app.add_api_route(
            _route.path, _route.endpoint, methods=list(_route.methods or ()),
            include_in_schema=False, name=_route.name,
            response_class=_route.response_class)

for _route in list(app.routes):
    if isinstance(_route, APIRoute) and "GET" in (_route.methods or ()):
        app.add_api_route(
            _route.path, _route.endpoint, methods=["HEAD"], include_in_schema=False,
            name=_route.name, response_class=_route.response_class,
            dependencies=_route.dependencies)
