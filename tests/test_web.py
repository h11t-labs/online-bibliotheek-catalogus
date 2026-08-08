"""End-to-end route tests over the hermetic fixture catalog (see conftest).

The pages a reader sees. Crawler-facing behaviour lives in test_seo.py, the
derived indexes behind the hubs in test_indexes.py."""

import re

from helpers import jsonld


def test_home_and_filters(client):
    for path in ["/", "/?zoek=ontdekking", "/?formaat=ebook", "/?formaat=audiobook",
                 "/?taal=Nederlands&taal=Engels", "/?ereader=1",
                 "/?jaar_van=2018&jaar_tot=2021", "/?sortering=added", "/?sortering=title",
                 "/?zoek=thriller&sortering=relevance", "/?pagina=2", "/?lijst=test-top"]:
        assert client.get(path).status_code == 200, path


def test_format_filter_shows_books_available_in_that_format(client):
    # 3 of the 5 works are available as an audiobook. The cards link the *book*,
    # never the audiobook edition's old URL — that whole second URL space is gone.
    body = client.get("/?formaat=audiobook").text
    assert body.count('class="book"') == 3
    assert "/boek/de-ontdekking--anna-vrij--001" in body
    assert "/book/002" not in body


def test_one_card_per_book_with_a_badge_per_format(client):
    # 001, 002 and 007 are three editions of one book. There is one card, one URL,
    # and a format icon per format — each jumping to that format's block on the page.
    body = client.get("/?zoek=ontdekking").text
    assert body.count('class="book"') == 1
    assert 'class="cover-link" href="/boek/de-ontdekking--anna-vrij--001"' in body
    assert 'class="fmt-ic ebook"' in body
    assert 'class="fmt-ic audio"' in body
    assert 'href="/boek/de-ontdekking--anna-vrij--001#luisterboek"' in body


def test_suggest(client):
    data = client.get("/suggesties?zoek=ontdek").json()
    assert any(t["ppn"] == "001" for t in data["titles"])
    for key in ("titles", "authors", "publishers", "genres", "languages", "lists"):
        assert key in data
    assert client.get("/suggesties?zoek=").json()["titles"] == []


def test_suggest_searches_keywords_and_includes_format(client):
    # "italiaans" only lives in book 005's Trefwoorden (keywords), not its title.
    data = client.get("/suggesties?zoek=italiaans").json()
    matches = [t for t in data["titles"] if t["ppn"] == "005"]
    assert matches and matches[0]["format"] == "ebook"


def test_autocomplete_shows_edition_format_badge(client):
    # the dropdown's own-format corner badge on each cover thumbnail
    body = client.get("/").text
    assert "ac-cover" in body and "ac-fmt" in body


def test_facet_endpoint(client):
    assert "Anna Vrij" in client.get("/facetten?type=author").json()["values"]
    assert client.get("/facetten?type=bogus").json()["values"] == []


def test_book_detail_and_404(client):
    assert client.get("/boek/de-ontdekking--anna-vrij--001").status_code == 200
    assert client.get("/book/zzznope").status_code == 404
    assert client.get("/boek/zzznope").status_code == 404


def test_book_detail_mobile_layout(client):
    # the cover forms a centered hero on phones (not a small left-aligned column),
    # and the meta table keeps a usable label width
    body = client.get("/boek/de-ontdekking--anna-vrij--001").text
    assert "align-items:center" in body
    assert ".edition .btn{width:100%" in body


CANON_001 = "/boek/de-ontdekking--anna-vrij--001"


def test_a_book_answers_at_exactly_one_url(client):
    """Every other spelling is a 404, including the ones that used to 301.

    The redirect layer is gone on purpose: a page that answers at six addresses is
    six chances for a crawler to pick the wrong one as canonical. /book/{ppn} was
    the old per-edition URL space and is retired outright."""
    assert client.get(CANON_001).status_code == 200
    for gone in ("/book/001", "/book/002", "/book/007",    # the retired URL space
                 "/boek/001",                             # bare id, no slug
                 "/boek/foute-slug--001",                 # a slug that has moved on
                 "/boek/de-ontdekking--anna-vrij--002",   # right slug, edition id
                 "/book/zzznope"):
        assert client.get(gone, follow_redirects=False).status_code == 404, gone


