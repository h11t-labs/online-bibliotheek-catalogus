"""Tests for the unattended harvest code (obc.scrape).

Everything runs against a FakeClient — no network — with the module-level data
paths monkeypatched to a tmp dir per test. The two checkpoint tests below pin the
C1 fix: a completed run's checkpoint must not leak into the next run of a
different mode (which used to enumerate nothing and wipe the catalog).
"""

from __future__ import annotations

import json

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
    """Like _listing_html but with optional title/author/summary spans, so sync's
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


def _seed_records(records_dir, rows: list[tuple[str, str]]) -> None:
    records_dir.mkdir(parents=True, exist_ok=True)
    for ppn, slug in rows:
        (records_dir / f"{ppn}.json").write_text(
            json.dumps({"ppn": ppn, "slug": slug}), encoding="utf-8")


@pytest.fixture
def paths(tmp_path, monkeypatch):
    """Point scrape's data paths at a tmp dir; return the tmp dir."""
    monkeypatch.setattr(scrape, "RECORDS_DIR", tmp_path / "records")
    monkeypatch.setattr(scrape, "CHECKPOINT", tmp_path / "checkpoint.json")
    monkeypatch.setattr(scrape, "EREADER_FILE", tmp_path / "ereader.json")
    return tmp_path


def _all_keys(tag: str, formats) -> set[str]:
    return {f"{tag}:{fmt}:{taal}" for fmt in formats for taal in scrape.LANGS}


def test_reconcile_after_completed_full_run_marks_nothing_removed(paths):
    # C1: a completed --full leaves every all:* cell in the checkpoint. reconcile
    # must clear it and re-enumerate, so records still in the catalog are NOT
    # falsely marked removed. (On the old code seen stays empty -> everything
    # removed -> the next normalize drops the whole catalog.)
    rows = [("001", "a"), ("002", "b"), ("003", "c")]
    fake = FakeClient(rows)
    _seed_records(paths / "records", rows)
    scrape._save_done(_all_keys("all", scrape.FORMATS))

    removed = scrape.reconcile(fake, list(scrape.FORMATS))

    assert removed == set()


def test_collect_ereader_rerun_reenumerates(paths):
    # C1: a completed prior ereader run (or the ereader pass of a completed --full)
    # leaves every er:* cell done. collect_ereader must strip its own namespace and
    # re-enumerate, so it returns the real PPN set and writes it — not an empty
    # ereader.json that would zero the e-reader flag on every e-book.
    rows = [("001", "a"), ("005", "e")]
    fake = FakeClient(rows)
    scrape._save_done(_all_keys("er", scrape.FORMATS))

    ppns = scrape.collect_ereader(fake)

    assert ppns == {"001", "005"}
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
    fake = FakeClient([("1", "a")])
    scrape._save_done(_all_keys("all", scrape.FORMATS))  # every cell already done
    seen: set[str] = set()

    scrape.browse_all(fake, list(scrape.FORMATS), seen, lambda r: None)

    assert fake.calls == []   # zero requests: all cells were resumed-as-done
    assert seen == set()


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
    records = paths / "records"
    records.mkdir(parents=True, exist_ok=True)
    # existing record (PPNs must be [0-9xX]+ for the listing parser) with a
    # detail-only field (isbn) not present in listing rows
    (records / "100.json").write_text(json.dumps(
        {"ppn": "100", "slug": "keep", "title": "Oud", "author": "Auteur",
         "format": "ebook", "isbn": "ISBN-OLD", "summary": "zelfde"}), encoding="utf-8")

    client = _OnePageClient([
        {"ppn": "100", "slug": "keep", "title": "Nieuw", "author": "Auteur",
         "summary": "zelfde"},                       # title changed -> update
        {"ppn": "200", "slug": "new", "title": "Nieuw boek"},   # not on disk -> new
    ])
    monkeypatch.setattr(scrape, "Client", lambda *a, **k: client)

    scrape.sync(3.0)

    keep = json.loads((records / "100.json").read_text(encoding="utf-8"))
    assert keep["title"] == "Nieuw"          # updated from the listing
    assert keep["isbn"] == "ISBN-OLD"        # old detail-only field preserved by merge
    assert (records / "200.json").exists()   # brand-new record written


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
