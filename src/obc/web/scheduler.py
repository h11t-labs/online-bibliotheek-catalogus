"""Catalog-refresh runner for the web service.

The refresh must run *inside* the web machine because that's where the SQLite
volume is mounted (a Fly volume attaches to one machine). A stateless external
cron can't do the work itself — it hits the token-protected ``POST /admin/refresh``
endpoint, which calls :func:`trigger_refresh`. The work (incremental sync + lists
+ normalize) runs in a background thread so the HTTP request returns immediately.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import threading

from ..log import logger

# The installed console script, not `uv run obc`: `uv run` re-syncs the env on
# every call (including the "dev" group), so each refresh step would re-resolve
# and hit PyPI. Resolved via PATH — the image puts /app/.venv/bin there, and a
# local `uv run obc serve` exports the same venv to child processes.
_OBC = ["obc"]

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


def _schema_stale() -> bool:
    """True when the live DB predates the works schema — then a normalize from
    the records already on disk fixes the shape in minutes, instead of the site
    503ing behind a full sync pipeline."""
    from .. import db
    try:
        conn = sqlite3.connect(f"file:{db.DEFAULT_DB}?mode=ro", uri=True)
        try:
            return not conn.execute("SELECT 1 FROM sqlite_master "
                                    "WHERE type='table' AND name='works'").fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return False   # no DB at all -> the full-harvest path handles it


def _default_cmds() -> list[list[str]]:
    """The refresh pipeline: harvest (full on an empty volume, else incremental),
    optionally fill in detail-only fields, refresh the recency ranking + curated
    lists, then a single normalize that reflects it all. The detail pass is gated
    by ``OBC_ENRICH=1`` because on a fresh volume it fetches every detail page,
    one request per title.

    E-reader/genre facets ride the incremental path via ``--details`` (new titles'
    detail pages carry both), so they no longer need a periodic full re-enumeration.
    Recency is a cheap bounded scan, refreshed here on the incremental path (a
    ``--full`` harvest already collects it)."""
    seeded = _seeded()
    harvest = ["scrape", "--sync"] if seeded else ["scrape", "--full"]
    cmds = [harvest]
    if seeded and _schema_stale():
        # The deploy that renames the tables finds the old DB on the volume, and
        # the site 503s until the shape matches. Rebuilding from the records
        # already there is minutes; waiting out sync -> lists -> normalize is not.
        cmds.insert(0, ["normalize"])
    if os.environ.get("OBC_ENRICH") == "1":
        cmds.append(["scrape", "--details"])
    if seeded:  # --full already ranks recency; the incremental path must refresh it
        cmds.append(["scrape", "--recent"])
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
