"""The catalog as the site needs it: connections, and the indexes derived per rebuild.

Three kinds of thing live here, all of which answer to "which catalog are we
serving right now":

* :data:`DB_PATH` and :func:`get_conn` — where the catalog is and how a request
  reads it.
* :func:`catalog_version` — the identity of the current catalog, which is what
  every cache below is keyed on.
* The derived indexes themselves (facets, the A-Z author index, the series slug
  map, the genre tree). Each is the same for every visitor, expensive to build,
  and true until the next rebuild.

Both the page routes (:mod:`obc.web.app`) and the crawler-facing ones
(:mod:`obc.web.seo`) read from here, which is why it is its own module: it is the
one layer they share, and it depends on neither.
"""

from __future__ import annotations

import collections
import os
import sqlite3
from pathlib import Path

from .. import db
from ..textnorm import slugify, surname_key
from . import cache, queries

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


def catalog_version() -> int | None:
    """Which catalog we are serving, as a value that changes on every rebuild.

    Nanoseconds rather than the seconds :func:`data_updated` shows: two rebuilds
    (or two fixture catalogs in a test) can land inside the same second, and a
    version that didn't move would keep every derived index below on the previous
    catalog.
    """
    try:
        return DB_PATH.stat().st_mtime_ns
    except OSError:
        return None


# Everything below is derived from the catalog as a whole and only changes when
# `normalize` swaps in a rebuilt file, so it is memoised per catalog version — see
# obc.web.cache for why that cache locks and swaps the way it does.
catalog_cache = cache.VersionedCache(catalog_version)


def facets(conn: sqlite3.Connection) -> dict:
    """Facet values for the search sidebar — identical for every request."""
    return catalog_cache.get("facets", lambda: queries.compute_facets(conn))


OTHER_LETTER = "overig"  # names that don't start with a plain A-Z letter

# Two ways to alphabetise a name index, both defensible: readers hunting a known
# writer look under the surname, readers browsing recognise the whole name.
BY_SURNAME, BY_FIRST = "achternaam", "voornaam"
AUTHOR_SORTS = (BY_SURNAME, BY_FIRST)


def author_letter(name: str, by: str = BY_SURNAME) -> str:
    """Bucket an author under a letter, or the catch-all."""
    key = surname_key(name) if by == BY_SURNAME else slugify(name)
    first = key[:1].upper()
    return first if "A" <= first <= "Z" else OTHER_LETTER


def letter_order(index: dict) -> list[str]:
    """A-Z first, the catch-all last."""
    return sorted(k for k in index if k != OTHER_LETTER) + \
        ([OTHER_LETTER] if OTHER_LETTER in index else [])


def author_entries(conn: sqlite3.Connection) -> list[dict]:
    """``[{name, titles}…]`` — every author, spelling variants merged.

    Variants are folded together because that is what the slug URL does: listing
    "Ad Van Schaik" and "Ad van Schaik" as two entries pointing at the same page
    would be a lie the hub tells about itself.

    Every author is listed, including the 13k with a single title. The
    MIN_INDEXABLE_TITLES rule is about what the *sitemap* promotes, not about what
    a reader is allowed to find — a browsable index that silently omits more than
    half the authors is simply broken.
    """
    counts = queries.author_title_counts(conn)
    merged: dict[str, dict] = {}
    for row in queries.author_index(conn):
        # A name with no Latin characters at all folds to "" and has no slug, so it
        # cannot be a hub or sitemap entry — and merging on that empty key would
        # fuse unrelated authors into one. Those keep their own encoded-name page.
        if not row["fold"]:
            continue
        # rows arrive title-count descending within a fold, so the first spelling
        # seen for a key is the one that carries the most titles
        merged.setdefault(row["fold"],
                          {"name": row["name"], "titles": counts.get(row["fold"], 0)})
    return list(merged.values())


def authors_by_letter(conn: sqlite3.Connection,
                      by: str = BY_SURNAME) -> dict[str, list[dict]]:
    """``{"A": [{name, titles}…], …, "overig": […]}`` — every author, bucketed.

    Cached per sort order, on top of the merge above: the two orders bucket and
    sort the same authors, so only the cheap half is done twice.

    Both lookups go through one pinned view. They are two halves of a single
    answer, and reading the version separately for each let a rebuild landing
    between them file the old catalog's authors under the new version — where
    they would stay, wrong, until the next rebuild.
    """
    # Resolved before build(), never inside it: the cache holds a plain lock while
    # building, so a builder that called back into it would deadlock.
    pinned = catalog_cache.pinned()
    entries = pinned.get("authors", lambda: author_entries(conn))

    def build() -> dict[str, list[dict]]:
        sort_key = surname_key if by == BY_SURNAME else slugify
        buckets: dict[str, list[dict]] = {}
        for entry in entries:
            buckets.setdefault(author_letter(entry["name"], by), []).append(entry)
        # the chosen key first, then the whole name, so a letter page reads as an index
        for rows in buckets.values():
            rows.sort(key=lambda e: (sort_key(e["name"]), slugify(e["name"])))
        return buckets

    return pinned.get(("authors", by), build)


