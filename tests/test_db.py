"""Storage-layer tests: bulk_load / stream_rebuild round-trips, works, FTS."""

from collections import Counter

import sampledata

from obc import db, work


def _build(path, *, stream=False, lists=None):
    conn = db.connect(path)
    if stream:
        # the streaming path expects records that already carry work_id (normalize
        # stamps them in its prepass), so the fixture stamps them the same way
        db.stream_rebuild(conn, work.stamp_work_ids(sampledata.records()), lists)
    else:
        db.bulk_load(conn, sampledata.records(), lists)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()


def test_bulk_load_roundtrip(tmp_path):
    _build(tmp_path / "c.db", lists=sampledata.lists())
    conn = db.connect(tmp_path / "c.db")
    s = db.stats(conn)
    assert s["editions"] == 9
    assert s["works"] == 5
    assert s["ebooks"] == 5        # works available as an e-book
    assert s["audiobooks"] == 3    # ...and as an audiobook (001, 004, 005)
    # many-to-many tables populated
    assert conn.execute("SELECT COUNT(*) FROM authors").fetchone()[0] == 5  # Cara Licht etc.
    assert conn.execute("SELECT COUNT(*) FROM work_lists").fetchone()[0] == 2  # 001, 003
    conn.close()


def test_works_aggregate_their_editions(tmp_path):
    """The work row answers every work-level question once, so no reader re-derives
    it: both availability flags, the per-format PPNs formats_map/editions_map used
    to compute per request, the edition count and the *oldest* year."""
    _build(tmp_path / "c.db")
    conn = db.connect(tmp_path / "c.db")
    w = conn.execute("SELECT * FROM works WHERE work_id = '001'").fetchone()
    assert w["has_ebook"] == 1 and w["has_audiobook"] == 1
    assert w["ebook_ppn"] == "001" and w["audiobook_ppn"] == "002"  # lowest per format
    assert w["n_editions"] == 3
    assert w["year"] == 2020            # MIN across 2020 / 2021 / 2023
    assert w["ereader"] == 1
    # the groups the fixture pins: key, cross-link and format-noise paths
    groups = {r["work_id"]: r["n_editions"] for r in conn.execute(
        "SELECT work_id, n_editions FROM works")}
    assert groups == {"001": 3, "003": 1, "004": 2, "005": 2, "006": 1}
    assert [r["ppn"] for r in conn.execute(
        "SELECT ppn FROM editions WHERE work_id = '001' ORDER BY ppn")] == \
        ["001", "002", "007"]
    conn.close()


def test_works_fts_pools_its_editions_text(tmp_path):
    """A work whose only mention of a word sits on a non-representative edition
    must still be findable — per-edition FTS rows plus a collapse-after-MATCH is
    exactly how a book the catalog holds returned zero results."""
    _build(tmp_path / "c.db")
    conn = db.connect(tmp_path / "c.db")
    rows = conn.execute(
        "SELECT work_id FROM works_fts WHERE works_fts MATCH ?",
        ('"walvisexpeditie"*',)).fetchall()
    assert [r["work_id"] for r in rows] == ["001"]
    conn.close()


def test_fts_match_folds_diacritics(tmp_path):
    _build(tmp_path / "c.db")
    conn = db.connect(tmp_path / "c.db")
    # "espana" must match "España" in work 001's summary (remove_diacritics)
    rows = conn.execute(
        'SELECT work_id FROM works_fts WHERE works_fts MATCH ?', ('"espana"*',)).fetchall()
    assert any(r["work_id"] == "001" for r in rows)
    conn.close()


def test_authors_are_persons_with_the_majority_spelling(tmp_path):
    """One row per person, not per spelling: 009 credits "Bob De Wit" and 003/004
    credit "Bob de Wit". The build merges them on the fold and picks the spelling
    carrying the most credits, so the shelf, the hub and the filter agree without
    a read-time vote."""
    _build(tmp_path / "c.db")
    conn = db.connect(tmp_path / "c.db")
    rows = conn.execute(
        "SELECT name, name_fold, n_works, surname_sort, first_sort FROM authors "
        "WHERE name_fold = 'bob de wit'").fetchall()
    assert len(rows) == 1
    assert rows[0]["name"] == "Bob de Wit"        # 2 credits vs 1 for "Bob De Wit"
    assert rows[0]["n_works"] == 2                # work 003 and work 004
    assert rows[0]["surname_sort"] == "wit"       # a reader looks under W
    assert rows[0]["first_sort"] == "bob-de-wit"
    # the work credited under two spellings gets ONE link (the work_authors PK)
    assert conn.execute("SELECT COUNT(*) FROM work_authors WHERE work_id = '004'"
                        ).fetchone()[0] == 1
    conn.close()


