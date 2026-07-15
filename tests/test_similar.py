"""LSA 'meer zoals dit' recommendations (obc.similar) + its query.

The build step needs the optional ``recommend`` extra (scikit-learn); those tests are
skipped when it isn't installed. The graceful-fallback test needs no extra.
"""

from __future__ import annotations

import sampledata

from obc import db
from obc.web import queries as Q


def test_similar_books_absent_table_is_graceful(ro_conn):
    """A catalog without book_similar (feature not built yet) must not error — the
    book page just omits the strip."""
    assert Q.similar_books(ro_conn, "001") == []


def _built_db(tmp_path, name="sim.db"):
    import pytest
    pytest.importorskip("sklearn")
    from obc import similar

    path = tmp_path / name
    conn = db.connect(path)
    db.bulk_load(conn, sampledata.records(), sampledata.lists())
    for m in similar.METHODS:
        similar.build_similar(conn, method=m, min_score=0.0)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    return path


def test_build_similar_populates_and_dedups(tmp_path):
    import pytest
    pytest.importorskip("sklearn")
    from obc import similar

    path = tmp_path / "sim.db"
    conn = db.connect(path)
    db.bulk_load(conn, sampledata.records(), sampledata.lists())
    written = similar.build_similar(conn, method="lsa", min_score=0.0)
    assert written >= 1

    def neighbours(ppn):
        return [r["other_ppn"] for r in conn.execute(
            "SELECT other_ppn FROM book_similar WHERE book_ppn = ? AND method='lsa' "
            "ORDER BY rank", (ppn,))]

    # 001 and 002 are the same work (e-book + audiobook) -> the twin is never
    # recommended, and no two neighbours are editions of one work
    assert "002" not in neighbours("001")
    for src in ("001", "002", "003", "004"):
        nb = neighbours(src)
        assert len(nb) == len(set(nb))
    conn.close()


def test_similar_books_query_returns_display_rows(tmp_path):
    path = _built_db(tmp_path)
    ro = Q.connect_ro(path)
    rows = Q.similar_books(ro, "003")
    ro.close()
    assert rows, "expected at least one recommendation for 003"
    r = rows[0]
    assert set(r.keys()) >= {"ppn", "title", "author", "cover_url", "format", "score"}
