"""New York Times Best Sellers — via the official NYT Books API.

Needs a free API key (https://developer.nytimes.com → "Books API"). Set it as
the ``NYT_API_KEY`` environment variable. One call to ``lists/full-overview``
returns every current list at once.
"""

from __future__ import annotations

import os

import httpx

from ..log import logger

API = "https://api.nytimes.com/svc/books/v3/lists/full-overview.json"
SOURCE_URL = "https://www.nytimes.com/books/best-sellers/"
_UA = "online-bibliotheek-catalogus/0.1 (personal project)"


def _title_case(t: str | None) -> str | None:
    # NYT titles come in ALL CAPS; tidy them for display (matching is case-folded)
    return t.title() if t and t.isupper() else t


def parse_overview(data: dict) -> list[dict]:
    out = []
    for lst in data.get("results", {}).get("lists", []):
        enc = lst.get("list_name_encoded") or str(lst.get("list_id"))
        name = lst.get("display_name") or lst.get("list_name") or enc
        items = [{
            "position": b.get("rank"),
            "title": _title_case(b.get("title")),
            "author": b.get("author"),
            "isbn": b.get("primary_isbn13") or b.get("primary_isbn10"),
            "cover_url": b.get("book_image"),
        } for b in lst.get("books", [])]
        if not items:
            continue
        out.append({
            "slug": f"nyt-{enc}",
            "name": f"NYT — {name}",
            "url": SOURCE_URL,
            "description": f"New York Times bestsellerlijst: {name}.",
            "items": items,
        })
    return out


def fetch_all() -> list[dict]:
    key = (os.environ.get("NYT_API_KEY") or "").strip()
    if not key:
        logger.warning("NYT_API_KEY is not set (or empty) — skipping NYT lists. "
                       "Add it to .env (free key at developer.nytimes.com → Books API).")
        return []
    r = httpx.get(API, params={"api-key": key}, headers={"User-Agent": _UA}, timeout=30)
    r.raise_for_status()
    return parse_overview(r.json())
