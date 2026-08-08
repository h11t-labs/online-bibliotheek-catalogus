"""Tests for the unattended harvest code (obc.scrape).

Everything runs against a FakeClient — no network — with the module-level data
paths monkeypatched to a tmp dir per test. The checkpoint tests below pin the C1
fix at its source: a completed run must not leave its checkpoint behind, because
the next run would then enumerate nothing and wipe the catalog.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from obc import scrape


def _listing_html(rows: list[tuple[str, str]]) -> str:
    """Minimal ``ul.rich-list`` page parse_listing understands. Empty rows -> a
    page past the end (no <li>), which stops pagination."""
    items = "".join(
        f'<li><a class="image-link" href="/catalogus/{ppn}/{slug}">t</a></li>'
        for ppn, slug in rows
    )
    return f'<ul class="rich-list">{items}</ul>'


def _rich_listing_html(items: list[dict]) -> str:
    """Like _listing_html but with optional title/auteur/summary spans, so sync's
    merge/signature logic has real fields to compare."""
    lis = []
    for it in items:
        parts = [f'<a class="image-link" href="/catalogus/{it["ppn"]}/{it["slug"]}">t</a>']
        if it.get("title"):
            parts.append(f'<span class="title">{it["title"]}</span>')
        if it.get("author"):
            parts.append(f'<span class="creator">{it["author"]}</span>')
        if it.get("summary"):
            parts.append(f'<p class="maintext">{it["summary"]}</p>')
        lis.append(f"<li>{''.join(parts)}</li>")
    return f'<ul class="rich-list">{"".join(lis)}</ul>'


class FakeClient:
    """Stub for obc.client.Client. Returns the same rows on page 1 of every query
    and an empty page afterwards, logging each call. Works both as a context
    manager (``with Client(...)``) and passed in directly."""

    def __init__(self, rows: list[tuple[str, str]] | None = None):
        self.rows = rows or []
        self.calls: list[tuple[dict, int]] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_listing_html(self, params: dict, page: int = 1) -> str:
        self.calls.append((dict(params), page))
        return _listing_html(self.rows if page == 1 else [])


def _seed_records(_unused, rows: list[tuple[str, str]]) -> None:
    """Put bare records in the store, as a browse pass would."""
    from obc import raw
    conn = raw.connect(scrape.RAW_DB)
    for ppn, slug in rows:
        raw.put(conn, {"ppn": ppn, "slug": slug})
    conn.close()


def _stored(ppn: str) -> dict | None:
    from obc import raw
    conn = raw.connect(scrape.RAW_DB)
    try:
        return raw.get(conn, ppn)
    finally:
        conn.close()


@pytest.fixture
def paths(tmp_path, monkeypatch):
    """Point scrape's data paths at a tmp dir; return the tmp dir."""
    monkeypatch.setattr(scrape, "CHECKPOINT", tmp_path / "checkpoint.json")
    monkeypatch.setattr(scrape, "EREADER_FILE", tmp_path / "ereader.json")
    monkeypatch.setattr(scrape, "RAW_DB", tmp_path / "raw.db")
    return tmp_path


def _all_keys(tag: str, formats) -> set[str]:
    return {f"{tag}:{fmt}:{taal}" for fmt in formats for taal in scrape.LANGS}


def test_a_completed_checkpoint_is_cleared_before_the_walk(paths):
    # C1, at the source. The checkpoint exists so an *interrupted* run can resume.
    # A namespace already covering every cell means the last run finished, so the
    # next one must start fresh — otherwise it skips everything and concludes the
    # catalog is empty. Clearing it on the way *out* is not the same thing: the run
    # that meets a stale checkpoint still does nothing, which is exactly what a
    # June checkpoint did to the first real run of this store.
    fake = FakeClient([("001", "a"), ("002", "b")])
    scrape._save_done(_all_keys("all", scrape.FORMATS))
    seen: set[str] = set()

    assert scrape.browse_all(fake, list(scrape.FORMATS), seen, lambda r: None) is True
    assert seen == {"001", "002"}          # it really did enumerate


