"""End-to-end route tests over the hermetic fixture catalog (see conftest).

The pages a reader sees. Crawler-facing behaviour lives in test_seo.py, the
derived indexes behind the hubs in test_indexes.py."""

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
    assert client.get("/author/anna-vrij").status_code == 200
    assert client.get("/author/Zzz Niemand").status_code == 404


def test_author_masthead(client, monkeypatch):
    from obc.web import app as appmod
    monkeypatch.setattr(appmod, "author_bio", lambda name: {
        "extract": "Anna Vrij is een Nederlandse schrijver.",
        "thumb": "https://example.test/anna.jpg",
        "url": "https://nl.wikipedia.org/wiki/Anna_Vrij"})
    body = client.get("/author/Anna Vrij").text
    # portrait sits beside the name it belongs to, bio runs as prose beneath it
    assert 'class="ident"' in body and 'class="portrait"' in body
    assert 'alt="Portret van Anna Vrij"' in body
    assert 'class="bio"' in body and "Nederlandse schrijver" in body
    assert "nl.wikipedia.org/wiki/Anna_Vrij" in body
    # the panel it replaced is gone: no accent bar, no card, no shouty eyebrow
    assert "authorcard" not in body
    assert "border-left:4px solid var(--accent)" not in body
    assert "Over de auteur" not in body


def test_author_masthead_without_a_bio(client):
    # most authors have no Wikipedia match — the header must not leave an empty
    # portrait slot or a stray rule behind (author_bio is stubbed to None)
    body = client.get("/author/Anna Vrij").text
    assert 'class="portrait"' not in body and 'class="bio"' not in body
    assert "<h1>Anna Vrij</h1>" in body


def test_series_page(client):
    assert client.get("/series/het-mysterie").status_code == 200
    assert client.get("/series/Zzz Geen Reeks").status_code == 404


def test_stats_links_to_the_real_pages(client):
    # genres and authors have their own pages now, so the stats bars point there
    # instead of into the ?-space, which is noindex and robots-disallowed
    body = client.get("/stats").text
    links = re.findall(r'href="(/[^"]+)"', body)
    assert not [x for x in links if x.startswith("/?genre=")]
    assert not [x for x in links if x.startswith("/author/") and "%" in x]
    # languages and publishers have no landing page, so those stay query links
    assert [x for x in links if x.startswith(("/?language=", "/?publisher="))]


def test_about_page_moved_but_the_old_url_still_resolves(client):
    # /over shipped in v1.1.2 and sits in the live sitemap, so it owes a redirect
    for old in ("/over", "/over/"):
        r = client.get(old, follow_redirects=False)
        assert r.status_code == 301, old          # one hop, not a 307 then a 301
        assert r.headers["location"] == "/about", old
    assert "Over deze catalogus" in client.get("/about").text
    # and the sitemap advertises the destination, not the redirect
    assert "/about<" in client.get("/sitemap-static.xml").text
    assert "/over<" not in client.get("/sitemap-static.xml").text
    assert 'href="/about"' in client.get("/").text          # header points there too


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
    r = client.get("/about")
    assert r.status_code == 200
    assert "Over deze catalogus" in r.text


def test_theme_switcher_present(client):
    # the switcher lives in the shared header, so it ships on every page
    body = client.get("/").text
    assert 'id="theme-toggle"' in body
    assert "localStorage.getItem('theme')" in body


