"""Text normalisation helpers: author splitting, publisher canonicalisation,
and fuzzy match keys used for curated-list matching."""

from __future__ import annotations

import re
import unicodedata

_AUTHOR_SPLIT = re.compile(r"\s*[|;]\s*")


def split_authors(value: str | None) -> list[str]:
    """Split a multi-author string on '|' / ';' into individual names.

    Commas are intentionally NOT split on — in this catalog they appear inside
    names ("Buren, van"), not as separators.
    """
    if not value:
        return []
    parts = [p.strip(" \t,") for p in _AUTHOR_SPLIT.split(value)]
    return list(dict.fromkeys(p for p in parts if p))


def publisher_key(value: str | None) -> str:
    """Loose grouping key so 'De Correspondent, Amsterdam' and
    'de Correspondent, [Amsterdam]' collapse together."""
    if not value:
        return ""
    s = value.lower().replace("[", "").replace("]", "")
    s = re.sub(r"\s+", " ", s).strip(" .,")
    return s


# Curated publisher aliases for cases plain key-folding can't merge (different
# words / imprints). Each entry: canonical name + folded substrings that map to
# it. Extend this list as you spot more. First match wins.
PUBLISHER_ALIASES: list[tuple[str, list[str]]] = [
    ("De Correspondent, Amsterdam", ["correspondent"]),
    ("Das Mag, Amsterdam", ["das mag"]),
    # "Bert Bakker" is a Prometheus sub-imprint and stays distinct from the main
    # "Prometheus, Amsterdam"; merge only the Bert Bakker spelling variants.
    ("Prometheus Bert Bakker, Amsterdam", ["bert bakker"]),
]


def canonical_publisher(value: str | None, fallback: str | None = None) -> str | None:
    """Map a publisher to a curated canonical name, else return ``fallback``
    (typically the most-common spelling of its group) or the value itself."""
    if not value:
        return value
    f = fold(value)
    for canon, patterns in PUBLISHER_ALIASES:
        if any(p in f for p in patterns):
            return canon
    return fallback if fallback is not None else value


def fold(value: str | None) -> str:
    """Lowercase, strip diacritics and non-alphanumerics — for matching."""
    if not value:
        return ""
    s = unicodedata.normalize("NFKD", value)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def match_key(title: str | None, author: str | None) -> str:
    """Catalog-match key from title + first author surname token."""
    a = fold(author).split()
    return f"{fold(title)}|{a[-1] if a else ''}"


# Conservative series patterns — only explicit markers, to avoid false positives
# (e.g. "1984" or "Catch-22" must NOT be treated as series).
_SERIES_PATTERNS = [
    re.compile(r"^(?P<s>.+?)\s*[:\-]\s*deel\s*(?P<n>\d+)\b", re.I),
    re.compile(r"\(\s*(?P<s>[^()]+?)\s*[,;]?\s*deel\s*(?P<n>\d+)\s*\)", re.I),
    re.compile(r"\bdeel\s*(?P<n>\d+)\s+van\s+(?:de\s+)?(?:reeks|serie)\s+(?P<s>[^.()]+)", re.I),
]


def detect_series(title: str | None) -> tuple[str | None, int | None]:
    """Extract (series name, number) from a title when it has an explicit
    'deel N' marker; otherwise (None, None)."""
    if not title:
        return None, None
    for p in _SERIES_PATTERNS:
        m = p.search(title)
        if m:
            return m.group("s").strip(" :-,;"), int(m.group("n"))
    return None, None
