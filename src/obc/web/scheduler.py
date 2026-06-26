"""Catalog-refresh runner for the web service.

The refresh must run *inside* the web machine because that's where the SQLite
volume is mounted (a Fly/Railway/Render volume attaches to one machine). So a
stateless external cron can't do the work itself — instead it hits the
token-protected ``POST /admin/refresh`` endpoint, which calls
:func:`trigger_refresh` here. The actual work (incremental sync + lists +
normalize) runs in a background thread so the HTTP request returns immediately.

An optional in-process interval scheduler (:func:`start`) remains for hosts
without external cron — enable with ``OBC_SYNC_HOURS`` / ``OBC_LISTS_HOURS``.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time

from ..log import logger

_OBC = ["uv", "run", "obc"]

# The weekly refresh: pick up new/changed titles, refresh curated lists, rebuild.
# A single normalize at the end reflects both the sync and the lists update.
REFRESH_CMDS = [["scrape", "--sync"], ["lists", "update"], ["normalize"]]

_lock = threading.Lock()  # ensures only one refresh runs at a time


def _run(cmds: list[list[str]]) -> None:
    for cmd in cmds:
        logger.info(f"[refresh] running: {' '.join(cmd)}")
        try:
            subprocess.run(_OBC + cmd, check=True)
        except (subprocess.CalledProcessError, OSError) as e:
            logger.warning(f"[refresh] {' '.join(cmd)} failed: {e}")


def _run_locked(cmds: list[list[str]]) -> None:
    try:
        _run(cmds)
    finally:
        _lock.release()


def trigger_refresh(cmds: list[list[str]] | None = None) -> bool:
    """Start a refresh in a background thread. Returns False if one is already
    running (so callers can answer 409)."""
    if not _lock.acquire(blocking=False):
        return False
    threading.Thread(target=_run_locked, args=(cmds or REFRESH_CMDS,),
                     daemon=True).start()
    return True


def _loop(interval_s: float, cmds: list[list[str]]) -> None:
    while True:
        time.sleep(interval_s)
        if _lock.acquire(blocking=False):
            _run_locked(cmds)  # releases the lock when done
        else:
            logger.info("[refresh] skip interval run — already running")


def _hours(name: str) -> float:
    try:
        return float(os.environ.get(name, "0") or 0)
    except ValueError:
        return 0.0


def start() -> None:
    """Optional in-process interval scheduler (off unless *_HOURS are set).
    Prefer the external Fly cron -> /admin/refresh path; this is a fallback."""
    sync_h = _hours("OBC_SYNC_HOURS")
    lists_h = _hours("OBC_LISTS_HOURS")
    if sync_h > 0:
        threading.Thread(target=_loop, args=(sync_h * 3600,
                         [["scrape", "--sync"], ["normalize"]]), daemon=True).start()
        logger.info(f"[refresh] interval sync+normalize every {sync_h}h")
    if lists_h > 0:
        threading.Thread(target=_loop, args=(lists_h * 3600,
                         [["lists", "update"], ["normalize"]]), daemon=True).start()
        logger.info(f"[refresh] interval lists+normalize every {lists_h}h")
