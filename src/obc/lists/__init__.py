"""Curated book lists (bestsellers, prizes, nominations).

Each *provider* is a callable returning one or more list dicts
(``{slug, name, url, description, items:[{position,title,author,isbn,cover_url}]}``).
Lists are written to ``data/raw/lists/{slug}.json``; :func:`obc.normalize` then
matches items to catalog PPNs and fills ``lists`` / ``list_items`` / ``work_lists``.

Add an automated list: write a ``fetch_all()`` provider and append it to PROVIDERS.
Add a one-off / manually curated list: just drop a JSON file with the same shape
into ``data/raw/lists/`` — normalize picks it up. ``obc lists update`` only
rewrites provider slugs, so hand-made files are preserved.
"""

from __future__ import annotations

import datetime
import json
import os
import re

from ..config import LISTS_DIR  # rebindable module-level path (see obc.config)
from ..log import logger
from . import bestseller60, nyt, wikiprize

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
        name = provider.__module__.rsplit(".", 1)[-1]
        try:
            results = provider()
        except Exception as e:  # one bad provider shouldn't kill the rest
            logger.warning(f"provider {provider.__module__} failed: {e}")
            results = []
        # Fewer lists than this provider's files already on disk means the rest
        # are silently going stale (upstream markup/API change) — surface that.
        existing = len(list(LISTS_DIR.glob(f"{name}*.json")))
        if len(results) < existing:
            logger.warning(f"{name}: produced {len(results)} list(s) but {existing} "
                           f"{name}*.json file(s) exist — those were not refreshed")
        for data in results:
            # Slugs become filenames; NYT's embed API-supplied text, so strip
            # anything that could escape LISTS_DIR ("/", "..").
            slug = re.sub(r"[^a-z0-9-]", "", str(data["slug"]).lower())
            if not slug:
                logger.warning(f"{name}: unusable slug {data['slug']!r} — skipped")
                continue
            if slugs and slug not in slugs:
                continue
            data["slug"] = slug
            data["updated_at"] = datetime.datetime.now().isoformat(timespec="seconds")
            target = LISTS_DIR / f"{slug}.json"
            tmp = target.with_name(target.name + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, target)  # atomic: never leave truncated JSON at the live path
            logger.info(f"  {slug}: {len(data.get('items', []))} items")
            written += 1
    logger.info(f"Wrote {written} list(s). Run `obc normalize` to match them to the catalog.")
