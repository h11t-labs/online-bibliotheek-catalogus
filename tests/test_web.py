"""End-to-end route tests over the hermetic fixture catalog (see conftest)."""

import json
import re


def test_home_and_filters(client):
    for path in ["/", "/?q=ontdekking", "/?format=ebook", "/?format=audiobook",
                 "/?language=Nederlands&language=Engels", "/?ereader=1",
                 "/?year_from=2018&year_to=2021", "/?sort=added", "/?sort=title",
                 "/?q=thriller&sort=relevance", "/?page=2", "/?list=test-top"]:
        assert client.get(path).status_code == 200, path


def test_format_filter_renders_only_matches(client):
    body = client.get("/?format=audiobook").text
    assert "/book/002" in body  # the audiobook edition is shown


def test_merged_editions_one_card_links_each_edition(client):
    # 001 (e-book) and 002 (audiobook) are the same work under different PPNs. Search
    # collapses them into ONE card: the cover + title open the e-book by default, and
    # each edition has its own clickable format icon on the right of the cover.
    body = client.get("/?q=ontdekking").text
    assert body.count('class="book"') == 1                 # a single merged card
    assert 'class="cover-link" href="/book/001"' in body   # default select -> e-book
    assert 'class="fmt-ic ebook"' in body                  # e-book icon...
    assert 'class="fmt-ic audio"' in body                  # ...and audiobook icon
    assert 'href="/book/002"' in body                      # audiobook edition reachable


def test_suggest(client):
    data = client.get("/suggest?q=ontdek").json()
    assert any(t["ppn"] == "001" for t in data["titles"])
    for key in ("titles", "authors", "publishers", "genres", "languages", "lists"):
        assert key in data
    assert client.get("/suggest?q=").json()["titles"] == []


def test_suggest_searches_keywords_and_includes_format(client):
    # "italiaans" only lives in book 005's Trefwoorden (keywords), not its title.
    data = client.get("/suggest?q=italiaans").json()
    matches = [t for t in data["titles"] if t["ppn"] == "005"]
    assert matches and matches[0]["format"] == "ebook"


def test_autocomplete_shows_edition_format_badge(client):
    # the dropdown's own-format corner badge on each cover thumbnail
    body = client.get("/").text
    assert "ac-cover" in body and "ac-fmt" in body


def test_facet_endpoint(client):
    assert "Anna Vrij" in client.get("/facet?type=author").json()["values"]
    assert client.get("/facet?type=bogus").json()["values"] == []


def test_book_detail_and_404(client):
    assert client.get("/book/001").status_code == 200
    assert client.get("/book/zzznope").status_code == 404


def test_book_detail_mobile_layout(client):
    # the cover + borrow button form a centered hero on phones (not a small left-aligned
    # column with a tiny button), and the meta table keeps a usable label width
    body = client.get("/book/001").text
    assert "align-items:center" in body
    assert ".poster .btn{width:100%" in body


def test_author_page(client):
    assert client.get("/author/Anna Vrij").status_code == 200
    assert client.get("/author/Zzz Niemand").status_code == 404


def test_series_page(client):
    assert client.get("/series/Het Mysterie").status_code == 200
    assert client.get("/series/Zzz Geen Reeks").status_code == 404


def test_lists_pages(client):
    assert client.get("/lists").status_code == 200
    assert client.get("/list/test-top").status_code == 200
    assert client.get("/list/test-top?show=available").status_code == 200
    assert client.get("/list/test-top?show=unavailable").status_code == 200
    assert client.get("/list/zzznope").status_code == 404


def test_stats_health_static(client):
    assert client.get("/stats").status_code == 200
    assert client.get("/favicon.svg").status_code == 200
    assert client.get("/healthz").json() == {"status": "ok"}


def test_about_page(client):
    r = client.get("/over")
    assert r.status_code == 200
    assert "Over deze catalogus" in r.text


def test_theme_switcher_present(client):
    # the switcher lives in the shared header, so it ships on every page
    body = client.get("/").text
    assert 'id="theme-toggle"' in body
    assert "localStorage.getItem('theme')" in body


def test_robots_and_sitemaps(client):
    robots = client.get("/robots.txt")
    assert robots.status_code == 200
    assert "Disallow: /*?" in robots.text and "Sitemap:" in robots.text
    idx = client.get("/sitemap.xml")
    assert idx.status_code == 200 and "<sitemapindex" in idx.text
    stat = client.get("/sitemap-static.xml")
    assert stat.status_code == 200 and "/over" in stat.text
    books = client.get("/sitemap-books-1.xml")
    assert books.status_code == 200 and "/book/001" in books.text


