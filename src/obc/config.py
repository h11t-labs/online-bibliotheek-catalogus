"""Central data-path configuration. Everything lives under one root:

OBC_DATA  root directory for all catalog data   (default: ./data)
OBC_DB    path of the SQLite catalog file       (default: $OBC_DATA/catalog.db)

Modules import these constants and rebind them at module level, so the existing
`scrape.RAW_DB` / `normalize.EREADER_FILE` / ... names (monkeypatched by
tests and by web/scheduler.py) keep working unchanged.
"""

import os
from pathlib import Path

from . import __version__

# One versioned User-Agent for every outbound request (scrape client, list
# providers, Wikipedia bio).
USER_AGENT = (f"online-bibliotheek-catalogus/{__version__} "
              "(personal catalog project)")

DATA_DIR = Path(os.environ.get("OBC_DATA", "data"))
RAW_DIR = DATA_DIR / "raw"
HTML_CACHE = RAW_DIR / "html"
LISTS_DIR = RAW_DIR / "lists"
EREADER_FILE = RAW_DIR / "ereader.json"
GENRES_FILE = RAW_DIR / "genres.json"
RECENT_FILE = RAW_DIR / "recent.json"
# Everything the harvest knows (see obc.raw): the parsed record and the detail
# page it came from, one row per PPN. Source data, not derived — kept out of
# catalog.db, which every normalize throws away and rebuilds.
RAW_DB = RAW_DIR / "raw.db"
CHECKPOINT = DATA_DIR / "checkpoint.json"
DEFAULT_DB = Path(os.environ.get("OBC_DB", DATA_DIR / "catalog.db"))
