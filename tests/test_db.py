"""Storage-layer tests: bulk_load / stream_rebuild round-trips, FTS, upsert."""

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


def test_upsert_is_idempotent(tmp_path):
    conn = db.connect(tmp_path / "c.db")
    db.init_db(conn)
    rec = sampledata.records()[0]
    db.upsert_book(conn, rec)
    db.upsert_book(conn, rec)  # second call must not duplicate anything
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM books_fts WHERE ppn = ?", (rec["ppn"],)).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM book_genres WHERE book_ppn = ?", (rec["ppn"],)).fetchone()[0] == 1
    conn.close()


def test_genre_hierarchy_codes(tmp_path):
    recs = [{"ppn": "1", "title": "x", "subjects": ["Natuur & Dieren", "Wilde dieren"]}]
    conn = db.connect(tmp_path / "g.db")
    db.bulk_load(conn, recs)
    db.set_genre_codes(conn, {"Natuur & Dieren": "2.0", "Wilde dieren": "2.6"})
    rows = {r["name"]: (r["code"], r["parent"])
            for r in conn.execute("SELECT name, code, parent FROM genres")}
    assert rows["Natuur & Dieren"] == ("2.0", None)   # top-level (X.0)
    assert rows["Wilde dieren"] == ("2.6", "2.0")     # sub-genre -> parent 2.0
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