def test_work_slugs_are_title_and_author(tmp_path):
    _build(tmp_path / "c.db")
    conn = db.connect(tmp_path / "c.db")
    slugs = {r["work_id"]: r["slug"] for r in conn.execute(
        "SELECT work_id, slug FROM works")}
    assert slugs["001"] == "de-ontdekking--anna-vrij"
    assert slugs["004"] == "het-mysterie-deel-2--bob-de-wit"
    # never a double hyphen inside a piece, so '--' stays an unambiguous separator
    assert all(p and "--" not in p for s in slugs.values() for p in s.split("--"))
    conn.close()


def test_work_slug_drops_an_empty_piece_with_its_separator(tmp_path):
    recs = [{"ppn": "1", "title": "Zonder Auteur", "format": "ebook"},
            {"ppn": "2", "title": "Λήδα", "author": "Βάρβαρούση", "format": "ebook"}]
    conn = db.connect(tmp_path / "s.db")
    db.bulk_load(conn, recs)
    slugs = {r["work_id"]: r["slug"] for r in conn.execute(
        "SELECT work_id, slug FROM works")}
    conn.close()
    assert slugs["1"] == "zonder-auteur"   # no author -> no trailing separator
    assert slugs["2"] == ""                # nothing slugs -> the route serves the id


def test_series_and_genre_taxonomy_are_built(tmp_path):
    """The series map and the genre taxonomy are deterministic functions of the
    catalog, so the build owns them — the web layer used to rebuild both per
    process (eight cold /genres requests walked 157k rows each)."""
    conn = db.connect(tmp_path / "t.db")
    db.bulk_load(conn, sampledata.records(), sampledata.lists())
    genre_code = {("volwassenen", "Spanning & Thrillers"): "4.0",
                  ("volwassenen", "Thrillers"): "4.10"}
    db.set_work_genre_parents(conn, (genre_code, Counter(dict.fromkeys(genre_code, 1))))
    db.build_genre_taxonomy(conn)

    assert [tuple(r) for r in conn.execute("SELECT slug, name, titles FROM series")] == \
        [("het-mysterie", "Het Mysterie", 1)]
    assert conn.execute(
        "SELECT series_slug FROM works WHERE work_id = '004'").fetchone()[0] == \
        "het-mysterie"
    pages = {r["slug"]: r["titles"] for r in conn.execute(
        "SELECT slug, titles FROM genre_pages")}
    assert pages["spanning-thrillers"] == 2       # works 003 and 004, counted once each
    assert pages["literatuur-romans"] == 2        # works 001 and 006
    # one tree row per (audience, slug); no audience in the fixture -> default shelf
    auds = {r["audience"] for r in conn.execute("SELECT audience FROM genre_tree")}
    assert auds == {"volwassenen"}
    conn.close()


def test_work_genre_parent_resolved_per_audience(tmp_path):
    """Jeugd and volwassenen reuse the same facet numbers, so a genre name shared by
    both (e.g. "Misdaad & Mysterie": jeugd sub of "Spanning & Avontuur", volwassenen
    sub of "Spanning & Thrillers") must get its parent per the work's own audience."""
    recs = [
        {"ppn": "j", "title": "jeugd", "audience": "Jeugd",
         "subjects": ["Spanning & Avontuur", "Misdaad & Mysterie"]},
        {"ppn": "v", "title": "volw", "audience": "Volwassenen",
         "subjects": ["Spanning & Thrillers", "Misdaad & Mysterie"]},
    ]
    conn = db.connect(tmp_path / "g.db")
    db.bulk_load(conn, recs)
    genre_code = {
        ("jeugd", "Spanning & Avontuur"): "4.0",
        ("jeugd", "Misdaad & Mysterie"): "4.1",
        ("volwassenen", "Spanning & Thrillers"): "4.0",
        ("volwassenen", "Spanning & Avontuur"): "4.0",  # a name leaked into volwassenen
        ("volwassenen", "Misdaad & Mysterie"): "4.1",
    }
    genre_count = Counter({
        ("jeugd", "Spanning & Avontuur"): 5, ("jeugd", "Misdaad & Mysterie"): 5,
        ("volwassenen", "Spanning & Thrillers"): 50,    # the real volwassenen 4.0
        ("volwassenen", "Spanning & Avontuur"): 1,      # the rare leak
        ("volwassenen", "Misdaad & Mysterie"): 50,
    })
    db.set_work_genre_parents(conn, (genre_code, genre_count))

    def parent(work_id):
        return conn.execute(
            "SELECT p.name FROM work_genres wg JOIN genres g ON g.id = wg.genre_id "
            "LEFT JOIN genres p ON p.id = wg.parent_id "
            "WHERE wg.work_id = ? AND g.name = 'Misdaad & Mysterie'", (work_id,)
        ).fetchone()[0]

    assert parent("j") == "Spanning & Avontuur"    # jeugd work -> jeugd parent
    assert parent("v") == "Spanning & Thrillers"   # most-common wins over the leak
    conn.close()


