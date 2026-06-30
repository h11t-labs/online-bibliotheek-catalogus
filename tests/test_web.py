"""End-to-end route tests over the hermetic fixture catalog (see conftest)."""


def test_home_and_filters(client):
    for path in ["/", "/?q=ontdekking", "/?format=ebook", "/?format=audiobook",
                 "/?language=Nederlands&language=Engels", "/?ereader=1",
                 "/?year_from=2018&year_to=2021", "/?sort=added", "/?sort=title",
                 "/?q=thriller&sort=relevance", "/?page=2", "/?list=test-top"]:
        assert client.get(path).status_code == 200, path


def test_format_filter_renders_only_matches(client):
    body = client.get("/?format=audiobook").text
    assert "/book/002" in body  # the audiobook edition is shown


def test_suggest(client):
    data = client.get("/suggest?q=ontdek").json()
    assert any(t["ppn"] == "001" for t in data["titles"])
    for key in ("titles", "authors", "publishers", "genres", "languages", "lists"):
        assert key in data
    assert client.get("/suggest?q=").json()["titles"] == []


def test_facet_endpoint(client):
    assert "Anna Vrij" in client.get("/facet?type=author").json()["values"]
    assert client.get("/facet?type=bogus").json()["values"] == []


def test_book_detail_and_404(client):
    assert client.get("/book/001").status_code == 200
    assert client.get("/book/zzznope").status_code == 404


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


def test_goatcounter_snippet_present(client):
    body = client.get("/").text
    assert "obc.goatcounter.com/count" in body
    assert "//gc.zgo.at/count.js" in body


def test_per_page_and_toolbar(client):
    body = client.get("/").text
    assert 'class="toolbar"' in body                        # sort + per-page above results
    assert 'rail-toggle' in body                            # collapsible filter block header
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


def test_admin_refresh_requires_token(client):
    # No OBC_REFRESH_TOKEN configured in tests -> always unauthorized.
    assert client.post("/admin/refresh").status_code == 401
    assert client.post("/admin/refresh",
                       headers={"Authorization": "Bearer nope"}).status_code == 401
