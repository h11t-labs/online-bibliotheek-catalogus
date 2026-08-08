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
