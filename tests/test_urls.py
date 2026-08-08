"""The URL scheme itself: every route answers, and nothing links off it.

A rename is exactly the change that leaves a template pointing at a path that no
longer exists, so this file walks the site rather than asserting one page at a
time. It is the guard the /author -> /auteur pass would have needed.
"""

# Every public URL shape, with its Dutch query parameters.
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


def test_a_retired_parameter_is_redirected_even_when_the_path_never_moved(client):
    """`/?q=ontdek` is a bookmark from before the rename.

    The path is fine — `/` never moved — so the rename table has nothing to say
    about it, and `/` accepts only `zoek` now. Without a redirect it answers 200
    with the entire unfiltered catalog, which looks like the search silently
    breaking. A query that needs nothing must not be touched, or every `+` versus
    `%20` difference would cost a 301.
    """
    for old, new in (("/?q=ontdek", "/?zoek=ontdek"),
                     ("/?format=audiobook", "/?formaat=audiobook"),
                     ("/?sort=title&page=2", "/?sortering=title&pagina=2"),
                     ("/lijst/test-top?show=available", "/lijst/test-top?toon=available")):
        r = client.get(old, follow_redirects=False)
        assert r.status_code == 301, old
        assert r.headers["location"] == new, old
        assert client.get(new).status_code == 200, new
    # untouched: no retired key, so no redirect at all
    for fine in ("/?zoek=ontdek", "/?genre=Spanning+%26+Thrillers", "/?ereader=1",
                 "/genres?publiek=jeugd", "/facetten?type=author&zoek=a"):
        assert client.get(fine, follow_redirects=False).status_code == 200, fine


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
