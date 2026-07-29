"""Everything the harvest knows, in one file: ``data/raw/raw.db``.

One row per PPN, holding two different kinds of thing and keeping them apart:

* ``detail_html`` — the detail page **as the library served it**. The source.
* ``record`` — what the parser understood of it, merged with the browse row.
  Derived from that source, and re-derivable at any time (``obc reparse``).

That distinction is the whole point, and it used to be missing: only the parsed
record was kept, so a change to *the parser* — as opposed to the data model — had
nothing to rebuild from. #34 taught ``detail.parse_detail`` to keep the "ook
beschikbaar als" hrefs it had been discarding, and recovering them cost three
hours of re-fetching 17,916 pages that had already carried the answer once.

**One file, not 69k.** The records used to be a file per PPN. At ~1.6 KB of
content each in 4 KB blocks, 259 MB on disk held 111 MB of data — 150 MB lost to
block granularity alone — and the volume's inode table has run out once already.
A row per PPN costs one inode, makes "which titles still need a detail page" an
indexed predicate rather than a directory walk, and makes a write atomic.

**Timestamps are columns, not fields inside the record.** They describe our
dealings with a title rather than the title itself, so a reparse can rewrite the
parse without touching the history.

Deliberately not ``catalog.db``: that one is derived and is thrown away and
rebuilt on every normalize; this one is the source and is only ever added to.
"""

from __future__ import annotations

import datetime
import gzip
import json
import sqlite3
from collections.abc import Iterable, Iterator
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    ppn         TEXT PRIMARY KEY,
    record      TEXT NOT NULL,
    detail_html BLOB,
    first_seen  TEXT NOT NULL,
    updated_at  TEXT,
    detail_at   TEXT,
    removed_at  TEXT
);
"""

# Columns that belong to the row rather than to the record, folded into the dict
# on the way out so everything downstream keeps seeing one flat record.
_STAMPS = ("first_seen", "updated_at", "detail_at", "removed_at")

# Every column a record is made of — deliberately not ``SELECT *``. The page is
# 86% of the store (708 MB against 114 MB of records) and no reader of a *record*
# ever looks at it, but naming it in the projection still drags it off disk. That
# put 1.6 GB through the page cache per normalize (this is walked twice), on a
# machine with 132 MB of cache and a 593 MB catalog to serve — which is how a
# background rebuild came to make every aggregate page on the live site take 20s.
_COLS = "ppn, record, " + ", ".join(_STAMPS)


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def connect(path: str | Path) -> sqlite3.Connection:
    """Open (creating if needed) the raw store."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _body(rec: dict) -> str:
    """The record without its stamps, canonically ordered so two equal records
    compare equal as text (which is what makes a no-op write detectable)."""
    return json.dumps({k: v for k, v in rec.items()
                       if k not in _STAMPS},
                      ensure_ascii=False, sort_keys=True)


def _to_record(row: sqlite3.Row) -> dict:
    rec = json.loads(row["record"])
    for stamp in _STAMPS:
        if row[stamp]:
            rec[stamp] = row[stamp]
    return rec


def put(conn: sqlite3.Connection, rec: dict) -> bool:
    """Store a record; returns whether anything actually changed.

    ``first_seen`` is set once and never moves; ``updated_at`` moves only when the
    parsed content differs. A pass that finds nothing new writes nothing at all,
    which is what keeps a full harvest from rewriting 69k rows to say the same
    thing.
    """
    ppn, blob, now = rec["ppn"], _body(rec), _now()
    row = conn.execute("SELECT record FROM records WHERE ppn = ?", (ppn,)).fetchone()
    if row is None:
        conn.execute("INSERT INTO records (ppn, record, first_seen) VALUES (?, ?, ?)",
                     (ppn, blob, now))
        conn.commit()
        return True
    if row["record"] == blob:
        return False
    conn.execute("UPDATE records SET record = ?, updated_at = ? WHERE ppn = ?",
                 (blob, now, ppn))
    conn.commit()
    return True


def put_detail(conn: sqlite3.Connection, ppn: str, html: str) -> None:
    """Keep the detail page as served, and stamp when it was fetched.

    Compressed per row: the pages are ~33 KB of which the great majority is
    identical AEM chrome, and gzip takes them to ~10 KB. Committed per page — the
    pass that fills this store runs for hours, and an uncommitted page is a page
    that has to be fetched again.
    """
    conn.execute("UPDATE records SET detail_html = ?, detail_at = ? WHERE ppn = ?",
                 (gzip.compress(html.encode("utf-8"), 6), _now(), ppn))
    conn.commit()


def get(conn: sqlite3.Connection, ppn: str) -> dict | None:
    row = conn.execute(f"SELECT {_COLS} FROM records WHERE ppn = ?",
                       (ppn,)).fetchone()
    return _to_record(row) if row else None


def detail_html(conn: sqlite3.Connection, ppn: str) -> str | None:
    row = conn.execute("SELECT detail_html FROM records WHERE ppn = ?",
                       (ppn,)).fetchone()
    if row is None or row["detail_html"] is None:
        return None
    return gzip.decompress(row["detail_html"]).decode("utf-8")


def iter_records(conn: sqlite3.Connection) -> Iterator[dict]:
    """Every record, in ppn order. Streams: normalize walks this twice, and 69k
    records in memory is not something a 512 MB machine should be asked to hold."""
    for row in conn.execute(f"SELECT {_COLS} FROM records ORDER BY ppn"):
        yield _to_record(row)


def iter_details(conn: sqlite3.Connection) -> Iterator[tuple[str, str]]:
    """``(ppn, html)`` for every stored page — what ``obc reparse`` walks."""
    for row in conn.execute(
            "SELECT ppn, detail_html FROM records "
            "WHERE detail_html IS NOT NULL ORDER BY ppn"):
        yield row["ppn"], gzip.decompress(row["detail_html"]).decode("utf-8")


def without_detail(conn: sqlite3.Connection) -> list[dict]:
    """Records holding no stored page — exactly the ones still to fetch.

    One predicate, where there used to be two passes with two selectors. Keeping
    the page removes the second reason to go back: ``--enrich`` skipped anything
    that already had an ISBN and so could never capture the cross-links, and
    ``--relink`` existed only to walk those same pages again. A record with no
    slug has no URL to fetch, so it is not a candidate.
    """
    return [_to_record(row) for row in conn.execute(
        f"SELECT {_COLS} FROM records WHERE detail_html IS NULL "
        "AND json_extract(record, '$.slug') IS NOT NULL ORDER BY ppn")]


def known_ppns(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT ppn FROM records")}


def mark_removed(conn: sqlite3.Connection, ppns: Iterable[str]) -> int:
    """Stamp ``removed_at`` on titles the catalog no longer lists (the UI hides
    them). The row stays: the stored page is still the best answer we have about
    that title, and a title can come back."""
    rows = [(_now(), ppn) for ppn in ppns]
    conn.executemany("UPDATE records SET removed_at = ? WHERE ppn = ?", rows)
    conn.commit()
    return len(rows)


def count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]


def detail_count(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM records WHERE detail_html IS NOT NULL").fetchone()[0]