def test_editions_of_a_work_lookup_uses_index_not_scan(tmp_path):
    """The book page's "editions of this work" lookup is now one indexed read on
    editions.work_id, replacing a case-insensitive (title, author) match that
    full-scanned every row (~4s on Fly's shared CPU)."""
    conn = db.connect(tmp_path / "x.db")
    db.bulk_load(conn, sampledata.records(), sampledata.lists())
    plan = " ".join(r["detail"] for r in conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM editions WHERE work_id = '001'"))
    conn.close()
    assert "idx_editions_work" in plan and "SCAN editions" not in plan, plan


def test_stream_rebuild_equivalent_to_bulk_load(tmp_path):
    """The low-memory streaming path must produce the same catalog as bulk_load."""
    _build(tmp_path / "bulk.db", stream=False, lists=sampledata.lists())
    _build(tmp_path / "stream.db", stream=True, lists=sampledata.lists())

    def snapshot(path):
        conn = db.connect(path)
        snap = {
            "editions": conn.execute("SELECT COUNT(*) FROM editions").fetchone()[0],
            "works": sorted(tuple(r) for r in conn.execute(
                "SELECT work_id, slug, n_editions, has_ebook, has_audiobook, year "
                "FROM works")),
            "genres": sorted(r["name"] for r in conn.execute("SELECT name FROM genres")),
            "authors": sorted(tuple(r) for r in conn.execute(
                "SELECT name, name_fold, n_works, surname_sort FROM authors")),
            "work_authors": conn.execute("SELECT COUNT(*) FROM work_authors").fetchone()[0],
            "work_genres": conn.execute("SELECT COUNT(*) FROM work_genres").fetchone()[0],
            "publishers": sorted(tuple(r) for r in conn.execute("SELECT name, n FROM publishers")),
            "languages": sorted(tuple(r) for r in conn.execute("SELECT name, n FROM languages")),
            "series": sorted(tuple(r) for r in conn.execute("SELECT slug, name, titles FROM series")),
            "fts": conn.execute("SELECT COUNT(*) FROM works_fts").fetchone()[0],
            "work_lists": conn.execute("SELECT COUNT(*) FROM work_lists").fetchone()[0],
        }
        conn.close()
        return snap

    assert snapshot(tmp_path / "bulk.db") == snapshot(tmp_path / "stream.db")


def test_browse_sorts_are_indexed(tmp_path):
    """Every queries.SORTS ordering needs an index on works, so a browse page reads
    its page from the index instead of sorting every match in a temp B-tree. Every
    works row is a book, so no boolean prefix column is needed any more."""
    from obc.web import queries
    _build(tmp_path / "c.db")
    conn = db.connect(tmp_path / "c.db")
    # sql IS NULL for SQLite's own auto-indexes (sqlite_autoindex_works_1)
    idx = {r["name"]: r["sql"] for r in conn.execute(
        "SELECT name, sql FROM sqlite_master "
        "WHERE type='index' AND tbl_name='works' AND sql IS NOT NULL")}
    assert "year DESC" in idx["idx_works_year"]
    assert "added_rank" in idx["idx_works_added"]
    assert "COLLATE NOCASE" in idx["idx_works_title"]
    # every sort key (bar relevance, which is bm25 over the FTS table) is covered
    for name in set(queries.SORTS) - {"relevance", "year_asc"}:
        key = name.removesuffix("_desc")
        assert any(key in sql for sql in idx.values()), f"no index serves sort={name}"
    # the primary_edition family is gone, not renamed
    assert not [n for n in idx if "primary" in n]
    conn.close()


def test_rebuild_collects_index_statistics(tmp_path):
    """Both rebuild paths must leave sqlite_stat1 populated. Without statistics the
    planner may drive a genre/auteur/list filter off the works side and walk every
    row instead of the few thousand link entries for that value."""
    for stream in (False, True):
        path = tmp_path / f"c{int(stream)}.db"
        _build(path, stream=stream)
        conn = db.connect(path)
        tables = {r[0] for r in conn.execute("SELECT tbl FROM sqlite_stat1")}
        assert "works" in tables, f"no statistics for works (stream={stream})"
        conn.close()


def test_load_prior_ereader_falls_back_to_the_old_books_table(tmp_path):
    """On the deploy that introduces this schema the live DB still has the old
    shape, and this function exists to stop a rebuild blanking the whole e-reader
    facet — so it must read either table."""
    import sqlite3
    new = tmp_path / "new.db"
    _build(new)
    assert db.load_prior_ereader(new)["001"] == 1

    old = tmp_path / "old.db"
    conn = sqlite3.connect(old)
    conn.executescript("CREATE TABLE books (ppn TEXT PRIMARY KEY, ereader INTEGER);"
                       "INSERT INTO books VALUES ('001', 1), ('003', 0), ('x', NULL);")
    conn.commit()
    conn.close()
    assert db.load_prior_ereader(old) == {"001": 1, "003": 0}
    assert db.load_prior_ereader(tmp_path / "absent.db") == {}
