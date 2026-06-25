"""Curated book lists (bestsellers, prizes, nominations).

Each *provider* is a callable returning one or more list dicts
(``{slug, name, url, description, items:[{position,title,author,isbn,cover_url}]}``).
Lists are written to ``data/raw/lists/{slug}.json``; :func:`obc.normalize` then
matches items to catalog PPNs and fills ``lists`` / ``list_items`` / ``book_lists``.

Add an automated list: write a ``fetch_all()`` provider and append it to PROVIDERS.
Add a one-off / manually curated list: just drop a JSON file with the same shape
into ``data/raw/lists/`` — normalize picks it up. ``obc lists update`` only
rewrites provider slugs, so hand-made files are preserved.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path

from . import bestseller60, nyt, wikiprize
from ..log import logger

LISTS_DIR = Path("data/raw/lists")

# providers: each returns a list of list-dicts
PROVIDERS = [
    bestseller60.fetch_all,
    nyt.fetch_all,  # needs NYT_API_KEY env var (free at developer.nytimes.com)
    wikiprize.fetch_all,  # Dutch literary prizes via Wikipedia
]


def update(slugs: list[str] | None = None) -> None:
    LISTS_DIR.mkdir(parents=True, exist_ok=True)
    written = 0
    for provider in PROVIDERS:
        try:
            results = provider()
        except Exception as e:  # one bad provider shouldn't kill the rest
            logger.warning(f"provider {provider.__module__} failed: {e}")
            continue
        for data in results:
            if slugs and data["slug"] not in slugs:
                continue
            data["updated_at"] = datetime.datetime.now().isoformat(timespec="seconds")
            (LISTS_DIR / f"{data['slug']}.json").write_text(
                json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            logger.info(f"  {data['slug']}: {len(data.get('items', []))} items")
            written += 1
    logger.info(f"Wrote {written} list(s). Run `obc normalize` to match them to the catalog.")
