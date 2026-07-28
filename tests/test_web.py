"""End-to-end route tests over the hermetic fixture catalog (see conftest)."""

import json
import re


def test_home_and_filters(client):
    for path in ["/", "/?q=ontdekking", "/?format=ebook", "/?format=audiobook",
                 "/?language=Nederlands&language=Engels", "/?ereader=1",
                 "/?year_from=2018&year_to=2021", "/?sort=added", "/?sort=title",
                 "/?q=thriller&sort=relevance", "/?page=2", "/?list=test-top"]:
        assert client.get(path).status_code == 200, path


def test_format_filter_shows_books_available_in_that_format(client):
    # 3 of the 5 works are available as an audiobook. The cards link the *book*,
    # never the audiobook edition's old URL — that whole second URL space is gone.
    body = client.get("/?format=audiobook").text
    assert body.count('class="book"') == 3
    assert "/boek/de-ontdekking--anna-vrij--001" in body
    assert "/book/002" not in body


def test_one_card_per_book_with_a_badge_per_format(client):
    # 001, 002 and 007 are three editions of one book. There is one card, one URL,
    # and a format icon per format — each jumping to that format's block on the page.
    body = client.get("/?q=ontdekking").text
    assert body.count('class="book"') == 1
    assert 'class="cover-link" href="/boek/de-ontdekking--anna-vrij--001"' in body
    assert 'class="fmt-ic ebook"' in body
    assert 'class="fmt-ic audio"' in body
    assert 'href="/boek/de-ontdekking--anna-vrij--001#luisterboek"' in body


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


def test_old_book_urls_redirect_to_the_one_canonical_url(client):
    """Both editions' /book/{ppn} URLs — ~68k of them indexed, ~12k of them
    duplicates of another page — 301 to the book's single canonical URL. A stale or
    wrong slug does too: the id is the truth, the slug is cosmetic."""
    for old in ("/book/001", "/book/002", "/book/007",     # e-book + both audiobooks
                "/boek/001",                              # bare id, no slug
                "/boek/foute-slug--001",                  # a slug that has moved on
                "/boek/de-ontdekking--anna-vrij--002"):   # right slug, edition id
        r = client.get(old, follow_redirects=False)
        assert r.status_code == 301, old
        assert r.headers["location"] == CANON_001, old
    assert client.get("/book/zzznope", follow_redirects=False).status_code == 404


def test_canonical_book_page_has_one_block_per_edition(client):
    """The honest answer to "each item has its own properties sometimes": shared
    facts once, then a block per edition with its own fields and borrow link."""
    body = client.get(CANON_001).text
    assert 'id="e-book"' in body and 'id="luisterboek"' in body
    # its own "Lenen op onlinebibliotheek.nl" button per edition, three editions
    for ppn in ("001", "002", "007"):
        assert f"/catalogus/{ppn}/" in body, ppn
    assert body.count("Lenen op onlinebibliotheek.nl") == 3
    # a narrator is an audiobook fact: it must not appear above the e-book block
    assert "Jan Stem" in body
    assert "Jan Stem" not in body[:body.index('id="e-book"')]
    assert "Piet Stem" in body                       # the second audiobook's narrator
    # the badge that used to link the twin's own page anchors into this one
    assert 'href="#luisterboek"' in body


