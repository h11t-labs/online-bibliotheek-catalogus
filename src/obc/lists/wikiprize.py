"""Dutch literary prizes via Wikipedia (winners & nominees).

Wikipedia is stable and scrapeable server-side; we read the article wikitext and
pull entries shaped like ``[[Author]] … ''Title''`` from list/table lines. Add a
prize by appending to PRIZES.
"""

from __future__ import annotations

import re

import httpx

from ..log import logger

API = "https://nl.wikipedia.org/w/api.php"
_UA = "online-bibliotheek-catalogus/0.1 (personal project)"

# (slug, display name, Wikipedia page title)
PRIZES = [
    ("libris-literatuurprijs", "Libris Literatuurprijs", "Libris Literatuur Prijs"),
    ("boekenbon-literatuurprijs", "Boekenbon Literatuurprijs", "Boekenbon Literatuurprijs"),
    ("ns-publieksprijs", "NS Publieksprijs", "NS Publieksprijs"),
    ("gouden-boekenuil", "Gouden Boekenuil", "Gouden Boekenuil"),
]

_LINK = re.compile(r"\[\[([^\]|]+?)(?:\|[^\]]+)?\]\]")
_ITALIC = re.compile(r"''+([^']{2,140}?)''+")
_YEAR = re.compile(r"^\d{3,4}$")
_WIKILINK = re.compile(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]")


def _clean(text: str) -> str:
    text = _WIKILINK.sub(r"\1", text)            # [[A|B]] -> B, [[A]] -> A
    text = re.sub(r"\([^)]*\)$", "", text)        # drop trailing "(roman)" disambig
    return text.strip(" '\"")


_YEAR4 = re.compile(r"\b(19\d{2}|20\d{2})\b")


def parse_wikitext(wt: str) -> list[dict]:
    items, seen = [], set()
    cur_year = None
    for line in wt.splitlines():
        line = line.strip()
        # a section/line that names a year (e.g. "=== 2014 ===" or "* 2014:")
        ym = _YEAR4.search(line)
        if ym and (line.startswith("=") or line.startswith("*") or line.startswith("|")):
            cur_year = int(ym.group(1))
        if not (line.startswith("*") or line.startswith("|")):
            continue
        tm = _ITALIC.search(line)
        if not tm:
            continue
        title = _clean(tm.group(1))
        title_link = _WIKILINK.sub(r"\1", tm.group(1)).strip()
        authors = [a.strip() for a in _LINK.findall(line)
                   if not _YEAR.match(a.strip()) and _clean(a) != title and a.strip() != title_link]
        author = authors[0] if authors else None
        if not title or not author or title.lower() == author.lower():
            continue
        key = (title.lower(), author.lower())
        if key in seen:
            continue
        seen.add(key)
        line_year = int(ym.group(1)) if ym else cur_year
        items.append({"title": title, "author": author, "isbn": None,
                      "cover_url": None, "year": line_year})
    # newest prizes first; position is just a stable ordinal
    items.sort(key=lambda it: (-(it["year"] or 0)))
    for i, it in enumerate(items, 1):
        it["position"] = i
    return items


def _wikitext(page: str) -> str:
    r = httpx.get(API, params={"action": "parse", "page": page, "format": "json",
                               "prop": "wikitext", "redirects": 1},
                  headers={"User-Agent": _UA}, timeout=20)
    r.raise_for_status()
    return r.json().get("parse", {}).get("wikitext", {}).get("*", "")


def fetch_all() -> list[dict]:
    out = []
    for slug, name, page in PRIZES:
        try:
            items = parse_wikitext(_wikitext(page))
        except (httpx.HTTPError, KeyError, ValueError) as e:
            logger.warning(f"{slug}: Wikipedia fetch failed ({e})")
            continue
        if len(items) >= 3:
            out.append({
                "slug": slug, "name": name,
                "url": f"https://nl.wikipedia.org/wiki/{page.replace(' ', '_')}",
                "description": f"Winnaars en genomineerden van de {name} (bron: Wikipedia).",
                "items": items,
            })
    return out
