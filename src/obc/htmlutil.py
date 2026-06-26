"""Tiny HTML-parsing helpers shared by the listing and detail parsers."""

from __future__ import annotations

import re


def node_text(node) -> str:
    """Collapsed, stripped text content of a BeautifulSoup node ("" if None)."""
    if node is None:
        return ""
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
