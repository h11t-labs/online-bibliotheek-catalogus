"""Small filesystem / JSON helpers used across the harvest + load pipeline."""

from __future__ import annotations

import json
import os
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
    """Write ``data`` as UTF-8 JSON, creating parent directories as needed.

    Written to a sibling ``.tmp`` and renamed into place: these files live on a
    volume that has hit 93% full, and a truncated half-write would otherwise be
    read back as "no data" (``read_json`` returns the default on invalid JSON).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=indent), encoding="utf-8"
    )
    os.replace(tmp, path)
