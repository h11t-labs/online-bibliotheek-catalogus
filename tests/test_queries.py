"""Data-access layer (obc.web.queries) against the hermetic fixture catalog.

The fixture is 9 editions forming 5 works (see sampledata), so every assertion
below is about *books* — which is the whole point of the model.
"""

from obc.web import queries as Q


def _ppns(result):
    return {r["work_id"] for r in result.rows}


def test_browse_all_newest_first(ro_conn):
    res = Q.search(ro_conn, Q.SearchFilters(sort="year_desc"), 1, 50)
    assert res.total == 5
    years = [r["year"] for r in res.rows]
    assert years == sorted(years, reverse=True)


def test_format_filter_counts_works_available_in_that_format(ro_conn):
    """``?formaat=audiobook`` means "books you can listen to", not "audiobook rows".

    The old model skipped its own collapse whenever a format filter was set, on
    the assumption that a work then has only that one edition — false for work 001
    (two audiobooks), and exactly why /luisterboeken was removed again.
    """
    res = Q.search(ro_conn, Q.SearchFilters(format="audiobook"), 1, 50)
    assert _ppns(res) == {"001", "004", "005"}
    assert res.total == 3
    assert _ppns(Q.search(ro_conn, Q.SearchFilters(format="ebook"), 1, 50)) == \
        {"001", "003", "004", "005", "006"}


def test_fts_finds_a_work_by_a_word_only_its_audiobook_carries(ro_conn):
    """"walvisexpeditie" lives only in edition 007's summary. Per-edition FTS rows
    plus a collapse-after-MATCH returned zero results for a book the catalog holds;
    the work's FTS row pools its editions' text, so it is findable."""
    res = Q.search(ro_conn, Q.SearchFilters(q="walvisexpeditie"), 1, 50)
    assert _ppns(res) == {"001"}


def test_fts_query_matches_title_and_summary(ro_conn):
    res = Q.search(ro_conn, Q.SearchFilters(q="ontdekking", sort="relevance"), 1, 50)
    assert _ppns(res) == {"001"}


def test_fts_folds_diacritics(ro_conn):
    res = Q.search(ro_conn, Q.SearchFilters(q="espana"), 1, 50)
    assert "001" in _ppns(res)


def test_language_and_year_filters(ro_conn):
    assert _ppns(Q.search(ro_conn, Q.SearchFilters(languages=("Engels",)), 1, 50)) == {"003"}
    # work 001's year is the oldest of its editions (2020), not the audiobook's
    res = Q.search(ro_conn, Q.SearchFilters(year_from=2020, year_to=2021), 1, 50)
    assert _ppns(res) == {"001"}


def test_ereader_author_genre_list_filters(ro_conn):
    assert _ppns(Q.search(ro_conn, Q.SearchFilters(ereader=True), 1, 50)) == {"001", "005"}
    assert _ppns(Q.search(ro_conn, Q.SearchFilters(authors=("Cara Licht",)), 1, 50)) == {"003"}
    assert _ppns(Q.search(
        ro_conn, Q.SearchFilters(genres=("Spanning & Thrillers",)), 1, 50)) == {"003", "004"}
    assert _ppns(Q.search(ro_conn, Q.SearchFilters(lists=("test-top",)), 1, 50)) == {"001", "003"}


def test_author_filter_matches_a_variant_spelling(ro_conn):
    """Only the canonical spelling survives as authors.name now, so an ?auteur= URL
    carrying a variant — a link someone already has, or a stale crawl — has to keep
    working. The filter matches on the fold."""
    for spelling in ("Bob de Wit", "Bob De Wit", "BOB DE WIT"):
        assert _ppns(Q.search(ro_conn, Q.SearchFilters(authors=(spelling,)), 1, 50)) == \
            {"003", "004"}, spelling


def test_pagination(ro_conn):
    page1 = Q.search(ro_conn, Q.SearchFilters(sort="title"), 1, 2)
    page2 = Q.search(ro_conn, Q.SearchFilters(sort="title"), 2, 2)
    assert page1.total == 5
    assert len(page1.rows) == 2
    assert _ppns(page1).isdisjoint(_ppns(page2))


