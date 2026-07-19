"""Load cached records from ``data/raw/`` into the SQLite catalog (fast rebuild).

Besides loading book records it also:
* resolves the e-reader flag, preferring a per-title detail-page flag, then the
  ``data/raw/ereader.json`` side-file, then the value already in the live DB (so
  a missing side-file never silently blanks the whole facet);
* canonicalises publisher spellings to the most-common variant;
* splits multi-author strings into individual authors;
* matches curated lists (``data/raw/lists/*.json``) to catalog PPNs.
"""

from __future__ import annotations

import os
import re
import sqlite3
from collections import Counter
from pathlib import Path

from . import db

# Data paths live in obc.config; imported (and rebindable) at module level so
# `normalize.EREADER_FILE` etc. stay monkeypatchable by tests and the scheduler.
from .config import (
    EREADER_FILE,
    GENRES_FILE,
    LISTS_DIR,
    RAW_DIR,
    RECENT_FILE,
)
from .log import logger
from .textnorm import (
    canonical_author,
    canonical_publisher,
    detect_series,
    match_key,
    publisher_key,
    split_authors,
    valid_language,
)
from .util import read_json


def _read(path: Path):
    data = read_json(path, default=[])
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    return [data] if isinstance(data, dict) else []


def _load_aux(db_path: Path | None = None) -> tuple[set, bool, dict, dict, dict]:
    """Load the small side-files: e-reader flags, facet genres, recency ranks.
    Also snapshot the live DB's known e-reader flags (``prior_ereader``) so a
    missing ereader side-file preserves the flag instead of blanking it."""
    ereader: set[str] = set()
    have_ereader = EREADER_FILE.exists()
    if have_ereader:
        data = read_json(EREADER_FILE)
        have_ereader = data is not None
        ereader = set(data or [])
    genres_map: dict[str, list] = read_json(GENRES_FILE, default={}) or {}
    recent_map: dict[str, int] = read_json(RECENT_FILE, default={}) or {}
    prior_ereader: dict[str, int] = db.load_prior_ereader(db_path)
    return ereader, have_ereader, genres_map, recent_map, prior_ereader


def _transform(r: dict, ereader: set, have_ereader: bool, genres_map: dict,
               recent_map: dict, canon: dict, prior_ereader: dict) -> dict | None:
    """Enrich one raw record in place; return it, or None to drop it. Files are
    named ``{ppn}.json`` (one record per ppn), so no cross-file dedup is needed."""
    ppn = r.get("ppn")
    if not ppn or r.get("removed_at"):  # drop removed / id-less titles
        return None
    if r.get("format") == "ebook" and r.get("ereader") is None:
        # precedence: a per-title detail flag (set upstream, freshest — covers new
        # titles not yet in the side-file) already won by being non-None; else the
        # ereader side-file; else the value last known in the live DB, so a missing
        # side-file preserves the facet rather than zeroing it.
        if have_ereader:
            r["ereader"] = 1 if ppn in ereader else 0
        elif ppn in prior_ereader:
            r["ereader"] = prior_ereader[ppn]
    r["language"] = valid_language(r.get("language"))  # drop non-language junk
    # split + canonicalise authors (merge known aliases like Bernlef/J. Bernlef)
    authors = [canonical_author(a) for a in split_authors(r.get("author"))]
    r["authors"] = list(dict.fromkeys(a for a in authors if a))
    if r["authors"]:
        r["author"] = ", ".join(r["authors"])
    if ppn in genres_map:  # merge facet-derived genres with detail-page subjects
        r["subjects"] = list(dict.fromkeys((r.get("subjects") or []) + genres_map[ppn]))
    if ppn in recent_map:
        r["added_rank"] = recent_map[ppn]
    if isinstance(r.get("keywords"), list):  # detail page -> store as one string
        r["keywords"] = ", ".join(r["keywords"]) or None
    # The detail page's explicit "Serie" field wins; otherwise sniff the title/note.
    if not r.get("series"):
        s, no = detect_series(r.get("title"))
        if not s:
            s, no = detect_series(r.get("note"))
        if s:
            r["series"], r["series_no"] = s, no
    p = r.get("publisher")
    if p:
        r["publisher"] = canonical_publisher(p, canon.get(publisher_key(p), p))
    return r


