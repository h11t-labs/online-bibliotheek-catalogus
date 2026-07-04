"""Storage-layer tests: bulk_load / stream_rebuild round-trips, FTS."""

from collections import Counter

import sampledata

from obc import db


def _build(path, *, stream=False, lists=None):
    conn = db.connect(path)
    if stream:
        db.stream_rebuild(conn, sampledata.records(), lists)
    else:
        db.bulk_load(conn, sampledata.records(), lists)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()


def test_bulk_load_roundtrip(tmp_path):
    _build(tmp_path / "c.db", lists=sampledata.lists())
    conn = db.connect(tmp_path / "c.db")
    s = db.stats(conn)
    assert s["books"] == 6
    assert s["ebooks"] == 5
    assert s["audiobooks"] == 1
    # many-to-many tables populated
    assert conn.execute("SELECT COUNT(*) FROM authors").fetchone()[0] == 5  # Cara Licht etc.
    assert conn.execute("SELECT COUNT(*) FROM book_lists").fetchone()[0] == 2  # 001, 003
    conn.close()


def test_fts_match_folds_diacritics(tmp_path):
    _build(tmp_path / "c.db")
    conn = db.connect(tmp_path / "c.db")
    # "espana" must match "España" in book 001's summary (remove_diacritics)
    rows = conn.execute(
        'SELECT ppn FROM books_fts WHERE books_fts MATCH ?', ('"espana"*',)).fetchall()
    assert any(r["ppn"] == "001" for r in rows)
    conn.close()


def test_book_genre_parent_resolved_per_audience(tmp_path):
    """Jeugd and volwassenen reuse the same facet numbers, so a genre name shared by
    both (e.g. "Misdaad & Mysterie": jeugd sub of "Spanning & Avontuur", volwassenen
    sub of "Spanning & Thrillers") must get its parent per the book's own audience."""
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
    db.set_book_genre_parents(conn, (genre_code, genre_count))

    def parent(ppn):
        return conn.execute(
            "SELECT p.name FROM book_genres bg JOIN genres g ON g.id = bg.genre_id "
            "LEFT JOIN genres p ON p.id = bg.parent_id "
            "WHERE bg.book_ppn = ? AND g.name = 'Misdaad & Mysterie'", (ppn,)).fetchone()[0]

    assert parent("j") == "Spanning & Avontuur"    # jeugd book -> jeugd parent
    assert parent("v") == "Spanning & Thrillers"   # most-common wins over the leak
    conn.close()


def test_editions_lookup_uses_index_not_scan(tmp_path):
    """The book page's "other editions of this work" lookup must hit the
    case-insensitive (title, author) index — a full scan is ~4s on Fly's shared CPU."""
    import sampledata
    conn = db.connect(tmp_path / "x.db")
    db.bulk_load(conn, sampledata.records(), sampledata.lists())
    plan = " ".join(r["detail"] for r in conn.execute(
        "EXPLAIN QUERY PLAN SELECT ppn, format FROM books "
        "WHERE lower(title)=lower('x') AND lower(COALESCE(author,''))=lower(COALESCE('y','')) "
        "AND format IS NOT NULL"))
    conn.close()
    assert "USING INDEX" in plan and "SCAN books" not in plan, plan


def test_stream_rebuild_equivalent_to_bulk_load(tmp_path):
    """The low-memory streaming path must produce the same catalog as bulk_load."""
    _build(tmp_path / "bulk.db", stream=False, lists=sampledata.lists())
    _build(tmp_path / "stream.db", stream=True, lists=sampledata.lists())

    def snapshot(path):
        conn = db.connect(path)
        snap = {
            "books": conn.execute("SELECT COUNT(*) FROM books").fetchone()[0],
            "genres": sorted(r["name"] for r in conn.execute("SELECT name FROM genres")),
            "authors": sorted(r["name"] for r in conn.execute("SELECT name FROM authors")),
            "book_authors": conn.execute("SELECT COUNT(*) FROM book_authors").fetchone()[0],
            "publishers": sorted(tuple(r) for r in conn.execute("SELECT name, n FROM publishers")),
            "languages": sorted(tuple(r) for r in conn.execute("SELECT name, n FROM languages")),
            "fts": conn.execute("SELECT COUNT(*) FROM books_fts").fetchone()[0],
            "book_lists": conn.execute("SELECT COUNT(*) FROM book_lists").fetchone()[0],
        }
        conn.close()
        return snap

    assert snapshot(tmp_path / "bulk.db") == snapshot(tmp_path / "stream.db")
