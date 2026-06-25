"""Load cached records from ``data/raw/`` into the SQLite catalog (fast rebuild).

Besides loading book records it also:
* applies the e-reader flag from ``data/raw/ereader.json``;
* canonicalises publisher spellings to the most-common variant;
* splits multi-author strings into individual authors;
* matches curated lists (``data/raw/lists/*.json``) to catalog PPNs.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import db
from .log import logger
from .textnorm import (canonical_publisher, detect_series, fold, match_key,
                       publisher_key, split_authors)

RAW_DIR = Path("data/raw")
RECORDS_DIR = RAW_DIR / "records"
EREADER_FILE = RAW_DIR / "ereader.json"
GENRES_FILE = RAW_DIR / "genres.json"
RECENT_FILE = RAW_DIR / "recent.json"
LISTS_DIR = RAW_DIR / "lists"


def _read(path: Path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    return [data] if isinstance(data, dict) else []


def _canonical_publishers(records: list[dict]) -> dict[str, str]:
    groups: dict[str, Counter] = {}
    for r in records:
        p = r.get("publisher")
        if p:
            groups.setdefault(publisher_key(p), Counter())[p] += 1
    return {k: ctr.most_common(1)[0][0] for k, ctr in groups.items()}


def load_records(raw_dir: Path = RAW_DIR) -> list[dict]:
    paths = sorted((raw_dir / "records").rglob("*.json"))
    records: list[dict] = []
    with ThreadPoolExecutor(max_workers=16) as ex:
        for chunk in ex.map(_read, paths):
            records.extend(chunk)

    ereader: set[str] = set()
    have_ereader = EREADER_FILE.exists()
    if have_ereader:
        try:
            ereader = set(json.loads(EREADER_FILE.read_text()))
        except (json.JSONDecodeError, OSError):
            have_ereader = False

    genres_map: dict[str, list] = {}
    if GENRES_FILE.exists():
        try:
            genres_map = json.loads(GENRES_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    recent_map: dict[str, int] = {}
    if RECENT_FILE.exists():
        try:
            recent_map = json.loads(RECENT_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    by_ppn: dict[str, dict] = {}
    for r in records:
        ppn = r.get("ppn")
        if not ppn or r.get("removed_at"):  # drop removed titles
            continue
        if have_ereader and r.get("format") == "ebook":
            r["ereader"] = 1 if ppn in ereader else 0
        r["authors"] = split_authors(r.get("author"))
        # merge facet-derived genres with any detail-page subjects
        if ppn in genres_map:
            r["subjects"] = list(dict.fromkeys((r.get("subjects") or []) + genres_map[ppn]))
        if ppn in recent_map:
            r["added_rank"] = recent_map[ppn]
        s, no = detect_series(r.get("title"))
        if not s:
            s, no = detect_series(r.get("note"))
        if s:
            r["series"], r["series_no"] = s, no
        by_ppn[ppn] = r  # last write wins
    deduped = list(by_ppn.values())

    # canonicalise publishers: curated aliases first, else most-common per group
    canon = _canonical_publishers(deduped)
    for r in deduped:
        p = r.get("publisher")
        if p:
            r["publisher"] = canonical_publisher(p, canon.get(publisher_key(p), p))
    return deduped


def match_lists(records: list[dict]) -> list[dict]:
    """Match curated-list items (data/raw/lists/*.json) to catalog PPNs."""
    files = sorted(LISTS_DIR.glob("*.json")) if LISTS_DIR.exists() else []
    if not files:
        return []
    by_isbn: dict[str, str] = {}
    by_key: dict[str, str] = {}
    for r in records:
        ppn = r["ppn"]
        if r.get("isbn"):
            by_isbn.setdefault(re.sub(r"\D", "", r["isbn"]), ppn)
        for a in (r.get("authors") or [r.get("author")]):
            by_key.setdefault(match_key(r.get("title"), a), ppn)

    out = []
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
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
                          "ppn": ppn})
        out.append({**{k: data.get(k) for k in
                       ("slug", "name", "url", "description", "updated_at")},
                    "items": items})
        logger.info(f"  list '{data.get('slug')}': matched {matched}/{len(data.get('items', []))}")
    return out


def normalize(raw_dir: Path = RAW_DIR, db_path: Path = db.DEFAULT_DB) -> dict:
    records = load_records(raw_dir)
    lists = match_lists(records)
    conn = db.connect(db_path)
    n = db.bulk_load(conn, records, lists)  # bulk_load (re)creates the schema
    s = db.stats(conn)
    conn.close()
    logger.info(f"Normalized {n} record(s). DB now: {s}")
    return s


if __name__ == "__main__":
    normalize()
