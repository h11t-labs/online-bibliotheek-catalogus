"""Data-access layer (obc.web.queries) against the hermetic fixture catalog."""

from obc.web import queries as Q


def _ppns(result):
    return {r["ppn"] for r in result.rows}


def test_book_detail_tolerates_pre_hierarchy_schema(tmp_path):
    """A catalog built before the book_genres.parent_id column (the window right
    after a schema-changing deploy) must not 503 the book page — book_detail falls
    back to a flat genre list instead of raising OperationalError."""
    import sampledata

    from obc import db
    path = tmp_path / "old.db"
    conn = db.connect(path)
    db.bulk_load(conn, sampledata.records(), sampledata.lists())
    # rebuild book_genres without parent_id (the old schema)
    conn.executescript(
        "PRAGMA foreign_keys=OFF;"
        "CREATE TABLE bg_old (book_ppn TEXT, genre_id INTEGER, PRIMARY KEY(book_ppn, genre_id));"
        "INSERT INTO bg_old(book_ppn, genre_id) SELECT book_ppn, genre_id FROM book_genres;"
        "DROP TABLE book_genres;"
        "ALTER TABLE bg_old RENAME TO book_genres;")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    ro = Q.connect_ro(path)
    detail = Q.book_detail(ro, "001")
    ro.close()
    assert detail is not None
    assert all(g["parent"] is None for g in detail["genres"])  # flat fallback


def test_browse_all_newest_first(ro_conn):
    # 6 editions but 001+002 are one work (e-book+audiobook) -> merged to 5 results
    res = Q.search(ro_conn, Q.SearchFilters(sort="year_desc"), 1, 50)
    assert res.total == 5
    years = [r["year"] for r in res.rows]
    assert years == sorted(years, reverse=True)


def test_format_filter(ro_conn):
    res = Q.search(ro_conn, Q.SearchFilters(format="audiobook"), 1, 50)
    assert _ppns(res) == {"002"}


def test_fts_query_matches_title_and_summary(ro_conn):
    # both editions match, but they are one work -> merged to the e-book representative
    res = Q.search(ro_conn, Q.SearchFilters(q="ontdekking", sort="relevance"), 1, 50)
    assert _ppns(res) == {"001"}


def test_fts_folds_diacritics(ro_conn):
    res = Q.search(ro_conn, Q.SearchFilters(q="espana"), 1, 50)
    assert "001" in _ppns(res)


def test_language_and_year_filters(ro_conn):
    assert _ppns(Q.search(ro_conn, Q.SearchFilters(languages=("Engels",)), 1, 50)) == {"003"}
    res = Q.search(ro_conn, Q.SearchFilters(year_from=2020, year_to=2021), 1, 50)
    assert _ppns(res) == {"001"}  # 001+002 are one work -> merged


def test_ereader_author_genre_list_filters(ro_conn):
    assert _ppns(Q.search(ro_conn, Q.SearchFilters(ereader=True), 1, 50)) == {"001", "005"}
    assert _ppns(Q.search(ro_conn, Q.SearchFilters(authors=("Cara Licht",)), 1, 50)) == {"003"}
    assert _ppns(Q.search(
        ro_conn, Q.SearchFilters(genres=("Spanning & Thrillers",)), 1, 50)) == {"003", "004"}
    assert _ppns(Q.search(ro_conn, Q.SearchFilters(lists=("test-top",)), 1, 50)) == {"001", "003"}


def test_pagination(ro_conn):
    page1 = Q.search(ro_conn, Q.SearchFilters(sort="title"), 1, 2)
    page2 = Q.search(ro_conn, Q.SearchFilters(sort="title"), 2, 2)
    assert page1.total == 5  # 6 editions, 001+002 merged into one work
    assert len(page1.rows) == 2
    assert _ppns(page1).isdisjoint(_ppns(page2))


def test_formats_map_links_both_editions(ro_conn):
    res = Q.search(ro_conn, Q.SearchFilters(format="ebook"), 1, 50)
    fmap = Q.formats_map(ro_conn, res.rows)
    assert fmap["001"] == ["audiobook", "ebook"]  # the work exists in both


def test_compute_facets(ro_conn):
    f = Q.compute_facets(ro_conn)
    assert set(f["formats"]) == {"audiobook", "ebook"}
    assert "Nederlands" in f["languages"]
    assert any(lst["slug"] == "test-top" for lst in f["lists"])


