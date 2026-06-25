"""Harvest the catalog into ``data/raw/records/{ppn}.json``, then ``obc normalize``.

Two enumeration modes:

* ``--browse`` (default, full catalog): walk the catalog via the faceted browse
  pages ``zoekresultaten.catalogus[.N].html?type=…&jaar=…``. Listing rows already
  carry rich metadata (title, author, summary, language, year, publisher, format,
  pages/duration, cover), so one request covers ~20 books. The site caps the
  pager at 50 pages (≈1000 results); when a (format, year) partition is capped we
  recursively split it by language → category → audience until each piece fits.
  Records are de-duplicated by PPN and the work is resumable per (format, year).

* ``--from-file PATH`` — fetch + parse individual detail pages from a list of
  catalog URLs / ``ppn,slug`` lines / JSON. Also used by ``--enrich`` to add
  detail-only fields (ISBN, full subjects, narrator) to browsed records.
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
from pathlib import Path
from typing import Iterable, Iterator

from .client import Client
from .listing import parse_listing
from .log import logger

RECORDS_DIR = Path("data/raw/records")
CHECKPOINT = Path("data/checkpoint.json")

_URL_RE = re.compile(r"/catalogus/([0-9xX]+)/([^/?#\s]+)")

# The result pager UI caps at 50 links, but deep page URLs work up to ~500
# pages (a hard 10,000-result cap per query). So we partition only enough to
# keep each query under that cap.
PAGE_CAP = 500
FORMATS = {"ebook": "E-book", "audiobook": "Digitaal_luisterboek"}
# Every record has a `type` and a `taal` (these facets sum exactly to the total),
# so (type x taal) is an exhaustive partition. Only Dutch exceeds 10k.
LANGS = ["dut", "eng", "fre", "ger", "fry", "spa", "ita", "lat", "gre",
         "pap", "rus", "lim", "mul"]
# Dutch is year-rich (~98% have a `jaar`), so a per-year split keeps each cell
# under the cap; a maker-sort window mops up the few year-less ones.
YEARS = list(range(2027, 1899, -1))
EREADER_FILE = Path("data/raw/ereader.json")
GENRES_FILE = Path("data/raw/genres.json")
RECENT_FILE = Path("data/raw/recent.json")

# onderwerp (subject) facet code -> genre name, per audience. These ARE the
# site's genres; tagging books via the facet avoids fetching detail pages.
GENRES_VW = {
    "2.0": "Literatuur & Romans", "3.0": "Romantisch", "4.0": "Spanning & Thrillers",
    "7.0": "Biografie & Waargebeurd", "10.0": "Gezin & Gezondheid",
    "16.0": "Filosofie & Religie", "9.0": "Geschiedenis & Politiek",
    "19.0": "Mens & Maatschappij", "1.0": "Fantasy & Sciencefiction",
}
GENRES_JD = {
    "4.0": "Spanning & Avontuur", "3.0": "Liefde & Vriendschap",
    "19.0": "Persoonlijke onderwerpen", "22.0": "Young Adult",
    "5.0": "Fantasy & Sciencefiction", "10.0": "Familie & Gezin", "1.0": "Grappig",
    "23.0": "Dagelijks leven", "2.0": "Natuur & Dieren", "7.0": "Geschiedenis",
    "21.0": "Verhalenboeken", "9.0": "Sport & Vrije tijd", "6.0": "Sprookjes",
    "15.0": "Samenleving",
}


# --------------------------------------------------------------------------- #
# browse enumeration (full catalog via q=*)
# --------------------------------------------------------------------------- #
def _paginate(client: Client, params: dict[str, str], on_record,
              seen: set[str], max_page: int = PAGE_CAP) -> int:
    """Page through one query, calling on_record for unseen PPNs. Returns the
    last page that had results."""
    page = 1
    while page <= max_page:
        recs, _ = parse_listing(client.get_listing_html(params, page))
        if not recs:
            break
        for r in recs:
            if r["ppn"] not in seen:
                seen.add(r["ppn"])
                on_record(r)
        page += 1
    return page - 1


def _enumerate_cell(client: Client, base: dict[str, str], on_record,
                    seen: set[str]) -> None:
    """Completely enumerate one (type, taal[, leesvorm]) cell, working around
    the 10k cap by adding per-year + a maker-sort window when capped."""
    last = _paginate(client, {**base, "sorteer": "titel"}, on_record, seen)
    if last >= PAGE_CAP:  # capped (Dutch): add year partitions + author window
        for year in YEARS:
            _paginate(client, {**base, "jaar": str(year), "sorteer": "titel"},
                      on_record, seen)
        _paginate(client, {**base, "sorteer": "maker"}, on_record, seen)


def browse_all(client: Client, formats: Iterable[str], seen: set[str],
               on_record, ereader: bool = False) -> None:
    """Enumerate the catalog per (format x language). Resumable per cell.

    With ``ereader=True`` only the e-reader-available subset is visited
    (``leesvorm=ereader``) — used to flag which e-books work on an e-reader.
    """
    done = _load_done()
    tag = "er" if ereader else "all"
    for fmt in formats:
        for taal in LANGS:
            key = f"{tag}:{fmt}:{taal}"
            if key in done:
                continue
            base = {"q": "*", "type": FORMATS[fmt], "taal": taal}
            if ereader:
                base["leesvorm"] = "ereader"
            before = len(seen)
            _enumerate_cell(client, base, on_record, seen)
            done.add(key)
            _save_done(done)
            logger.info(f"  {key}: +{len(seen) - before} (total {len(seen)})")


def _paginate_flat(client: Client, params: dict[str, str], on_record,
                   max_page: int = PAGE_CAP) -> None:
    """Page straight through a query (no dedup/splitting), cap at the 10k limit."""
    page = 1
    while page <= max_page:
        recs, _ = parse_listing(client.get_listing_html(params, page))
        if not recs:
            break
        for r in recs:
            on_record(r)
        page += 1


def collect_genres(client: Client) -> dict[str, list[str]]:
    """Tag books with genres by paging each subject (onderwerp) facet directly —
    no detail-page fetching. Split by language so the dominant Dutch subjects
    mostly stay under the 10k cap. Writes ppn -> [genre names] to GENRES_FILE."""
    import json as _json
    ppn_genres: dict[str, set] = {}
    for fmt in FORMATS:
        for doel, table, param in (("volwassenen", GENRES_VW, "onderwerpVolwassenen"),
                                   ("jeugd", GENRES_JD, "onderwerpJeugd")):
            for code, name in table.items():
                before = len(ppn_genres)
                for taal in LANGS:
                    base = {"q": "*", "type": FORMATS[fmt], "doelgroep": doel,
                            "taal": taal, param: code, "sorteer": "titel"}
                    _paginate_flat(client, base,
                                   lambda r, nm=name: ppn_genres.setdefault(
                                       r["ppn"], set()).add(nm))
                logger.info(f"  {fmt}/{doel}/{name}: +{len(ppn_genres)-before} "
                      f"(total {len(ppn_genres)})")
    out = {ppn: sorted(g) for ppn, g in ppn_genres.items()}
    GENRES_FILE.parent.mkdir(parents=True, exist_ok=True)
    GENRES_FILE.write_text(_json.dumps(out, ensure_ascii=False))
    logger.info(f"Tagged {len(out)} books with genres")
    return out


def collect_recent(client: Client, max_page: int = 250) -> dict[str, int]:
    """Rank the most recently licensed titles (newest first) for a
    'Recent toegevoegd' sort. Writes ppn -> rank (0 = newest) to RECENT_FILE."""
    import json as _json
    rank: dict[str, int] = {}
    n, page = 0, 1
    while page <= max_page:
        recs, _ = parse_listing(client.get_listing_html(
            {"q": "*", "sorteer": "licentie_datum"}, page))
        if not recs:
            break
        for r in recs:
            if r["ppn"] not in rank:
                rank[r["ppn"]] = n
                n += 1
        page += 1
    RECENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    RECENT_FILE.write_text(_json.dumps(rank))
    logger.info(f"Recency-ranked {len(rank)} recently added titles")
    return rank


def collect_ereader(client: Client) -> set[str]:
    """Enumerate e-reader-available e-books; persist the PPN set for normalize."""
    seen: set[str] = set()
    ppns: set[str] = set()
    browse_all(client, ["ebook"], seen, lambda r: ppns.add(r["ppn"]), ereader=True)
    EREADER_FILE.parent.mkdir(parents=True, exist_ok=True)
    EREADER_FILE.write_text(json.dumps(sorted(ppns)))
    logger.info(f"e-reader-available e-books: {len(ppns)}")
    return ppns


# --------------------------------------------------------------------------- #
# file enumeration (detail pages)
# --------------------------------------------------------------------------- #
def enumerate_from_file(path: Path) -> Iterator[tuple[str, str]]:
    text = path.read_text(encoding="utf-8").strip()
    if text.startswith("["):
        for obj in json.loads(text):
            if isinstance(obj, dict):
                if obj.get("ppn") and obj.get("slug"):
                    yield str(obj["ppn"]), str(obj["slug"])
                elif obj.get("url"):
                    m = _URL_RE.search(obj["url"])
                    if m:
                        yield m.group(1), m.group(2)
        return
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _URL_RE.search(line)
        if m:
            yield m.group(1), m.group(2)
        elif "," in line:
            ppn, slug = line.split(",", 1)
            yield ppn.strip(), slug.strip()


# --------------------------------------------------------------------------- #
# checkpoint + record writing
# --------------------------------------------------------------------------- #
def _load_done() -> set[str]:
    if CHECKPOINT.exists():
        try:
            return set(json.loads(CHECKPOINT.read_text()))
        except (json.JSONDecodeError, OSError):
            pass
    return set()


def _save_done(done: set[str]) -> None:
    CHECKPOINT.parent.mkdir(parents=True, exist_ok=True)
    CHECKPOINT.write_text(json.dumps(sorted(done)))


def _writer():
    RECORDS_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now().isoformat(timespec="seconds")

    def write(rec: dict) -> None:
        rec.setdefault("scraped_at", now)
        (RECORDS_DIR / f"{rec['ppn']}.json").write_text(
            json.dumps(rec, ensure_ascii=False), encoding="utf-8")

    return write


def _existing_ppns() -> set[str]:
    return {p.stem for p in RECORDS_DIR.glob("*.json")} if RECORDS_DIR.exists() else set()


def enrich(rate: float, limit=None) -> None:
    """Add detail-only fields (ISBN, full subjects, narrator, audience) to
    listing-sourced records that lack them. Resumable: records that already have
    an ISBN are skipped."""
    write = _writer()
    todo = []
    for path in sorted(RECORDS_DIR.glob("*.json")):
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if rec.get("isbn") or not rec.get("slug"):
            continue
        todo.append(rec)
    logger.info(f"Enriching {len(todo)} record(s) lacking detail fields")
    n = 0
    with Client(per_second=rate) as client:
        for rec in todo:
            detail = client.fetch_detail(rec["ppn"], rec["slug"])
            if detail:
                merged = {**rec, **{k: v for k, v in detail.items() if v}}
                merged["source"] = "listing+detail"
                write(merged)
                n += 1
                if n % 50 == 0:
                    logger.info(f"  …{n} enriched")
            if limit and n >= limit:
                break
    logger.info(f"Enriched {n} record(s)")


def harvest_details(pairs: Iterable[tuple[str, str]], rate: float, limit):
    write = _writer()
    n = 0
    with Client(per_second=rate) as client:
        for ppn, slug in pairs:
            rec = client.fetch_detail(ppn, slug)
            if rec:
                write(rec)
                n += 1
                if n % 50 == 0:
                    logger.info(f"  …{n} detail records")
            if limit and n >= limit:
                break
    logger.info(f"Harvested {n} detail record(s) -> {RECORDS_DIR}")


# --------------------------------------------------------------------------- #
# incremental sync (efficient updates)
# --------------------------------------------------------------------------- #
_SIG_FIELDS = ("title", "author", "year", "publisher", "format", "summary")


def _sig(rec: dict) -> tuple:
    return tuple(rec.get(f) for f in _SIG_FIELDS)


def sync(rate: float, max_pages: int = 300, streak_stop: int = 120) -> None:
    """Pick up new / changed titles cheaply by paging newest-by-license first
    and stopping once we hit a long run of already-known unchanged records."""
    write = _writer()
    new = updated = streak = 0
    with Client(per_second=rate) as client:
        page = 1
        while page <= max_pages and streak < streak_stop:
            recs, _ = parse_listing(client.get_listing_html(
                {"q": "*", "sorteer": "licentie_datum"}, page))
            if not recs:
                break
            for r in recs:
                path = RECORDS_DIR / f"{r['ppn']}.json"
                if path.exists():
                    old = json.loads(path.read_text(encoding="utf-8"))
                    if _sig(old) == _sig({**old, **{k: v for k, v in r.items() if v}}):
                        streak += 1
                        continue
                    write({**old, **{k: v for k, v in r.items() if v}})
                    updated += 1
                else:
                    write(r)
                    new += 1
                streak = 0
            page += 1
    logger.info(f"sync: +{new} new, {updated} updated (scanned {page - 1} pages)")


def reconcile(rate: float, formats: Iterable[str]) -> set[str]:
    """Full enumeration to detect removals: PPNs on disk but no longer in the
    catalog are stamped ``removed_at`` (the UI hides them)."""
    seen: set[str] = set()
    with Client(per_second=rate) as client:
        browse_all(client, formats, seen, lambda r: None)
    removed = _existing_ppns() - seen
    stamp = datetime.datetime.now().isoformat(timespec="seconds")
    for ppn in removed:
        path = RECORDS_DIR / f"{ppn}.json"
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
            rec["removed_at"] = stamp
            path.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        except (OSError, json.JSONDecodeError):
            pass
    logger.info(f"reconcile: {len(seen)} live, {len(removed)} marked removed")
    return removed


# --------------------------------------------------------------------------- #
# cli
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="obc scrape")
    src = p.add_mutually_exclusive_group()
    src.add_argument("--full", action="store_true",
                     help="complete catalog enumeration + e-reader flags (default)")
    src.add_argument("--ereader", action="store_true",
                     help="only refresh the e-reader-available PPN set")
    src.add_argument("--genres", action="store_true",
                     help="only refresh genre tags (via subject facets)")
    src.add_argument("--recent", action="store_true",
                     help="only refresh the recently-added ranking")
    src.add_argument("--sync", action="store_true",
                     help="incremental: pick up new/changed titles (newest first)")
    src.add_argument("--reconcile", action="store_true",
                     help="full scan to mark removed titles")
    src.add_argument("--from-file", type=Path, help="detail pages from a URL list")
    src.add_argument("--enrich", action="store_true",
                     help="add detail fields (ISBN, genres) to browsed records")
    p.add_argument("--formats", default="ebook,audiobook",
                   help="comma list: ebook,audiobook")
    p.add_argument("--rate", type=float, default=3.0, help="requests/second")
    p.add_argument("--limit", type=int, default=None, help="(file mode) max records")
    args = p.parse_args(argv)
    formats = [f.strip() for f in args.formats.split(",") if f.strip() in FORMATS]

    if args.from_file:
        harvest_details(enumerate_from_file(args.from_file), args.rate, args.limit)
    elif args.enrich:
        enrich(args.rate, args.limit)
    elif args.ereader:
        with Client(per_second=args.rate) as client:
            collect_ereader(client)
    elif args.genres:
        with Client(per_second=args.rate) as client:
            collect_genres(client)
    elif args.recent:
        with Client(per_second=args.rate) as client:
            collect_recent(client)
    elif args.sync:
        sync(args.rate)
    elif args.reconcile:
        with Client(per_second=args.rate) as client:
            reconcile(args.rate, formats)
    else:  # --full (default)
        seen = _existing_ppns()
        write = _writer()
        logger.info(f"Full enumeration of {formats} (resuming with {len(seen)} PPNs)")
        with Client(per_second=args.rate) as client:
            browse_all(client, formats, seen, write)
            collect_ereader(client)
            collect_genres(client)
            collect_recent(client)
        logger.info(f"Done. {len(seen)} unique records in {RECORDS_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
