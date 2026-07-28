"""LSA 'meer zoals dit' recommendations (obc.similar) + its query.

The build step needs the optional ``recommend`` extra (scikit-learn); those tests are
skipped when it isn't installed. The graceful-fallback test needs no extra.
"""

from __future__ import annotations

import sampledata

from obc import db
from obc.web import queries as Q


def test_similar_books_absent_table_is_graceful(ro_conn):
    """A catalog without work_similar (feature not built yet) must not error — the
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


def test_build_similar_recommends_works(tmp_path):
    import pytest
    pytest.importorskip("sklearn")
    from obc import similar

    path = tmp_path / "sim.db"
    conn = db.connect(path)
    db.bulk_load(conn, sampledata.records(), sampledata.lists())
    written = similar.build_similar(conn, method="lsa", min_score=0.0)
    assert written >= 1

    works = {r["work_id"] for r in conn.execute("SELECT work_id FROM works")}

    def neighbours(work_id):
        return [r["other_work_id"] for r in conn.execute(
            "SELECT other_work_id FROM work_similar WHERE work_id = ? AND method='lsa' "
            "ORDER BY rank", (work_id,))]

    # Vectorising works instead of editions makes the old post-hoc de-duplication
    # unnecessary — but the property it protected still has to hold: a recommended
    # work appears at most once and is never the source itself.
    for src in works:
        nb = neighbours(src)
        assert len(nb) == len(set(nb)), src
        assert src not in nb, src
        assert set(nb) <= works, src
    # only works are recommended, so no edition PPN can leak into the strip
    assert not neighbours("002")
    # a stale pre-works table is cleaned up rather than left to confuse
    assert not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='book_similar'"
    ).fetchone()
    conn.close()


def test_similar_books_query_returns_display_rows(tmp_path):
    path = _built_db(tmp_path)
    ro = Q.connect_ro(path)
    rows = Q.similar_books(ro, "003")
    ro.close()
    assert rows, "expected at least one recommendation for 003"
    r = rows[0]
    # both availability flags, so the strip badges every format the book has
    assert set(r.keys()) >= {"work_id", "title", "author", "slug", "cover_url",
                             "has_ebook", "has_audiobook", "score"}