def test_work_rows_carry_the_format_flags_and_edition_ppns(ro_conn):
    """What formats_map / editions_map used to compute with two extra queries per
    result page now rides along on the row the page already fetched."""
    row = next(r for r in Q.search(ro_conn, Q.SearchFilters(), 1, 50).rows
               if r["work_id"] == "001")
    assert row["has_ebook"] == 1 and row["has_audiobook"] == 1
    assert row["ebook_ppn"] == "001" and row["audiobook_ppn"] == "002"


def test_compute_facets(ro_conn):
    f = Q.compute_facets(ro_conn)
    assert set(f["formats"]) == {"audiobook", "ebook"}
    assert "Nederlands" in f["languages"]
    assert "Bob de Wit" in f["authors"]
    assert any(lst["slug"] == "test-top" for lst in f["lists"])


def test_suggest(ro_conn):
    titles = Q.suggest(ro_conn, "ontdek", 7)["title_rows"]
    assert len(titles) == 1                      # one row per work, never a twin
    assert titles[0]["work_id"] == "001"
    assert titles[0]["ebook_ppn"] == "001" and titles[0]["audiobook_ppn"] == "002"
    assert titles[0]["slug"] == "de-ontdekking--anna-vrij"
    assert "Anna Vrij" in Q.suggest(ro_conn, "anna", 7)["authors"]  # author autocomplete
    assert Q.suggest(ro_conn, "", 7) is None


def test_suggest_matches_keywords_not_just_title(ro_conn):
    # "italiaans" is only in work 005's keywords (Trefwoorden), not its title/subjects.
    # The live search-bar dropdown used to only match the title column, so a keyword-only
    # term showed nothing there even though the full search page found it.
    titles = Q.suggest(ro_conn, "italiaans", 7)["title_rows"]
    assert any(r["work_id"] == "005" for r in titles)
    assert titles[0]["format"] in ("ebook", "audiobook")  # format is available to render


def test_facet_values(ro_conn):
    assert "Anna Vrij" in Q.facet_values(ro_conn, "author")
    assert "Querido, Amsterdam" in Q.facet_values(ro_conn, "publisher")
    assert Q.facet_values(ro_conn, "bogus") == []


def test_limit_clamps_hostile_values(ro_conn):
    # SQLite treats LIMIT -1 as unlimited, so callers must clamp. suggest/facetten_values
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
    from collections import Counter

    from obc import db
    recs = [{"ppn": "1", "title": "x", "audience": "Volwassenen",
             "subjects": ["Literatuur & Romans", "Sociale romans",
                          "Spanning & Thrillers", "Historische spanning",
                          "Gezin & Gezondheid"]}]
    conn = db.connect(tmp_path / "g.db")
    db.bulk_load(conn, recs)
    genre_code = {
        ("volwassenen", "Literatuur & Romans"): "2.0",
        ("volwassenen", "Sociale romans"): "2.1",
        ("volwassenen", "Spanning & Thrillers"): "4.0",
        ("volwassenen", "Historische spanning"): "4.1",
        ("volwassenen", "Gezin & Gezondheid"): "10.0",
    }
    db.set_work_genre_parents(conn, (genre_code, Counter(dict.fromkeys(genre_code, 1))))
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
    assert detail["work"]["title"] == "De Ontdekking"
    # the summary is the longest non-empty across the work's three editions — the
    # representative is chosen by format, so it is not always the fullest blurb
    assert detail["work"]["summary"] == max(
        (e["summary"] for e in detail["editions"] if e["summary"]), key=len)
    assert [e["ppn"] for e in detail["editions"]] == ["001", "002", "007"]
    assert detail["editions"][0]["format"] == "ebook"     # e-book first
    assert detail["editions"][1]["narrator"] == "Jan Stem"
    assert "Anna Vrij" in detail["authors"]
    bl = next(b for b in detail["work_lists"] if b["slug"] == "test-top")
    assert bl["won"] == 1  # carried through from the list item


def test_book_detail_redirects_a_non_representative_edition(ro_conn):
    """/book/{audiobook_ppn} used to render a near-identical duplicate page. It
    resolves to its work now, so the route can 301 and the link keeps working."""
    assert Q.book_detail(ro_conn, "002") == {"redirect": "001"}
    assert Q.book_detail(ro_conn, "009") == {"redirect": "004"}
    assert Q.book_detail(ro_conn, "nope") is None