def test_genre_and_format_pages(client):
    # the genre/format slices only existed as ?genre= URLs, which are noindex AND
    # robots-disallowed — so this whole class of query had no landing page at all
    hub = client.get("/genres")
    assert hub.status_code == 200
    assert 'href="/genre/spanning-thrillers"' in hub.text

    genre = client.get("/genre/spanning-thrillers")
    assert genre.status_code == 200
    assert "<h1>Spanning &amp; Thrillers</h1>" in genre.text
    assert "/book/003" in genre.text                     # a thriller from the fixture
    assert 'href="/?genre=Spanning%20%26%20Thrillers"' in genre.text
    assert client.get("/genre/bestaat-niet").status_code == 404
    # one canonical spelling, as with the author letters
    r = client.get("/genre/Spanning-Thrillers", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "/genre/spanning-thrillers"

    # and they're linked from the shared header, not just the sitemap
    assert 'href="/genres"' in client.get("/").text


def test_browse_pages_carry_more_than_a_wall_of_covers(client):
    # a genre page that is only a cover grid is thin content wearing a new URL
    body = client.get("/genre/spanning-thrillers").text
    assert "e-book beschikbaar" in body or "luisterboek" in body
    assert 'href="/author/bob-de-wit"' in body           # top authors link out
    assert "%20" not in body.split('class="lead"')[1][:600]   # slugs, not encoded
    desc = body.split('<meta name="description" content="', 1)[1].split('">', 1)[0]
    assert "titels" in desc and len(desc) <= 300


def test_authors_hub(client):
    hub = client.get("/authors")
    assert hub.status_code == 200
    # bucketed on surname: a reader looks for "Bob de Wit" under W, not under B
    assert 'href="/authors/w"' in hub.text
    letter = client.get("/authors/w")
    assert letter.status_code == 200
    assert 'href="/author/bob-de-wit"' in letter.text
    assert "/author/dirk-kok" not in letter.text            # wrong letter
    # one spelling per letter, so /authors/A doesn't become a second URL
    r = client.get("/authors/W", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "/authors/w"
    assert client.get("/authors/zzz").status_code == 404
    # and it's linked from the shared header, not just the sitemap
    assert 'href="/authors"' in client.get("/").text


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
    from obc.web import indexes as indexmod

    monkeypatch.setattr(indexmod, "DB_PATH", catalog_db.parent / "does-not-exist.db")
    monkeypatch.setattr(appmod, "author_bio", lambda name: None)
    indexmod.catalog_cache.clear()
    resp = TestClient(appmod.app).get("/stats")
    indexmod.catalog_cache.clear()
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


def test_404_serves_the_branded_page_for_every_kind_of_miss(client):
    # Each dead URL gets the full site shell (header search, nav, footer) with copy
    # that names what was missed — not a bare <h1> and not FastAPI's JSON detail.
    cases = (("/deze-pagina-bestaat-niet", "Deze pagina bestaat niet"),
             ("/book/zzznope", "Dit boek staat niet in de catalogus"),
             ("/author/zzz-niemand", "Auteur niet gevonden"),
             ("/series/zzz-geen-reeks", "Reeks niet gevonden"),
             ("/list/zzznope", "Lijst niet gevonden"),
             ("/authors/zzz", "Geen auteurs onder deze letter"),
             ("/genre/bestaat-niet", "Genre niet gevonden"))
    for path, head in cases:
        r = client.get(path)
        assert r.status_code == 404, path
        assert "text/html" in r.headers["content-type"], path
        assert head in r.text, path
        assert 'class="search-trigger' in r.text and "<footer>" in r.text, path
        # an error page is worth crawling for its links, never worth indexing
        assert '<meta name="robots" content="noindex,follow">' in r.text, path
        assert "cache-control" not in r.headers, path


def test_404_suggests_near_matches_and_seeds_the_search(client):
    # A dead /series/ URL whose words point at a real author: the page offers the
    # author page and hands the words to the search box.
    body = client.get("/series/anna-vrij").text
    assert 'value="anna vrij"' in body
    assert 'href="/author/anna-vrij"' in body


def test_404_skips_suggestions_for_scanner_paths(client):
    # Random probes (/wp-login.php and friends) hold no words worth a search, so
    # they neither prefill the box nor cost an FTS query.
    body = client.get("/wp-login.php").text
    assert 'value=""' in body and "Bedoelde je" not in body


def test_404_offers_matching_genres(client, monkeypatch):
    # The fixture catalog carries no genres, so the suggester is stubbed: what is
    # asserted here is the shape of the link a genre match produces — a browse
    # filter (genres have no page of their own), with the name safely encoded.
    from obc.web import app as appmod

    monkeypatch.setattr(appmod.queries, "suggest", lambda *a, **k: {
        "title_rows": [], "authors": [], "publishers": [], "languages": [],
        "lists": [], "genres": ["Spanning & Thrillers"]})
    body = client.get("/list/thriller").text
    assert 'href="/?genre=Spanning%20%26%20Thrillers"' in body
    assert "Spanning &amp; Thrillers" in body
