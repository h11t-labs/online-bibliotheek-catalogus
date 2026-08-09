"""Harvest the catalog into ``data/raw/raw.db`` (see obc.raw), then ``obc normalize``.

Two enumeration modes:

* ``--browse`` (default, full catalog): walk the catalog via the faceted browse
  pages ``zoekresultaten.catalogus[.N].html?type=…&jaar=…``. Listing rows already
  carry rich metadata (title, author, summary, language, year, publisher, format,
  pages/duration, cover), so one request covers ~20 books. The site caps the
  pager at 50 pages (≈1000 results); when a (format, year) partition is capped we
  recursively split it by language → category → audience until each piece fits.
  Records are de-duplicated by PPN and the work is resumable per (format, year).

* ``--from-file PATH`` — fetch + parse individual detail pages from a list of
  catalog URLs / ``ppn,slug`` lines / JSON.

``--details`` sits on top of the first mode: it fetches the detail page for every
browsed record still missing something only that page carries — ISBN, the full
subject list, narrator, the e-reader flag, and the cross-edition links that decide
work identity.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Iterable, Iterator
from datetime import date
from pathlib import Path

import httpx

from . import raw
from .client import Client

# Data paths live in obc.config; imported (and rebindable) at module level so
# `scrape.RAW_DB` etc. stay monkeypatchable by tests and the scheduler.
from .config import (
    CHECKPOINT,
    EREADER_FILE,
    GENRES_FILE,
    RAW_DB,
    RECENT_FILE,
)
from .detail import parse_detail
from .listing import NotAResultsPage, parse_listing, total_results
from .log import logger
from .util import read_json, write_json

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
#
# `jaar` is the *original* publication year, not the edition's. So the classics
# sit where they were written: Moby Dick under 1851, Couperus under 1889. This
# range used to start at 1900 and silently lost every one of them — they fell in
# no year cell at all, and only whatever the maker-sort window happened to catch
# came back. 84 titles on the live catalog, found by comparing against an older
# copy that no longer exists. The floor is 1400 because the oldest Dutch title in
# the catalog is well inside it and 500 extra empty queries cost ~90s against a
# Dutch walk of 26 minutes.
#
# The ceiling comes off the clock for the same reason the floor moved. It used to
# be a hardcoded 2027, written when that was next year, and nothing would have
# bumped it: every 2028 title would have fallen in no cell either, the same loss
# at the other end. Evaluated at import, so a run that spans New Year keeps the
# ceiling it started with — the scheduler restarts often enough for that not to
# matter, and +2 leaves a year of slack anyway.
#
# Only Dutch is split this way — every other language fits under the 10k cap and
# is walked whole, which is why the loss was 88 Dutch titles and one English.
YEARS = list(range(date.today().year + 2, 1399, -1))

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


def _merge(base: dict, new: dict) -> dict:
    """Overlay only the truthy values of ``new`` onto ``base`` (a shallow copy)."""
    return {**base, **{k: v for k, v in new.items() if v}}


# --------------------------------------------------------------------------- #
# browse enumeration (full catalog via q=*)
# --------------------------------------------------------------------------- #
def _paginate(client: Client, params: dict[str, str], on_record,
              seen: set[str], max_page: int = PAGE_CAP) -> tuple[int, int | None]:
    """Page through one query, calling on_record for unseen PPNs. Returns the
    last page that had results and the result count the site claims (from
    page 1), which callers check their enumeration against."""
    page, total = 1, None
    while page <= max_page:
        html = client.get_listing_html(params, page)
        if page == 1:
            total = total_results(html)
        recs, _ = parse_listing(html)
        if not recs:
            break
        for r in recs:
            if r["ppn"] not in seen:
                seen.add(r["ppn"])
                on_record(r)
        page += 1
    return page - 1, total


def _enumerate_cell(client: Client, base: dict[str, str], on_record,
                    seen: set[str]) -> tuple[int, int | None]:
    """Completely enumerate one (type, taal[, leesvorm]) cell, working around
    the 10k cap by adding per-year + a maker-sort window when capped. Returns
    (records added, the total the site claims for the cell) so browse_all can
    tell a covered cell from one with a partition hole."""
    before = len(seen)
    last, total = _paginate(client, {**base, "sorteer": "titel"}, on_record, seen)
    if last >= PAGE_CAP:  # capped (Dutch): add year partitions + author window
        for year in YEARS:
            ylast, _ = _paginate(client, {**base, "jaar": str(year),
                                          "sorteer": "titel"}, on_record, seen)
            if ylast >= PAGE_CAP:
                # a single year past 10k has no further split here — say so
                # loudly instead of losing the tail without a trace
                logger.error(f"  jaar={year} zit zelf aan de 10k-cap "
                             f"({base.get('type')}/{base.get('taal')}) — "
                             "titels voorbij het venster ontbreken")
        _paginate(client, {**base, "sorteer": "maker"}, on_record, seen)
    return len(seen) - before, total


def _shortfall(got: int, claimed: int) -> bool:
    """Did an enumeration fall meaningfully short of what the site claims?
    The catalog changes under a 26-minute walk, so offset pagination drifts by
    a few titles; only a shortfall beyond that noise indicates a hole."""
    return got < claimed - max(20, claimed // 100)


def browse_all(client: Client, formats: Iterable[str], seen: set[str],
               on_record, ereader: bool = False) -> bool:
    """Enumerate the catalog per (format x language). Resumable per cell.

    With ``ereader=True`` only the e-reader-available subset is visited
    (``leesvorm=ereader``) — used to flag which e-books work on an e-reader.

    Returns whether ``seen`` ended up covering the whole catalog: false when a
    cell was skipped because a previous, interrupted run had already done it.
    That is the difference between "these are all the PPNs there are" and "these
    are the ones I happened to visit", and only the first may be used to conclude
    that a record on disk has been removed from the catalog.

    The checkpoint describes one run, and a namespace that already covers every
    cell means the last run finished — so it is cleared here, before the walk,
    rather than resumed. Clearing it on the way *out* instead is not the same
    thing and does not work: the run that meets a stale checkpoint still skips
    everything, so a second ``--full`` was needed before the first one did
    anything. Resume state from an *interrupted* run is partial, and survives.
    """
    tag = "er" if ereader else "all"
    cells = {f"{tag}:{fmt}:{taal}" for fmt in formats for taal in LANGS}
    done = _load_done()
    if cells <= done:
        done -= cells
        _save_done(done)
    complete = True
    for fmt in formats:
        # Σ of the per-taal claimed totals, checked against the unfiltered
        # format count below: LANGS is a hardcoded list, and a language the
        # library adds would otherwise fall in no cell — the exact shape of the
        # pre-1900 hole, one axis over. Only meaningful when every cell ran in
        # this walk and reported a count.
        fmt_total, fmt_counted = 0, True
        for taal in LANGS:
            key = f"{tag}:{fmt}:{taal}"
            if key in done:
                complete = False
                fmt_counted = False
                continue
            base = {"q": "*", "type": FORMATS[fmt], "taal": taal}
            if ereader:
                base["leesvorm"] = "ereader"
            try:
                added, total = _enumerate_cell(client, base, on_record, seen)
            except (httpx.HTTPError, NotAResultsPage) as e:
                # not checkpointed: the next run redoes this cell. `seen` is now
                # partial, so nothing downstream may conclude removals from it.
                logger.error(f"  {key}: enumeratie mislukt ({e}) — cel blijft open")
                complete = False
                fmt_counted = False
                continue
            done.add(key)
            _save_done(done)
            if total is None:
                fmt_counted = False
            else:
                fmt_total += total
                if _shortfall(added, total):
                    logger.error(f"  {key}: {added} van {total} geclaimde "
                                 "resultaten geënumereerd — partitie-gat?")
                    complete = False
            logger.info(f"  {key}: +{added} (total {len(seen)})")
        if fmt_counted:
            probe = {"q": "*", "type": FORMATS[fmt]}
            if ereader:
                probe["leesvorm"] = "ereader"
            try:
                claimed = total_results(client.get_listing_html(probe))
            except httpx.HTTPError:
                claimed = None
            if claimed is not None and _shortfall(fmt_total, claimed):
                logger.error(f"  {fmt}: taal-cellen claimen samen {fmt_total}, "
                             f"de catalogus {claimed} — ontbreekt er een taal "
                             "in LANGS?")
                complete = False
    return complete


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
    if page > max_page:  # ran into the cap, not out of results
        logger.warning(f"platte walk kapt af op pagina {max_page} voor {params} "
                       "— de staart voorbij 10k ontbreekt")


def collect_genres(client: Client) -> dict[str, list[str]]:
    """Tag books with genres by paging each subject (onderwerp) facet directly —
    no detail-page fetching. Split by language so the dominant Dutch subjects
    mostly stay under the 10k cap. Writes ppn -> [genre names] to GENRES_FILE."""
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
    write_json(GENRES_FILE, out)
    logger.info(f"Tagged {len(out)} books with genres")
    return out


def collect_recent(client: Client, max_page: int = 250) -> dict[str, int]:
    """Rank the most recently licensed titles (newest first) for a
    'Recent toegevoegd' sort. Writes ppn -> rank (0 = newest) to RECENT_FILE."""
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
    write_json(RECENT_FILE, rank)
    logger.info(f"Recency-ranked {len(rank)} recently added titles")
    return rank


def collect_ereader(client: Client) -> set[str]:
    """Enumerate e-reader-available e-books; persist the PPN set for normalize.

    This used to clear its own ``er:*`` checkpoint namespace first, because a
    completed prior run left every cell marked done — so the next run enumerated
    nothing and wrote an empty ereader.json, zeroing the flag on every e-book.
    ``browse_all`` clears its namespace when it finishes now, which fixes that
    without also destroying the resume state of a run that was interrupted.
    """
    seen: set[str] = set()
    ppns: set[str] = set()
    complete = browse_all(client, ["ebook"], seen, lambda r: ppns.add(r["ppn"]),
                          ereader=True)
    if not complete:
        # A resumed run skipped cells a previous, interrupted one finished, so
        # `ppns` misses those cells' e-books — writing it would zero the flag on
        # all of them at the next normalize. Keep the last complete answer.
        prior = set(read_json(EREADER_FILE, default=[]) or [])
        logger.warning(f"e-reader-walk onvolledig — vorige set blijft staan "
                       f"({len(prior)} e-books); draai --ereader opnieuw")
        return prior
    write_json(EREADER_FILE, sorted(ppns))
    logger.info(f"e-reader-available e-books: {len(ppns)}")
    return ppns


# --------------------------------------------------------------------------- #
# file enumeration (detail pages)
# --------------------------------------------------------------------------- #
_PPN_ONLY_RE = re.compile(r"[0-9xX]+")


def enumerate_from_file(path: Path) -> Iterator[tuple[str, str]]:
    text = path.read_text(encoding="utf-8").strip()
    if text.startswith(("[", "{")):
        objs = json.loads(text)
        if isinstance(objs, dict):  # a single object is a one-item list
            objs = [objs]
        for obj in objs:
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
            if _PPN_ONLY_RE.fullmatch(ppn.strip()):
                yield ppn.strip(), slug.strip()
            else:
                logger.warning(f"regel zonder geldige ppn overgeslagen: {line!r}")


# --------------------------------------------------------------------------- #
# checkpoint + record writing
# --------------------------------------------------------------------------- #
# The checkpoint records completed browse cells (keys like "all:ebook:dut" /
# "er:audiobook:eng") so a single interrupted run can resume where it stopped.
# Contract: it only ever describes the *current* run. Each consumer that starts a
# fresh enumeration is responsible for clearing its own namespace first — otherwise
# a completed run's checkpoint makes the next run skip everything and enumerate an
# empty catalog (see reconcile() and collect_ereader()).
def _load_done() -> set[str]:
    return set(read_json(CHECKPOINT, default=[]) or [])


def _save_done(done: set[str]) -> None:
    write_json(CHECKPOINT, sorted(done))


def _store():
    return raw.connect(RAW_DB)


def details(rate: float, limit=None) -> None:
    """Fetch the detail page for every record whose page we have never had.

    ISBN, the full subject/genre list, narrator, audience, age band, series,
    keywords, the e-reader flag and the cross-links all live on that page and
    nowhere else — and now the page itself is kept, so this is the last time any
    of it has to come off the network.
    """
    store = _store()
    todo = raw.without_detail(store)
    logger.info(f"Fetching detail pages for {len(todo)} record(s)")
    n = 0
    # cache=False switches off the loose per-file HTML cache: the store holds the
    # same bytes compressed, in one file, and is not wiped by every normalize.
    with Client(per_second=rate, cache=False) as client:
        for rec in todo:
            # Fetched and parsed in two steps, unlike client.fetch_detail, so the
            # page can be kept: parsing is what we happen to want from it today,
            # and re-deriving that later must not cost another 69k requests.
            html = client.get_detail_html(rec["ppn"], rec["slug"])
            if not html:
                continue
            detail = parse_detail(html, ppn=rec["ppn"])
            if not detail:
                # A soft-200 ("titel niet gevonden", maintenance chrome) parsed
                # to nothing. Storing it would stamp detail_at and this pass
                # would never come back for the real page.
                logger.warning(f"  {rec['ppn']}: pagina parset naar niets — "
                               "niet opgeslagen")
                continue
            raw.put_detail(store, rec["ppn"], html)
            merged = _merge(rec, detail)
            # _merge keeps only truthy values, so a detail ereader=0 (app-only)
            # would be dropped — carry the flag through explicitly so app-only
            # e-books aren't left blank.
            if detail.get("ereader") is not None:
                merged["ereader"] = detail["ereader"]
            raw.put(store, merged)
            n += 1
            if n % 50 == 0:
                logger.info(f"  …{n} fetched")
            if limit and n >= limit:
                break
    logger.info(f"Fetched {n} detail page(s); {raw.detail_count(store)} stored")
    store.close()


def reparse() -> int:
    """Re-derive every record from its stored page. No network.

    This is what the pages are for: a parser change becomes a local rebuild. #34
    taught ``detail.parse_detail`` to keep the "ook beschikbaar als" hrefs it had
    been discarding, and recovering them cost three hours of re-fetching pages
    that had already said so once.

    Merges rather than replaces: the page is the truth about what the detail page
    says and nothing else, so the browse-row fields on the record stay as they are.
    """
    store = _store()
    n = changed = 0
    for ppn, html in raw.iter_details(store):
        n += 1
        detail = parse_detail(html, ppn=ppn)
        if not detail:
            continue
        rec = raw.get(store, ppn) or {"ppn": ppn}
        merged = _merge(rec, detail)
        if detail.get("ereader") is not None:
            merged["ereader"] = detail["ereader"]
        if raw.put(store, merged):
            changed += 1
        if n % 5000 == 0:
            logger.info(f"  …{n} reparsed ({changed} changed)")
    store.close()
    logger.info(f"Reparsed {n} stored page(s); {changed} record(s) changed")
    return changed


def harvest_details(pairs: Iterable[tuple[str, str]], rate: float, limit):
    store = _store()
    n = 0
    with Client(per_second=rate) as client:
        for ppn, slug in pairs:
            html = client.get_detail_html(ppn, slug)
            if not html:
                continue
            rec = parse_detail(html, ppn=ppn)
            if rec:
                raw.put(store, rec)
                raw.put_detail(store, ppn, html)
                n += 1
                if n % 50 == 0:
                    logger.info(f"  …{n} detail records")
            if limit and n >= limit:
                break
    store.close()
    logger.info(f"Harvested {n} detail record(s) into the store")


# --------------------------------------------------------------------------- #
# incremental sync (efficient updates)
# --------------------------------------------------------------------------- #
_SIG_FIELDS = ("title", "author", "year", "publisher", "format", "summary")


def _sig(rec: dict) -> tuple:
    return tuple(rec.get(f) for f in _SIG_FIELDS)


def sync(rate: float, max_pages: int = 300, streak_stop: int = 120) -> None:
    """Pick up new / changed titles cheaply by paging newest-by-license first
    and stopping once we hit a long run of already-known unchanged records."""
    store = _store()
    new = updated = streak = 0
    with Client(per_second=rate) as client:
        page = 1
        while page <= max_pages and streak < streak_stop:
            recs, _ = parse_listing(client.get_listing_html(
                {"q": "*", "sorteer": "licentie_datum"}, page))
            if not recs:
                break
            for r in recs:
                old = raw.get(store, r["ppn"])
                if old is not None:
                    merged = _merge(old, r)
                    # A removed-marked title showing up in a listing is news even
                    # when its fields didn't change: it came back. Counting it in
                    # the unchanged streak would treat the ghost as evidence to
                    # stop looking; put(live=True) clears the stamp instead.
                    if _sig(old) == _sig(merged) and not old.get("removed_at"):
                        streak += 1
                        continue
                    raw.put(store, merged, live=True)
                    updated += 1
                else:
                    raw.put(store, r, live=True)
                    new += 1
                streak = 0
            page += 1
    store.close()
    logger.info(f"sync: +{new} new, {updated} updated (scanned {page - 1} pages)")


def mark_removed(seen: set[str], max_frac: float | None = None) -> set[str]:
    """Stamp ``removed_at`` on records the catalog no longer lists (the UI hides them).

    Only ever called with a ``seen`` set that covers the whole catalog. Concluding
    "absent from this run, therefore gone" from a partial enumeration marks the
    entire catalog removed, and the next normalize then drops it — which is
    precisely what a leftover checkpoint used to cause.

    The completeness gate upstream can still be fooled (a soft-200 that parses as
    an empty page, a facet value missing from LANGS), so this end refuses
    implausible volumes too: real removals are a trickle, and a large batch is an
    enumeration hole until a human says otherwise (``OBC_REMOVAL_MAX_FRAC``).
    """
    if max_frac is None:
        max_frac = float(os.environ.get("OBC_REMOVAL_MAX_FRAC", "0.05"))
    store = _store()
    known = raw.known_ppns(store)
    removed = known - seen
    if len(known) >= 1000 and len(removed) > max_frac * len(known):
        sample = ", ".join(sorted(removed)[:5])
        logger.error(
            f"weiger {len(removed)} van {len(known)} records als verwijderd te "
            f"stempelen (>{max_frac:.0%}; o.a. {sample}) — dat is een "
            "enumeratie-gat, geen massaverwijdering; zet OBC_REMOVAL_MAX_FRAC "
            "hoger als het echt zo is")
        store.close()
        return set()
    fresh = raw.mark_removed(store, removed)
    store.close()
    logger.info(f"{len(seen)} live, {fresh} newly marked removed "
                f"({len(removed)} missing in total)")
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
    src.add_argument("--from-file", type=Path, help="detail pages from a URL list")
    src.add_argument("--details", action="store_true",
                     help="fetch the detail page for every record still missing "
                          "something it provides (ISBN, genres, narrator, "
                          "e-reader flag, cross-links)")
    p.add_argument("--formats", default="ebook,audiobook",
                   help="comma list: ebook,audiobook")
    p.add_argument("--rate", type=float, default=3.0, help="requests/second")
    p.add_argument("--limit", type=int, default=None, help="(file mode) max records")
    args = p.parse_args(argv)
    names = [f.strip() for f in args.formats.split(",") if f.strip()]
    unknown = [n for n in names if n not in FORMATS]
    if unknown:
        # Dropping a typo silently would quietly narrow the run instead
        p.error(f"onbekend formaat: {', '.join(unknown)} "
                f"(kies uit: {', '.join(FORMATS)})")
    formats = names

    if args.from_file:
        harvest_details(enumerate_from_file(args.from_file), args.rate, args.limit)
    elif args.details:
        details(args.rate, args.limit)
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
    else:  # --full (default)
        # `seen` starts empty and collects what *this run* enumerates, which is
        # what makes removal detection possible: it used to be seeded with every
        # PPN already on disk, so "on disk but not in the catalog" was empty by
        # construction and a separate --reconcile pass had to re-walk the whole
        # catalog to answer the same question.
        seen: set[str] = set()
        store = _store()
        logger.info(f"Full enumeration of {formats}")

        def on_record(rec: dict) -> None:
            # merge, never overwrite: a browse row carries the listing fields only,
            # and writing it over a record would drop the ISBN, genres and
            # cross-links its detail page put there. live=True: a listing row is
            # proof the catalog carries the title, which un-marks a removal.
            old = raw.get(store, rec["ppn"])
            raw.put(store, _merge(old, rec) if old else rec, live=True)

        with Client(per_second=args.rate) as client:
            complete = browse_all(client, formats, seen, on_record)
            collect_ereader(client)
            collect_genres(client)
            collect_recent(client)
        store.close()
        # Two ways this run's `seen` can fail to be the whole catalog, and both
        # would mark good records removed: a resumed run skipped the cells the
        # previous one finished, and --formats narrowed the enumeration to part of
        # the catalog (with `ebook` alone, every audiobook is "missing").
        if complete and set(formats) == set(FORMATS):
            mark_removed(seen)
        else:
            logger.info("partial enumeration — not checking for removed titles")
        done = _store()
        logger.info(f"Done. {raw.count(done)} records, "
                    f"{raw.detail_count(done)} with a stored page")
        done.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