def test_an_interrupted_run_still_resumes(paths):
    # the other half of the contract: cells a killed run finished are not redone,
    # and the run reports itself incomplete so nothing may be called removed.
    fake = FakeClient([("001", "a")])
    scrape._save_done({"all:ebook:dut"})
    seen: set[str] = set()

    assert scrape.browse_all(fake, list(scrape.FORMATS), seen, lambda r: None) is False
    assert not any(p == {"q": "*", "type": "E-book", "taal": "dut"}
                   for p, _page in fake.calls)


def _full_run(monkeypatch, rows, argv):
    """Drive the --full path with a FakeClient and the side-file passes stubbed."""
    monkeypatch.setattr(scrape, "Client", lambda **kw: FakeClient(rows))
    for name in ("collect_ereader", "collect_genres", "collect_recent"):
        monkeypatch.setattr(scrape, name, lambda client: None)
    scrape.main(argv)


def test_a_resumed_full_run_marks_nothing_removed(paths, monkeypatch):
    # A run that skipped cells has seen only part of the catalog, so "in the store
    # but not seen" is not evidence of removal. Marking it anyway wipes the catalog
    # on the next normalize — the failure the old --reconcile pass was written
    # around, now unexpressible: the stamp is gated on browse_all reporting
    # completeness.
    rows = [("001", "a"), ("002", "b")]
    _seed_records(None, [*rows, ("003", "gone")])
    scrape._save_done({"all:ebook:dut"})

    _full_run(monkeypatch, rows, ["--full"])

    assert "removed_at" not in _stored("003")


def test_a_narrowed_full_run_marks_nothing_removed(paths, monkeypatch):
    # --formats ebook enumerates no audiobooks, so every audiobook in the store is
    # "missing" from a run that was never looking for one.
    rows = [("001", "a")]
    _seed_records(None, [*rows, ("002", "an-audiobook")])

    _full_run(monkeypatch, rows, ["--full", "--formats", "ebook"])

    assert "removed_at" not in _stored("002")


def test_a_full_run_keeps_what_the_detail_pass_added(paths, monkeypatch):
    # a browse row carries the listing fields only; writing it over a record would
    # drop the ISBN and the cross-links that decide work identity
    from obc import raw

    conn = raw.connect(scrape.RAW_DB)
    raw.put(conn, {"ppn": "001", "slug": "a", "isbn": "978",
                   "related_ppns": ["002"]})
    conn.close()

    _full_run(monkeypatch, [("001", "a")], ["--full"])

    rec = _stored("001")
    assert rec["isbn"] == "978" and rec["related_ppns"] == ["002"]


def test_a_complete_enumeration_marks_what_the_catalog_dropped(paths):
    rows = [("001", "a"), ("002", "b")]
    _seed_records(None, [*rows, ("003", "gone")])
    fake = FakeClient(rows)
    seen: set[str] = set()

    assert scrape.browse_all(fake, list(scrape.FORMATS), seen, lambda r: None) is True
    assert scrape.mark_removed(seen) == {"003"}

    assert _stored("003")["removed_at"]
    assert "removed_at" not in _stored("001")


def test_collect_ereader_rerun_reenumerates(paths):
    # the e-reader pass writes its file only at the very end, so a run that
    # enumerated nothing would write an empty ereader.json and zero the flag on
    # every e-book. Running it twice must give the same answer twice.
    fake = FakeClient([("001", "a"), ("005", "e")])

    assert scrape.collect_ereader(fake) == {"001", "005"}
    assert scrape.collect_ereader(fake) == {"001", "005"}

    written = json.loads((paths / "ereader.json").read_text(encoding="utf-8"))
    assert set(written) == {"001", "005"}


# --------------------------------------------------------------------------- #
# enumerate_from_file (pure function)
# --------------------------------------------------------------------------- #
def test_enumerate_from_file_url_lines(tmp_path):
    p = tmp_path / "urls.txt"
    p.write_text(
        "https://www.onlinebibliotheek.nl/catalogus/123/mijn-boek\n"
        "\n"
        "geen match op deze regel\n", encoding="utf-8")
    assert list(scrape.enumerate_from_file(p)) == [("123", "mijn-boek")]


def test_enumerate_from_file_ppn_slug_lines(tmp_path):
    p = tmp_path / "pairs.txt"
    p.write_text("123, mijn-boek\n456,ander-boek\n", encoding="utf-8")
    assert list(scrape.enumerate_from_file(p)) == [("123", "mijn-boek"), ("456", "ander-boek")]


