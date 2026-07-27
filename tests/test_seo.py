"""What the site tells crawlers: canonical URLs, structured data, robots, sitemaps.

Covers obc.web.seo, plus the canonical-host redirect and the HEAD twins in
obc.web.app that exist for the same audience."""

import re

from helpers import jsonld


def test_author_urls_are_slugs(client):
    from obc.textnorm import fold, slugify
    # a slug round-trips into a name_fold, which is what makes it an indexed
    # lookup instead of a stored column — and what merges the catalog's spelling
    # duplicates ("Ad Van Schaik" / "Ad van Schaik") onto one page
    for name in ("Ad Van Schaik", "Ad van Schaik", "Agnès Martin-Lugand"):
        assert slugify(name).replace("-", " ") == fold(name)
    assert slugify("Ad Van Schaik") == slugify("Ad van Schaik") == "ad-van-schaik"
    assert slugify("Lisbeth Imbo") == "lisbeth-imbo"
    assert slugify("Λήδα Βάρβαρούση") == ""     # no Latin characters to slug

    assert client.get("/author/anna-vrij").status_code == 200
    # the old percent-encoded links keep working and move to the slug
    for legacy in ("/author/Anna Vrij", "/author/Anna%20Vrij", "/author/ANNA-VRIJ"):
        r = client.get(legacy, follow_redirects=False)
        assert r.status_code == 301, legacy
        assert r.headers["location"] == "/author/anna-vrij", legacy
    # and nothing links to the encoded form any more
    book = client.get("/book/001").text
    assert 'href="/author/anna-vrij"' in book
    assert "/author/Anna%20Vrij" not in book
    crumbs = [d for d in jsonld(book) if d.get("@type") == "BreadcrumbList"][0]
    assert any(i.get("item", "").endswith("/author/anna-vrij")
               for i in crumbs["itemListElement"])