def test_book_page_without_authors_gets_a_title_only_slug(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from obc import db
    from obc.web import app as appmod
    path = tmp_path / "noauthor.db"
    conn = db.connect(path)
    db.bulk_load(conn, [{"ppn": "1", "title": "Zonder Auteur", "format": "ebook"}])
    db.build_genre_taxonomy(conn)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    monkeypatch.setattr(appmod, "DB_PATH", path)
    monkeypatch.setattr(appmod, "author_bio", lambda name: None)
    client = TestClient(appmod.app)
    # the empty author piece drops together with its separator
    assert client.get("/boek/zonder-auteur--1").status_code == 200
    r = client.get("/boek/1", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "/boek/zonder-auteur--1"


def test_sitemap_books_lists_only_canonical_work_urls(client):
    body = client.get("/sitemap-books-1.xml").text
    locs = re.findall(r"<loc>(.*?)</loc>", body)
    assert len(locs) == 5                            # five books, not nine editions
    assert all("/boek/" in loc for loc in locs)
    assert not [loc for loc in locs if "/book/" in loc]
    assert any(loc.endswith(CANON_001) for loc in locs)


def test_format_landing_pages_are_honest_now(client):
    """#27 removed /e-books and /luisterboeken because they counted editions as
    titles and showed one work up to four times. ?format= is a work-level flag, so
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
    data = client.get("/suggest?q=ontdek").json()
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
    book = [d for d in _jsonld(client.get(CANON_001).text)
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
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE books (ppn TEXT PRIMARY KEY, title TEXT);"
                       "INSERT INTO books VALUES ('001', 'De Ontdekking');")
    conn.commit()
    conn.close()
    monkeypatch.setattr(appmod, "DB_PATH", path)
    resp = TestClient(appmod.app).get("/")
    assert resp.status_code == 503
    assert "wordt opgebouwd" in resp.text


def test_author_page(client):
    assert client.get("/author/anna-vrij").status_code == 200
    assert client.get("/author/Zzz Niemand").status_code == 404


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
    book = client.get("/boek/de-ontdekking--anna-vrij--001").text
    assert 'href="/author/anna-vrij"' in book
    assert "/author/Anna%20Vrij" not in book
    crumbs = [d for d in _jsonld(book) if d.get("@type") == "BreadcrumbList"][0]
    assert any(i.get("item", "").endswith("/author/anna-vrij")
               for i in crumbs["itemListElement"])


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


def test_series_urls_are_slugs(client):
    # the encoded form keeps working and moves to the slug, as with authors
    r = client.get("/series/Het Mysterie", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "/series/het-mysterie"
    book = client.get("/boek/het-mysterie-deel-2--bob-de-wit--004").text
    assert 'href="/series/het-mysterie"' in book
    assert "/series/Het%20Mysterie" not in book


def test_hub_lists_one_entry_per_slug(client, ro_conn):
    # the hub must not show "Ad Van Schaik" and "Ad van Schaik" as two entries
    # that lead to the same page — folding decides both the entry and who
    # qualifies, since two rows of one title each are one author with two
    from obc.textnorm import slugify
    from obc.web import app as appmod
    index = appmod._author_hub(ro_conn)
    entries = [e for rows in index.values() for e in rows]
    slugs = [slugify(e["name"]) for e in entries]
    assert len(set(slugs)) == len(slugs), "two hub entries share a slug"
    hub = client.get("/authors/w").text
    assert hub.count('href="/author/bob-de-wit"') == 1


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


def test_authors_are_alphabetised_on_surname(client):
    # "Alexander Klöpping" belongs under K; bucketing on the first character of
    # the full name filed every author under their first name instead
    from obc.textnorm import surname_key
    assert surname_key("Alexander Klöpping") == "klopping"
    assert surname_key("Bob de Wit") == "wit"              # particle skipped
    assert surname_key("Gerda van Wageningen") == "wageningen"
    assert surname_key("Buren, van") == "buren"            # already inverted
    assert surname_key("Bernlef") == "bernlef"             # single token
    assert surname_key("") == "" and surname_key(None) == ""
    # fold() decomposes diacritics, which deletes the Latin letters that have no
    # combining form — "Strøm" folds to "str m" and would file under M
    assert surname_key("Anita Strøm") == "strom"
    assert surname_key("Anja Røyne") == "royne"
    assert surname_key("Arndís Þórarinsdóttir") == "thorarinsdottir"
    assert surname_key("Arnaldur Indriðason") == "indridason"
    assert surname_key("Andrzej Sapkowski Ł") == "l"       # a bare particle-less token
    # generation markers, editorial roles and "and others" are not the surname,
    # but they are the last token — all of these exist in the live catalog
    assert surname_key("A.H. Huussen jr.") == "huussen"
    assert surname_key("Jan Blokker Jr.") == "blokker"
    assert surname_key("R.R. Hopkinson Sr.") == "hopkinson"
    assert surname_key("Ferdinand Bordewijk e.a.") == "bordewijk"
    assert surname_key("Klaartje Gras e.v.a.") == "gras"
    assert surname_key("Daniël Mok c.s.") == "mok"
    # editorial roles are stripped as brackets, not as words — a word rule filed
    # the real surname in "Ludique le Vert" under L
    assert surname_key("Wim Kloppenburg (red.)") == "kloppenburg"
    assert surname_key("Simon Dikker Hupkes (samensteller)") == "hupkes"
    assert surname_key("Adam J.B. Lane (ill.)") == "lane"
    assert surname_key("Ludique le Vert") == "vert"
    # an apostrophe is a separator to fold(), which cut "O'Brien" down to "brien"
    assert surname_key("Ally O'Brien") == "obrien"
    assert surname_key("Jean-Michel Caradec'h") == "caradech"
    assert surname_key("Adriaan van 't Spijker") == "spijker"   # 't stays a particle
    # two authors are websites; the TLD is not their surname
    assert surname_key("Vakantietaal.nl") == "vakantietaal"
    assert surname_key("Onno van Gelder jr.") == "gelder"   # suffix and particle
    # ...but a suffix rule must leave a name behind: "SR" is this author's name
    assert surname_key("Mariela SR") == "sr"
    # a single letter can genuinely be the surname, so it is left alone
    assert surname_key("Christiane F") == "f"
    assert surname_key("Drs. P") == "p"
    assert client.get("/authors/w").status_code == 200     # Bob de Wit lives here
    assert client.get("/authors/b").status_code == 404     # ...and not here


def test_hub_can_alphabetise_on_first_name_too(client, ro_conn):
    # both orders are defensible — hunting a known writer you look under the
    # surname, browsing you recognise the whole name — so the hub offers both
    from obc.web import app as appmod
    by_surname = appmod._author_hub(ro_conn, appmod.BY_SURNAME)
    by_first = appmod._author_hub(ro_conn, appmod.BY_FIRST)
    assert sorted(by_surname) == ["K", "L", "S", "V", "W"]   # Kok, Licht, Sol, Vrij, de Wit
    assert sorted(by_first) == ["A", "B", "C", "D", "E"]     # ...same five, by first name
    hub = client.get("/authors/w").text
    assert 'href="/author/bob-de-wit"' in hub
    assert 'href="/author/bob-de-wit"' in client.get("/authors/b?sort=voornaam").text
    hub = client.get("/authors?sort=voornaam").text
    assert 'href="/authors/b?sort=voornaam"' in hub     # letter links keep the order
    assert 'class="on"' in hub
    # an unknown value falls back rather than 404s, and the canonical stays clean
    assert client.get("/authors?sort=onzin").status_code == 200
    assert '<link rel="canonical" href="http://testserver/authors">' in \
        client.get("/authors?sort=voornaam").text


def test_hub_counts_match_the_page_they_link_to(client, ro_conn):
    # the threshold decides what goes in the sitemap, so a count that disagrees
    # with its own page would publish a "2 titles" author whose page shows one
    from obc.textnorm import slugify
    from obc.web import app as appmod
    from obc.web import queries
    for rows in appmod._author_hub(ro_conn).values():
        for entry in rows:
            fold_key = slugify(entry["name"]).replace("-", " ")
            shelf = queries.author_books_by_fold(ro_conn, fold_key)
            assert entry["titles"] == len(shelf), entry["name"]


def test_unsluggable_authors_are_not_merged_into_one_entry(client, ro_conn):
    # fold() returns "" for a name with no Latin characters; using that as a merge
    # key would fuse every such author into a single hub entry with a summed count
    from obc.web import app as appmod
    entries = [e for rows in appmod._author_hub(ro_conn).values() for e in rows]
    assert not [e for e in entries if not e["name"].strip()]
    from obc.textnorm import slugify
    assert all(slugify(e["name"]) for e in entries), "an entry has no slug to link to"


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


def test_robots_and_sitemaps(client):
    robots = client.get("/robots.txt")
    assert robots.status_code == 200
    assert "Disallow: /*?" in robots.text and "Sitemap:" in robots.text
    idx = client.get("/sitemap.xml")
    assert idx.status_code == 200 and "<sitemapindex" in idx.text
    stat = client.get("/sitemap-static.xml")
    assert stat.status_code == 200 and "/about" in stat.text
    books = client.get("/sitemap-books-1.xml")
    assert books.status_code == 200 and "/boek/de-ontdekking--anna-vrij--001" in books.text


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
    assert "/author/elena-sol" not in browse        # 1 title
    assert client.get("/author/elena-sol").status_code == 200   # still reachable
    # the fixture's only series has a single part, so it's held back as well
    assert "/series/" not in browse
    assert [r["name"] for r in queries.series_index(ro_conn)] == ["Het Mysterie"]


def test_lastmod_only_where_it_is_truthful(client):
    # the catalog tracks no per-record change date, so stamping every book URL
    # with the rebuild time would be a hint search engines learn to distrust
    assert "<lastmod>" in client.get("/sitemap.xml").text
    assert "<lastmod>" in client.get("/sitemap-static.xml").text
    assert "<lastmod>" not in client.get("/sitemap-books-1.xml").text


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




def test_colliding_genre_spellings_share_one_page(ro_conn):
    # the catalog holds "Biografieën" twice — combining diaeresis and precomposed —
    # and both fold to `biografieen`. Every spelling has to end up on that one page,
    # or a genre (and its books) silently vanishes. The merge runs at build time now;
    # what the page reads back is genre_pages.
    from obc.textnorm import slugify
    from obc.web import queries
    pages = queries.genre_pages(ro_conn)
    covered = {name for p in pages for name in queries.genre_spellings(ro_conn, p["slug"])}
    all_genres = {r["name"] for r in ro_conn.execute("SELECT name FROM genres")}
    assert covered == all_genres, "a genre spelling reaches no page"
    for page in pages:
        assert slugify(page["name"]) == page["slug"]


def test_genre_slugs_are_unique_and_stable(client, ro_conn):
    from obc.textnorm import slugify
    from obc.web import queries
    assert slugify("Spanning & Thrillers") == "spanning-thrillers"
    assert slugify("Poëzie & Theater") == "poezie-theater"
    rows = queries.genre_pages(ro_conn)
    slugs = [r["slug"] for r in rows]
    assert all(slugs) and len(set(slugs)) == len(slugs)   # no blanks, no collisions
    browse = client.get("/sitemap-browse.xml").text
    # the hub lists every genre; the sitemap nominates the ones that aggregate
    for r in rows:
        promoted = f"/genre/{r['slug']}<" in browse
        assert promoted == (r["titles"] >= queries.MIN_INDEXABLE_TITLES), r["name"]
    assert "/genres<" in browse


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


def test_seo_meta_and_jsonld(client):
    home = client.get("/").text
    assert '<meta name="description"' in home
    assert '<link rel="canonical"' in home
    assert 'content="index,follow"' in home          # bare browse is indexable
    assert 'content="noindex,follow"' in client.get("/?q=de").text  # filtered -> noindex
    book = client.get("/boek/de-ontdekking--anna-vrij--001").text
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
    # the ?-carrying variants are noindex and robots-disallowed; marking them up as
    # "the site" too would hand Search competing copies of the same entity.
    # ?sort= and ?view= carry no chips and no query text, so they'd slip through a
    # filters-only check — the rule keys off the query string itself.
    for path in ("/?q=de", "/?format=ebook", "/?page=2", "/?sort=title",
                 "/?view=list", "/?per_page=48", "/about", "/boek/de-ontdekking--anna-vrij--001"):
        assert not [d for d in _jsonld(client.get(path).text)
                    if d.get("@type") == "WebSite"], path


def test_book_jsonld_uses_a_language_code_and_a_tidy_description(client):
    from obc.textnorm import language_code
    assert language_code("Nederlands") == "nl" and language_code("Engels") == "en"
    assert language_code("nederlands") == "nl"        # folded lookup
    assert language_code("Klingon") is None
    assert language_code("Schots") is None            # Scots vs Gaelic — ambiguous
    assert language_code(None) is None
    book = [d for d in _jsonld(client.get("/boek/de-ontdekking--anna-vrij--001").text)
            if d.get("@type") == "Book"][0]
    assert book["inLanguage"] == "nl"                 # not "Nederlands"
    assert not book["description"].startswith('"')    # no wrapping quote mark
    assert "\n" not in book["description"]
    spanish = [d for d in _jsonld(client.get("/boek/poesia-espanola--elena-sol--006").text)
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
    body = client.get("/boek/de-ontdekking--anna-vrij--001").text
    crumbs = [d for d in _jsonld(body) if d.get("@type") == "BreadcrumbList"][0]
    for item in crumbs["itemListElement"]:
        if "item" in item:
            path = item["item"].replace("http://testserver", "")
            assert client.get(path).status_code == 200, item["item"]


def test_home_title_and_description_quote_the_catalog_size(client):
    home = client.get("/").text
    assert "<title>Online Bibliotheek Catalogus — " in home   # brand first
    assert "e-books en luisterboeken van de online Bibliotheek" in home


def test_breadcrumbs_jsonld(client):
    crumbs = [d for d in _jsonld(client.get("/boek/de-ontdekking--anna-vrij--001").text)
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
        assert any(d.get("@type") == "BreadcrumbList" for d in _jsonld(body)), path


def test_book_meta_description_is_snippet_clean(client):
    from obc.web.app import _snippet
    # publisher blurbs arrive quote-wrapped and line-broken; a snippet must not
    assert _snippet('  "Een\n  mooi   boek."  ') == "Een mooi boek."
    assert _snippet("a" * 40 + " " + "b" * 200, limit=50).endswith("…")
    assert len(_snippet("woord " * 100)) <= 156
    assert not _snippet('"geciteerd"').startswith('"')
    body = client.get("/boek/de-ontdekking--anna-vrij--001").text
    desc = re.search(r'<meta name="description" content="(.*?)">', body).group(1)
    assert desc and "\n" not in desc and not desc.startswith("&#34;")



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


def test_crawl_delay_lets_bing_finish_a_pass(client):
    # Google ignores Crawl-delay but Bing takes it literally: at 10s a full pass
    # over 64k+ URLs took over a week, so most of the catalog was never crawled.
    delay = [ln for ln in client.get("/robots.txt").text.splitlines()
             if ln.startswith("Crawl-delay:")]
    assert delay == ["Crawl-delay: 1"]


def test_head_is_answered_like_get(client):
    # FastAPI's APIRoute doesn't add HEAD to GET routes, so every page used to
    # answer 405 — link checkers and monitors read that as a broken URL.
    for path in ("/", "/boek/de-ontdekking--anna-vrij--001", "/about", "/robots.txt", "/sitemap.xml", "/healthz"):
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
    monkeypatch.setattr(appmod, "SITE_URL", "https://example.nl")
    monkeypatch.setattr(appmod, "_CANONICAL_HOST", "example.nl")

    r = client.get("/boek/de-ontdekking--anna-vrij--001", headers={"host": "www.example.nl"},
                   follow_redirects=False)
    assert r.status_code == 301
    assert r.headers["location"] == \
        "https://example.nl/boek/de-ontdekking--anna-vrij--001"
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
    monkeypatch.setattr(appmod, "SITE_URL", "https://example.nl")
    monkeypatch.setattr(appmod, "_CANONICAL_HOST", "example.nl")
    for host in ("onlinebibliotheekcatalogus.nl", "localhost:8000", "1.2.3.4:8000",
                 "www.something-else.nl"):
        assert client.get("/", headers={"host": host},
                          follow_redirects=False).status_code == 200, host


def test_alias_matching_ignores_ports_on_both_sides(client, monkeypatch):
    # a SITE_URL carrying a port (a local run, a staging origin) must still
    # recognise its own host and its www. alias, not silently match nothing
    from obc.web import app as appmod
    monkeypatch.setattr(appmod, "SITE_URL", "http://example.nl:8001")
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
    assert "public" in client.get("/boek/de-ontdekking--anna-vrij--001").headers.get("cache-control", "")
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
    resp = TestClient(appmod.app).get("/stats")
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
def _catalog_with_genre_parents(tmp_path):
    """A fixture catalog where book_genres.parent_id is actually populated.

    The live enrichment that fills it needs the detail pages, which the hermetic
    fixture has no way to fetch — so the hierarchy is stamped directly here.
    Without this the parent/child code would ship untested: every local catalog
    has parent_id NULL throughout.
    """
    import sampledata

    from obc import db
    path = tmp_path / "hier.db"
    conn = db.connect(path)
    db.bulk_load(conn, sampledata.records(), sampledata.lists())
    ids = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM genres")}
    top, sub = "Spanning & Thrillers", "Thrillers"
    if sub not in ids:
        conn.execute("INSERT INTO genres(name) VALUES (?)", (sub,))
        ids[sub] = conn.execute("SELECT id FROM genres WHERE name = ?", (sub,)).fetchone()[0]
        wid = conn.execute(
            "SELECT work_id FROM work_genres WHERE genre_id = ? LIMIT 1",
            (ids[top],)).fetchone()[0]
        conn.execute("INSERT INTO work_genres(work_id, genre_id) VALUES (?, ?)",
                     (wid, ids[sub]))
    conn.execute("UPDATE work_genres SET parent_id = ? WHERE genre_id = ?",
                 (ids[top], ids[sub]))
    # one jeugd title, so the hub has two audiences to switch between — the live
    # catalog files 5.762 books that way and gives them their own taxonomy
    conn.execute("UPDATE works SET audience = 'Jeugd' WHERE work_id = '005'")
    conn.commit()
    # the taxonomy is a build artifact, so rebuild it now that the parents are set
    db.build_genre_taxonomy(conn)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")   # so mode=ro readers see it
    conn.close()
    return path


def test_genre_hierarchy(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from obc.web import app as appmod
    from obc.web import queries
    path = _catalog_with_genre_parents(tmp_path)
    monkeypatch.setattr(appmod, "DB_PATH", path)
    monkeypatch.setattr(appmod, "author_bio", lambda name: None)
    conn = queries.connect_ro(path)

    assert queries.genre_page(conn, "thrillers")["parent_slug"] == "spanning-thrillers"
    assert [c["slug"] for c in queries.genre_children(conn, "spanning-thrillers")] == \
        ["thrillers"]
    # a top genre has no parent
    assert queries.genre_page(conn, "spanning-thrillers")["parent_slug"] == ""

    client = TestClient(appmod.app)
    hub = client.get("/genres").text
    # the hub is headed by top genres, with their children beneath them
    assert 'href="/genre/spanning-thrillers"' in hub
    assert hub.index("/genre/spanning-thrillers") < hub.index("/genre/thrillers")
    # jeugd and volwassenen are separate taxonomies, switched rather than stacked
    # A tree may only state what its own audience files: every parent it shows
    # must be one this audience's own rows actually name.
    for aud, _label in appmod.AUDIENCES:
        for top in queries.genre_tree(conn, aud):
            for kid in top["children"]:
                # works with an unknown audience land on the default shelf, so the
                # check has to resolve the audience the way the build does
                known = [a for a, _ in appmod.AUDIENCES]
                marks = ",".join("?" * len(known))
                named = conn.execute(
                    "SELECT COUNT(*) FROM work_genres wg "
                    "JOIN genres g ON g.id = wg.genre_id "
                    "JOIN genres p ON p.id = wg.parent_id "
                    "JOIN works w ON w.work_id = wg.work_id "
                    "WHERE g.name = ? AND p.name = ? AND ? = (CASE WHEN "
                    f"  lower(COALESCE(w.audience, '')) IN ({marks}) "
                    "  THEN lower(COALESCE(w.audience, '')) ELSE ? END)",
                    (kid["name"], top["name"], aud, *known,
                     appmod.AUDIENCES[0][0])).fetchone()[0]
                assert named, f"{aud}: {kid['name']} under {top['name']}"
    assert 'class="audbar"' in hub
    assert 'href="/genres?publiek=jeugd"' in hub
    jeugd = client.get("/genres?publiek=jeugd").text
    assert jeugd != hub and 'class="on"' in jeugd
    # an unknown audience falls back instead of 404ing, and the canonical stays clean
    assert client.get("/genres?publiek=onzin").status_code == 200
    assert '<link rel="canonical" href="http://testserver/genres">' in jeugd

    child = client.get("/genre/thrillers").text
    assert 'href="/genre/spanning-thrillers"' in child       # a way back up
    crumbs = [d for d in _jsonld(child) if d.get("@type") == "BreadcrumbList"][0]
    assert [i["name"] for i in crumbs["itemListElement"]] == [
        "Home", "Genres", "Spanning & Thrillers", "Thrillers"]

    parent = client.get("/genre/spanning-thrillers").text
    assert 'class="subgenres"' in parent and 'href="/genre/thrillers"' in parent
    conn.close()
