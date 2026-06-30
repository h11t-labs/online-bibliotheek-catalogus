"""Load cached records from ``data/raw/`` into the SQLite catalog (fast rebuild).

Besides loading book records it also:
* applies the e-reader flag from ``data/raw/ereader.json``;
* canonicalises publisher spellings to the most-common variant;
* splits multi-author strings into individual authors;
* matches curated lists (``data/raw/lists/*.json``) to catalog PPNs.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from pathlib import Path

from . import db
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

RAW_DIR = Path("data/raw")
RECORDS_DIR = RAW_DIR / "records"
EREADER_FILE = RAW_DIR / "ereader.json"
GENRES_FILE = RAW_DIR / "genres.json"
RECENT_FILE = RAW_DIR / "recent.json"
LISTS_DIR = RAW_DIR / "lists"


def _read(path: Path):
    data = read_json(path, default=[])
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    return [data] if isinstance(data, dict) else []


def _load_aux() -> tuple[set, bool, dict, dict]:
    """Load the small side-files: e-reader flags, facet genres, recency ranks."""
    ereader: set[str] = set()
    have_ereader = EREADER_FILE.exists()
    if have_ereader:
        data = read_json(EREADER_FILE)
        have_ereader = data is not None
        ereader = set(data or [])
    genres_map: dict[str, list] = read_json(GENRES_FILE, default={}) or {}
    recent_map: dict[str, int] = read_json(RECENT_FILE, default={}) or {}
    return ereader, have_ereader, genres_map, recent_map


def _transform(r: dict, ereader: set, have_ereader: bool, genres_map: dict,
               recent_map: dict, canon: dict) -> dict | None:
    """Enrich one raw record in place; return it, or None to drop it. Files are
    named ``{ppn}.json`` (one record per ppn), so no cross-file dedup is needed."""
    ppn = r.get("ppn")
    if not ppn or r.get("removed_at"):  # drop removed / id-less titles
        return None
    if have_ereader and r.get("format") == "ebook":
        r["ereader"] = 1 if ppn in ereader else 0
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


def _prepass(paths: list[Path]) -> tuple[dict, dict, dict, dict]:
    """One streaming pass to build the publisher-canon map, the isbn/title lookup
    maps for list matching, and the genre name->facet-code map (for the hierarchy)
    — without holding records in RAM."""
    groups: dict[str, Counter] = {}
    by_isbn: dict[str, str] = {}
    by_key: dict[str, str] = {}
    code_of: dict[str, str] = {}
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
            # Namespace the facet code by audience: onderwerpJeugd and
            # onderwerpVolwassenen reuse the same numbers (2.0 = jeugd "Natuur &
            # Dieren" vs volwassenen "Literatuur & Romans"), so without the prefix a
            # sub-genre's parent code would match the wrong audience's top genre.
            aud = (r.get("audience") or "").strip().lower()
            for g in (r.get("genres") or []):
                if g.get("name") and g.get("code"):
                    code_of.setdefault(g["name"], f"{aud}|{g['code']}")
    canon = {k: ctr.most_common(1)[0][0] for k, ctr in groups.items()}
    return canon, by_isbn, by_key, code_of


def iter_records(paths: list[Path], aux: tuple, canon: dict):
    """Yield enriched records one at a time (constant memory)."""
    ereader, have_ereader, genres_map, recent_map = aux
    for path in paths:
        for r in _read(path):
            t = _transform(r, ereader, have_ereader, genres_map, recent_map, canon)
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


def _reclaim_disk(db_path: Path, raw_dir: Path) -> None:
    """Tidy up before a rebuild: drop stale SQLite sidecars + any leftover temp DB
    from a crashed run, plus the on-disk HTML cache (not needed to rebuild). The
    *live* catalog DB is left untouched — the rebuild builds a temp copy and swaps
    it in atomically, so readers keep seeing the old DB until the swap (no downtime)."""
    db_path = Path(db_path)
    tmp = db_path.with_name(db_path.name + ".tmp")
    for p in (Path(f"{db_path}-wal"), Path(f"{db_path}-shm"), Path(f"{db_path}-journal"),
              tmp, Path(f"{tmp}-wal"), Path(f"{tmp}-shm"), Path(f"{tmp}-journal")):
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


def normalize(raw_dir: Path = RAW_DIR, db_path: Path = db.DEFAULT_DB) -> dict:
    db_path = Path(db_path)
    _reclaim_disk(db_path, raw_dir)
    paths = sorted((raw_dir / "records").rglob("*.json"))
    aux = _load_aux()
    canon, by_isbn, by_key, code_of = _prepass(paths)  # canon + match maps + genre codes
    lists = match_lists(by_isbn, by_key)
    # Build into a temp DB, then swap it in atomically — the web app keeps serving
    # the old, complete catalog throughout the rebuild (no "wordt opgebouwd" window).
    tmp = db_path.with_name(db_path.name + ".tmp")
    conn = db.connect(tmp)
    # stream records in batches — constant memory, no full in-RAM load
    n = db.stream_rebuild(conn, iter_records(paths, aux, canon), lists)
    db.set_genre_codes(conn, code_of)  # stamp the genre facet code + parent (hierarchy)
    s = db.stats(conn)
    conn.close()
    os.replace(tmp, db_path)  # atomic on the same filesystem
    for sc in (f"{db_path}-wal", f"{db_path}-shm", f"{db_path}-journal"):
        Path(sc).unlink(missing_ok=True)
    logger.info(f"Normalized {n} record(s). DB now: {s}")
    return s


if __name__ == "__main__":
    normalize()
