"""Catalog-refresh runner for the web service.

The refresh must run *inside* the web machine because that's where the SQLite
volume is mounted (a Fly volume attaches to one machine). A stateless external
cron can't do the work itself — it hits the token-protected ``POST /admin/refresh``
endpoint, which calls :func:`trigger_refresh`. The work (incremental sync + lists
+ normalize) runs in a background thread so the HTTP request returns immediately.
"""

from __future__ import annotations

import subprocess
import threading

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
