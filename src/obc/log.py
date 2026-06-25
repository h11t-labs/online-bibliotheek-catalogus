"""Central loguru logger for the project."""

from __future__ import annotations

import sys

from loguru import logger

logger.remove()
logger.add(
    sys.stderr,
    level="INFO",
    format="<green>{time:HH:mm:ss}</green> <level>{level: <7}</level> {message}",
    colorize=True,
)

__all__ = ["logger"]
