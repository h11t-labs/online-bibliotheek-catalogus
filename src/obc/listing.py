"""Parse a ``zoekresultaten.catalogus[.N].html`` results page.

Each result ``<li>`` in the ``ul.rich-list`` already carries rich metadata, so
the catalog can be built from listing pages alone (~1 request per 20 books)
without fetching every detail page. Detail-page enrichment (ISBN, full subject
list, narrator, audience) is optional and additive.

:func:`parse_listing` returns ``(records, max_page)`` where ``max_page`` is the
highest page number in the pager — used by the scraper as a partition-size
signal (the site caps the pager at 50 pages = 1000 results).
"""

from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup

from .htmlutil import node_text

_PPN_RE = re.compile(r"/catalogus/([0-9xX]+)/([^/?#\"]+)")
_PAGER_RE = re.compile(r"zoekresultaten\.catalogus\.(\d+)\.html")
_YEAR_RE = re.compile(r"\b(1\d{3}|20\d{2})\b")


def max_page(html: str) -> int:
    nums = [int(n) for n in _PAGER_RE.findall(html)]
    return max(nums) if nums else 1


def parse_listing(html: str) -> tuple[list[dict[str, Any]], int]:
    soup = BeautifulSoup(html, "lxml")
    records: list[dict[str, Any]] = []

    for li in soup.select("ul.rich-list > li"):
        link = li.find("a", class_="image-link") or li.find("a", href=True)
        href = link.get("href") if link else None
        if not href:
            continue
        m = _PPN_RE.search(href)
        if not m:
            continue
        ppn, slug = m.group(1), m.group(2)

        rec: dict[str, Any] = {
            "ppn": ppn,
            "slug": slug,
            "url": f"https://www.onlinebibliotheek.nl/catalogus/{ppn}/{slug}",
            "title": node_text(li.select_one("span.title")) or None,
            "author": (node_text(li.select_one("span.creator")).rstrip(" |").strip()
                       or None),
            "summary": node_text(li.select_one("p.maintext")) or None,
            "source": "listing",
        }

        img = li.select_one("img.cover, img.viz")
        if img and img.get("src"):
            rec["cover_url"] = img["src"]

        # format from the "medium" line classes / text
        medium = li.select_one("p.additional.medium, p[class*='medium']")
        cls = " ".join(medium.get("class", [])) if medium else ""
        mtext = node_text(medium).lower() if medium else ""
        if "digitalaudiobook" in cls or "luisterboek" in mtext:
            rec["format"] = "audiobook"
        elif "ebook" in cls or "e-book" in mtext:
            rec["format"] = "ebook"

        _parse_additional(li, rec)
        records.append(rec)

    return records, max_page(html)


def _parse_additional(li, rec: dict[str, Any]) -> None:
    """Parse the ``p.additional`` lines: category + 'lang | extent | pub | year'."""
    for p in li.select("p.additional"):
        txt = node_text(p)
        if not txt:
            continue
        low = txt.lower()
        if low in ("fictie", "non-fictie", "nonfictie"):
            rec["category"] = "nonfictie" if "non" in low else "fictie"
            continue
        if "|" not in txt:
            continue
        parts = [s.strip() for s in txt.split("|")]
        # parts[0] is the language, unless this item has no language and the line
        # starts with the category instead (e.g. "Fictie | 320 pagina's | …").
        if parts[0].lower() in ("fictie", "non-fictie", "nonfictie"):
            rec.setdefault("category", "nonfictie" if "non" in parts[0].lower() else "fictie")
        elif len(parts[0]) <= 24:
            rec.setdefault("language", parts[0] or None)
        for part in parts[1:]:
            _parse_extent(part, rec)
        # publisher = second-to-last, year = last (when 4 parts present)
        if len(parts) >= 3:
            ym = _YEAR_RE.search(parts[-1])
            if ym:
                rec["year"] = int(ym.group())
                pub = parts[-2]
            else:
                pub = parts[-1]
            if pub and "pagina" not in pub.lower() and "uur" not in pub.lower():
                rec.setdefault("publisher", pub)


def _parse_extent(part: str, rec: dict[str, Any]) -> None:
    """e.g. "47 pagina's (ePub3, 9,8 MB)" or "6 uur 54 minuten (596 MB)"."""
    low = part.lower()
    inner = re.search(r"\(([^)]*)\)", part)
    size_m = re.search(r"\d+(?:[.,]\d+)?\s*[KMGT]B", part, re.I)
    if "pagina" in low:
        pm = re.search(r"\d+", part)
        if pm:
            rec["pages"] = int(pm.group())
        if inner:
            feat = re.search(r"ePub\w*|pdf|mp3|daisy|html", inner.group(1), re.I)
            if feat:
                rec.setdefault("features", feat.group(0))
        if size_m:
            rec.setdefault("size", size_m.group(0).strip())
    elif "uur" in low or "minuten" in low or re.match(r"\d+:\d+", part):
        dur = re.sub(r"\s*\(.*\)", "", part).strip()
        rec.setdefault("duration", dur)
        if size_m:
            rec.setdefault("size", size_m.group(0).strip())