def test_canonical_book_page_has_one_block_per_edition(client):
    """The honest answer to "each item has its own properties sometimes": shared
    facts once, then a block per edition with its own fields and borrow link."""
    body = client.get(CANON_001).text
    assert 'id="e-book"' in body and 'id="luisterboek"' in body
    # its own borrow button per edition, three editions
    for ppn in ("001", "002", "007"):
        assert f"/catalogus/{ppn}/" in body, ppn
    # one primary CTA per *format* under the cover, carrying that format's colour…
    assert body.count('class="borrow-btn ebook"') == 1
    assert body.count('class="borrow-btn audio"') == 1
    # …and a button in the block only for the edition the CTA cannot offer: the
    # second audiobook. No link is ever shown twice.
    assert body.count("lenen ↗") == 1
    assert 'class="borrow-more"' in body             # points at that leftover edition
    # a narrator is an audiobook fact: it must not appear above the e-book block
    assert "Jan Stem" in body
    assert "Jan Stem" not in body[:body.index('id="e-book"')]
    assert "Piet Stem" in body                       # the second audiobook's narrator
    # one work with two editions, so neither format is the other's afterthought
    assert "Ook als luisterboek" not in body
    assert body.count('class="badge"') == 1 and body.count('class="badge audio"') == 1


def test_book_page_without_authors_gets_a_title_only_slug(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from obc import db
    from obc.web import app as appmod
    from obc.web import indexes as indexmod
    path = tmp_path / "noauthor.db"
    conn = db.connect(path)
    db.bulk_load(conn, [{"ppn": "1", "title": "Zonder Auteur", "format": "ebook"}])
    db.build_genre_taxonomy(conn)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    monkeypatch.setattr(indexmod, "DB_PATH", path)
    monkeypatch.setattr(appmod, "author_bio", lambda name: None)
    client = TestClient(appmod.app)
    # the empty author piece drops together with its separator
    assert client.get("/boek/zonder-auteur--1").status_code == 200
    assert client.get("/boek/1", follow_redirects=False).status_code == 404


def test_sitemap_books_lists_only_canonical_work_urls(client):
    body = client.get("/sitemap-books-1.xml").text
    locs = re.findall(r"<loc>(.*?)</loc>", body)
    assert len(locs) == 5                            # five books, not nine editions
    assert all("/boek/" in loc for loc in locs)
    assert not [loc for loc in locs if "/book/" in loc]
    assert any(loc.endswith(CANON_001) for loc in locs)


def test_format_landing_pages_are_honest_now(client):
    """#27 removed /e-books and /luisterboeken because they counted editions as
    titles and showed one work up to four times. ?formaat= is a work-level flag, so
    the counts and the cards below them are the same books."""
    ebooks = client.get("/e-books")
    assert ebooks.status_code == 200
    assert ebooks.text.count('class="book"') == 5
    audio = client.get("/luisterboeken")
    assert audio.status_code == 200
    assert audio.text.count('class="book"') == 3
    browse = client.get("/sitemap-browse.xml").text
    assert "/e-books<" in browse and "/luisterboeken<" in browse


def test_suggest_carries_the_canonical_url_and_both_editions(client):
    data = client.get("/suggesties?zoek=ontdek").json()
    assert len(data["titles"]) == 1                  # never a twin in the dropdown
    title = data["titles"][0]
    assert title["ppn"] == "001"
    assert title["editions"] == {"ebook": "001", "audiobook": "002"}
    # new: the row click uses this instead of composing /book/{ppn} and eating a 301
    assert title["url"] == CANON_001


def test_book_jsonld_carries_one_workexample_per_edition(client):
    """One Book for the work with a workExample per edition — the pattern
    schema.org documents for this — replacing two competing Book entities that each
    claimed the same title."""
    book = [d for d in jsonld(client.get(CANON_001).text)
            if d.get("@type") == "Book"][0]
    assert book["url"].endswith(CANON_001)
    assert "bookFormat" not in book                  # that is per example now
    assert "isbn" not in book
    examples = book["workExample"]
    assert len(examples) == 3
    assert examples[0]["bookFormat"] == "https://schema.org/EBook"
    assert examples[1]["bookFormat"] == "https://schema.org/AudiobookFormat"
    assert {e["isbn"] for e in examples} == {"9789021400001", "9789021400002",
                                             "9789021400007"}


def test_old_schema_db_serves_the_bootstrap_503(tmp_path, monkeypatch):
    """The deploy window: the volume still holds the pre-works DB until the refresh
    completes. That must render "de catalogus wordt opgebouwd", not a stack trace."""
    import sqlite3

    from fastapi.testclient import TestClient

    from obc.web import app as appmod
    from obc.web import indexes as indexmod
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE books (ppn TEXT PRIMARY KEY, title TEXT);"
                       "INSERT INTO books VALUES ('001', 'De Ontdekking');")
    conn.commit()
    conn.close()
    monkeypatch.setattr(indexmod, "DB_PATH", path)
    resp = TestClient(appmod.app).get("/")
    assert resp.status_code == 503
    assert "wordt opgebouwd" in resp.text


