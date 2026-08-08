"""The URL scheme itself: every route answers, and nothing links off it.

A rename is exactly the change that leaves a template pointing at a path that no
longer exists, so this file walks the site rather than asserting one page at a
time. It is the guard the /author -> /auteur pass would have needed.
"""

# Every public URL shape, with its Dutch query parameters.
CANON_001 = "/boek/de-ontdekking--anna-vrij--001"

PATHS = [
    "/", "/?zoek=de", "/?formaat=ebook", "/?taal=Nederlands", "/?sortering=title",
    "/?pagina=2", "/?weergave=list", "/?per_pagina=48", "/?lijst=test-top",
    "/?auteur=Anna%20Vrij", "/?jaar_van=2018&jaar_tot=2021", "/?ereader=1",
    "/boek/de-ontdekking--anna-vrij--001",
    "/auteurs", "/auteurs/w", "/auteur/anna-vrij", "/reeks/het-mysterie",
    "/genres", "/genres?publiek=jeugd", "/genre/spanning-thrillers",
    "/uitgever/querido-amsterdam",
    "/lijsten", "/lijsten?sortering=total", "/lijst/test-top",
    "/lijst/test-top?toon=available", "/over", "/statistieken",
    "/e-books", "/luisterboeken",
    "/suggesties?zoek=ontdek", "/facetten?type=author&zoek=a",
    "/robots.txt", "/sitemap.xml", "/sitemap-static.xml", "/sitemap-browse.xml",
    "/sitemap-books-1.xml", "/healthz",
]


def test_every_url_shape_answers(client):
    assert [(p, client.get(p).status_code) for p in PATHS
            if client.get(p).status_code != 200] == []


def test_nothing_the_site_renders_points_at_a_redirect_or_a_404(client):
    """Walk every href on every page shape and demand a 200 without following.

    Not "no 404s" but "no 3xx either": the site now keeps exactly one redirect
    layer, the English->Dutch rename, and it exists for search engines holding old
    URLs — not for the site to link into. A 301 here means a template was missed.
    """
    import re

    seen, bad = set(), []
    for path in PATHS:
        r = client.get(path)
        if "html" not in r.headers.get("content-type", ""):
            continue
        for href in sorted(set(re.findall(r'href="(/[^"#]*)"', r.text))):
            if href in seen:
                continue
            seen.add(href)
            code = client.get(href, follow_redirects=False).status_code
            # The A-Z sort toggle keeps the current letter, and a letter that has
            # authors by surname need not have any by first name — a real 404 in
            # this 5-author fixture, never one in a 22k-author catalog. Predates
            # the rename; not what this test is watching.
            if code != 200 and "sortering=voornaam" not in href:
                bad.append((path, href, code))
    assert bad == [], bad
    assert len(seen) > 40, f"only walked {len(seen)} links — did the crawl break?"


# Every parameter name the routes stopped accepting. FastAPI ignores an unknown
# key silently, so a page emitting one of these still answers 200 — it just does
# nothing the reader asked for. That is invisible to a status-code walk.
RETIRED_KEYS = ("q", "format", "language", "publisher", "author", "list",
                "year_from", "year_to", "sort", "view", "page", "per_page", "show")


def test_no_page_emits_a_parameter_the_routes_no_longer_accept(client):
    """The filter form submitted `format`, `sort`, `language`, … after the rename.

    Nothing 404s and no link is broken: Apply just silently dropped most of what
    was ticked. The href walk above cannot see that, so this reads the retired
    names straight out of the rendered HTML — query keys and form fields both.
    """
    bad = []
    for path in PATHS:
        r = client.get(path)
        if "html" not in r.headers.get("content-type", ""):
            continue
        for key in RETIRED_KEYS:
            for pattern in (f"?{key}=", f"&{key}=", f'name="{key}"'):
                if pattern in r.text:
                    bad.append((path, pattern))
    assert bad == [], bad


def test_the_rename_301_translates_the_old_parameter_names_too(client):
    """A 301 that keeps `?q=` lands you on a page that ignores it.

    The parameters were renamed in the same pass as the paths, so an old URL
    carries old keys — dropping them turns the redirect into a page that quietly
    answers the wrong question. Keys that never changed pass through untouched.
    """
    cases = (("/list/test-top?show=available", "/lijst/test-top?toon=available"),
             ("/authors/w?sort=voornaam", "/auteurs/w?sortering=voornaam"),
             ("/suggest?q=ontdek", "/suggesties?zoek=ontdek"),
             ("/about?ereader=1", "/over?ereader=1"))
    for old, new in cases:
        r = client.get(old, follow_redirects=False)
        assert r.status_code == 301, old
        assert r.headers["location"] == new, old
    assert client.get("/suggesties?zoek=ontdek").json()["titles"]