def test_author_shelves_and_series(ro_conn):
    # work 001 is three editions but one card — the shelf shows books
    assert len(Q.author_books(ro_conn, "Anna Vrij")) == 1
    assert len(Q.author_books_by_fold(ro_conn, "anna vrij")) == 1
    # 003 + the merged 004/009: one shelf despite the two spellings of his name
    assert {r["work_id"] for r in Q.author_books_by_fold(ro_conn, "bob de wit")} == \
        {"003", "004"}
    assert Q.author_display_name(ro_conn, "anna vrij") == "Anna Vrij"
    assert Q.author_display_name(ro_conn, "niemand") is None
    assert {r["work_id"] for r in Q.series_books(ro_conn, "het-mysterie")} == {"004"}
    assert Q.series_books(ro_conn, "geen-reeks") == []
    assert Q.series_row(ro_conn, "het-mysterie")["name"] == "Het Mysterie"
    assert Q.series_row(ro_conn, "geen-reeks") is None


def test_authors_are_persons_with_one_row_and_the_majority_spelling(ro_conn):
    rows = Q.author_index(ro_conn)
    assert len(rows) == 5                      # five people, not six spellings
    wit = next(r for r in rows if r["name_fold"] == "bob de wit")
    assert wit["name"] == "Bob de Wit"         # majority spelling, chosen at build
    assert wit["titles"] == 2
    assert wit["surname_sort"] == "wit" and wit["first_sort"] == "bob-de-wit"


def test_browse_summary_counts_availability(ro_conn):
    s = Q.browse_summary(ro_conn, Q.SearchFilters())
    assert s["ebooks"] == 5
    assert s["audiobooks"] == 3
    assert s["ereader"] == 2
    assert [a["name"] for a in s["authors"]][0] == "Bob de Wit"   # 2 works
    # the summary describes the same set as the shelf below it, format filter or not
    audio = Q.browse_summary(ro_conn, Q.SearchFilters(format="audiobook"))
    assert audio["audiobooks"] == 3


def test_genre_pages_and_tree_are_read_not_derived(ro_conn):
    # the fixture catalog carries no facet codes, so every genre is top-level
    page = Q.genre_page(ro_conn, "spanning-thrillers")
    assert page["name"] == "Spanning & Thrillers"
    assert page["titles"] == 2                 # works 003 and 004
    assert Q.genre_page(ro_conn, "bestaat-niet") is None
    assert Q.genre_children(ro_conn, "spanning-thrillers") == []
    tree = Q.genre_tree(ro_conn, "volwassenen")
    assert {g["slug"] for g in tree} == {s["slug"] for s in Q.genre_pages(ro_conn)}
    assert Q.genre_tree(ro_conn, "jeugd") == []


def test_lists_overview_counts(ro_conn):
    row = next(r for r in Q.lists_overview(ro_conn, "name") if r["slug"] == "test-top")
    assert row["total"] == 3
    assert row["available"] == 2
    items = Q.list_items(ro_conn, Q.list_row(ro_conn, "test-top")["id"])
    assert len(items) == 3
    first = items[0]
    assert first["work_id"] == "001" and first["slug"] == "de-ontdekking--anna-vrij"
    # both availability flags, so a list row can badge every format the book has
    assert first["bebook"] == 1 and first["baudio"] == 1


def test_web_stats(ro_conn):
    s = Q.web_stats(ro_conn)
    assert s["total"] == 5          # boeken
    assert s["editions"] == 9       # ...and the files you borrow them as
    assert s["ebooks"] == 5
    assert s["audiobooks"] == 3
    assert ("Bob de Wit", 2) in [tuple(r) for r in s["top_authors"]]


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
    db.set_work_genre_parents(conn, (genre_code, Counter(dict.fromkeys(genre_code, 1))))
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    ro = Q.connect_ro(tmp_path / "g.db")
    rows = {r["name"]: r["parent"] for r in Q.web_stats(ro)["genres"]}
    ro.close()
    assert rows["Literatuur & Romans"] is None
    assert rows["Sociale romans"] == "Literatuur & Romans"