def test_enumerate_from_file_json_array(tmp_path):
    p = tmp_path / "recs.json"
    p.write_text(json.dumps([
        {"ppn": "1", "slug": "een"},
        {"url": "https://x/catalogus/2/twee"},
        {"nothing": "useful"},
    ]), encoding="utf-8")
    assert list(scrape.enumerate_from_file(p)) == [("1", "een"), ("2", "twee")]


# --------------------------------------------------------------------------- #
# _paginate
# --------------------------------------------------------------------------- #
def test_paginate_dedups_and_stops_on_empty_page():
    fake = FakeClient([("1", "a"), ("2", "b"), ("1", "a")])  # dup within the page
    seen: set[str] = set()
    got: list[str] = []
    last = scrape._paginate(fake, {"q": "*"}, lambda r: got.append(r["ppn"]), seen)
    assert got == ["1", "2"]          # duplicate PPN skipped via `seen`
    assert last == 1                  # page 1 had results, page 2 was empty
    assert [pg for _, pg in fake.calls] == [1, 2]


def test_paginate_respects_max_page():
    class AlwaysClient:
        def __init__(self):
            self.pages: list[int] = []

        def get_listing_html(self, params, page=1):
            self.pages.append(page)
            return _listing_html([(f"{page}01", "s")])  # every page has a fresh row

    c = AlwaysClient()
    last = scrape._paginate(c, {"q": "*"}, lambda r: None, set(), max_page=3)
    assert last == 3
    assert c.pages == [1, 2, 3]


# --------------------------------------------------------------------------- #
# browse_all resume (the legitimate behavior step 4 preserved)
# --------------------------------------------------------------------------- #
def test_browse_all_skips_cells_already_in_checkpoint(paths):
    # a partial namespace is an interrupted run: those cells are not walked again
    fake = FakeClient([("1", "a")])
    skipped = {f"all:ebook:{taal}" for taal in scrape.LANGS}
    scrape._save_done(skipped)
    seen: set[str] = set()

    scrape.browse_all(fake, list(scrape.FORMATS), seen, lambda r: None)

    asked = {p.get("type") for p, _page in fake.calls}
    assert asked == {"Digitaal_luisterboek"}   # every e-book cell was resumed-as-done


# --------------------------------------------------------------------------- #
# sync
# --------------------------------------------------------------------------- #
class _OnePageClient:
    """Rich listing on page 1, empty afterwards (pagination stops naturally)."""

    def __init__(self, items: list[dict]):
        self.items = items

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_listing_html(self, params, page=1):
        return _rich_listing_html(self.items if page == 1 else [])


class _RepeatClient:
    """Same rows on EVERY page, so pagination only stops via streak_stop."""

    def __init__(self, rows: list[tuple[str, str]]):
        self.rows = rows
        self.calls: list[int] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_listing_html(self, params, page=1):
        self.calls.append(page)
        return _listing_html(self.rows)


def test_sync_writes_new_and_merge_preserves_old_fields(paths, monkeypatch):
    from obc import raw

    conn = raw.connect(scrape.RAW_DB)
    raw.put(conn, {"ppn": "001", "slug": "a", "title": "Oud", "isbn": "978"})
    conn.close()

    class SyncClient(FakeClient):
        def get_listing_html(self, params, page=1):
            self.calls.append((dict(params), page))
            if page > 1:
                return _rich_listing_html([])
            return _rich_listing_html([
                {"ppn": "001", "slug": "a", "title": "Nieuw"},
                {"ppn": "009", "slug": "i", "title": "Onbekend"}])

    monkeypatch.setattr(scrape, "Client", lambda **kw: SyncClient())
    scrape.sync(rate=99)

    changed = _stored("001")
    assert changed["title"] == "Nieuw"      # the listing refreshes what it knows
    assert changed["isbn"] == "978"         # and leaves the detail fields alone
    assert _stored("009")["title"] == "Onbekend"