def _prepass(paths: list[Path]) -> tuple[dict, dict, dict, tuple]:
    """One streaming pass to build the publisher-canon map, the isbn/title lookup
    maps for list matching, and the genre name->facet-code map (for the hierarchy)
    — without holding records in RAM."""
    groups: dict[str, Counter] = {}
    by_isbn: dict[str, str] = {}
    by_key: dict[str, str] = {}
    genre_code: dict[tuple[str, str], str] = {}
    genre_count: Counter = Counter()
    for path in paths:
        for r in _read(path):
            ppn = r.get("ppn")
            if not ppn or r.get("removed_at"):
                continue
            p = r.get("publisher")
            if p:
                groups.setdefault(publisher_key(p), Counter())[p] += 1
            if r.get("isbn"):
                by_isbn.setdefault(re.sub(r"\D", "", r["isbn"]), ppn)
            authors = [canonical_author(a) for a in split_authors(r.get("author"))] \
                or [r.get("author")]
            for a in authors:
                by_key.setdefault(match_key(r.get("title"), a), ppn)
            # (audience, genre name) -> facet code. onderwerpJeugd and
            # onderwerpVolwassenen reuse the same numbers (2.0 = jeugd "Natuur &
            # Dieren" vs volwassenen "Literatuur & Romans"), so the code — and thus a
            # genre's parent — is only meaningful within one audience.
            aud = (r.get("audience") or "").strip().lower()
            for g in (r.get("genres") or []):
                if g.get("name") and g.get("code"):
                    genre_code.setdefault((aud, g["name"]), g["code"])
                    genre_count[(aud, g["name"])] += 1
    canon = {k: ctr.most_common(1)[0][0] for k, ctr in groups.items()}
    return canon, by_isbn, by_key, (genre_code, genre_count)


def iter_records(paths: list[Path], aux: tuple, canon: dict):
    """Yield enriched records one at a time (constant memory)."""
    ereader, have_ereader, genres_map, recent_map, prior_ereader = aux
    total = len(paths)
    for seen, path in enumerate(paths, 1):
        if seen % 10_000 == 0 or seen == total:
            logger.info(f"[normalize] loading records: {seen}/{total}")
        for r in _read(path):
            t = _transform(r, ereader, have_ereader, genres_map, recent_map,
                           canon, prior_ereader)
            if t is not None:
                yield t


def match_lists(by_isbn: dict, by_key: dict) -> list[dict]:
    """Match curated-list items (data/raw/lists/*.json) to catalog PPNs using the
    isbn/title maps from :func:`_prepass`."""
    files = sorted(LISTS_DIR.glob("*.json")) if LISTS_DIR.exists() else []
    if not files:
        return []
    out = []
    for f in files:
        data = read_json(f)
        if not isinstance(data, dict):
            continue
        items, seen = [], set()
        matched = 0
        for it in data.get("items", []):
            ppn = None
            isbn = re.sub(r"\D", "", it.get("isbn") or "")
            if isbn and isbn in by_isbn:
                ppn = by_isbn[isbn]
            if not ppn:
                ppn = by_key.get(match_key(it.get("title"), it.get("author")))
            if ppn and ppn in seen:
                ppn = None  # avoid mapping two list slots to the same book
            if ppn:
                seen.add(ppn)
                matched += 1
            items.append({"position": it.get("position"), "year": it.get("year"),
                          "title": it.get("title"), "author": it.get("author"),
                          "isbn": it.get("isbn"), "cover_url": it.get("cover_url"),
                          "ppn": ppn, "won": it.get("won")})
        out.append({**{k: data.get(k) for k in
                       ("slug", "name", "url", "description", "updated_at")},
                    "items": items})
        logger.info(f"  list '{data.get('slug')}': matched {matched}/{len(data.get('items', []))}")
    return out


def _checkpoint_live(db_path: Path) -> None:
    """Fold a live WAL back into the DB so sidecars shrink to nothing — the safe
    replacement for deleting -wal/-shm files out from under readers. No-op if the
    DB doesn't exist or isn't WAL."""
    if not Path(db_path).exists():
        return
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
    except sqlite3.Error:
        pass


