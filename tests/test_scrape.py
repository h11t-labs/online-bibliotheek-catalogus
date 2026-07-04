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
