"""Catalog-refresh runner for the web service.

The refresh must run *inside* the web machine because that's where the SQLite
volume is mounted (a Fly volume attaches to one machine). A stateless external
cron can't do the work itself — it hits the token-protected ``POST /admin/refresh``
endpoint, which calls :func:`trigger_refresh`. The work (incremental sync + lists
+ normalize) runs in a background thread so the HTTP request returns immediately.
"""

from __future__ import annotations

import os
import subprocess
import threading

from ..log import logger

_OBC = ["uv", "run", "obc"]

_lock = threading.Lock()  # ensures only one refresh runs at a time


def _seeded() -> bool:
    """True once the volume holds harvested records to refresh from. On a fresh
    volume (first deploy) there are none, so we do a full harvest instead of an
    incremental sync that would only pick up the newest titles."""
    from ..scrape import RECORDS_DIR
    try:
        return next(RECORDS_DIR.glob("*.json"), None) is not None
    except OSError:
        return False


def _default_cmds() -> list[list[str]]:
    """The refresh pipeline: harvest (full on an empty volume, else incremental),
    optionally enrich detail-only fields (age/series/keywords), refresh curated
    lists, then a single normalize that reflects it all. Enrich is gated by
    ``OBC_ENRICH=1`` since the first full pass fetches every detail page (slow)."""
    harvest = ["scrape", "--sync"] if _seeded() else ["scrape", "--full"]
    cmds = [harvest]
    if os.environ.get("OBC_ENRICH") == "1":
        cmds.append(["scrape", "--enrich"])
    cmds += [["lists", "update"], ["normalize"]]
    return cmds


def _run(cmds: list[list[str]]) -> None:
    for cmd in cmds:
        logger.info(f"[refresh] running: {' '.join(cmd)}")
        try:
            subprocess.run(_OBC + cmd, check=True)
        except (subprocess.CalledProcessError, OSError) as e:
            logger.warning(f"[refresh] {' '.join(cmd)} failed: {e}")


def _run_locked(cmds: list[list[str]]) -> None:
    try:
        # Free disk before scraping so even the incremental sync + lists writes fit
        # on a tight volume (drops stale WAL/journal sidecars + the HTML cache).
        try:
            from .. import db
            from ..normalize import RAW_DIR, _reclaim_disk
            _reclaim_disk(db.DEFAULT_DB, RAW_DIR)
        except Exception as e:  # never let cleanup abort the refresh
            logger.warning(f"[refresh] disk reclaim skipped: {e}")
        _run(cmds)
    finally:
        _lock.release()


def trigger_refresh(cmds: list[list[str]] | None = None) -> bool:
    """Start a refresh in a background thread. Returns False if one is already
    running (so callers can answer 409)."""
    if not _lock.acquire(blocking=False):
        return False
    threading.Thread(target=_run_locked, args=(cmds or _default_cmds(),),
                     daemon=True).start()
    return True