def test_a_trailing_slash_settles_in_one_hop(client):
    """A slash is punctuation, not a different spelling, so it is normalised.

    Starlette already 307s `/auteurs/`, but the `:path` routes capture the slash
    and 404 on it — so `/auteur/anna-vrij/` was a dead end, and an old English URL
    with a slash 301'd to a Dutch one that then 404'd. Handled before routing now,
    which also makes it a 301 rather than a 307. Renamed paths still settle in a
    single hop: `/authors/` goes straight to `/auteurs`, not via `/authors`.
    """
    for old, new in (("/auteurs/", "/auteurs"),
                     ("/auteur/anna-vrij/", "/auteur/anna-vrij"),
                     ("/reeks/het-mysterie/", "/reeks/het-mysterie"),
                     ("/boek/de-ontdekking--anna-vrij--001/", CANON_001),
                     ("/over/", "/over"), ("/genres/", "/genres")):
        r = client.get(old, follow_redirects=False)
        assert r.status_code == 301, old
        assert r.headers["location"] == new, old
        assert client.get(new).status_code == 200, new


def test_an_old_url_with_a_trailing_slash_still_lands_in_one_hop(client):
    """`/author/anna-vrij/` used to 301 to `/auteur/anna-vrij/` — a 404.

    The `:path` routes capture the trailing slash, so it fails the exact-slug
    check on arrival. The hub paths were merely wasteful: a 301 to `/auteurs/`
    and then Starlette's own 307 to `/auteurs`.
    """
    for old, new in (("/authors/", "/auteurs"),
                     ("/author/anna-vrij/", "/auteur/anna-vrij"),
                     ("/series/het-mysterie/", "/reeks/het-mysterie"),
                     ("/about/", "/over"), ("/lists/", "/lijsten")):
        r = client.get(old, follow_redirects=False)
        assert r.status_code == 301, old
        assert r.headers["location"] == new, old
        assert client.get(new).status_code == 200, new


def test_a_redirect_can_never_leave_our_own_origin():
    """The trailing-slash rule opened an actual open redirect.

    `request.url.path` for `//evil.example/` is `//evil.example/`, and stripping
    the trailing slash left `//evil.example` — which a browser reads as a *host*,
    not a path. Against the running server that was a real
    `301 Location: http://evil.example/`.

    Asserted on the guard rather than through TestClient, which normalises some
    of these shapes away before the app ever sees them — the shapes still reach a
    real server, so the guard is what has to hold.
    """
    from obc.web.app import _is_own_path

    for hostile in ("//evil.example",              # read as a host outright
                    "///evil.example",
                    "/.//evil.example",            # a host once RFC 3986 drops the "."
                    "/auteurs/..//evil.example",   # ... even after the rename rewrote it
                    "/\\evil.example",             # browsers fold a backslash to a slash
                    "/auteurs/\\/evil.example",
                    "evil.example",                # not rooted at all
                    "https://evil.example"):
        assert not _is_own_path(hostile), hostile
    for ours in ("/auteurs", "/auteur/anna-vrij", "/boek/de-ontdekking--anna-vrij--001",
                 "/uitgever/querido-amsterdam", "/genre/spanning-thrillers", "/over"):
        assert _is_own_path(ours), ours


def test_the_shapes_that_are_ours_still_move(client):
    """The guard must not cost the redirects it sits in front of."""
    for path, target in (("/authors/", "/auteurs"), ("/authors//", "/auteurs"),
                         ("/auteur/anna-vrij/", "/auteur/anna-vrij"),
                         ("/boek/de-ontdekking--anna-vrij--001/", CANON_001)):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 301, path
        assert r.headers["location"] == target, path


def test_a_browse_page_links_to_its_own_hub(client):
    """The crumb on browse.html was hard-coded to "Alle genres".

    A publisher page has nothing to do with the genre hub, and neither do
    /e-books and /luisterboeken — which have carried that link since they landed.
    The site header links /genres from every page, so this reads the crumb only.
    """
    import re

    def crumb(path):
        m = re.search(r'<div class="crumb">(.*?)</div>', client.get(path).text, re.S)
        return m.group(1) if m else ""

    assert 'href="/genres">Alle genres' in crumb("/genre/spanning-thrillers")
    for path in ("/uitgever/querido-amsterdam", "/e-books", "/luisterboeken"):
        assert "/genres" not in crumb(path), path
        assert 'href="/"' in crumb(path), path          # still gets its way back


