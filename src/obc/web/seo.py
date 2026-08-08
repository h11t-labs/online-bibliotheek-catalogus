"""What the site tells search engines: canonical URLs, breadcrumbs, sitemaps, robots.

Kept apart from :mod:`obc.web.app` because it is a different audience with
different rules. A reader follows links and never sees a `<lastmod>`; a crawler
has a budget, reads exactly these files, and is unforgiving about a URL that
answers in two places or claims a freshness it can't back up. The page routes
borrow :func:`origin`, :func:`breadcrumbs` and the path helpers from here so that
a link, a breadcrumb item and a sitemap entry can never spell the same page
differently.

This module owns :data:`SITE_URL`, since every absolute URL the site emits is
derived from it.
"""

from __future__ import annotations

import datetime
import os
import sqlite3
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from ..textnorm import slugify
from . import indexes, queries

# Absolute site origin for canonical/OG/sitemap URLs (e.g. https://…fly.dev). Empty
# locally → those fall back to the request's own base URL.
SITE_URL = os.environ.get("OBC_SITE_URL", "").rstrip("/")
# The brand Google should print above the result instead of the bare domain. It
# is fed to Search through WebSite structured data and og:site_name — the two
# strongest signals, see https://developers.google.com/search/docs/appearance/site-names
SITE_NAME = "Online Bibliotheek Catalogus"
SITEMAP_PAGE = 45000  # book URLs per sitemap file (under the 50k/file limit)

router = APIRouter(include_in_schema=False)


# --------------------------------------------------------------------------- #
# canonical paths
# --------------------------------------------------------------------------- #
def author_path(name: str) -> str:
    """The canonical URL path for an author: ``/auteur/lisbeth-imbo``.

    A handful of names (Greek script, a stray "|" row) hold no Latin characters
    and fold to nothing, so they have no slug and keep their encoded name. One
    helper so links, breadcrumbs and sitemaps can never drift apart.
    """
    return f"/auteur/{slugify(name) or quote(name, safe='')}"


def series_path(name: str) -> str:
    """Canonical URL path for a series, mirroring :func:`author_path`."""
    return f"/reeks/{slugify(name) or quote(name, safe='')}"


def genre_path(name: str) -> str:
    """Canonical URL path for a genre, mirroring :func:`author_path`."""
    return f"/genre/{slugify(name) or quote(name, safe='')}"


def book_href(slug: str | None, work_id: str) -> str:
    """``/boek/de-ontdekking--anna-vrij--001`` — the one canonical URL per book.

    The id is the truth and the slug is cosmetic: the route reads the id as
    everything after the **last** ``--``, and our slugs never contain a double
    hyphen (the library's own edition slugs sometimes end in one, which is why
    they are not used here). An empty slug drops with its separator, so a book
    whose title and author both hold no Latin characters still has a URL.
    """
    return f"/boek/{slug}--{work_id}" if slug else f"/boek/{work_id}"


def book_path(row) -> str:
    """The canonical path for a work row (or any row carrying work_id + slug).

    One helper, so a link, a breadcrumb, a sitemap entry and the suggest JSON can
    never disagree about where a book lives.
    """
    return book_href(row["slug"], row["work_id"])


def origin(request: Request) -> str:
    """The absolute origin to build URLs on: the configured one, else this request's."""
    return SITE_URL or str(request.base_url).rstrip("/")


# --------------------------------------------------------------------------- #
# structured data
# --------------------------------------------------------------------------- #
def breadcrumbs(request: Request, *trail: tuple[str, str]) -> dict:
    """schema.org BreadcrumbList from ``(label, path)`` pairs, Home prepended.

    Lets Search print a readable trail ("Home › Auteur › Titel") in place of the
    raw URL. The final crumb is the page you're on, so it gets no ``item`` link
    — pass an empty path for it.
    """
    base = origin(request)
    items = [{"@type": "ListItem", "position": 1, "name": "Home", "item": base + "/"}]
    for pos, (label, path) in enumerate(trail, start=2):
        crumb = {"@type": "ListItem", "position": pos, "name": label}
        if path:
            crumb["item"] = base + path
        items.append(crumb)
    return {"@context": "https://schema.org", "@type": "BreadcrumbList",
            "itemListElement": items}


# --------------------------------------------------------------------------- #
# robots.txt + (paginated) sitemap
# --------------------------------------------------------------------------- #
def _xml_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


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


