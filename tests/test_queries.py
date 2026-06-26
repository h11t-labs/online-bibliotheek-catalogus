"""Data-access layer (obc.web.queries) against the hermetic fixture catalog."""

from obc.web import queries as Q


def _ppns(result):
    return {r["ppn"] for r in result.rows}


def test_browse_all_newest_first(ro_conn):
    res = Q.search(ro_conn, Q.SearchFilters(sort="year_desc"), 1, 50)
    assert res.total == 6
    years = [r["year"] for r in res.rows]
    assert years == sorted(years, reverse=True)


def test_format_filter(ro_conn):
    res = Q.search(ro_conn, Q.SearchFilters(format="audiobook"), 1, 50)
    assert _ppns(res) == {"002"}


def test_fts_query_matches_title_and_summary(ro_conn):
    res = Q.search(ro_conn, Q.SearchFilters(q="ontdekking", sort="relevance"), 1, 50)
    assert _ppns(res) == {"001", "002"}


def test_fts_folds_diacritics(ro_conn):
    res = Q.search(ro_conn, Q.SearchFilters(q="espana"), 1, 50)
    assert "001" in _ppns(res)


def test_language_and_year_filters(ro_conn):
    assert _ppns(Q.search(ro_conn, Q.SearchFilters(languages=("Engels",)), 1, 50)) == {"003"}
    res = Q.search(ro_conn, Q.SearchFilters(year_from=2020, year_to=2021), 1, 50)
    assert _ppns(res) == {"001", "002"}


def test_ereader_author_genre_list_filters(ro_conn):
    assert _ppns(Q.search(ro_conn, Q.SearchFilters(ereader=True), 1, 50)) == {"001", "005"}
    assert _ppns(Q.search(ro_conn, Q.SearchFilters(authors=("Cara Licht",)), 1, 50)) == {"003"}
    assert _ppns(Q.search(
        ro_conn, Q.SearchFilters(genres=("Spanning & Thrillers",)), 1, 50)) == {"003", "004"}
    assert _ppns(Q.search(ro_conn, Q.SearchFilters(lists=("test-top",)), 1, 50)) == {"001", "003"}


def test_pagination(ro_conn):
    page1 = Q.search(ro_conn, Q.SearchFilters(sort="title"), 1, 2)
    page2 = Q.search(ro_conn, Q.SearchFilters(sort="title"), 2, 2)
    assert page1.total == 6
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


def test_facet_values(ro_conn):
    assert "Anna Vrij" in Q.facet_values(ro_conn, "author")
    assert "Querido, Amsterdam" in Q.facet_values(ro_conn, "publisher")
    assert Q.facet_values(ro_conn, "bogus") == []


def test_book_detail(ro_conn):
    detail = Q.book_detail(ro_conn, "001")
    assert detail["row"]["title"] == "De Ontdekking"
    assert detail["editions"].get("audiobook") == "002"  # the audiobook edition
    assert "Anna Vrij" in detail["authors"]
    assert any(bl["slug"] == "test-top" for bl in detail["book_lists"])
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