def test_author_page(client):
    assert client.get("/auteur/anna-vrij").status_code == 200
    assert client.get("/auteur/Zzz Niemand").status_code == 404


def test_author_masthead(client, monkeypatch):
    from obc.web import app as appmod
    monkeypatch.setattr(appmod, "author_bio", lambda name: {
        "extract": "Anna Vrij is een Nederlandse schrijver.",
        "thumb": "https://example.test/anna.jpg",
        "url": "https://nl.wikipedia.org/wiki/Anna_Vrij"})
    body = client.get("/auteur/anna-vrij").text
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
    body = client.get("/auteur/anna-vrij").text
    assert 'class="portrait"' not in body and 'class="bio"' not in body
    assert "<h1>Anna Vrij</h1>" in body


def test_series_page(client):
    assert client.get("/reeks/het-mysterie").status_code == 200
    assert client.get("/reeks/Zzz Geen Reeks").status_code == 404


def test_stats_links_to_the_real_pages(client):
    # genres and authors have their own pages now, so the stats bars point there
    # instead of into the ?-space, which is noindex and robots-disallowed
    body = client.get("/statistieken").text
    links = re.findall(r'href="(/[^"]+)"', body)
    assert not [x for x in links if x.startswith("/?genre=")]
    assert not [x for x in links if x.startswith("/auteur/") and "%" in x]
    # languages and publishers have no landing page, so those stay query links
    assert [x for x in links if x.startswith(("/?taal=", "/?uitgever="))]


def test_english_paths_301_to_their_dutch_names(client):
    """The one redirect layer this site keeps.

    ~11.3k /author/ and /series/ URLs were indexed under the English scheme, so
    they move over rather than being thrown away. Query strings ride along, and it
    is one hop — an old path on an alias host must not cost two."""
    for old, new in (("/authors", "/auteurs"), ("/authors/w", "/auteurs/w"),
                     ("/author/anna-vrij", "/auteur/anna-vrij"),
                     ("/series/het-mysterie", "/reeks/het-mysterie"),
                     ("/lists", "/lijsten"), ("/list/test-top", "/lijst/test-top"),
                     ("/about", "/over"), ("/stats", "/statistieken")):
        r = client.get(old, follow_redirects=False)
        assert r.status_code == 301, old
        assert r.headers["location"] == new, old
        assert client.get(new).status_code == 200, new
    r = client.get("/authors/w?sortering=voornaam", follow_redirects=False)
    assert r.headers["location"] == "/auteurs/w?sortering=voornaam"
    assert "Over deze catalogus" in client.get("/over").text
    # and the sitemap advertises the destination, never the redirect
    static = client.get("/sitemap-static.xml").text
    assert "/over<" in static and "/about<" not in static
    assert 'href="/over"' in client.get("/").text          # header points there too