def test_seo_meta_and_jsonld(client):
    home = client.get("/").text
    assert '<meta name="description"' in home
    assert '<link rel="canonical"' in home
    assert 'content="index,follow"' in home          # bare browse is indexable
    assert 'content="noindex,follow"' in client.get("/?q=de").text  # filtered -> noindex
    book = client.get("/book/001").text
    assert "application/ld+json" in book and "Book" in book
    assert 'property="og:image"' in book             # cover as OG image


def _jsonld(body: str) -> list[dict]:
    """Every ld+json block on a page, parsed."""
    return [json.loads(m) for m in
            re.findall(r'<script type="application/ld\+json">(.*?)</script>', body, re.S)]


def test_site_name_signals_on_home(client):
    # Google prints the site name above the result off WebSite structured data
    # first and og:site_name second — without them it falls back to the domain.
    home = client.get("/").text
    assert 'property="og:site_name" content="Online Bibliotheek Catalogus"' in home
    site = [d for d in _jsonld(home) if d.get("@type") == "WebSite"]
    assert len(site) == 1
    assert site[0]["name"] == "Online Bibliotheek Catalogus"
    assert site[0]["url"].endswith("/")


def test_website_jsonld_only_on_bare_home(client):
    # the filtered/paged variants are noindex; marking them up as "the site" too
    # would hand Search competing copies of the same entity
    for path in ("/?q=de", "/?format=ebook", "/?page=2", "/over", "/book/001"):
        assert not [d for d in _jsonld(client.get(path).text)
                    if d.get("@type") == "WebSite"], path


def test_home_title_and_description_quote_the_catalog_size(client):
    home = client.get("/").text
    assert "<title>Online Bibliotheek Catalogus — " in home   # brand first
    assert "e-books en luisterboeken van de online Bibliotheek" in home


def test_breadcrumbs_jsonld(client):
    crumbs = [d for d in _jsonld(client.get("/book/001").text)
              if d.get("@type") == "BreadcrumbList"]
    assert len(crumbs) == 1
    items = crumbs[0]["itemListElement"]
    assert [i["position"] for i in items] == list(range(1, len(items) + 1))
    assert items[0]["name"] == "Home" and items[0]["item"].endswith("/")
    assert "item" not in items[-1]                   # the page you're on gets no link
    # a book by a known author routes through that author's page
    assert any(i.get("item", "").startswith(items[0]["item"] + "author/") for i in items)
    # and the other detail pages carry a trail too
    for path in ("/author/Anna Vrij", "/list/test-top", "/over", "/stats"):
        body = client.get(path).text
        assert any(d.get("@type") == "BreadcrumbList" for d in _jsonld(body)), path


def test_book_meta_description_is_snippet_clean(client):
    from obc.web.app import _snippet
    # publisher blurbs arrive quote-wrapped and line-broken; a snippet must not
    assert _snippet('  "Een\n  mooi   boek."  ') == "Een mooi boek."
    assert _snippet("a" * 40 + " " + "b" * 200, limit=50).endswith("…")
    assert len(_snippet("woord " * 100)) <= 156
    assert not _snippet('"geciteerd"').startswith('"')
    body = client.get("/book/001").text
    desc = re.search(r'<meta name="description" content="(.*?)">', body).group(1)
    assert desc and "\n" not in desc and not desc.startswith("&#34;")


def test_google_verification_tag_opt_in(client, monkeypatch):
    assert "google-site-verification" not in client.get("/").text
    from obc.web import app as appmod
    appmod._templates.env.globals["google_verification"] = "tok123"
    try:
        assert 'name="google-site-verification" content="tok123"' in client.get("/").text
    finally:
        appmod._templates.env.globals["google_verification"] = appmod.GOOGLE_VERIFICATION


def test_goatcounter_snippet_present(client):
    body = client.get("/").text
    assert "obc.goatcounter.com/count" in body
    assert "//gc.zgo.at/count.js" in body


def test_per_page_and_toolbar(client):
    body = client.get("/").text
    assert 'class="toolbar"' in body                        # sort + per-page above results
    assert 'rail-toggle' in body                            # collapsible filter block header
    assert 'IntersectionObserver' in body                   # infinite-scroll enhancement
    assert 'nav-toggle' in body                             # mobile header hamburger
    assert 'name="per_page"' in body                        # carried on the filter form
    assert client.get("/?per_page=48").status_code == 200    # a valid option
    assert client.get("/?per_page=999").status_code == 200   # invalid -> clamped, no error