def test_sync_stops_after_streak_of_unchanged(paths, monkeypatch):
    records = paths / "records"
    records.mkdir(parents=True, exist_ok=True)
    # two records whose signature the (field-less) listing rows won't change
    for ppn in ("11", "22"):
        (records / f"{ppn}.json").write_text(json.dumps(
            {"ppn": ppn, "slug": ppn, "title": f"T-{ppn}", "format": "ebook"}),
            encoding="utf-8")

    client = _RepeatClient([("11", "s1"), ("22", "s2")])  # unchanged, on every page
    monkeypatch.setattr(scrape, "Client", lambda *a, **k: client)

    scrape.sync(3.0, max_pages=50, streak_stop=3)

    # It halts on the unchanged streak, not on max_pages or an empty page: only a
    # couple of pages are fetched even though the client would serve them forever.
    assert len(client.calls) <= 3


# --------------------------------------------------------------------------- #
# --details (the pass that replaced --enrich + --relink)
# --------------------------------------------------------------------------- #
def test_details_stamps_and_keeps_what_it_fetched(paths, monkeypatch):
    from obc import raw

    _seed_records(None, [("001", "a")])
    conn = raw.connect(scrape.RAW_DB)
    raw.put(conn, {"ppn": "002", "slug": "b"})
    raw.put_detail(conn, "002", "<html>already have this one</html>")
    conn.close()

    fetched = []

    class DetailClient(FakeClient):
        def get_detail_html(self, ppn, slug):
            fetched.append(ppn)
            return f"<html>{ppn}</html>"

    monkeypatch.setattr(scrape, "Client", lambda **kw: DetailClient())
    monkeypatch.setattr(scrape, "parse_detail",
                        lambda html, ppn: {"ppn": ppn, "isbn": "979", "ereader": 0})
    scrape.details(rate=99)

    assert fetched == ["001"]                    # 002 already has its page
    rec = _stored("001")
    assert rec["isbn"] == "979" and rec["detail_at"]
    # _merge keeps truthy values only, so an app-only e-book (ereader=0) would
    # lose the flag rather than record it
    assert rec["ereader"] == 0
    # and the page itself is kept, so this fetch never has to happen again
    conn = raw.connect(scrape.RAW_DB)
    assert raw.detail_html(conn, "001") == "<html>001</html>"
    conn.close()




def test_reading_records_leaves_the_pages_on_disk(paths):
    """A record read must not drag its page along.

    The page is 86% of the store — 708 MB against 114 MB of records — and no
    reader of a record ever looks at it. `SELECT *` still pulled it off disk,
    which put 1.6 GB through the page cache per normalize (the store is walked
    twice) on a machine with 132 MB of cache and a 593 MB catalog to serve. The
    live symptom was a nightly rebuild making every aggregate page take 20s.
    """
    from obc import raw

    conn = raw.connect(scrape.RAW_DB)
    raw.put(conn, {"ppn": "001", "slug": "a", "title": "De Ontdekking"})
    raw.put_detail(conn, "001", "<html>" + "x" * 100_000 + "</html>")

    assert "detail_html" not in raw._COLS   # not in the projection at all
    assert raw.get(conn, "001")["title"] == "De Ontdekking"
    assert next(raw.iter_records(conn))["title"] == "De Ontdekking"
    # …and the page is still there for the one reader that does want it
    assert raw.detail_html(conn, "001").startswith("<html>")


def test_the_year_split_reaches_the_classics():
    """`jaar` is the *original* publication year, not the edition's.

    So Moby Dick sits under 1851 and Couperus under 1889, and a range starting at
    1900 put them in no cell at all — reachable only by whatever the maker-sort
    window happened to catch. That silently lost 84 titles from the live catalog,
    found only by comparing against an older copy that no longer exists.

    Only Dutch is split by year (every other language fits under the 10k cap and
    is walked whole), which is why the loss was 88 Dutch titles and one English.

    The ceiling is asserted against the clock, not against a literal. A literal
    was what let the old upper bound rot: `>= 2027` stays true forever, so the
    test would have kept passing through the same loss at the other end.
    """
    assert 1851 in scrape.YEARS, "Moby Dick"
    assert 1889 in scrape.YEARS, "Couperus, Eline Vere"
    assert 1605 in scrape.YEARS, "Don Quijote"
    assert max(scrape.YEARS) >= date.today().year + 1  # and still covers new licences
