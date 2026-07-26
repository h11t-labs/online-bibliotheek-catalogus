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

import collections
import datetime
import os
import re
import sqlite3
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, unquote, urlencode

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.routing import APIRoute
from fastapi.templating import Jinja2Templates

from .. import db
from ..textnorm import language_code, slugify, surname_key
from . import queries
from .bio import author_bio

DB_PATH = Path(os.environ.get("OBC_DB", db.DEFAULT_DB))
PAGE_SIZE = 24
PER_PAGE_OPTIONS = (12, 24, 48, 96)  # selectable items-per-page (PAGE_SIZE is the default)
# Absolute site origin for canonical/OG/sitemap URLs (e.g. https://…fly.dev). Empty
# locally → those fall back to the request's own base URL.
SITE_URL = os.environ.get("OBC_SITE_URL", "").rstrip("/")
SITEMAP_PAGE = 45000  # book URLs per sitemap file (under the 50k/file limit)
# The brand Google should print above the result instead of the bare domain. It
# is fed to Search through WebSite structured data and og:site_name — the two
# strongest signals, see https://developers.google.com/search/docs/appearance/site-names
SITE_NAME = "Online Bibliotheek Catalogus"

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


def _author_path(name: str) -> str:
    """The canonical URL path for an author: ``/author/lisbeth-imbo``.

    A handful of names (Greek script, a stray "|" row) hold no Latin characters
    and fold to nothing, so they have no slug and keep their encoded name. One
    helper so links, breadcrumbs and sitemaps can never drift apart.
    """
    return f"/author/{slugify(name) or quote(name, safe='')}"


def _series_path(name: str) -> str:
    """Canonical URL path for a series, mirroring :func:`_author_path`."""
    return f"/series/{slugify(name) or quote(name, safe='')}"


def _genre_path(name: str) -> str:
    """Canonical URL path for a genre, mirroring :func:`_author_path`."""
    return f"/genre/{slugify(name) or quote(name, safe='')}"


def _nlnum(n: int) -> str:
    """9803 -> '9.803' (Dutch thousands separator)."""
    return f"{n:,}".replace(",", ".")


def _data_updated() -> float | None:
    """Epoch seconds the catalog was last (re)built — the DB file's mtime."""
    try:
        return DB_PATH.stat().st_mtime
    except OSError:
        return None


_templates.env.filters["coverw"] = _coverw
_templates.env.filters["nldate"] = _nldate
_templates.env.filters["author_path"] = _author_path
_templates.env.filters["series_path"] = _series_path
_templates.env.filters["genre_path"] = _genre_path
_templates.env.filters["nlnum"] = _nlnum
_templates.env.globals["url_with"] = _url_with
_templates.env.globals["url_without"] = _url_without
_templates.env.globals["data_updated"] = _data_updated
_templates.env.globals["site_url"] = SITE_URL
_templates.env.globals["site_name"] = SITE_NAME

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
_CACHE_PREFIXES = ("/book/", "/author", "/series/", "/list", "/stats", "/about",
                   "/genre")


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
_CANONICAL_HOST = _hostname(SITE_URL) if SITE_URL else ""
# Health checks and the cron machine reach the app over the private network under
# the machine's own Host; those paths are never redirected.
_NO_REDIRECT = ("/healthz", "/admin/")


def _is_alias(host: str) -> bool:
    """Is ``host`` a known alias of the canonical one (so safe to 301 away)?"""
    host = _hostname(host)
    if not _CANONICAL_HOST or not host or host == _CANONICAL_HOST:
        return False
    return host == f"www.{_CANONICAL_HOST}" or host.endswith(".fly.dev")


@app.middleware("http")
async def _canonical_host(request: Request, call_next):
    if (_is_alias(request.headers.get("host", ""))
            and not request.url.path.startswith(_NO_REDIRECT)):
        query = f"?{request.url.query}" if request.url.query else ""
        return RedirectResponse(SITE_URL + request.url.path + query, status_code=301)
    return await call_next(request)


@app.middleware("http")
async def _response_headers(request: Request, call_next):
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
        elif any(path == p or path.startswith(p) for p in _CACHE_PREFIXES):
            response.headers["Cache-Control"] = "public, max-age=3600"
    return response