def test_suggest(ro_conn):
    titles = Q.suggest(ro_conn, "ontdek", 7)["title_rows"]
    assert any(r["ppn"] == "001" for r in titles)
    assert "Anna Vrij" in Q.suggest(ro_conn, "anna", 7)["authors"]  # author autocomplete
    assert Q.suggest(ro_conn, "", 7) is None


def test_suggest_matches_keywords_not_just_title(ro_conn):
    # "italiaans" is only in book 005's keywords (Trefwoorden), not its title/subjects.
    # The live search-bar dropdown used to only match the title column, so a keyword-only
    # term showed nothing there even though the full search page found it.
    titles = Q.suggest(ro_conn, "italiaans", 7)["title_rows"]
    assert any(r["ppn"] == "005" for r in titles)
    assert titles[0]["format"] in ("ebook", "audiobook")  # format is available to render


def test_facet_values(ro_conn):
    assert "Anna Vrij" in Q.facet_values(ro_conn, "author")
    assert "Querido, Amsterdam" in Q.facet_values(ro_conn, "publisher")
    assert Q.facet_values(ro_conn, "bogus") == []


def test_limit_clamps_hostile_values(ro_conn):
    # SQLite treats LIMIT -1 as unlimited, so callers must clamp. suggest/facet_values
    # do this defensively even when reached outside the (validated) HTTP routes.
    assert Q._limit(-1, 7, 20) == 1
    assert Q._limit(999, 30, 50) == 50
    assert Q._limit("x", 7, 20) == 7  # junk -> default
    # a negative limit must not turn into "all rows"
    assert len(Q.suggest(ro_conn, "e", -1)["title_rows"]) <= 20
    assert len(Q.facet_values(ro_conn, "author", "", -1)) <= 50


def test_book_detail_hides_top_genre_shown_via_a_subgenre_chip(tmp_path):
    # A book tagged with both "Literatuur & Romans" and its sub "Sociale romans" (and
    # likewise for Spanning & Thrillers) should not show the top-level genre as its own
    # separate chip — "Literatuur & Romans › Sociale romans" already conveys it. A
    # top-level genre with no child present ("Gezin & Gezondheid") must still show.
    from obc import db
    recs = [{"ppn": "1", "title": "x", "audience": "Volwassenen",
             "subjects": ["Literatuur & Romans", "Sociale romans",
                          "Spanning & Thrillers", "Historische spanning",
                          "Gezin & Gezondheid"]}]
    from collections import Counter
    conn = db.connect(tmp_path / "g.db")
    db.bulk_load(conn, recs)
    genre_code = {
        ("volwassenen", "Literatuur & Romans"): "2.0",
        ("volwassenen", "Sociale romans"): "2.1",
        ("volwassenen", "Spanning & Thrillers"): "4.0",
        ("volwassenen", "Historische spanning"): "4.1",
        ("volwassenen", "Gezin & Gezondheid"): "10.0",
    }
    db.set_book_genre_parents(conn, (genre_code, Counter(dict.fromkeys(genre_code, 1))))
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    ro = Q.connect_ro(tmp_path / "g.db")
    names = {g["name"] for g in Q.book_detail(ro, "1")["genres"]}
    ro.close()
    assert "Literatuur & Romans" not in names   # superseded by its child chip
    assert "Spanning & Thrillers" not in names  # superseded by its child chip
    assert "Sociale romans" in names and "Historische spanning" in names
    assert "Gezin & Gezondheid" in names        # no child -> stays visible


def test_book_detail(ro_conn):
    detail = Q.book_detail(ro_conn, "001")
    assert detail["row"]["title"] == "De Ontdekking"
    assert detail["editions"].get("audiobook") == "002"  # the audiobook edition
    assert "Anna Vrij" in detail["authors"]
    bl = next(b for b in detail["book_lists"] if b["slug"] == "test-top")
    assert bl["won"] == 1  # carried through from the list item
    assert Q.book_detail(ro_conn, "nope") is None


def test_author_and_series(ro_conn):
    assert len(Q.author_books(ro_conn, "Anna Vrij")) == 2
    assert {r["ppn"] for r in Q.series_books(ro_conn, "Het Mysterie")} == {"004"}


def test_lists_overview_counts(ro_conn):
    row = next(r for r in Q.lists_overview(ro_conn, "name") if r["slug"] == "test-top")
    assert row["total"] == 3
    assert row["available"] == 2
    items = Q.list_items(ro_conn, Q.list_row(ro_conn, "test-top")["id"])
    assert len(items) == 3


def test_web_stats(ro_conn):
    s = Q.web_stats(ro_conn)
    assert s["total"] == 6
    assert s["ebooks"] == 5
    assert s["audiobooks"] == 1