def test_cache_control_and_crawl_delay(client):
    # bots are throttled so one small VM can serve 68k pages
    assert "Crawl-delay" in client.get("/robots.txt").text
    # stable detail pages are publicly cacheable, offloading repeat/crawler hits
    assert "public" in client.get("/book/001").headers.get("cache-control", "")
    # volatile / non-content endpoints stay uncached
    assert "cache-control" not in client.get("/healthz").headers
    assert "cache-control" not in client.get("/suggest?q=a").headers


def test_mobile_theme_switch_present(client):
    # mobile full-page menu gets an explicit 3-way switch, not just a cycling icon
    body = client.get("/").text
    assert 'class="theme-row"' in body
    for opt in ("system", "light", "dark"):
        assert f'data-theme-opt="{opt}"' in body
    assert 'id="theme-toggle"' in body  # the desktop cycling button still exists


def test_admin_refresh_requires_token(client):
    # No OBC_REFRESH_TOKEN configured in tests -> always unauthorized.
    assert client.post("/admin/refresh").status_code == 401
    assert client.post("/admin/refresh",
                       headers={"Authorization": "Bearer nope"}).status_code == 401


def test_suggest_and_facet_reject_hostile_limits(client):
    # LIMIT -1 is "unlimited" in SQLite; the routes constrain the parameter so a
    # hostile request can't ask for every row. FastAPI validation -> 422.
    assert client.get("/suggest?q=ontdek&limit=-1").status_code == 422
    assert client.get("/suggest?q=ontdek&limit=99").status_code == 422
    assert client.get("/facet?type=author&limit=-1").status_code == 422
    assert client.get("/facet?type=author&limit=99").status_code == 422
    # in-range values still work
    assert client.get("/suggest?q=ontdek&limit=5").status_code == 200
    assert client.get("/facet?type=author&limit=10").status_code == 200


def test_unknown_sql_error_is_not_hidden_as_bootstrap(client, monkeypatch):
    # A genuine SQL bug must surface as a 500, not the friendly "catalogus wordt
    # opgebouwd" 503 page (which is only for a not-yet-built DB).
    import sqlite3

    import pytest

    from obc.web import queries

    def boom(_conn):
        raise sqlite3.OperationalError("no such column: b.bogus")

    monkeypatch.setattr(queries, "web_stats", boom)
    with pytest.raises(sqlite3.OperationalError):
        client.get("/stats")


def test_missing_db_shows_friendly_bootstrap_page(catalog_db, monkeypatch):
    # A missing DB file ("unable to open database file") IS a bootstrap state -> 503.
    from fastapi.testclient import TestClient

    from obc.web import app as appmod

    monkeypatch.setattr(appmod, "DB_PATH", catalog_db.parent / "does-not-exist.db")
    monkeypatch.setattr(appmod, "author_bio", lambda name: None)
    appmod._facets_cache.update(key=None, data=None)
    resp = TestClient(appmod.app).get("/stats")
    appmod._facets_cache.update(key=None, data=None)
    assert resp.status_code == 503
    assert "wordt opgebouwd" in resp.text


def test_version_matches_package_metadata():
    from importlib.metadata import version

    import obc
    assert obc.__version__ == version("online-bibliotheek-catalogus")


def test_security_headers_on_every_response(client):
    for path in ("/", "/book/001"):
        r = client.get(path)
        assert r.status_code == 200
        assert r.headers["X-Content-Type-Options"] == "nosniff"
        assert r.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
        csp = r.headers["Content-Security-Policy"]
        assert "default-src 'self'" in csp
        assert "gc.zgo.at" in csp  # GoatCounter script host must be allowed


def test_connect_ro_usable_across_threads(catalog_db):
    """A connection from connect_ro must be usable from a thread other than the one
    that opened it. FastAPI runs a yield-dependency's setup and the route handler on
    different threadpool threads, so without check_same_thread=False this raised an
    intermittent sqlite3.ProgrammingError under load. Regression guard.
    """
    import threading

    from obc.web import queries

    conn = queries.connect_ro(catalog_db)  # opened in this (main) thread
    out = {}

    def use():
        try:
            out["n"] = conn.execute("SELECT count(*) FROM books").fetchone()[0]
        except Exception as exc:  # noqa: BLE001 — capture to assert in main thread
            out["err"] = exc

    t = threading.Thread(target=use)
    t.start()
    t.join()
    conn.close()

    assert "err" not in out, f"cross-thread use raised: {out.get('err')!r}"
    assert out["n"] > 0