@router.get("/robots.txt")
def robots_txt(request: Request):
    lines = ["User-agent: *",
             # Throttle bots — one small VM serving 68k pages. Google ignores
             # Crawl-delay, but Bing honours it literally: at 10s a full pass over
             # the catalog took more than a week, so most of it was never seen.
             # Detail pages are public-cacheable for an hour, so 1s is affordable.
             "Crawl-delay: 1",
             "Disallow: /suggesties", "Disallow: /facetten", "Disallow: /admin/",
             "Disallow: /*?",  # the infinite filtered-search URL space
             f"Sitemap: {origin(request)}/sitemap.xml"]
    return Response("\n".join(lines) + "\n", media_type="text/plain")


@router.get("/sitemap.xml")
def sitemap_index(request: Request,
                  conn: sqlite3.Connection = Depends(indexes.get_conn)):
    total = queries.total_works(conn)
    base = origin(request)
    pages = max(1, (total + SITEMAP_PAGE - 1) // SITEMAP_PAGE)
    maps = [f"{base}/sitemap-static.xml", f"{base}/sitemap-browse.xml",
            *[f"{base}/sitemap-books-{i}.xml" for i in range(1, pages + 1)]]
    # Every child sitemap is regenerated from the catalog, so the rebuild time is
    # an honest lastmod for the *files* even where it wouldn't be for their URLs.
    mod = _w3c(indexes.data_updated())
    mod = f"<lastmod>{mod}</lastmod>" if mod else ""
    locs = "".join(f"<sitemap><loc>{_xml_escape(m)}</loc>{mod}</sitemap>" for m in maps)
    body = ('<?xml version="1.0" encoding="UTF-8"?>'
            '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f"{locs}</sitemapindex>")
    return Response(body, media_type="application/xml")


@router.get("/sitemap-static.xml")
def sitemap_static(request: Request,
                   conn: sqlite3.Connection = Depends(indexes.get_conn)):
    slugs = [r["slug"] for r in conn.execute("SELECT slug FROM lists ORDER BY slug")]
    paths = ["/", "/over", "/lijsten", "/statistieken", *[f"/lijst/{s}" for s in slugs]]
    # These really are rewritten by every rebuild (new titles, new list positions).
    return _sitemap(origin(request), paths, lastmod=_w3c(indexes.data_updated()))


@router.get("/sitemap-browse.xml")
def sitemap_browse(request: Request,
                   conn: sqlite3.Connection = Depends(indexes.get_conn)):
    """The aggregation pages: the A-Z hub, author pages and series pages.

    These answer the queries the catalog is actually searched with ("boeken van
    X", "Y reeks op volgorde") and were in no sitemap at all. Only pages that
    aggregate two or more titles are listed — see queries.MIN_INDEXABLE_TITLES.
    """
    index = indexes.authors_by_letter(conn)
    letters = indexes.letter_order(index)
    paths = ["/auteurs", "/genres", "/e-books", "/luisterboeken"]
    paths += [f"/auteurs/{letter.lower()}" for letter in letters]
    # The hubs list everything; the sitemap only nominates pages that aggregate
    # something, so single-title pages stay reachable without being advertised as
    # destinations. Slugs, never encoded names: a sitemap full of URLs that
    # immediately 301 wastes the crawl budget it exists to spend well.
    paths += [author_path(row["name"])
              for letter in letters for row in index[letter]
              if row["titles"] >= queries.MIN_INDEXABLE_TITLES]
    paths += [f"/reeks/{r['slug']}" for r in sorted(
        queries.series_index(conn), key=lambda r: r["slug"])
        if r["titles"] >= queries.MIN_INDEXABLE_TITLES]
    paths += [f"/genre/{r['slug']}" for r in queries.genre_pages(conn)
              if r["titles"] >= queries.MIN_INDEXABLE_TITLES]
    return _sitemap(origin(request), paths)


@router.get("/sitemap-books-{n}.xml")
def sitemap_books(request: Request, n: int,
                  conn: sqlite3.Connection = Depends(indexes.get_conn)):
    # One URL per book. The old sitemap listed both editions of every twinned
    # title — ~12k URLs whose content duplicated another page, split link equity
    # and spent crawl budget on one small VM for nothing.
    rows = conn.execute("SELECT work_id, slug FROM works ORDER BY work_id LIMIT ? OFFSET ?",
                        (SITEMAP_PAGE, (max(n, 1) - 1) * SITEMAP_PAGE)).fetchall()
    return _sitemap(origin(request), [book_path(r) for r in rows])