def test_autocomplete_offers_one_row_per_publisher_page(tmp_path):
    """Five spellings of one publisher are one destination, not five suggestions.

    They all fold to the same slug, so publisherHref() sends every one of them to
    the same URL — offering them separately just crowds out distinct results.
    """
    from obc import db
    from obc.web import queries
    path = tmp_path / "pub.db"
    conn = db.connect(path)
    db.bulk_load(conn, [
        {"ppn": "1", "title": "Een", "format": "ebook",
         "publisher": "Keuken Pers, Utrecht"},
        {"ppn": "2", "title": "Twee", "format": "ebook",
         "publisher": "keuken pers, utrecht"},
        {"ppn": "3", "title": "Drie", "format": "ebook",
         "publisher": "Keuken Pers, Utrecht"},
        {"ppn": "4", "title": "Vier", "format": "ebook", "publisher": "Kookboeken BV"}])
    db.build_genre_taxonomy(conn)
    conn.close()

    ro = queries.connect_ro(path)
    assert queries.suggest(ro, "keuken")["publishers"] == ["Keuken Pers, Utrecht"]
    # and that one row is the spelling the publisher page itself puts in its heading
    assert queries.publisher_page(ro, "keuken-pers-utrecht")["name"] == \
        "Keuken Pers, Utrecht"
    ro.close()


def test_everything_that_ranks_publishers_merges_them_the_way_the_page_does():
    """A publisher page merges its spellings; the lists that link to it must too.

    Otherwise one publisher occupies several rows that all go to the same URL,
    with its titles split across them — and ranking on the largest spelling drops
    a merged publisher below a smaller single-spelling one. Live catalog: three
    of the top twelve had a second spelling, and the dashboard counted 1.581
    publishers where there are 1.527 pages.
    """
    import sqlite3

    from obc.web import queries
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE publishers (name TEXT, name_fold TEXT, n INTEGER)")
    conn.executemany("INSERT INTO publishers VALUES (?, ?, ?)", [
        ("Gesplitst, Amsterdam", "gesplitst amsterdam", 6),
        ("gesplitst, amsterdam", "gesplitst amsterdam", 6),
        ("Heel, Utrecht", "heel utrecht", 10)])

    rows = conn.execute(queries._MERGED_PUBLISHERS.format(where="1"), (12,)).fetchall()
    # 6 + 6 outranks 10, and there is one row per page, not one per spelling
    assert [(r["name"], r["n"]) for r in rows] == [
        ("Gesplitst, Amsterdam", 12), ("Heel, Utrecht", 10)]
    # the label is the spelling the publisher page itself would show
    assert queries.publisher_page(conn, "gesplitst-amsterdam")["name"] == \
        "Gesplitst, Amsterdam"


def test_an_edition_only_publisher_is_stated_not_linked(client, monkeypatch):
    """The book page carries *two* Uitgever rows, and neither may point at a filter.

    The work's row links its publisher page. The edition's row — shown when an
    edition names a different publisher — links nothing at all: publisher pages
    are built from `works.publisher`, the representative edition's, so on the live
    catalog 38 edition-only publishers have no page and 5.838 have one that does
    not list this book. It used to point at `/?publisher=`, the English parameter
    the search route stopped accepting, so it opened the unfiltered catalog.

    The fixture has no edition that disagrees with its work, which is why a walk
    over the real catalog found this and the suite did not.
    """
    from obc.web import app as appmod

    real = appmod.queries.book_detail

    def with_a_differing_edition(conn, ppn):
        detail = real(conn, ppn)
        editions = [dict(e) for e in detail["editions"]]
        editions[-1]["publisher"] = "LuisterEffect, Prinsenbeek"
        return {**detail, "editions": editions}

    monkeypatch.setattr(appmod.queries, "book_detail", with_a_differing_edition)
    body = client.get(CANON_001).text
    assert "LuisterEffect, Prinsenbeek" in body            # still stated
    assert "/uitgever/luistereffect-prinsenbeek" not in body   # but not linked
    assert 'href="/uitgever/querido-amsterdam"' in body    # the work's still is
    for key in RETIRED_KEYS:
        assert f"?{key}=" not in body, key