def test_web_stats_genres_carry_parent(tmp_path):
    # The stats page's genre bars show "Parent › Kind" like the book page — each row
    # is (name, parent, count); a top-level genre's own row has parent=None.
    from collections import Counter

    from obc import db
    recs = [{"ppn": "1", "title": "x", "audience": "Volwassenen",
             "subjects": ["Literatuur & Romans", "Sociale romans"]}]
    conn = db.connect(tmp_path / "g.db")
    db.bulk_load(conn, recs)
    genre_code = {("volwassenen", "Literatuur & Romans"): "2.0",
                  ("volwassenen", "Sociale romans"): "2.1"}
    db.set_book_genre_parents(conn, (genre_code, Counter(dict.fromkeys(genre_code, 1))))
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    ro = Q.connect_ro(tmp_path / "g.db")
    rows = {r["name"]: r["parent"] for r in Q.web_stats(ro)["genres"]}
    ro.close()
    assert rows["Literatuur & Romans"] is None
    assert rows["Sociale romans"] == "Literatuur & Romans"


def test_web_stats_tolerates_pre_hierarchy_schema(tmp_path):
    """web_stats must keep /stats online against a catalog built before the
    book_genres.parent_id column (the window right after a schema-changing deploy),
    falling back to flat parent-less genre rows instead of raising."""
    import sampledata

    from obc import db
    path = tmp_path / "old.db"
    conn = db.connect(path)
    db.bulk_load(conn, sampledata.records(), sampledata.lists())
    # rebuild book_genres without parent_id (the old schema)
    conn.executescript(
        "PRAGMA foreign_keys=OFF;"
        "CREATE TABLE bg_old (book_ppn TEXT, genre_id INTEGER, PRIMARY KEY(book_ppn, genre_id));"
        "INSERT INTO bg_old(book_ppn, genre_id) SELECT book_ppn, genre_id FROM book_genres;"
        "DROP TABLE book_genres;"
        "ALTER TABLE bg_old RENAME TO book_genres;")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    ro = Q.connect_ro(path)
    s = Q.web_stats(ro)
    ro.close()
    assert s["total"] == 6
    assert all(r["parent"] is None for r in s["genres"])  # flat fallback


def test_relevance_weights_subjects_above_summary(tmp_path):
    """bm25 weights are positional over ALL fts columns incl. the UNINDEXED ppn, so
    the ranking needs 5 weights (0.0 for ppn). With the old 4-weight expression the
    10.0 lands on ppn and subjects/summary both get 1.0 — a subjects-only hit and a
    summary-only hit then score identically. The two books below are byte-identical
    except which column holds the unique term (so bm25 length-normalisation is the
    same for both); only the column weight can break the tie. New weights rank the
    subjects hit strictly first; the old ones tie (rowid order -> summary first)."""
    from obc import db
    term = "zqxwordtest"
    filler_subj, filler_summ = "vulonderwerp", "vulsamenvatting korte zin"
    # SUM inserted first (lower rowid): on the tied old weights it sorts ahead,
    # which is exactly the wrong order the fix corrects.
    # distinct authors (same token length) so the edition-merge keeps them as two
    # separate works — the term lives in subjects/summary, never the author, so bm25
    # length-normalisation stays identical and only the column weight breaks the tie.
    recs = [
        {"ppn": "SUM", "title": "Zelfde titel", "author": "Auteur Aaa",
         "authors": ["Auteur Aaa"], "format": "ebook", "language": "Nederlands",
         "subjects": [filler_subj], "summary": f"{filler_summ} {term}"},
        {"ppn": "SUB", "title": "Zelfde titel", "author": "Auteur Bbb",
         "authors": ["Auteur Bbb"], "format": "ebook", "language": "Nederlands",
         "subjects": [filler_subj, term], "summary": filler_summ},
    ]
    # ~20 fillers so the term isn't in every row (bm25 IDF is 0 otherwise).
    recs += [
        {"ppn": f"F{i:02d}", "title": "Zelfde titel", "author": "Zelfde Auteur",
         "authors": ["Zelfde Auteur"], "format": "ebook", "language": "Nederlands",
         "subjects": [filler_subj], "summary": filler_summ}
        for i in range(20)
    ]
    path = tmp_path / "rel.db"
    conn = db.connect(path)
    db.bulk_load(conn, recs, [])
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    ro = Q.connect_ro(path)
    res = Q.search(ro, Q.SearchFilters(q=term, sort="relevance"), 1, 50)
    ro.close()
    order = [r["ppn"] for r in res.rows]
    assert order == ["SUB", "SUM"]
