"""Parse an onlinebibliotheek.nl ``/catalogus/{ppn}/{slug}`` detail page into a record.

The detail pages are server-rendered AEM HTML with a stable structure:

* ``<link rel="canonical">``           -> PPN + slug + canonical URL
* ``<span class="creator">``           -> author(s)
* ``<span class="title">``             -> title
* ``<title>`` (``... | <format> | ``)  -> format (e-book / luisterboek)
* ``<meta name="description">``        -> summary
* ``<img class="representative">``      -> cover image URL (leibniz.zbkb.nl)
* a ``<dl>`` of ``<dt>/<dd>`` pairs    -> language, publisher, year, ISBN,
  pages, size, narrator, duration, subjects (genres), features, notes.

Output is a plain ``dict`` with normalised keys, suitable for ``normalize.py``.
"""

from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup

from .htmlutil import node_text

# Dutch <dt> label -> normalised field name. The "Onderwerpen" label varies
# ("Onderwerpen: Volwassenen" / "Onderwerpen: Jeugd") so it is matched by prefix.
_LABEL_MAP = {
    "taal": "language",
    "uitgever": "publisher",
    "verschenen": "year",
    "isbn": "isbn",
    "aantal pagina's": "pages",
    "omvang": "size",
    "kenmerken": "features",
    "verteller": "narrator",
    "speelduur": "duration",
    "aantekening": "note",
    "ook beschikbaar als": "also_available_as",
    "leeftijd": "age",        # children's books, e.g. "9-12 jaar" / "AA" / "AVI…"
    "inhoud": "category",     # "Fictie" / "Non-fictie"
}


def _parse_serie(value: str) -> tuple[str | None, int | None]:
    """'De spannende avonturen met Dolfi (7)' -> ('De spannende avonturen met Dolfi', 7)."""
    value = (value or "").strip()
    if not value:
        return None, None
    m = re.search(r"\(\s*(\d+)\s*\)\s*$", value)
    no = int(m.group(1)) if m else None
    name = re.sub(r"\s*\(\s*\d+\s*\)\s*$", "", value).strip(" .,;")
    return (name or None), no

# onlinebibliotheek.nl: /catalogus/{ppn}/{slug}
_CANONICAL_RE = re.compile(r"/catalogus/([0-9xX]+)/([^/?#]+)")
# bibliotheek.nl canonical variant: /catalogus/titel.{ppn}.html/{slug}/
_CANONICAL_TITEL_RE = re.compile(r"/catalogus/titel\.([0-9xX]+)\.html/?([^/?#]*)")
# cover URL carries the PPN: .../id/PPN%3A{ppn} or PPN:{ppn}
_COVER_PPN_RE = re.compile(r"PPN(?:%3A|:)([0-9xX]+)", re.IGNORECASE)


def parse_detail(html: str, ppn: str | None = None) -> dict[str, Any]:
    """Parse detail-page ``html`` into a normalised record dict.

    ``ppn`` may be supplied by the caller (from the URL); otherwise it is read
    from the canonical link. Returns ``{}`` if the page is not a catalog record.
    """
    soup = BeautifulSoup(html, "lxml")

    # --- cover (also a PPN source) -----------------------------------------
    img = soup.select_one("img.representative")
    cover_url = img.get("src") if img and img.get("src") else None

    # --- ppn / slug --------------------------------------------------------
    slug = None
    canonical = soup.find("link", rel="canonical")
    canon_href = canonical.get("href") if canonical else None
    if canon_href:
        m = _CANONICAL_RE.search(canon_href) or _CANONICAL_TITEL_RE.search(canon_href)
        if m:
            ppn = ppn or m.group(1)
            slug = m.group(2) or None
    if not ppn and cover_url:
        m = _COVER_PPN_RE.search(cover_url)
        if m:
            ppn = m.group(1)
    if not ppn:
        return {}

    rec: dict[str, Any] = {
        "ppn": ppn,
        "slug": slug,
        "url": f"https://www.onlinebibliotheek.nl/catalogus/{ppn}/{slug or ''}".rstrip("/"),
    }
    rec["cover_url"] = cover_url or _cover_fallback(ppn)

    # --- title / author ----------------------------------------------------
    rec["title"] = node_text(soup.select_one("span.title")) or None
    creators = [node_text(c) for c in soup.select("span.creator")]
    creators = [c for c in creators if c]
    rec["author"] = " ; ".join(dict.fromkeys(creators)) or None

    # --- format from <title>: "Title - Author | e-book | de online Bib..." --
    title_tag = node_text(soup.find("title"))
    rec["format"] = _format_from_title(title_tag)

    # Fallback title/author from <title> if the spans were empty.
    if not rec["title"] and title_tag:
        head = title_tag.split("|", 1)[0].strip()
        if " - " in head:
            t, a = head.rsplit(" - ", 1)
            rec["title"] = rec["title"] or t.strip()
            rec["author"] = rec["author"] or a.strip()
        else:
            rec["title"] = head

    # --- summary -----------------------------------------------------------
    desc = soup.find("meta", attrs={"name": "description"})
    rec["summary"] = (desc.get("content").strip() if desc and desc.get("content") else None)

    # --- dt/dd metadata ----------------------------------------------------
    subjects: list[str] = []
    keywords: list[str] = []
    audience = None
    for dt in soup.find_all("dt"):
        dd = dt.find_next_sibling("dd")
        if dd is None:
            continue
        label = node_text(dt).rstrip(":").strip()
        value = node_text(dd)
        low = label.lower()

        if low.startswith("onderwerpen"):
            parts = [s.strip() for s in re.split(r"[|·•]", value) if s.strip()]
            if ":" in label:  # "Onderwerpen: Jeugd" -> the curated genre facets
                audience = label.split(":", 1)[1].strip() or None
                subjects.extend(parts)
            else:             # plain "Onderwerpen" -> free keyword tags
                keywords.extend(parts)
            continue
        if low == "serie":
            s, no = _parse_serie(value)
            if s:
                rec["series"], rec["series_no"] = s, no
            continue

        field = _LABEL_MAP.get(low)
        if not field:
            continue
        if field == "year":
            ym = re.search(r"\d{4}", value)
            rec["year"] = int(ym.group()) if ym else None
        elif field == "pages":
            pm = re.search(r"\d+", value.replace(".", ""))
            rec["pages"] = int(pm.group()) if pm else None
        elif field == "duration":
            # prefer the "H:MM:SS" form; keep first seen otherwise
            if rec.get("duration") and ":" not in value:
                continue
            rec["duration"] = value
        elif field == "category":
            low_v = value.lower()
            rec["category"] = ("nonfictie" if "non" in low_v
                               else "fictie" if "fictie" in low_v else value)
        else:
            rec.setdefault(field, value)

    rec["audience"] = audience
    rec["subjects"] = list(dict.fromkeys(subjects))   # dedupe, keep order
    rec["keywords"] = list(dict.fromkeys(keywords))
    return rec


def _format_from_title(title_tag: str) -> str | None:
    parts = [p.strip().lower() for p in title_tag.split("|")]
    for p in parts:
        if "luisterboek" in p:
            return "audiobook"
        if "e-book" in p or "ebook" in p:
            return "ebook"
    return None


def _cover_fallback(ppn: str) -> str:
    return (
        f"https://leibniz.zbkb.nl/assets/id/PPN:{ppn}"
        "?aid=kb-online-bibliotheek&sid=21&width=320"
    )
