"""Tests for the refresh runner (obc.web.scheduler) — no subprocesses, no network.

The actual work (_run -> subprocess) is stubbed; we only exercise the single-flight
lock and the full-vs-sync command selection."""

from __future__ import annotations

import threading
import time

from obc import normalize, scrape
from obc.web import scheduler


def _wait_until(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


def _seed_store(tmp_path) -> None:
    """One harvested record, which is what "the volume has something to refresh
    from" means."""
    from obc import raw
    conn = raw.connect(tmp_path / "raw.db")
    raw.put(conn, {"ppn": "1", "slug": "a"})
    conn.close()


def test_trigger_refresh_is_single_flight(monkeypatch):
    # Never touch real data: neutralise the disk-reclaim step _run_locked runs.
    monkeypatch.setattr(normalize, "_reclaim_disk", lambda *a, **k: None)

    started = threading.Event()
    release = threading.Event()

    def blocking_run(cmds):
        started.set()
        release.wait(timeout=5)

    monkeypatch.setattr(scheduler, "_run", blocking_run)

    # First trigger acquires the lock and starts the (blocked) worker.
    assert scheduler.trigger_refresh(["scrape", "--sync"]) is True
    assert started.wait(timeout=5)
    # While it holds the lock, a second trigger is refused (-> caller answers 409).
    assert scheduler.trigger_refresh(["scrape", "--sync"]) is False

    # Let the worker finish; the lock is released in _run_locked's finally.
    release.set()
    assert _wait_until(lambda: not scheduler._lock.locked())
    # A fresh trigger now succeeds again (release is set, so this worker exits fast).
    assert scheduler.trigger_refresh(["scrape", "--sync"]) is True
    assert _wait_until(lambda: not scheduler._lock.locked())


def test_trigger_refresh_releases_the_lock_when_the_command_build_fails(monkeypatch):
    """_default_cmds used to run *after* the lock was acquired: one exception there
    (an unreadable volume, say) held the lock forever, and every later refresh
    answered 409 until a restart."""
    import pytest

    def boom():
        raise RuntimeError("volume unreadable")

    monkeypatch.setattr(scheduler, "_default_cmds", boom)
    with pytest.raises(RuntimeError):
        scheduler.trigger_refresh()
    assert not scheduler._lock.locked()

    # and the next refresh still goes through (explicit cmds, stubbed run)
    monkeypatch.setattr(normalize, "_reclaim_disk", lambda *a, **k: None)
    monkeypatch.setattr(scheduler, "_run", lambda cmds: None)
    assert scheduler.trigger_refresh([["scrape", "--sync"]]) is True
    assert _wait_until(lambda: not scheduler._lock.locked())


def test_seeded_reads_an_unopenable_raw_store_as_unseeded(monkeypatch):
    """raw.connect raises sqlite3.OperationalError (not OSError) on a corrupt or
    read-only volume; that means "not seeded", not a crash out of the refresh."""
    import sqlite3

    from obc import raw

    def boom(path):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(raw, "connect", boom)
    assert scheduler._seeded() is False


def test_default_cmds_full_on_empty_sync_when_seeded(tmp_path, monkeypatch):
    monkeypatch.setattr(scrape, "RAW_DB", tmp_path / "raw.db")
    monkeypatch.delenv("OBC_ENRICH", raising=False)

    # empty store -> a full harvest
    assert scheduler._default_cmds()[0] == ["scrape", "--full"]

    # once a record file exists -> an incremental sync
    _seed_store(tmp_path)
    assert scheduler._default_cmds()[0] == ["scrape", "--sync"]


def test_default_cmds_refreshes_recency_on_incremental_path(tmp_path, monkeypatch):
    monkeypatch.setattr(scrape, "RAW_DB", tmp_path / "raw.db")
    monkeypatch.delenv("OBC_ENRICH", raising=False)

    # a full harvest already collects recency -> no separate --recent step
    assert ["scrape", "--recent"] not in scheduler._default_cmds()

    # incremental path can't re-derive recency from --sync, so it runs --recent
    _seed_store(tmp_path)
    assert ["scrape", "--recent"] in scheduler._default_cmds()


def test_default_cmds_normalizes_first_when_the_schema_is_stale(tmp_path, monkeypatch):
    """The deploy that renames the tables finds the old DB on the volume, and the
    site 503s until the shape matches. Rebuilding from the records already there is
    minutes; waiting out sync -> lists -> normalize is not."""
    import sqlite3

    from obc import db

    monkeypatch.setattr(scrape, "RAW_DB", tmp_path / "raw.db")
    _seed_store(tmp_path)   # seeded volume
    monkeypatch.delenv("OBC_ENRICH", raising=False)

    stale = tmp_path / "stale.db"
    conn = sqlite3.connect(stale)
    conn.executescript("CREATE TABLE books (ppn TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(db, "DEFAULT_DB", stale)
    assert scheduler._schema_stale() is True
    assert scheduler._default_cmds()[0] == ["normalize"]

    current = tmp_path / "current.db"
    conn = sqlite3.connect(current)
    conn.executescript("CREATE TABLE works (work_id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(db, "DEFAULT_DB", current)
    assert scheduler._schema_stale() is False
    assert scheduler._default_cmds()[0] == ["scrape", "--sync"]

    # no DB at all is the fresh-volume case: the full-harvest path handles it
    monkeypatch.setattr(db, "DEFAULT_DB", tmp_path / "absent.db")
    assert scheduler._schema_stale() is False
    assert ["normalize"] not in scheduler._default_cmds()[:1]
