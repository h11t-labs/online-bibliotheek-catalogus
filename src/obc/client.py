"""Polite HTTP client for onlinebibliotheek.nl.

Provides a rate-limited, retrying fetcher plus :func:`fetch_detail`, which
downloads and parses a ``/catalogus/{ppn}/{slug}`` page into a record dict.

Etiquette (personal-use scrape): a descriptive User-Agent, ~1 request/second by
default, exponential backoff on 429/5xx, and on-disk HTML caching so re-runs do
not re-hit the server.

:func:`get_listing_html` fetches the faceted browse/result pages used by
:mod:`obc.scrape` for full-catalog enumeration.
"""

from __future__ import annotations

import threading
import time
from urllib.parse import urlencode

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .config import HTML_CACHE  # rebindable module-level path (see obc.config)
from .detail import parse_detail

# Catalog browse: zoekresultaten.catalogus[.N].html?<facets>
BROWSE_BASE = "https://www.onlinebibliotheek.nl/zoekresultaten.catalogus"

USER_AGENT = (
    "online-bibliotheek-catalogus/0.1 (personal catalog project; "
    "contact: see repository)"
)
BASE = "https://www.onlinebibliotheek.nl"


class RateLimiter:
    """Simple thread-safe minimum-interval limiter."""

    def __init__(self, per_second: float = 1.0):
        self._min_interval = 1.0 / per_second if per_second > 0 else 0.0
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            sleep = self._next - now
            if sleep > 0:
                time.sleep(sleep)
            self._next = max(now, self._next) + self._min_interval


class Client:
    def __init__(self, per_second: float = 1.0, timeout: float = 30.0,
                 cache: bool = True):
        self.limiter = RateLimiter(per_second)
        self.cache = cache
        self._http = httpx.Client(
            headers={"User-Agent": USER_AGENT,
                     "Accept": "text/html,application/json,*/*",
                     "Accept-Language": "nl,en;q=0.8"},
            timeout=timeout, follow_redirects=True,
        )
        if cache:
            HTML_CACHE.mkdir(parents=True, exist_ok=True)

    def close(self) -> None:
        self._http.close()

    def __enter__(self): return self
    def __exit__(self, *a): self.close()

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
        wait=wait_exponential(multiplier=2, min=2, max=60),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    def _get(self, url: str) -> httpx.Response:
        self.limiter.wait()
        r = self._http.get(url)
        if r.status_code == 429 or r.status_code >= 500:
            r.raise_for_status()
        return r

    def get_detail_html(self, ppn: str, slug: str) -> str | None:
        cache_path = HTML_CACHE / f"{ppn}.html"
        if self.cache and cache_path.exists():
            return cache_path.read_text(encoding="utf-8")
        url = f"{BASE}/catalogus/{ppn}/{slug}.html"
        r = self._get(url)
        if r.status_code != 200:
            return None
        html = r.text
        if self.cache:
            cache_path.write_text(html, encoding="utf-8")
        return html

    def get_listing_html(self, params: dict[str, str], page: int = 1) -> str:
        """Fetch a catalog browse page. Page 1 has no selector; page N uses
        ``zoekresultaten.catalogus.N.html``."""
        sel = "" if page <= 1 else f".{page}"
        url = f"{BROWSE_BASE}{sel}.html?{urlencode(params)}"
        return self._get(url).text

    def fetch_detail(self, ppn: str, slug: str) -> dict | None:
        html = self.get_detail_html(ppn, slug)
        if not html:
            return None
        rec = parse_detail(html, ppn=ppn)
        return rec or None


if __name__ == "__main__":
    # quick probe: fetch and print one record
    import json
    import sys

    ppn = sys.argv[1] if len(sys.argv) > 1 else "416728413"
    slug = sys.argv[2] if len(sys.argv) > 2 else "moby-dick-herman-melville"
    with Client(cache=False) as c:
        print(json.dumps(c.fetch_detail(ppn, slug), ensure_ascii=False, indent=2))