def test_lists_pages(client):
    assert client.get("/lijsten").status_code == 200
    assert client.get("/lijst/test-top").status_code == 200
    assert client.get("/lijst/test-top?toon=available").status_code == 200
    assert client.get("/lijst/test-top?toon=unavailable").status_code == 200
    assert client.get("/lijst/zzznope").status_code == 404


def test_stats_health_static(client):
    assert client.get("/statistieken").status_code == 200
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


def test_genre_and_format_pages(client):
    # the genre/format slices only existed as ?genre= URLs, which are noindex AND
    # robots-disallowed — so this whole class of query had no landing page at all
    hub = client.get("/genres")
    assert hub.status_code == 200
    assert 'href="/genre/spanning-thrillers"' in hub.text

    genre = client.get("/genre/spanning-thrillers")
    assert genre.status_code == 200
    assert "<h1>Spanning &amp; Thrillers</h1>" in genre.text
    assert "/boek/thriller-in-de-nacht--bob-de-wit--003" in genre.text  # a fixture thriller
    # the "filter in zoeken" link carries the same spelling the page filters on
    assert 'href="/?genre=Spanning+%26+Thrillers"' in genre.text
    assert client.get("/genre/bestaat-niet").status_code == 404
    # one spelling per genre, as with the author letters — the rest is a miss
    assert client.get("/genre/Spanning-Thrillers",
                      follow_redirects=False).status_code == 404

    # and they're linked from the shared header, not just the sitemap
    assert 'href="/genres"' in client.get("/").text


def test_browse_pages_carry_more_than_a_wall_of_covers(client):
    # a genre page that is only a cover grid is thin content wearing a new URL
    body = client.get("/genre/spanning-thrillers").text
    assert "e-book beschikbaar" in body or "luisterboek" in body
    assert 'href="/auteur/bob-de-wit"' in body           # top authors link out
    assert "%20" not in body.split('class="lead"')[1][:600]   # slugs, not encoded
    desc = body.split('<meta name="description" content="', 1)[1].split('">', 1)[0]
    assert "titels" in desc and len(desc) <= 300




def test_publisher_pages(client):
    """"Uitgever" on a book page went to ?uitgever= — noindex and robots-disallowed,
    so the 1.5k publishers in the catalog had no page to link to. Now they do, and
    the book page links it directly instead of pointing at a filtered search."""
    book = client.get(CANON_001).text
    assert 'href="/uitgever/querido-amsterdam"' in book
    assert 'href="/?uitgever=' not in book

    page = client.get("/uitgever/querido-amsterdam")
    assert page.status_code == 200
    assert "<h1>Querido, Amsterdam</h1>" in page.text
    assert CANON_001 in page.text                          # 001 is the Querido title
    assert "/boek/koken-met-liefde--dirk-kok--005" not in page.text   # Keuken Pers
    # one spelling per publisher, as everywhere else — no second URL for this page
    assert client.get("/uitgever/Querido-Amsterdam").status_code == 404
    assert client.get("/uitgever/bestaat-niet").status_code == 404
    # and it is nominated to search engines on the same terms as the other
    # aggregation pages: two titles or more, or it is a weaker copy of one book
    browse = client.get("/sitemap-browse.xml").text
    assert "/uitgever/spanning-bv<" in browse               # 003 + 004
    assert "/uitgever/querido-amsterdam<" not in browse      # one work only


