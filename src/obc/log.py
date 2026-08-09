"""Central loguru logger for the project."""

from __future__ import annotations

import os
import sys

from loguru import logger

logger.remove()
logger.add(
    sys.stderr,
    level=os.environ.get("OBC_LOG_LEVEL", "INFO").upper(),
    format="<green>{time:HH:mm:ss}</green> <level>{level: <7}</level> {message}",
    colorize=None,  # auto-detect: ANSI on a tty, plain text in shipped/piped logs
)

__all__ = ["logger"]