def test_series_urls_are_slugs(client):
    # the encoded form keeps working and moves to the slug, as with authors
    r = client.get("/series/Het Mysterie", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "/series/het-mysterie"
    book = client.get("/book/004").text
    assert 'href="/series/het-mysterie"' in book
    assert "/series/Het%20Mysterie" not in book


def test_robots_and_sitemaps(client):
    robots = client.get("/robots.txt")
    assert robots.status_code == 200
    assert "Disallow: /*?" in robots.text and "Sitemap:" in robots.text
    idx = client.get("/sitemap.xml")
    assert idx.status_code == 200 and "<sitemapindex" in idx.text
    stat = client.get("/sitemap-static.xml")
    assert stat.status_code == 200 and "/about" in stat.text
    books = client.get("/sitemap-books-1.xml")
    assert books.status_code == 200 and "/book/001" in books.text


def test_sitemap_lists_the_aggregation_pages(client):
    # author + series pages answer the queries the catalog gets searched with,
    # and used to be in no sitemap at all — reachable only from a book page
    idx = client.get("/sitemap.xml").text
    assert "/sitemap-browse.xml" in idx
    browse = client.get("/sitemap-browse.xml")
    assert browse.status_code == 200
    assert "/authors<" in browse.text and "/authors/w<" in browse.text
    assert "/author/bob-de-wit<" in browse.text     # 2 works -> its own page
    # Anna Vrij has one work in two formats: one card, so not its own page
    assert "/author/anna-vrij<" not in browse.text
    # slugs, never encoded names: a sitemap of URLs that immediately 301 wastes
    # exactly the crawl budget this sitemap exists to spend well
    assert "%20" not in browse.text and "%2F" not in browse.text


def test_sitemap_skips_single_title_aggregations(client, ro_conn):
    # a page for an author (or a "series") with one title is a weaker copy of that
    # title's own page; thousands of them dilute the ones that do add something
    from obc.web import queries
    browse = client.get("/sitemap-browse.xml").text
    assert "/author/dirk-kok" not in browse         # 1 title
    assert client.get("/author/dirk-kok").status_code == 200   # still reachable
    # the fixture's only series has a single part, so it's held back as well
    assert "/series/" not in browse
    assert [r["name"] for r in queries.series_index(ro_conn)] == ["Het Mysterie"]


def test_lastmod_only_where_it_is_truthful(client):
    # the catalog tracks no per-record change date, so stamping every book URL
    # with the rebuild time would be a hint search engines learn to distrust
    assert "<lastmod>" in client.get("/sitemap.xml").text
    assert "<lastmod>" in client.get("/sitemap-static.xml").text
    assert "<lastmod>" not in client.get("/sitemap-books-1.xml").text


def test_seo_meta_and_jsonld(client):
    home = client.get("/").text
    assert '<meta name="description"' in home
    assert '<link rel="canonical"' in home
    assert 'content="index,follow"' in home          # bare browse is indexable
    assert 'content="noindex,follow"' in client.get("/?q=de").text  # filtered -> noindex
    book = client.get("/book/001").text
    assert "application/ld+json" in book and "Book" in book
    assert 'property="og:image"' in book             # cover as OG image


def test_site_name_signals_on_home(client):
    # Google prints the site name above the result off WebSite structured data
    # first and og:site_name second — without them it falls back to the domain.
    home = client.get("/").text
    assert 'property="og:site_name" content="Online Bibliotheek Catalogus"' in home
    site = [d for d in jsonld(home) if d.get("@type") == "WebSite"]
    assert len(site) == 1
    assert site[0]["name"] == "Online Bibliotheek Catalogus"
    assert site[0]["url"].endswith("/")


def test_website_jsonld_only_on_bare_home(client):
    # the ?-carrying variants are noindex and robots-disallowed; marking them up as
    # "the site" too would hand Search competing copies of the same entity.
    # ?sort= and ?view= carry no chips and no query text, so they'd slip through a
    # filters-only check — the rule keys off the query string itself.
    for path in ("/?q=de", "/?format=ebook", "/?page=2", "/?sort=title",
                 "/?view=list", "/?per_page=48", "/about", "/book/001"):
        assert not [d for d in jsonld(client.get(path).text)
                    if d.get("@type") == "WebSite"], path


def test_book_jsonld_uses_a_language_code_and_a_tidy_description(client):
    from obc.textnorm import language_code
    assert language_code("Nederlands") == "nl" and language_code("Engels") == "en"
    assert language_code("nederlands") == "nl"        # folded lookup
    assert language_code("Klingon") is None
    assert language_code("Schots") is None            # Scots vs Gaelic — ambiguous
    assert language_code(None) is None
    book = [d for d in jsonld(client.get("/book/001").text)
            if d.get("@type") == "Book"][0]
    assert book["inLanguage"] == "nl"                 # not "Nederlands"
    assert not book["description"].startswith('"')    # no wrapping quote mark
    assert "\n" not in book["description"]
    spanish = [d for d in jsonld(client.get("/book/006").text)
               if d.get("@type") == "Book"][0]
    assert spanish["inLanguage"] == "es"


def test_author_pages_survive_a_slash_in_the_name(client):
    # two catalog authors carry a slash ("Elizabeth August/Dreamshield"); with a
    # plain {name} route their page 404s, so both the book-page link and the
    # breadcrumb item URL would point at a dead URL
    # our handler answers (route matched) rather than FastAPI's routing 404
    for path in ("/author/Elizabeth August/Dreamshield",
                 "/author/Elizabeth%20August%2FDreamshield"):
        r = client.get(path)
        assert r.status_code == 404 and "Auteur niet gevonden" in r.text, path
    # every breadcrumb item URL must resolve — a trail into a 404 is worse than none
    body = client.get("/book/001").text
    crumbs = [d for d in jsonld(body) if d.get("@type") == "BreadcrumbList"][0]
    for item in crumbs["itemListElement"]:
        if "item" in item:
            path = item["item"].replace("http://testserver", "")
            assert client.get(path).status_code == 200, item["item"]


def test_home_title_and_description_quote_the_catalog_size(client):
    home = client.get("/").text
    assert "<title>Online Bibliotheek Catalogus — " in home   # brand first
    assert "e-books en luisterboeken van de online Bibliotheek" in home


def test_breadcrumbs_jsonld(client):
    crumbs = [d for d in jsonld(client.get("/book/001").text)
              if d.get("@type") == "BreadcrumbList"]
    assert len(crumbs) == 1
    items = crumbs[0]["itemListElement"]
    assert [i["position"] for i in items] == list(range(1, len(items) + 1))
    assert items[0]["name"] == "Home" and items[0]["item"].endswith("/")
    assert "item" not in items[-1]                   # the page you're on gets no link
    # a book by a known author routes through that author's page
    assert any(i.get("item", "").startswith(items[0]["item"] + "author/") for i in items)
    # and the other detail pages carry a trail too
    for path in ("/author/Anna Vrij", "/list/test-top", "/about", "/stats"):
        body = client.get(path).text
        assert any(d.get("@type") == "BreadcrumbList" for d in jsonld(body)), path


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


def test_crawl_delay_lets_bing_finish_a_pass(client):
    # Google ignores Crawl-delay but Bing takes it literally: at 10s a full pass
    # over 64k+ URLs took over a week, so most of the catalog was never crawled.
    delay = [ln for ln in client.get("/robots.txt").text.splitlines()
             if ln.startswith("Crawl-delay:")]
    assert delay == ["Crawl-delay: 1"]


def test_head_is_answered_like_get(client):
    # FastAPI's APIRoute doesn't add HEAD to GET routes, so every page used to
    # answer 405 — link checkers and monitors read that as a broken URL.
    for path in ("/", "/book/001", "/about", "/robots.txt", "/sitemap.xml", "/healthz"):
        head, get = client.head(path), client.get(path)
        assert head.status_code == 200, path
        assert head.status_code == get.status_code
        # a HEAD probe must report what a GET would, cache policy included
        assert head.headers.get("cache-control") == get.headers.get("cache-control"), path


def test_head_twins_stay_out_of_the_openapi_schema(client):
    # APIRoute freezes unique_id at construction, so widening an existing route's
    # methods would publish a HEAD operation carrying the GET's operationId
    import collections
    schema = client.get("/openapi.json").json()
    ops = [(m, op.get("operationId"))
           for p, methods in schema["paths"].items() for m, op in methods.items()]
    assert ops, "schema unexpectedly empty"
    assert not [m for m, _ in ops if m == "head"]
    dupes = [i for i, n in collections.Counter(i for _, i in ops).items() if n > 1]
    assert not dupes, dupes


def test_alternate_hosts_redirect_to_the_canonical_one(client, monkeypatch):
    from obc.web import app as appmod
    from obc.web import seo as seomod
    monkeypatch.setattr(seomod, "SITE_URL", "https://example.nl")
    monkeypatch.setattr(appmod, "_CANONICAL_HOST", "example.nl")

    r = client.get("/book/001", headers={"host": "www.example.nl"},
                   follow_redirects=False)
    assert r.status_code == 301
    assert r.headers["location"] == "https://example.nl/book/001"
    # the bounce is a response the site sends, so it carries the same security
    # headers as any other — it gets them in the same middleware pass
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert "Content-Security-Policy" in r.headers
    # the query string survives the bounce
    r = client.get("/?q=de", headers={"host": "app.fly.dev"}, follow_redirects=False)
    assert r.headers["location"] == "https://example.nl/?q=de"
    # the canonical host itself is served, not bounced (port and case ignored)
    assert client.get("/", headers={"host": "example.nl"}).status_code == 200
    assert client.get("/", headers={"host": "Example.NL:443"}).status_code == 200
    # internal callers (Fly health check, cron machine) reach the app under the
    # machine's own host and must never be redirected
    assert client.get("/healthz", headers={"host": "1.2.3.4:8000"}).status_code == 200
    assert client.post("/admin/refresh",
                       headers={"host": "1.2.3.4:8000"}).status_code == 401


def test_only_known_aliases_are_redirected(client, monkeypatch):
    # OBC_SITE_URL lives in a Fly secret that shadows the fly.toml default. If the
    # two ever disagree, "301 anything that isn't SITE_URL" would bounce the live
    # domain onto a stale one and deindex the site — so unknown hosts are served.
    from obc.web import app as appmod
    from obc.web import seo as seomod
    monkeypatch.setattr(seomod, "SITE_URL", "https://example.nl")
    monkeypatch.setattr(appmod, "_CANONICAL_HOST", "example.nl")
    for host in ("onlinebibliotheekcatalogus.nl", "localhost:8000", "1.2.3.4:8000",
                 "www.something-else.nl"):
        assert client.get("/", headers={"host": host},
                          follow_redirects=False).status_code == 200, host


def test_alias_matching_ignores_ports_on_both_sides(client, monkeypatch):
    # a SITE_URL carrying a port (a local run, a staging origin) must still
    # recognise its own host and its www. alias, not silently match nothing
    from obc.web import app as appmod
    from obc.web import seo as seomod
    monkeypatch.setattr(seomod, "SITE_URL", "http://example.nl:8001")
    monkeypatch.setattr(appmod, "_CANONICAL_HOST", appmod._hostname("http://example.nl:8001"))
    assert appmod._CANONICAL_HOST == "example.nl"
    assert client.get("/", headers={"host": "example.nl:8001"},
                      follow_redirects=False).status_code == 200
    r = client.get("/", headers={"host": "www.example.nl:8001"}, follow_redirects=False)
    assert r.status_code == 301
    assert r.headers["location"] == "http://example.nl:8001/"


def test_no_host_redirect_without_a_configured_site_url(client):
    # local dev / tests have OBC_SITE_URL unset — everything must still serve
    assert client.get("/", headers={"host": "localhost:8000"}).status_code == 200
    assert client.get("/", headers={"host": "app.fly.dev"}).status_code == 200


def test_cache_control(client):
    # stable detail pages are publicly cacheable, offloading repeat/crawler hits
    assert "public" in client.get("/book/001").headers.get("cache-control", "")
    # volatile / non-content endpoints stay uncached
    assert "cache-control" not in client.get("/healthz").headers
    assert "cache-control" not in client.get("/suggest?q=a").headers
