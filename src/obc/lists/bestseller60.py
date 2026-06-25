"""Bestseller 60 — the weekly Dutch top-60 from https://www.debestseller60.nl

The site also has genre toplists (Fictie, Non-Fictie, Jeugd, Koken, Spannend)
at clean sub-paths; each renders with the same card markup.
"""

from __future__ import annotations

import html
import re

import httpx

from ..log import logger

URL = "https://www.debestseller60.nl"
_UA = "online-bibliotheek-catalogus/0.1 (personal project)"

# (slug, display name, path, description)
SUBLISTS = [
    ("bestseller60", "Bestseller 60", "",
     "De wekelijkse Nederlandse top 60 best verkochte boeken."),
    ("bestseller60-fictie", "Bestseller 60 — Fictie", "fictie",
     "De best verkochte fictie van de week."),
    ("bestseller60-non-fictie", "Bestseller 60 — Non-fictie", "non-fictie",
     "De best verkochte non-fictie van de week."),
    ("bestseller60-jeugd", "Bestseller 60 — Jeugd", "jeugd",
     "De best verkochte kinder- en jeugdboeken van de week."),
    ("bestseller60-spannend", "Bestseller 60 — Spannend", "spannend",
     "De best verkochte spannende boeken van de week."),
    ("bestseller60-koken", "Bestseller 60 — Koken", "koken",
     "De best verkochte kookboeken van de week."),
]


def parse(page: str) -> list[dict]:
    items = []
    for block in re.split(r"card__position", page)[1:]:
        pos = re.search(r">\s*(\d+)\s*<", block)
        title = re.search(r'card__title[^>]*\btitle="([^"]+)"', block) \
            or re.search(r"card__title[^>]*>\s*([^<]+?)\s*<", block)
        author = re.search(r"card__author.*?<a[^>]*>\s*([^<]+?)\s*</a>", block, re.S)
        isbn = re.search(r"ISBN\s*(97[89]\d{10})", block)
        cover = re.search(r'src="(https://[^"]+/covers/[^"]+)"', block)
        if not title:
            continue
        items.append({
            "position": int(pos.group(1)) if pos else None,
            "title": html.unescape(title.group(1)).strip(),
            "author": html.unescape(author.group(1)).strip() if author else None,
            "isbn": isbn.group(1) if isbn else None,
            "cover_url": cover.group(1) if cover else None,
        })
    seen, out = set(), []
    for it in sorted(items, key=lambda x: x["position"] or 999):
        if it["position"] in seen:
            continue
        seen.add(it["position"])
        out.append(it)
    return out


def fetch_all() -> list[dict]:
    out = []
    with httpx.Client(headers={"User-Agent": _UA}, timeout=30,
                      follow_redirects=True) as client:
        for slug, name, path, desc in SUBLISTS:
            url = f"{URL}/{path}" if path else URL
            try:
                r = client.get(url)
                r.raise_for_status()
                items = parse(r.text)
            except (httpx.HTTPError, httpx.HTTPStatusError) as e:
                logger.warning(f"{slug}: fetch failed ({e})")
                continue
            if items:
                out.append({"slug": slug, "name": name, "url": url,
                            "description": desc, "items": items})
    return out