# Series get the same slug treatment as authors, but `books.series` is free text
# with no folded column to look up, so the slug -> spellings map is built once per
# catalog rebuild. 18 slugs cover more than one spelling ("De Stad" / "De stad");
# those share a page rather than splitting the shelf in two.
def series(conn: sqlite3.Connection) -> dict[str, dict]:
    """``{slug: {"name": display, "names": (spellings…), "titles": n}}``."""
    def build() -> dict[str, dict]:
        merged: dict[str, dict] = {}
        for row in queries.series_index(conn):
            slug = slugify(row["name"])
            if not slug:
                continue
            # rows arrive part-count descending, so the first spelling wins the heading
            entry = merged.setdefault(slug, {"name": row["name"], "names": [], "titles": 0})
            entry["names"].append(row["name"])
            entry["titles"] += row["titles"]
        return merged

    return catalog_cache.get("series", build)


# The catalog carries two taxonomies, not one: jeugd and volwassenen reuse genre
# names under different parents, and 67 of 213 subgenres sit somewhere different
# depending on which shelf you are standing at. Flattening them picked a winner
# and misfiled the loser, so the hub renders a tree per audience while the genre
# page itself stays a single URL covering both.
AUDIENCES = (("volwassenen", "Volwassenen"), ("jeugd", "Jeugd"))


def genre_data(conn: sqlite3.Connection) -> dict:
    """``{"flat": {slug: entry}, "trees": {audience: [top entries]}}``, cached.

    ``flat`` backs the genre page and the sitemap — one entry per slug, counted
    over distinct books because spelling variants share them (the catalog holds
    "Biografieën" twice, precomposed and with a combining diaeresis). ``trees`` is
    the hub's view: per audience, top genres carrying their own children.

    The counting itself happens in SQLite (see the genre statements in
    :mod:`obc.web.queries`); what is left here is the shaping the database can't
    express — picking a parent per audience, and hanging children off it.
    """
    def build() -> dict:
        known = {a for a, _ in AUDIENCES}
        # 2.567 books carry no audience, and a catalog built without the detail
        # pass has none at all — those land on the default shelf rather than
        # falling out of the hub entirely. No genre in the live catalog is
        # reachable *only* that way, so this shifts counts, never visibility.
        def shelf(audience: str) -> str:
            return audience if audience in known else AUDIENCES[0][0]

        # names arrive longest spelling first, so the first one seen per slug is
        # the one the page is headed with
        flat: dict[str, dict] = {}
        for row in queries.genre_names(conn):
            if not row["slug"]:
                continue
            entry = flat.setdefault(row["slug"], {"name": row["name"], "names": [],
                                                  "titles": 0, "children": []})
            entry["names"].append(row["name"])

        per_aud: dict[str, dict[str, dict]] = {a: {} for a in known}

        def bucket(audience: str, slug: str) -> dict:
            return per_aud[shelf(audience)].setdefault(
                slug, {"titles": 0, "parents": collections.Counter()})

        for row in queries.genre_title_counts(conn):
            if row["slug"] not in flat:
                continue
            flat[row["slug"]]["titles"] += row["titles"]
            bucket(row["audience"], row["slug"])["titles"] += row["titles"]

        parents: dict[str, collections.Counter] = collections.defaultdict(
            collections.Counter)
        for row in queries.genre_parent_links(conn):
            if row["slug"] not in flat:
                continue
            parents[row["slug"]][row["parent"]] += row["n"]
            bucket(row["audience"], row["slug"])["parents"][row["parent"]] += row["n"]

        for slug, entry in flat.items():
            named = parents[slug].most_common(1)
            parent = named[0][0] if named else ""
            entry["parent"] = parent if parent and parent != slug else ""
        for slug, entry in flat.items():
            entry["children"] = sorted(
                (c for c, o in flat.items() if o["parent"] == slug),
                key=lambda c: (-flat[c]["titles"], c))

        trees: dict[str, list[dict]] = {}
        for aud, _label in AUDIENCES:
            rows = per_aud[aud]
            parent_of, titles_of = {}, {}
            for slug, a in rows.items():
                # A tree may only state what its own audience files. Borrowing the
                # catalog-wide parent when this audience names none put "Film" under
                # Kunst & Cultuur for adults, where all three adult records carry no
                # parent at all — a relationship the data never asserts. An explicit
                # parent still outranks a bare "top level" *within* the audience.
                named = [(pslug, n) for pslug, n in a["parents"].items()
                         if pslug and pslug != slug and pslug in rows]
                parent_of[slug] = max(named, key=lambda kv: kv[1])[0] if named else ""
                titles_of[slug] = a["titles"]
            tops = []
            for slug in rows:
                if parent_of[slug]:
                    continue
                kids = sorted((c for c in rows if parent_of[c] == slug),
                              key=lambda c: -titles_of[c])
                tops.append({"name": flat[slug]["name"], "slug": slug,
                             "titles": titles_of[slug],
                             "children": [{"name": flat[c]["name"], "slug": c,
                                           "titles": titles_of[c]} for c in kids]})
            trees[aud] = sorted(tops, key=lambda g: (-g["titles"], g["name"].lower()))

        return {"flat": flat, "trees": trees}

    return catalog_cache.get("genres", build)


def genres(conn: sqlite3.Connection) -> dict[str, dict]:
    """``{slug: {name, names, titles, parent, children}}`` across both audiences."""
    return genre_data(conn)["flat"]