def test_relevance_weights_subjects_above_summary(tmp_path):
    """bm25 weights are positional over ALL fts columns incl. the UNINDEXED work_id, so
    the ranking needs 5 weights (0.0 for work_id). With the old 4-weight expression the
    10.0 lands on the id and subjects/summary both get 1.0 — a subjects-only hit and a
    summary-only hit then score identically. The two books below are byte-identical
    except which column holds the unique term (so bm25 length-normalisation is the
    same for both); only the column weight can break the tie. New weights rank the
    subjects hit strictly first; the old ones tie (rowid order -> summary first)."""
    from obc import db
    term = "zqxwordtest"
    filler_subj, filler_summ = "vulonderwerp", "vulsamenvatting korte zin"
    # SUM inserted first (lower rowid): on the tied old weights it sorts ahead,
    # which is exactly the wrong order the fix corrects.
    # distinct authors (same token length) so the two stay separate works — the term
    # lives in subjects/summary, never the author, so bm25 length-normalisation stays
    # identical and only the column weight breaks the tie.
    recs = [
        {"ppn": "SUM", "title": "Zelfde titel", "author": "Auteur Aaa",
         "authors": ["Auteur Aaa"], "format": "ebook", "language": "Nederlands",
         "subjects": [filler_subj], "summary": f"{filler_summ} {term}"},
        {"ppn": "SUB", "title": "Zelfde titel", "author": "Auteur Bbb",
         "authors": ["Auteur Bbb"], "format": "ebook", "language": "Nederlands",
         "subjects": [filler_subj, term], "summary": filler_summ},
    ]
    # ~20 fillers so the term isn't in every row (bm25 IDF is 0 otherwise). They need
    # distinct authors too: sharing one would make them a single work sharing a title,
    # and 20 documents would collapse into 1.
    recs += [
        {"ppn": f"F{i:02d}", "title": "Zelfde titel", "author": f"Auteur F{i:02d}",
         "authors": [f"Auteur F{i:02d}"], "format": "ebook", "language": "Nederlands",
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
    order = [r["work_id"] for r in res.rows]
    assert order == ["SUB", "SUM"]




def test_the_format_filter_counts_without_touching_the_table(ro_conn):
    """The exact total behind /e-books and every ?formaat= search.

    Unindexed, `COUNT(*) FROM works WHERE has_ebook = 1` is a full scan: 175ms
    locally over 50,373 rows, and seconds on a 512MB VM where the 593MB table is
    not in the page cache — 99.8% of the /e-books page, whose 120 rows cost 0.4ms.
    A covering index answers it without reading the table at all.
    """
    for col in ("has_ebook", "has_audiobook"):
        plan = " ".join(r[-1] for r in ro_conn.execute(
            f"EXPLAIN QUERY PLAN SELECT COUNT(*) FROM works WHERE {col} = 1"))
        assert "COVERING INDEX" in plan, f"{col}: {plan}"
        assert "SCAN works" not in plan, f"{col}: {plan}"


def test_a_bare_text_search_counts_inside_the_index(ro_conn):
    """The count must not join to `works` just to be discarded.

    Counting FTS matches through `JOIN works` costs one row lookup per match —
    51,980 of them for "de" on the live catalog, none of which the count uses.
    FTS5 counts its own rows: 231ms -> 37ms locally, and it never touches the
    593MB table, which is what matters on a machine whose page cache holds a
    fifth of the catalog.
    """
    f = Q.SearchFilters(q="ontdekking")
    viaidx = Q.search(ro_conn, f, 1, 24).total
    joined = ro_conn.execute(
        "SELECT COUNT(*) FROM works w JOIN works_fts ft ON ft.work_id = w.work_id "
        "WHERE works_fts MATCH ?", [Q.fts_match("ontdekking")]).fetchone()[0]
    assert viaidx == joined

    # …and with any other filter the join is back, because it applies the filter
    both = Q.SearchFilters(q="ontdekking", format="audiobook")
    assert Q.search(ro_conn, both, 1, 24).total <= viaidx


def test_parse_year_stays_lenient_but_bounded():
    assert Q.parse_year(" 2020 ") == 2020
    assert Q.parse_year("-44") == -44
    for junk in ("", None, "abc", "20a0", "--5", "²"):
        assert Q.parse_year(junk) is None, junk
    # 20 nines passes isdigit() but overflows SQLite's 64-bit binding -> was a 500
    assert Q.parse_year("9" * 20) is None
    assert Q.parse_year("-" + "9" * 20) is None


def test_fts_match_caps_the_term_count():
    q = " ".join(f"woord{i}" for i in range(40))
    assert Q.fts_match(q).count("*") == 12
    # short queries are untouched
    assert Q.fts_match("de ontdekking") == '"de"* "ontdekking"*'