def _reclaim_disk(db_path: Path, raw_dir: Path) -> None:
    """Tidy up before a rebuild: drop any leftover temp DB (+ its sidecars) from a
    crashed run and the on-disk HTML cache (not needed to rebuild). The *live*
    catalog DB is left in place — the rebuild builds a temp copy and swaps it in
    atomically, so readers keep seeing the old DB until the swap (no downtime). Its
    WAL is *checkpointed* (folded back in) rather than deleted: unlinking a live
    DB's -wal/-shm under an open reader — or a hot -journal — can corrupt reads."""
    db_path = Path(db_path)
    _checkpoint_live(db_path)
    tmp = db_path.with_name(db_path.name + ".tmp")
    for p in (tmp, Path(f"{tmp}-wal"), Path(f"{tmp}-shm"), Path(f"{tmp}-journal")):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass
    html_cache = raw_dir / "html"
    if html_cache.is_dir():
        for f in html_cache.glob("*"):
            f.unlink(missing_ok=True)
    try:  # log what's left so a stubborn full volume is diagnosable
        st = os.statvfs(raw_dir if raw_dir.exists() else db_path.parent)
        rec = raw_dir / "records"
        n = sum(1 for _ in rec.glob("*.json")) if rec.exists() else 0
        logger.info(f"[reclaim] free {st.f_bavail * st.f_frsize // 1_000_000}"
                    f"/{st.f_blocks * st.f_frsize // 1_000_000}MB · {n} record files")
    except OSError:
        pass


def _build_similar(conn: sqlite3.Connection) -> None:
    """Precompute the "meer zoals dit" recommendations into the *temp* DB, before the
    atomic swap.

    ``book_similar`` is not part of the base schema — it is derived from the finished
    catalog — so a fresh rebuild never carries it over. Building it here (rather than
    as a separate step afterwards) means the swap publishes the catalog and its
    recommendations together: readers never see a new catalog with the strip missing,
    and a rebuilt PPN set can't leave stale neighbours behind.

    Optional: without the ``recommend`` extra (scikit-learn) the catalog is still
    perfectly usable, so a missing dependency logs a warning instead of failing the
    whole refresh."""
    try:
        from .similar import METHODS, build_similar
        for method in METHODS:
            build_similar(conn, method=method)
    except ImportError as e:
        logger.warning(f"[normalize] recommendations skipped ({e}); "
                       "install the extra with `uv sync --extra recommend`")
    except Exception as e:  # never let an optional extra abort the rebuild
        logger.warning(f"[normalize] recommendations failed: {e}")


def normalize(raw_dir: Path = RAW_DIR, db_path: Path = db.DEFAULT_DB) -> dict:
    db_path = Path(db_path)
    _reclaim_disk(db_path, raw_dir)
    paths = sorted((raw_dir / "records").rglob("*.json"))
    aux = _load_aux(db_path)  # reads the live DB's e-reader flags before the swap
    canon, by_isbn, by_key, genre_info = _prepass(paths)  # canon + match maps + genre codes
    lists = match_lists(by_isbn, by_key)
    # Build into a temp DB, then swap it in atomically — the web app keeps serving
    # the old, complete catalog throughout the rebuild (no "wordt opgebouwd" window).
    tmp = db_path.with_name(db_path.name + ".tmp")
    conn = db.connect(tmp)
    # stream records in batches — constant memory, no full in-RAM load
    n = db.stream_rebuild(conn, iter_records(paths, aux, canon), lists)
    db.set_book_genre_parents(conn, genre_info)  # per-book genre-hierarchy parent
    _build_similar(conn)  # into the temp DB, so the swap publishes both at once
    s = db.stats(conn)
    conn.close()
    # Fold the *old* live WAL into its DB and truncate it before the swap, so a
    # reader that opens the freshly-swapped file never pairs it with a stale -wal.
    # (Safer than deleting the sidecars, which can corrupt an open reader.)
    _checkpoint_live(db_path)
    os.replace(tmp, db_path)  # atomic on the same filesystem
    logger.info(f"Normalized {n} record(s). DB now: {s}")
    return s


if __name__ == "__main__":
    normalize()