def test_publisher_page_merges_the_spellings_that_fold_together():
    """One publisher reaches the catalog under several spellings.

    The live catalog holds "Ambo|Anthos Uitgevers, Amsterdam" twice (capital and
    lowercase "uitgevers") and five variants of "De Crime Compagnie, Laren NH".
    They fold to one slug, so the page has to filter on every spelling or it shows
    fewer titles than its own heading claims. The fixture catalog spells each
    publisher one way, so the merge is exercised here directly.
    """
    import sqlite3

    from obc.web import queries
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE publishers (name TEXT, name_fold TEXT, n INTEGER)")
    conn.executemany("INSERT INTO publishers VALUES (?, ?, ?)", [
        ("Ambo|Anthos uitgevers, Amsterdam", "ambo anthos uitgevers amsterdam", 3),
        ("Ambo|Anthos Uitgevers, Amsterdam", "ambo anthos uitgevers amsterdam", 9),
        ("Losse Uitgever", "losse uitgever", 1)])

    entry = queries.publisher_page(conn, "ambo-anthos-uitgevers-amsterdam")
    assert entry["name"] == "Ambo|Anthos Uitgevers, Amsterdam"   # the one most used
    assert entry["titles"] == 12                                 # every variant counted
    assert set(entry["spellings"]) == {"Ambo|Anthos uitgevers, Amsterdam",
                                       "Ambo|Anthos Uitgevers, Amsterdam"}
    assert queries.publisher_page(conn, "bestaat-niet") is None

    # the "filter in zoeken" link has to carry every spelling the page filters on,
    # or it opens a search with fewer titles than the page the reader just left
    from obc.web.app import _filter_url
    url = _filter_url("uitgever", entry["spellings"])
    assert url.count("uitgever=") == 2 and url.startswith("/?")
    # the sitemap sees one page per fold, not one per spelling
    pages = queries.publisher_pages(conn)
    assert [p["slug"] for p in pages] == ["ambo-anthos-uitgevers-amsterdam",
                                          "losse-uitgever"]
    assert pages[0]["titles"] == 12


def test_authors_hub(client):
    hub = client.get("/auteurs")
    assert hub.status_code == 200
    # bucketed on surname: a reader looks for "Bob de Wit" under W, not under B
    assert 'href="/auteurs/w"' in hub.text
    letter = client.get("/auteurs/w")
    assert letter.status_code == 200
    assert 'href="/auteur/bob-de-wit"' in letter.text
    assert "/auteur/dirk-kok" not in letter.text            # wrong letter
    # one spelling per letter, so /auteurs/W is not a second URL for this page
    assert client.get("/auteurs/W", follow_redirects=False).status_code == 404
    assert client.get("/auteurs/zzz").status_code == 404
    # and it's linked from the shared header, not just the sitemap
    assert 'href="/auteurs"' in client.get("/").text


def test_borrow_button_duration_reads_as_a_length(client):
    from obc.web.app import _dur_short
    # the catalog stores speelduur in both shapes; the button shows one
    assert _dur_short("3:17:19") == "3 u 17 min"
    assert _dur_short("9 uur 1 minuut") == "9 u 1 min"
    assert _dur_short("0:42:10") == "42 min"
    assert _dur_short("8:00:00") == "8 u"
    assert _dur_short("7 uur") == "7 u"
    # unparseable and empty pass through rather than guessing
    assert _dur_short("onbekend") == "onbekend"
    assert _dur_short(None) == ""


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
    assert 'name="per_pagina"' in body                      # carried on the filter form
    assert client.get("/?per_pagina=48").status_code == 200    # a valid option
    assert client.get("/?per_pagina=999").status_code == 200   # invalid -> clamped, no error


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
    assert client.get("/suggesties?zoek=ontdek&limit=-1").status_code == 422
    assert client.get("/suggesties?zoek=ontdek&limit=99").status_code == 422
    assert client.get("/facetten?type=author&limit=-1").status_code == 422
    assert client.get("/facetten?type=author&limit=99").status_code == 422
    # in-range values still work
    assert client.get("/suggesties?zoek=ontdek&limit=5").status_code == 200
    assert client.get("/facetten?type=author&limit=10").status_code == 200


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
        client.get("/statistieken")


