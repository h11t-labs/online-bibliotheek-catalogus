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


def test_default_cmds_full_on_empty_sync_when_seeded(tmp_path, monkeypatch):
    records = tmp_path / "records"
    records.mkdir()
    monkeypatch.setattr(scrape, "RECORDS_DIR", records)
    monkeypatch.delenv("OBC_ENRICH", raising=False)

    # empty records dir -> a full harvest
    assert scheduler._default_cmds()[0] == ["scrape", "--full"]

    # once a record file exists -> an incremental sync
    (records / "1.json").write_text("{}", encoding="utf-8")
    assert scheduler._default_cmds()[0] == ["scrape", "--sync"]
