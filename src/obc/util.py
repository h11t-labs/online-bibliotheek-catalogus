"""Small filesystem / JSON helpers used across the harvest + load pipeline."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def read_json(path: str | Path, default: Any = None) -> Any:
    """Parse a JSON file, returning ``default`` on a missing or invalid file.

    Centralises the ``json.loads(path.read_text())`` + try/except pattern used
    by the scraper and the normaliser so callers don't repeat error handling.
    """
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def write_json(path: str | Path, data: Any, *, indent: int | None = None) -> None:
    """Write ``data`` as UTF-8 JSON, creating parent directories as needed."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=indent), encoding="utf-8"
    )
