"""The catalog as the site needs it: the connection, and the A-Z author index.

Two kinds of thing live here, both answering "which catalog are we serving right
now":

* :data:`DB_PATH` and :func:`get_conn` — where the catalog is and how a request
  reads it.
* The author hub's letter buckets — the one index the database cannot hand over
  ready-made, because bucketing by first letter and sorting by two different keys
  is shaping, not counting.

Both the page routes (:mod:`obc.web.app`) and the crawler-facing ones
(:mod:`obc.web.seo`) read from here, which is why it is its own module: it is the
one layer they share, and it depends on neither.

There is no cache here any more, and that is the point. The four memoised indexes
that used to live in this module (facets, this index, the series slug map, the
genre tree) rebuilt per *process* what the catalog already determines per
*rebuild* — and caused a real incident: eight concurrent cold /genres requests
each walked 157k rows, took 23s apiece and pushed a 512 MB machine to 606 MB. The
build stamps all four now (see :mod:`obc.db`), so a route is request-parse ->
indexed reads -> render. Kill the process and the next request is exactly as fast.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from .. import db
from . import queries

DB_PATH = Path(os.environ.get("OBC_DB", db.DEFAULT_DB))


def get_conn():
    """Per-request read-only DB connection, always closed (FastAPI dependency).

    Reads the module-global DB_PATH at call time (tests monkeypatch it), not
    captured at import. If the DB isn't there yet, connect_ro raises
    OperationalError here and the bootstrap-503 handler renders the friendly page.
    """
    conn = queries.connect_ro(DB_PATH)
    try:
        yield conn
    finally:
        conn.close()


def data_updated() -> float | None:
    """Epoch seconds the catalog was last (re)built — the DB file's mtime."""
    try:
        return DB_PATH.stat().st_mtime
    except OSError:
        return None


OTHER_LETTER = "overig"  # names that don't start with a plain A-Z letter

# Two ways to alphabetise a name index, both defensible: readers hunting a known
# writer look under the surname, readers browsing recognise the whole name.
BY_SURNAME, BY_FIRST = "achternaam", "voornaam"
AUTHOR_SORTS = (BY_SURNAME, BY_FIRST)

# The catalog carries two taxonomies, not one: jeugd and volwassenen reuse genre
# names under different parents, and 67 of 213 subgenres sit somewhere different
# depending on which shelf you are standing at. Flattening them picked a winner
# and misfiled the loser, so the hub renders a tree per audience while the genre
# page itself stays a single URL covering both.
AUDIENCES = (("volwassenen", "Volwassenen"), ("jeugd", "Jeugd"))


def author_letter(sort_key: str) -> str:
    """Bucket an author under a letter, or the catch-all.

    Takes the *stamped* sort key (authors.surname_sort / first_sort) rather than
    re-deriving it from the name on every request.
    """
    first = (sort_key or "")[:1].upper()
    return first if "A" <= first <= "Z" else OTHER_LETTER


def letter_order(index: dict) -> list[str]:
    """A-Z first, the catch-all last."""
    return sorted(k for k in index if k != OTHER_LETTER) + \
        ([OTHER_LETTER] if OTHER_LETTER in index else [])


def authors_by_letter(conn: sqlite3.Connection,
                      by: str = BY_SURNAME) -> dict[str, list[dict]]:
    """``{"A": [{name, titles}…], …, "overig": […]}`` — every person, bucketed.

    One pass over ~10k tiny rows, on the two hub pages only. Both the fold-merge
    loop and the per-request title counting are gone: ``authors`` is already one
    row per person with its counts and both sort keys stamped.

    Every author is listed, including the 13k with a single title. The
    MIN_INDEXABLE_TITLES rule is about what the *sitemap* promotes, not about what
    a reader is allowed to find — a browsable index that silently omits more than
    half the authors is simply broken.
    """
    field = "surname_sort" if by == BY_SURNAME else "first_sort"
    buckets: dict[str, list[dict]] = {}
    # Rows arrive in the order the page wants them, so each bucket fills in order
    # and nothing is sorted here. This used to re-derive the sort key per row with
    # surname_key()/slugify() — 22,383 calls per request to rebuild the two
    # columns the build had already stamped, which cost 7.5s on the live hub.
    for row in queries.author_index(conn, by_surname=(by == BY_SURNAME)):
        # A name with no Latin characters at all folds to "" and has no slug, so it
        # cannot be a hub or sitemap entry — those keep their own encoded-name page.
        if not row["first_sort"]:
            continue
        buckets.setdefault(author_letter(row[field]), []).append(
            {"name": row["name"], "titles": row["titles"]})
    return buckets