def test_missing_db_shows_friendly_bootstrap_page(catalog_db, monkeypatch):
    # A missing DB file ("unable to open database file") IS a bootstrap state -> 503.
    from fastapi.testclient import TestClient

    from obc.web import app as appmod
    from obc.web import indexes as indexmod

    monkeypatch.setattr(indexmod, "DB_PATH", catalog_db.parent / "does-not-exist.db")
    monkeypatch.setattr(appmod, "author_bio", lambda name: None)
    resp = TestClient(appmod.app).get("/statistieken")
    assert resp.status_code == 503
    assert "wordt opgebouwd" in resp.text


def test_version_matches_package_metadata():
    from importlib.metadata import version

    import obc
    assert obc.__version__ == version("online-bibliotheek-catalogus")


def test_security_headers_on_every_response(client):
    for path in ("/", "/boek/de-ontdekking--anna-vrij--001"):
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
            out["n"] = conn.execute("SELECT count(*) FROM works").fetchone()[0]
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
             ("/boek/zzznope", "Dit boek staat niet in de catalogus"),
             ("/auteur/zzz-niemand", "Auteur niet gevonden"),
             ("/reeks/zzz-geen-reeks", "Reeks niet gevonden"),
             ("/lijst/zzznope", "Lijst niet gevonden"),
             ("/auteurs/zzz", "Geen auteurs onder deze letter"),
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
    # A dead /reeks/ URL whose words point at a real author: the page offers the
    # author page and hands the words to the search box.
    body = client.get("/reeks/anna-vrij").text
    assert 'value="anna vrij"' in body
    assert 'href="/auteur/anna-vrij"' in body


def test_404_skips_suggestions_for_scanner_paths(client):
    # Random probes (/wp-login.php and friends) hold no words worth a search, so
    # they neither prefill the box nor cost an FTS query.
    body = client.get("/wp-login.php").text
    assert 'value=""' in body and "Bedoelde je" not in body


def test_404_offers_matching_genres(client, monkeypatch):
    # The fixture catalog carries no genres, so the suggester is stubbed: what is
    # asserted here is the shape of the link a genre match produces — the genre's
    # own page, not the ?genre= filter, which is noindex and robots-disallowed.
    from obc.web import app as appmod

    monkeypatch.setattr(appmod.queries, "suggest", lambda *a, **k: {
        "title_rows": [], "authors": [], "publishers": [], "languages": [],
        "lists": [], "genres": ["Spanning & Thrillers"]})
    body = client.get("/lijst/thriller").text
    assert 'href="/genre/spanning-thrillers"' in body
    assert "Spanning &amp; Thrillers" in body


def test_404_genre_without_a_slug_keeps_the_filter_link(client, monkeypatch):
    # build_genre_taxonomy skips names that fold to nothing, so /genre/<encoded>
    # would 404. The filtered search still finds them, so that is where they go.
    from obc.web import app as appmod

    monkeypatch.setattr(appmod.queries, "suggest", lambda *a, **k: {
        "title_rows": [], "authors": [], "publishers": [], "languages": [],
        "lists": [], "genres": ["Θρίλερ"]})
    body = client.get("/lijst/thriller").text
    assert 'href="/genre/' not in body
    assert 'href="/?genre=%CE%98%CF%81%CE%AF%CE%BB%CE%B5%CF%81"' in body


def test_pagination_is_bounded(client):
    """`LIMIT n OFFSET m` walks and discards m rows, so depth costs linearly:
    measured over "de" (51,980 matches) page 1 is 373ms, page 100 657ms and the
    last page 4.2s — and the pager linked straight to that last page, so the worst
    case was one click from every search. The exact total is still shown; only the
    walk is bounded."""
    from obc.web.app import MAX_PAGES

    body = client.get(f"/?pagina={MAX_PAGES + 500}").text
    assert f"page={MAX_PAGES + 500}" not in body       # never offered
    # asking past the end serves the last reachable page rather than erroring
    assert client.get(f"/?pagina={MAX_PAGES + 500}").status_code == 200
