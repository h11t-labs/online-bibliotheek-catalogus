"""Central data-path configuration. Everything lives under one root:

OBC_DATA  root directory for all catalog data   (default: ./data)
OBC_DB    path of the SQLite catalog file       (default: $OBC_DATA/catalog.db)

Modules import these constants and rebind them at module level, so the existing
`scrape.RECORDS_DIR` / `normalize.EREADER_FILE` / ... names (monkeypatched by
tests and by web/scheduler.py) keep working unchanged.
"""

import os
from pathlib import Path

from . import __version__

# One versioned, contactable User-Agent for every outbound request (scrape client,
# list providers, Wikipedia bio). The contact address lives only here.
USER_AGENT = (f"online-bibliotheek-catalogus/{__version__} "
              "(personal catalog project; contact: account.anthropic@harmenvanpelt.nl)")

DATA_DIR = Path(os.environ.get("OBC_DATA", "data"))
RAW_DIR = DATA_DIR / "raw"
RECORDS_DIR = RAW_DIR / "records"
HTML_CACHE = RAW_DIR / "html"
LISTS_DIR = RAW_DIR / "lists"
EREADER_FILE = RAW_DIR / "ereader.json"
GENRES_FILE = RAW_DIR / "genres.json"
RECENT_FILE = RAW_DIR / "recent.json"
CHECKPOINT = DATA_DIR / "checkpoint.json"
DEFAULT_DB = Path(os.environ.get("OBC_DB", DATA_DIR / "catalog.db"))