def get_conn():
    """Per-request read-only DB connection, always closed (FastAPI dependency).

    Reads the module-global DB_PATH at call time (tests monkeypatch app.DB_PATH),
    not captured at import. If the DB isn't there yet, connect_ro raises
    OperationalError here and the bootstrap-503 handler renders the friendly page.
    """
    conn = queries.connect_ro(DB_PATH)
    try:
        yield conn
    finally:
        conn.close()


@app.exception_handler(sqlite3.OperationalError)
async def _db_unavailable(request: Request, exc: sqlite3.OperationalError):
    """Friendly page when the catalog DB isn't present yet (e.g. fresh volume).

    Only bootstrap-state errors (missing DB file / core tables not built yet) get
    the friendly 503 page; genuine SQL bugs re-raise so they surface as 500s in
    development and monitoring instead of hiding behind "catalogus wordt opgebouwd".
    """
    msg = str(exc).lower()
    bootstrap_errors = (
        "unable to open database file",
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
        conn = queries.connect_ro(DB_PATH)
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
    out = [{"kind": "author", "icon": "author", "label": name, "url": _author_path(name)}
           for name in data["authors"]]
    out += [{"kind": "list", "icon": "list", "label": lst["name"],
             "url": f"/list/{lst['slug']}"} for lst in data["lists"]]
    # A genre has no page of its own; it is a filter on the browse view.
    out += [{"kind": "genre", "icon": "genre", "label": name,
             "url": f"/?genre={quote(name, safe='')}"} for name in data["genres"]]
    if titles:
        out += [{"kind": "book", "icon": "book", "label": row["title"] or "—",
                 "sub": row["author"] or "", "url": f"/book/{row['ppn']}"}
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


# Facet values are identical for every request, so cache them and only recompute
# when the database file changes (i.e. after a normalize).
# Each of these is rebuilt from scratch when the catalog file changes, and the
# genre one walks 157k rows. Without a lock every concurrent cold request builds
# its own copy: eight simultaneous hits on /genres took 23s each and pushed the
# process to 606 MB — on a 512 MB machine. The lock makes one thread build while
# the others wait for its result.
_facets_cache: dict = {"key": None, "data": None}
_facets_cache_lock = threading.Lock()


def _facets(conn: sqlite3.Connection) -> dict:
    try:
        key = DB_PATH.stat().st_mtime_ns
    except OSError:
        key = None
    if _facets_cache["key"] == key and _facets_cache["data"] is not None:
        return _facets_cache["data"]
    with _facets_cache_lock:
        if _facets_cache["key"] == key and _facets_cache["data"] is not None:
            return _facets_cache["data"]
        data = queries.compute_facets(conn)
        _facets_cache.update(key=key, data=data)
        return data


# The A-Z author index is ~10k rows and identical for every visitor, so it is built
# once per catalog rebuild — same DB-mtime trick as the facet cache above.
_authors_cache: dict = {"key": None, "data": None}
_authors_cache_lock = threading.Lock()
_OTHER_LETTER = "overig"  # names that don't start with a plain A-Z letter


# Two ways to alphabetise a name index, both defensible: readers hunting a known
# writer look under the surname, readers browsing recognise the whole name.
BY_SURNAME, BY_FIRST = "achternaam", "voornaam"
AUTHOR_SORTS = (BY_SURNAME, BY_FIRST)


def _author_letter(name: str, by: str = BY_SURNAME) -> str:
    """Bucket an author under a letter, or the catch-all."""
    key = surname_key(name) if by == BY_SURNAME else slugify(name)
    first = key[:1].upper()
    return first if "A" <= first <= "Z" else _OTHER_LETTER


def _author_index(conn: sqlite3.Connection,
                  by: str = BY_SURNAME) -> dict[str, list[dict]]:
    """``{"A": [{name, titles}…], …, "overig": […]}`` — every author, bucketed.

    Spelling variants are folded together first, because that is what the slug URL
    does: listing "Ad Van Schaik" and "Ad van Schaik" as two entries pointing at
    the same page would be a lie the hub tells about itself.

    Every author is listed, including the 13k with a single title. The
    MIN_INDEXABLE_TITLES rule is about what the *sitemap* promotes, not about what
    a reader is allowed to find — a browsable index that silently omits more than
    half the authors is simply broken.
    """
    try:
        key = DB_PATH.stat().st_mtime_ns
    except OSError:
        key = None
    cached = _authors_cache["data"]
    if _authors_cache["key"] == key and cached is not None and by in cached:
        return cached[by]
    with _authors_cache_lock:
        if _authors_cache["key"] != key or _authors_cache["data"] is None:
            _authors_cache.update(key=key, data={})
        cached = _authors_cache["data"]
        if by in cached:
            return cached[by]
        counts = queries.author_title_counts(conn)
        merged: dict[str, dict] = {}
        for row in queries.author_index(conn):
            # A name with no Latin characters at all folds to "" and has no slug, so it
            # cannot be a hub or sitemap entry — and merging on that empty key would
            # fuse unrelated authors into one. Those keep their own encoded-name page.
            if not row["fold"]:
                continue
            # rows arrive title-count descending within a fold, so the first spelling
            # seen for a key is the one that carries the most titles
            merged.setdefault(row["fold"],
                              {"name": row["name"], "titles": counts.get(row["fold"], 0)})
        sort_key = surname_key if by == BY_SURNAME else slugify
        buckets: dict[str, list[dict]] = {}
        for entry in merged.values():
            buckets.setdefault(_author_letter(entry["name"], by), []).append(entry)
        # the chosen key first, then the whole name, so a letter page reads as an index
        for rows in buckets.values():
            rows.sort(key=lambda e: (sort_key(e["name"]), slugify(e["name"])))
        cached[by] = buckets
        return buckets


# Series get the same slug treatment as authors, but `books.series` is free text
# with no folded column to look up, so the slug -> spellings map is built once per
# catalog rebuild. 18 slugs cover more than one spelling ("De Stad" / "De stad");
# those share a page rather than splitting the shelf in two.
_series_cache: dict = {"key": None, "data": None}
_series_cache_lock = threading.Lock()


def _series_index(conn: sqlite3.Connection) -> dict[str, dict]:
    """``{slug: {"name": display, "names": (spellings…), "titles": n}}``."""
    try:
        key = DB_PATH.stat().st_mtime_ns
    except OSError:
        key = None
    if _series_cache["key"] == key and _series_cache["data"] is not None:
        return _series_cache["data"]
    with _series_cache_lock:
        if _series_cache["key"] == key and _series_cache["data"] is not None:
            return _series_cache["data"]
        merged: dict[str, dict] = {}
        for row in queries.series_index(conn):
            slug = slugify(row["name"])
            if not slug:
                continue
            # rows arrive part-count descending, so the first spelling wins the heading
            entry = merged.setdefault(slug, {"name": row["name"], "names": [], "titles": 0})
            entry["names"].append(row["name"])
            entry["titles"] += row["titles"]
        _series_cache.update(key=key, data=merged)
        return merged


def _letter_order(index: dict) -> list[str]:
    """A-Z first, the catch-all last."""
    return sorted(k for k in index if k != _OTHER_LETTER) + \
        ([_OTHER_LETTER] if _OTHER_LETTER in index else [])


# The catalog carries two taxonomies, not one: jeugd and volwassenen reuse genre
# names under different parents, and 67 of 213 subgenres sit somewhere different
# depending on which shelf you are standing at. Flattening them picked a winner
# and misfiled the loser, so the hub renders a tree per audience while the genre
# page itself stays a single URL covering both.
AUDIENCES = (("volwassenen", "Volwassenen"), ("jeugd", "Jeugd"))
_genres_cache: dict = {"key": None, "data": None}
_genres_cache_lock = threading.Lock()


def _genre_data(conn: sqlite3.Connection) -> dict:
    """``{"flat": {slug: entry}, "trees": {audience: [top entries]}}``, cached.

    ``flat`` backs the genre page and the sitemap — one entry per slug, counted
    over distinct books because spelling variants share them (the catalog holds
    "Biografieën" twice, precomposed and with a combining diaeresis). ``trees`` is
    the hub's view: per audience, top genres carrying their own children.
    """
    try:
        key = DB_PATH.stat().st_mtime_ns
    except OSError:
        key = None
    if _genres_cache["key"] == key and _genres_cache["data"] is not None:
        return _genres_cache["data"]
    with _genres_cache_lock:
        if _genres_cache["key"] == key and _genres_cache["data"] is not None:
            return _genres_cache["data"]

        flat: dict[str, dict] = {}
        per_aud: dict[str, dict[str, dict]] = {a: {} for a, _ in AUDIENCES}
        for row in queries.genre_books(conn):
            slug = slugify(row["name"])
            if not slug:
                continue
            entry = flat.setdefault(slug, {"name": row["name"], "names": [], "books": set(),
                                           "parents": collections.Counter()})
            if row["name"] not in entry["names"]:
                entry["names"].append(row["name"])
            entry["books"].add(row["ppn"])
            pslug = slugify(row["parent"] or "")
            entry["parents"][pslug] += 1
            # 2.567 books carry no audience, and a catalog built without the detail
            # pass has none at all — those land on the default shelf rather than
            # falling out of the hub entirely. No genre in the live catalog is
            # reachable *only* that way, so this shifts counts, never visibility.
            aud = per_aud[row["audience"] if row["audience"] in per_aud else AUDIENCES[0][0]]
            a = aud.setdefault(slug, {"books": set(), "parents": collections.Counter()})
            a["books"].add(row["ppn"])
            a["parents"][pslug] += 1

        for entry in flat.values():
            parent = entry["parents"].most_common(1)[0][0]
            entry["parent"] = parent if parent and parent != slugify(entry["name"]) else ""
            entry["titles"] = len(entry.pop("books"))
            entry["name"] = max(entry["names"], key=len)
            del entry["parents"]
        for slug, entry in flat.items():
            entry["children"] = sorted(
                (c for c, o in flat.items() if o["parent"] == slug),
                key=lambda c: (-flat[c]["titles"], c))

        trees: dict[str, list[dict]] = {}
        for aud, _label in AUDIENCES:
            rows = per_aud[aud]
            parent_of, titles_of = {}, {}
            for slug, a in rows.items():
                # A tree may only state what its own audience files. Borrowing the
                # catalog-wide parent when this audience names none put "Film" under
                # Kunst & Cultuur for adults, where all three adult records carry no
                # parent at all — a relationship the data never asserts. An explicit
                # parent still outranks a bare "top level" *within* the audience.
                named = [(pslug, n) for pslug, n in a["parents"].items()
                         if pslug and pslug != slug and pslug in rows]
                parent_of[slug] = max(named, key=lambda kv: kv[1])[0] if named else ""
                titles_of[slug] = len(a["books"])
            tops = []
            for slug in rows:
                if parent_of[slug]:
                    continue
                kids = sorted((c for c in rows if parent_of[c] == slug),
                              key=lambda c: -titles_of[c])
                tops.append({"name": flat[slug]["name"], "slug": slug,
                             "titles": titles_of[slug],
                             "children": [{"name": flat[c]["name"], "slug": c,
                                           "titles": titles_of[c]} for c in kids]})
            trees[aud] = sorted(tops, key=lambda g: (-g["titles"], g["name"].lower()))

        data = {"flat": flat, "trees": trees}
        _genres_cache.update(key=key, data=data)
        return data


def _genres(conn: sqlite3.Connection) -> dict[str, dict]:
    """``{slug: {name, names, titles, parent, children}}`` across both audiences."""
    return _genre_data(conn)["flat"]


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
        "formats_map": queries.formats_map(conn, result.rows),
        "lists_map": queries.lists_map(conn, result.rows),
        "parent": parent, "children": children or [],
        "breadcrumbs": _breadcrumbs(
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

    result = queries.search(conn, filters, page, page_size)
    rows = result.rows
    facets = _facets(conn)
    editions_map = queries.editions_map(conn, rows)
    lists_map = queries.lists_map(conn, rows)
    total_indexed = queries.total_books(conn)

    total = result.total
    pages = max(1, (total + page_size - 1) // page_size)
    list_names = {lst["slug"]: lst["name"] for lst in facets["lists"]}

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
                    "name": SITE_NAME,
                    "alternateName": ["Online Bibliotheek Zoekgids",
                                      "Catalogus online Bibliotheek"],
                    "url": _origin(request) + "/",
                    "inLanguage": "nl-NL"} if is_home else None),
        "format": format_, "language": language, "genre": genre,
        "publisher": publisher, "author": author, "list": lists_, "ereader": ereader,
        "year_from": state["year_from"], "year_to": state["year_to"], "sort": sort,
        "page": page, "pages": pages, "facets": facets, "page_size": page_size,
        "view": view, "per_page_options": list(PER_PAGE_OPTIONS),
        "chips": chips, "has_filters": bool(q or chips), "state": state,
        "robots": "noindex,follow" if (q or chips) else "index,follow",
        "editions_map": editions_map, "lists_map": lists_map,
        "list_options": [lst["slug"] for lst in facets["lists"]],
        "list_labels": {lst["slug"]: lst["name"] for lst in facets["lists"]},
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
    entry = _series_index(conn).get(slug)
    if entry:
        if name != slug:
            return RedirectResponse(f"/series/{slug}", status_code=301)
        rows = queries.series_books(conn, tuple(entry["names"]))
        name = entry["name"]
    else:
        rows = queries.series_books(conn, (name,))
    if not rows:
        return _not_found(request, "series", _slug_words(name))
    formats_map = queries.formats_map(conn, rows)
    return _templates.TemplateResponse(request, "series.html", {
        "name": name, "books": rows, "total": len(rows), "formats_map": formats_map,
        "breadcrumbs": _breadcrumbs(request, (f"Reeks {name}", "")),
        "meta_description": f"Alle {len(rows)} delen van de reeks {name} in de online "
                            f"Bibliotheek — op volgorde, met e-book en luisterboek."})


@app.get("/stats", response_class=HTMLResponse)
def stats_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    data = queries.web_stats(conn)
    return _templates.TemplateResponse(request, "stats.html", {
        "s": data, "breadcrumbs": _breadcrumbs(request, ("Statistieken", ""))})


@app.get("/about", response_class=HTMLResponse)
def about(request: Request):
    """Static 'about' page — independent of the catalog DB so it always renders."""
    return _templates.TemplateResponse(request, "about.html", {
        "breadcrumbs": _breadcrumbs(request, ("Over deze catalogus", ""))})


# /over shipped in v1.1.2 and is in the live sitemap, so unlike the other URLs
# renamed alongside it this one owes a permanent redirect.
# Both spellings, so /over/ lands on /about in one hop instead of taking
# Starlette's trailing-slash 307 first.
@app.get("/over", include_in_schema=False)
@app.get("/over/", include_in_schema=False)
def about_legacy():
    return RedirectResponse("/about", status_code=301)


# --------------------------------------------------------------------------- #
# SEO: robots.txt + (paginated) sitemap
# --------------------------------------------------------------------------- #
def _xml_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _origin(request: Request) -> str:
    return SITE_URL or str(request.base_url).rstrip("/")


def _breadcrumbs(request: Request, *trail: tuple[str, str]) -> dict:
    """schema.org BreadcrumbList from ``(label, path)`` pairs, Home prepended.

    Lets Search print a readable trail ("Home › Auteur › Titel") in place of the
    raw URL. The final crumb is the page you're on, so it gets no ``item`` link
    — pass an empty path for it.
    """
    base = _origin(request)
    items = [{"@type": "ListItem", "position": 1, "name": "Home", "item": base + "/"}]
    for pos, (label, path) in enumerate(trail, start=2):
        crumb = {"@type": "ListItem", "position": pos, "name": label}
        if path:
            crumb["item"] = base + path
        items.append(crumb)
    return {"@context": "https://schema.org", "@type": "BreadcrumbList",
            "itemListElement": items}


def _w3c(ts: float | None) -> str:
    """Epoch seconds as a W3C datetime for ``<lastmod>``; empty when unknown."""
    if not ts:
        return ""
    return datetime.datetime.fromtimestamp(ts, datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sitemap(base: str, paths: list[str], lastmod: str = "") -> Response:
    """A urlset for ``paths``.

    ``lastmod`` is opt-in per sitemap: search engines learn to ignore the hint
    when it's wrong, and the catalog tracks no per-record change date — stamping
    64k book URLs with "the day of the last rebuild" would be exactly that lie.
    The pages that genuinely re-render on every rebuild do get one.
    """
    mod = f"<lastmod>{lastmod}</lastmod>" if lastmod else ""
    locs = "".join(f"<url><loc>{_xml_escape(base + p)}</loc>{mod}</url>" for p in paths)
    body = ('<?xml version="1.0" encoding="UTF-8"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f"{locs}</urlset>")
    return Response(body, media_type="application/xml")


@app.get("/robots.txt", include_in_schema=False)
def robots_txt(request: Request):
    lines = ["User-agent: *",
             # Throttle bots — one small VM serving 68k pages. Google ignores
             # Crawl-delay, but Bing honours it literally: at 10s a full pass over
             # the catalog took more than a week, so most of it was never seen.
             # Detail pages are public-cacheable for an hour, so 1s is affordable.
             "Crawl-delay: 1",
             "Disallow: /suggest", "Disallow: /facet", "Disallow: /admin/",
             "Disallow: /*?",  # the infinite filtered-search URL space
             f"Sitemap: {_origin(request)}/sitemap.xml"]
    return Response("\n".join(lines) + "\n", media_type="text/plain")


@app.get("/sitemap.xml", include_in_schema=False)
def sitemap_index(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    total = queries.total_books(conn)
    base = _origin(request)
    pages = max(1, (total + SITEMAP_PAGE - 1) // SITEMAP_PAGE)
    maps = [f"{base}/sitemap-static.xml", f"{base}/sitemap-browse.xml",
            *[f"{base}/sitemap-books-{i}.xml" for i in range(1, pages + 1)]]
    # Every child sitemap is regenerated from the catalog, so the rebuild time is
    # an honest lastmod for the *files* even where it wouldn't be for their URLs.
    mod = _w3c(_data_updated())
    mod = f"<lastmod>{mod}</lastmod>" if mod else ""
    locs = "".join(f"<sitemap><loc>{_xml_escape(m)}</loc>{mod}</sitemap>" for m in maps)
    body = ('<?xml version="1.0" encoding="UTF-8"?>'
            '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f"{locs}</sitemapindex>")
    return Response(body, media_type="application/xml")


@app.get("/sitemap-static.xml", include_in_schema=False)
def sitemap_static(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    slugs = [r["slug"] for r in conn.execute("SELECT slug FROM lists ORDER BY slug")]
    paths = ["/", "/about", "/lists", "/stats", *[f"/list/{s}" for s in slugs]]
    # These really are rewritten by every rebuild (new titles, new list positions).
    return _sitemap(_origin(request), paths, lastmod=_w3c(_data_updated()))


@app.get("/sitemap-browse.xml", include_in_schema=False)
def sitemap_browse(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """The aggregation pages: the A-Z hub, author pages and series pages.

    These answer the queries the catalog is actually searched with ("boeken van
    X", "Y reeks op volgorde") and were in no sitemap at all. Only pages that
    aggregate two or more titles are listed — see queries.MIN_INDEXABLE_TITLES.
    """
    index = _author_index(conn)
    paths = ["/authors", "/genres"]
    paths += [f"/authors/{letter.lower()}" for letter in _letter_order(index)]
    # The hubs list everything; the sitemap only nominates pages that aggregate
    # something, so single-title pages stay reachable without being advertised as
    # destinations. Slugs, never encoded names: a sitemap full of URLs that
    # immediately 301 wastes the crawl budget it exists to spend well.
    paths += [_author_path(row["name"])
              for letter in _letter_order(index) for row in index[letter]
              if row["titles"] >= queries.MIN_INDEXABLE_TITLES]
    paths += [f"/series/{slug}" for slug, entry in sorted(_series_index(conn).items())
              if entry["titles"] >= queries.MIN_INDEXABLE_TITLES]
    paths += [f"/genre/{slug}" for slug, entry in sorted(_genres(conn).items())
              if entry["titles"] >= queries.MIN_INDEXABLE_TITLES]
    return _sitemap(_origin(request), paths)


@app.get("/sitemap-books-{n}.xml", include_in_schema=False)
def sitemap_books(request: Request, n: int, conn: sqlite3.Connection = Depends(get_conn)):
    rows = conn.execute("SELECT ppn FROM books ORDER BY ppn LIMIT ? OFFSET ?",
                        (SITEMAP_PAGE, (max(n, 1) - 1) * SITEMAP_PAGE)).fetchall()
    return _sitemap(_origin(request), [f"/book/{r['ppn']}" for r in rows])


@app.get("/genres", response_class=HTMLResponse)
def genres_index(request: Request, publiek: str = AUDIENCES[0][0],
                 conn: sqlite3.Connection = Depends(get_conn)):
    """Hub over the genre pages, one audience at a time.

    Jeugd and volwassenen are separate taxonomies, and stacking both made a page
    of 364 genres — a toggle shows the one you are actually browsing. Same shape
    as the author hub's sort toggle: the clean path stays canonical, and the
    ?-variant is robots-disallowed, so it adds no crawlable duplicate.
    """
    data = _genre_data(conn)
    index, trees = data["flat"], data["trees"]
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
        "genres": trees.get(publiek, []), "total": len(index),
        "breadcrumbs": _breadcrumbs(request, ("Genres", "")),
        "meta_description": f"Alle {_nlnum(len(index))} genres in de online "
                            f"Bibliotheek, apart voor volwassenen en jeugd — met "
                            f"het aantal e-books en luisterboeken per genre."})


@app.get("/genre/{slug}", response_class=HTMLResponse)
def genre_page(request: Request, slug: str,
               conn: sqlite3.Connection = Depends(get_conn)):
    entry = _genres(conn).get(slugify(slug))
    if entry is None:
        return HTMLResponse("<h1>Genre niet gevonden</h1>", status_code=404)
    if slug != slugify(slug):   # one canonical spelling per genre
        return RedirectResponse(f"/genre/{slugify(slug)}", status_code=301)
    index = _genres(conn)
    name = entry["name"]
    parent = index.get(entry["parent"]) if entry["parent"] else None
    return _browse_page(
        request, conn, heading=name,
        lead=f"Alle titels in het genre {name} uit de collectie van de online "
             f"Bibliotheek.",
        filters=queries.SearchFilters(genres=tuple(entry["names"]), sort="year_desc"),
        search_url=f"/?genre={quote(name, safe='')}",
        crumb=("Genres", "/genres"),
        parent=({"name": parent["name"], "slug": entry["parent"]} if parent else None),
        children=[{"name": index[c]["name"], "slug": c, "titles": index[c]["titles"]}
                  for c in entry["children"]])




@app.get("/authors", response_class=HTMLResponse)
def authors_index(request: Request, sort: str = BY_SURNAME,
                  conn: sqlite3.Connection = Depends(get_conn)):
    """A-Z hub over the author pages.

    Author pages used to hang off individual book pages only, which left ~10k of
    the site's most search-worthy pages ("boeken van X") several clicks deep and
    out of every sitemap.
    """
    sort = sort if sort in AUTHOR_SORTS else BY_SURNAME
    index = _author_index(conn, sort)
    letters = _letter_order(index)
    total = sum(len(rows) for rows in index.values())
    return _templates.TemplateResponse(request, "authors.html", {
        "letters": letters, "letter": "", "authors": [], "total": total, "sort": sort,
        "counts": {ltr: len(index[ltr]) for ltr in letters},
        "meta_description": f"Blader alfabetisch door alle {_nlnum(total)} auteurs "
                            f"in de online Bibliotheek — e-books en luisterboeken."})


@app.get("/authors/{letter}", response_class=HTMLResponse)
def authors_letter(request: Request, letter: str, sort: str = BY_SURNAME,
                   conn: sqlite3.Connection = Depends(get_conn)):
    sort = sort if sort in AUTHOR_SORTS else BY_SURNAME
    index = _author_index(conn, sort)
    key = letter.upper() if len(letter) == 1 else letter.lower()
    if key not in index:
        return _not_found(request, "letter")
    # One canonical spelling per letter, so /authors/A and /authors/a don't become
    # two URLs with the same content.
    canonical = key.lower()
    if letter != canonical:
        return RedirectResponse(f"/authors/{canonical}" +
                                (f"?sort={sort}" if sort != BY_SURNAME else ""),
                                status_code=301)
    rows = index[key]
    label = key.upper() if key != _OTHER_LETTER else "Overig"
    return _templates.TemplateResponse(request, "authors.html", {
        "letters": _letter_order(index), "letter": key, "label": label,
        "authors": rows, "total": len(rows), "sort": sort,
        "counts": {ltr: len(index[ltr]) for ltr in index},
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
    formats_map = queries.formats_map(conn, rows)
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
        "name": name, "books": rows, "total": len(rows),
        "formats_map": formats_map, "lists_map": lists_map,
        "author_lists": author_lists, "bio": author_bio(name),
        "breadcrumbs": _breadcrumbs(request, (name, "")),
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
        "breadcrumbs": _breadcrumbs(request, ("Lijsten", ""))})


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
        "breadcrumbs": _breadcrumbs(request, ("Lijsten", "/lists"), (lst["name"], "")),
        "meta_description": (lst["description"] or f"De lijst {lst['name']}")
                            + f" — {available} van {total} titels in de bibliotheek."})


@app.get("/book/{ppn}", response_class=HTMLResponse)
def book(request: Request, ppn: str, conn: sqlite3.Connection = Depends(get_conn)):
    detail = queries.book_detail(conn, ppn)
    if detail is None:
        # A PPN carries no readable words, so nothing seeds the search box here.
        return _not_found(request, "book")
    b = detail["row"]
    summary = (b["summary"] or "").strip()
    cover = _coverw(b["cover_url"], 400)
    # schema.org/Book structured data. Google itself only reads Book markup from
    # an onboarded feed, but Bing and LLM crawlers parse this, so it's worth
    # keeping correct: a BCP 47 code rather than the Dutch language name, and a
    # tidied blurb instead of the raw quote-wrapped, line-broken catalog text.
    jsonld = {"@context": "https://schema.org", "@type": "Book", "name": b["title"],
              "author": [{"@type": "Person", "name": a} for a in detail["authors"]] or None,
              "inLanguage": language_code(b["language"]),
              "isbn": b["isbn"], "publisher": b["publisher"],
              "datePublished": str(b["year"]) if b["year"] else None,
              "image": cover or None, "description": _snippet(summary, 1000) or None,
              "bookFormat": ("https://schema.org/AudiobookFormat"
                             if b["format"] == "audiobook" else "https://schema.org/EBook"),
              "url": f"{_origin(request)}/book/{ppn}"}
    jsonld = {k: v for k, v in jsonld.items() if v}
    # "meer zoals dit": LSA content-based recommendations (see obc.similar), shown as
    # a horizontal scroll strip on the book page.
    similar = queries.similar_books(conn, ppn, limit=20)
    return _templates.TemplateResponse(request, "book.html", {
        "b": b, "genres": detail["genres"], "editions": detail["editions"],
        "authors": detail["authors"], "book_lists": detail["book_lists"],
        "similar": similar,
        "meta_description": _snippet(summary) or f"{b['title']} in de online Bibliotheek.",
        "og_image": cover, "jsonld": jsonld,
        "breadcrumbs": _breadcrumbs(
            request,
            *([(detail["authors"][0], _author_path(detail["authors"][0]))]
              if detail["authors"] else []),
            (b["title"], ""))})


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
    # editions per suggested work, so the dropdown can show an e-book / audiobook icon
    # that links straight to that edition (the row itself opens the e-book).
    emap = queries.editions_map(conn, data["title_rows"])
    titles = [
        {"ppn": r["ppn"], "title": r["title"], "author": r["author"],
         "cover_url": _coverw(r["cover_url"], 80), "format": r["format"],
         "editions": emap.get(r["ppn"], {r["format"]: r["ppn"]})}
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
for _route in list(app.routes):
    if isinstance(_route, APIRoute) and "GET" in (_route.methods or ()):
        app.add_api_route(
            _route.path, _route.endpoint, methods=["HEAD"], include_in_schema=False,
            name=_route.name, response_class=_route.response_class,
            dependencies=_route.dependencies)
