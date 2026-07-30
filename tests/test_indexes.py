"""The indexes derived once per catalog rebuild (obc.web.indexes).

The A-Z author hub, the genre tree and the slug maps they are keyed on — plus
the invalidation that ties all of them to the catalog file itself."""


from helpers import jsonld


def test_hub_lists_one_entry_per_slug(client, ro_conn):
    # the hub must not show "Ad Van Schaik" and "Ad van Schaik" as two entries
    # that lead to the same page — folding decides both the entry and who
    # qualifies, since two rows of one title each are one author with two
    from obc.textnorm import slugify
    from obc.web import indexes as indexmod
    index = indexmod.authors_by_letter(ro_conn)
    entries = [e for rows in index.values() for e in rows]
    slugs = [slugify(e["name"]) for e in entries]
    assert len(set(slugs)) == len(slugs), "two hub entries share a slug"
    hub = client.get("/authors/w").text
    assert hub.count('href="/author/bob-de-wit"') == 1


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
    from obc.web import indexes as indexmod
    by_surname = indexmod.authors_by_letter(ro_conn, indexmod.BY_SURNAME)
    by_first = indexmod.authors_by_letter(ro_conn, indexmod.BY_FIRST)
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
    from obc.web import indexes as indexmod
    from obc.web import queries
    for rows in indexmod.authors_by_letter(ro_conn).values():
        for entry in rows:
            fold_key = slugify(entry["name"]).replace("-", " ")
            shelf = queries.author_books_by_fold(ro_conn, fold_key)
            assert entry["titles"] == len(shelf), entry["name"]


def test_unsluggable_authors_are_not_merged_into_one_entry(client, ro_conn):
    # fold() returns "" for a name with no Latin characters; using that as a merge
    # key would fuse every such author into a single hub entry with a summed count
    from obc.web import indexes as indexmod
    entries = [e for rows in indexmod.authors_by_letter(ro_conn).values() for e in rows]
    assert not [e for e in entries if not e["name"].strip()]
    from obc.textnorm import slugify
    assert all(slugify(e["name"]) for e in entries), "an entry has no slug to link to"


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
    from obc.web import indexes as indexmod
    from obc.web import queries
    path = _catalog_with_genre_parents(tmp_path)
    monkeypatch.setattr(indexmod, "DB_PATH", path)
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
    crumbs = [d for d in jsonld(child) if d.get("@type") == "BreadcrumbList"][0]
    assert [i["name"] for i in crumbs["itemListElement"]] == [
        "Home", "Genres", "Spanning & Thrillers", "Thrillers"]

    parent = client.get("/genre/spanning-thrillers").text
    assert 'class="subgenres"' in parent and 'href="/genre/thrillers"' in parent
    conn.close()




def test_the_hub_orders_on_the_stamped_keys(ro_conn):
    """No sort key is recomputed per request.

    `surname_sort` and `first_sort` are `surname_key(name)` and `slugify(name)`,
    stamped at build time. The hub used to call both again for every row it
    returned — 22,383 calls per request on the live catalog, to rebuild what the
    columns already held, which is most of the 7.5s the /authors page took. This
    asserts the ordering is the same as doing it the slow way, so the speed-up
    cannot quietly reshuffle a reader-facing index.
    """
    from obc.textnorm import slugify, surname_key
    from obc.web import indexes, queries

    for by in (indexes.BY_SURNAME, indexes.BY_FIRST):
        field = "surname_sort" if by == indexes.BY_SURNAME else "first_sort"
        slow: dict[str, list[dict]] = {}
        for row in queries.author_index(ro_conn):
            if not row["first_sort"]:
                continue
            slow.setdefault(indexes.author_letter(row[field]), []).append(
                {"name": row["name"], "titles": row["titles"]})
        for rows in slow.values():
            rows.sort(key=lambda e: (surname_key(e["name"]) if by == indexes.BY_SURNAME
                                     else slugify(e["name"]), slugify(e["name"])))

        assert indexes.authors_by_letter(ro_conn, by) == slow, by


def test_a_letter_page_reads_its_own_letter(ro_conn):
    """Cost proportional to the page, not to the catalog.

    The hub renders 27 counts and no authors; a letter page renders one letter.
    Both used to bucket all 22,383 authors first, so the catch-all — three people
    on the live catalog — cost the same 2.2s as the 2,562 under B. The letter is
    stamped at build time now (`authors.surname_letter` / `first_letter`).
    """
    from obc.web import indexes

    counts = indexes.letter_counts(ro_conn)
    assert counts, "fixture has no authors to bucket"
    # the same buckets the whole-catalog pass produces, which the sitemap still uses
    whole = indexes.authors_by_letter(ro_conn)
    assert counts == {ltr: len(rows) for ltr, rows in whole.items()}

    for by in (indexes.BY_SURNAME, indexes.BY_FIRST):
        for letter in indexes.letter_counts(ro_conn, by):
            assert indexes.authors_in_letter(ro_conn, letter, by) == \
                indexes.authors_by_letter(ro_conn, by)[letter], (by, letter)

    # the stamped letter is reachable by index, not by scanning the table
    plan = " ".join(r[-1] for r in ro_conn.execute(
        "EXPLAIN QUERY PLAN SELECT name FROM authors WHERE surname_letter = 'A'"))
    assert "SCAN authors" not in plan, plan
    assert "INDEX" in plan, plan


def test_the_stamped_letter_matches_the_sort_key(ro_conn):
    from obc import db
    for row in ro_conn.execute("SELECT surname_sort, surname_letter, first_sort, "
                               "first_letter FROM authors WHERE first_sort <> ''"):
        assert row["surname_letter"] == db._letter(row["surname_sort"])
        assert row["first_letter"] == db._letter(row["first_sort"])
