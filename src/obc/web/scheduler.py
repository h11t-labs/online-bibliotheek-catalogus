"""Optional in-process scheduler for periodic refreshes.

Railway volumes attach to a single service, so the cron work that updates the DB
has to run inside the web service rather than a separate cron service. Enable it
with env vars (hours; 0/unset = disabled):

    OBC_SYNC_HOURS=24      # `obc sync` (incremental catalog refresh)
    OBC_LISTS_HOURS=168    # `obc lists update` + `obc normalize` (weekly)

Each job runs in a daemon thread and shells out to the CLI so a long rebuild
never blocks the web workers.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time

from ..log import logger

_OBC = ["uv", "run", "obc"]


def _run(cmds: list[list[str]]) -> None:
    for cmd in cmds:
        logger.info(f"[cron] running: {' '.join(cmd)}")
        try:
            subprocess.run(_OBC + cmd, check=True)
        except (subprocess.CalledProcessError, OSError) as e:
            logger.warning(f"[cron] {' '.join(cmd)} failed: {e}")


def _loop(interval_s: float, cmds: list[list[str]]) -> None:
    while True:
        time.sleep(interval_s)
        _run(cmds)


def _hours(name: str) -> float:
    try:
        return float(os.environ.get(name, "0") or 0)
    except ValueError:
        return 0.0


def start() -> None:
    sync_h = _hours("OBC_SYNC_HOURS")
    lists_h = _hours("OBC_LISTS_HOURS")
    if sync_h > 0:
        threading.Thread(target=_loop, args=(sync_h * 3600, [["sync"]]),
                         daemon=True).start()
        logger.info(f"[cron] sync every {sync_h}h")
    if lists_h > 0:
        threading.Thread(target=_loop, args=(lists_h * 3600,
                         [["lists", "update"], ["normalize"]]), daemon=True).start()
        logger.info(f"[cron] lists+normalize every {lists_h}h")
