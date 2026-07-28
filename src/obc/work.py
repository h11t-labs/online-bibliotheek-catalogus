"""Work identity: group edition records (one per PPN) into works.

Evidence, strongest first:
1. the library's own "Ook beschikbaar als" cross-links (related_ppns),
2. a conservative normalised key (title stripped of format noise + first
   author's surname + language),
3. curated overrides (data/raw/work_overrides.json), applied last.
The representative edition — whose PPN doubles as the work_id — is the e-book
if the group has one, else the lowest PPN (string order, matching how the old
set_primary_editions chose).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .textnorm import fold, split_authors, surname_key
from .util import read_json


@dataclass(frozen=True)
class EditionMeta:
    """The slice of one record that work identity is decided on."""
    title: str | None
    author: str | None
    language: str | None
    format: str | None
    related_ppns: tuple[str, ...] = ()


# Format markers the library appends to a title ("De adoptie - luisterboek"),
# which split a work from its twin because the two titles then differ. Longest
# first, so a compound marker wins over its own suffix ("digitaal luisterboek"
# must not strip to "digitaal").
_FORMAT_MARKERS = (
    r"luisterboek \(digitaal\)",
    r"digitaal luisterboek",
    r"gesproken versie",
    r"luisterboek",
    r"audioboek",
    r"digitaal",
    r"e-book",
    r"epub3",
    r"epub2",
    r"ebook",
    r"epub",
    r"mp3",
)
_MARKERS = "|".join(_FORMAT_MARKERS)
# One trailing marker, optionally bracketed or introduced by a dash/colon. Only
# the tail is touched: a marker inside a real title ("Het luisterboek van opa")
# is not a format annotation.
_FORMAT_NOISE_RE = re.compile(
    rf"\s*(?:[-–—:;]\s*)?(?:\(\s*(?:{_MARKERS})\s*\)"
    rf"|\[\s*(?:{_MARKERS})\s*\]"
    rf"|\b(?:{_MARKERS}))\s*$",
    re.IGNORECASE)


def strip_format_noise(title: str | None) -> str:
    """Drop a trailing format annotation from a title.

    ``"De adoptie - luisterboek"`` -> ``"De adoptie"``. A title that *is* only a
    marker ("Luisterboek") is returned unchanged — stripping it would leave
    nothing to key on and merge every such record into one work.
    """
    if not title:
        return ""
    stripped = _FORMAT_NOISE_RE.sub("", title, count=1).strip()
    return stripped or title


def work_key(title: str | None, author: str | None,
             language: str | None) -> tuple[str, str]:
    """(title_fold, author_surname) — language is handled by group_editions, not here."""
    authors = split_authors(author)
    return (fold(strip_format_noise(title)),
            surname_key(authors[0]) if authors else "")


class _Union:
    """Union-find over PPNs (path-compressed, union by insertion order)."""

    def __init__(self) -> None:
        self._parent: dict[str, str] = {}

    def find(self, ppn: str) -> str:
        parent = self._parent
        root = parent.setdefault(ppn, ppn)
        while root != parent[root]:
            root = parent[root]
        while parent[ppn] != root:  # path compression
            parent[ppn], ppn = root, parent[ppn]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # lowest PPN wins the root, so grouping is independent of input order
            lo, hi = (ra, rb) if ra < rb else (rb, ra)
            self._parent[hi] = lo


def _pairs(overrides: dict | None, kind: str) -> list[tuple[str, str]]:
    """The ``merge`` / ``split`` pairs of an overrides file, junk entries dropped."""
    out = []
    for pair in (overrides or {}).get(kind) or ():
        if isinstance(pair, (list, tuple)) and len(pair) == 2:
            out.append((str(pair[0]), str(pair[1])))
    return out


def group_editions(meta: dict[str, EditionMeta],
                   overrides: dict | None = None) -> dict[str, str]:
    """{ppn: work_id} for every ppn in meta."""
    uf = _Union()
    for ppn in meta:
        uf.find(ppn)   # every ppn is at least its own work

    # (a) the conservative key. Same title+surname is not enough on its own: two
    # same-titled books in different languages are different books to anyone
    # using the language filter, so a bucket is partitioned by language first.
    buckets: dict[tuple[str, str], list[str]] = {}
    for ppn, m in meta.items():
        key = work_key(m.title, m.author, m.language)
        if not key[0]:
            continue   # no title to fold -> never unified by key
        buckets.setdefault(key, []).append(ppn)
    for members in buckets.values():
        by_lang: dict[str, list[str]] = {}
        for ppn in members:
            by_lang.setdefault(fold(meta[ppn].language), []).append(ppn)
        unknown = by_lang.pop("", [])
        for same_lang in by_lang.values():
            for other in same_lang[1:]:
                uf.union(same_lang[0], other)
        # A missing language (the field is junk-filtered, so NULL happens) matches
        # anything *within* an otherwise equal key — but only when the key leaves
        # no ambiguity about which language that would be.
        if len(by_lang) == 1:
            only = next(iter(by_lang.values()))[0]
            for ppn in unknown:
                uf.union(only, ppn)

    # (b) the library's own cross-links outrank the key: they may bridge two
    # different titles or languages. Only PPNs the catalog actually holds.
    for ppn, m in meta.items():
        for related in m.related_ppns:
            if related in meta:
                uf.union(ppn, related)

    # (c) curated exceptions, applied last
    for a, b in _pairs(overrides, "merge"):
        if a in meta and b in meta:
            uf.union(a, b)

    groups: dict[str, list[str]] = {}
    for ppn in meta:
        groups.setdefault(uf.find(ppn), []).append(ppn)
    # A split forces the *second* ppn of the pair out into its own work; the
    # first keeps the group. Applied to the materialised groups, so a later split
    # sees what an earlier one already detached.
    for a, b in _pairs(overrides, "split"):
        if a not in meta or b not in meta or uf.find(a) != uf.find(b):
            continue
        members = groups.get(uf.find(b))
        if members and b in members and len(members) > 1:
            members.remove(b)
            groups[b] = [b]

    out: dict[str, str] = {}
    for members in groups.values():
        rep = min(members, key=lambda p: (meta[p].format != "ebook", p))
        for ppn in members:
            out[ppn] = rep
    return out


def stamp_work_ids(records: list[dict[str, Any]],
                   overrides: dict | None = None) -> list[dict[str, Any]]:
    """Convenience for bulk callers/tests: build meta from the records, group,
    set rec['work_id'] on each (only where missing), return the same list."""
    meta = {}
    for r in records:
        ppn = r.get("ppn")
        if not ppn:
            continue
        related = r.get("related_ppns") or ()
        meta[ppn] = EditionMeta(
            title=r.get("title"), author=r.get("author"),
            language=r.get("language"), format=r.get("format"),
            related_ppns=tuple(related))
    work_of = group_editions(meta, overrides)
    for r in records:
        ppn = r.get("ppn")
        if ppn and not r.get("work_id"):
            r["work_id"] = work_of.get(ppn, ppn)
    return records


def load_overrides(path: Path) -> dict:
    """{'merge': [[ppn, ppn], ...], 'split': [[ppn, ppn], ...]} or {} if absent."""
    data = read_json(Path(path), default={})
    return data if isinstance(data, dict) else {}
