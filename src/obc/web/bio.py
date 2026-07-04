"""Best-effort short author biographies from the Dutch Wikipedia.

A separate module so the web routes don't carry external-HTTP concerns. Results
(including misses) are cached, and a same-named non-author page is filtered out
by requiring an author-ish word in the summary.
"""

from __future__ import annotations

from functools import lru_cache
from urllib.parse import quote

import httpx

from ..config import USER_AGENT

# The bio is a nice-to-have shown on author pages; it must never hold a page
# hostage waiting on Wikipedia. This lookup is synchronous in the request path
# and the cache is per-process (cold after every restart), so keep the timeout
# short — a slow Wikipedia should degrade to "no bio", not a slow page.
_TIMEOUT = 2.0
_http = httpx.Client(timeout=_TIMEOUT, follow_redirects=True,
                     headers={"User-Agent": USER_AGENT})

_AUTHOR_WORDS = ("schrijf", "schrijver", "schrijfster", "auteur", "dichter",
                 "romancier", "writer", "novelist", "poet", "journalist", "columnist",
                 "illustrator", "vertaler", "kinderboeken")


@lru_cache(maxsize=4096)
def author_bio(name: str) -> dict | None:
    """Return ``{extract, thumb, url}`` for ``name`` or ``None`` when there is no
    clear author page (cached, including misses)."""
    try:
        r = _http.get(
            "https://nl.wikipedia.org/api/rest_v1/page/summary/" + quote(name, safe=""))
        if r.status_code != 200:
            return None
        d = r.json()
        if d.get("type") == "disambiguation" or not d.get("extract"):
            return None
        blob = f"{d.get('description', '')} {d['extract']}".lower()
        if not any(w in blob for w in _AUTHOR_WORDS):
            return None  # likely a same-named non-author; skip
        return {"extract": d["extract"],
                "thumb": (d.get("thumbnail") or {}).get("source"),
                "url": (d.get("content_urls", {}).get("desktop", {}) or {}).get("page")}
    except (httpx.HTTPError, ValueError):
        # httpx.TimeoutException is a subclass of httpx.HTTPError, so a timed-out
        # Wikipedia (the whole point of the short _TIMEOUT) is caught here too.
        return None
