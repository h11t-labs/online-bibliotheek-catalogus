"""Memoisation for indexes that are derived from the catalog as a whole.

A handful of things the site serves are identical for every visitor and only
change when ``normalize`` swaps in a rebuilt catalog: the facet values, the A-Z
author index, the series slug map, the genre tree. Each walks a large part of the
database, so rebuilding one per request is out of the question — and each is
small enough to keep in memory once built.

:class:`VersionedCache` is the single place that lives. It keys on a
caller-supplied version (here the catalog file's mtime), so a rebuild invalidates
everything at once and no caller has to remember to clear anything.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Hashable
from typing import Any

# A version no caller can produce, so the initial (and cleared) state matches
# nothing — including a version of ``None``, which is what a missing DB yields.
_UNSET = object()


class VersionedCache:
    """Memoise values that hold for exactly one version of the catalog.

    Two properties matter here, both learned the hard way:

    * **One builder at a time.** These indexes walk the whole catalog; the genre
      one used to peak at 606 MB. Unlocked, eight concurrent cold requests each
      built their own copy and took the 512 MB machine down with them. One thread
      builds while the rest wait for its result.
    * **Version and data swap together.** They live in a single tuple that is
      replaced, never mutated, so a reader can never pair a fresh version with
      data left over from the previous catalog — which is exactly what reading
      the two out of separate slots allowed.

    The lock is a plain one, so a ``build()`` must not call back into the cache;
    resolve what it needs first and close over it.
    """

    def __init__(self, version: Callable[[], Any]) -> None:
        self._version = version
        self._lock = threading.Lock()
        self._snapshot: tuple[Any, dict[Hashable, Any]] = (_UNSET, {})

    def get(self, key: Hashable, build: Callable[[], Any]) -> Any:
        """The cached value for ``key``, calling ``build()`` on a miss."""
        version = self._version()
        cached_version, entries = self._snapshot
        if cached_version == version and key in entries:
            return entries[key]
        with self._lock:
            cached_version, entries = self._snapshot
            if cached_version != version:
                entries = {}          # a rebuild landed: everything is stale
            elif key in entries:
                return entries[key]   # another thread built it while we waited
            value = build()
            self._snapshot = (version, {**entries, key: value})
            return value

    def clear(self) -> None:
        """Forget everything. Used by the tests, which point the app at a
        different fixture catalog per case."""
        with self._lock:
            self._snapshot = (_UNSET, {})
