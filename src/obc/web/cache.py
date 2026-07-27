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
        return self._get(key, build, self._version())

    def pinned(self) -> PinnedCache:
        """A view fixed to the version as of now — see :class:`PinnedCache`."""
        return PinnedCache(self, self._version())

    def _get(self, key: Hashable, build: Callable[[], Any], version: Any) -> Any:
        cached_version, entries = self._snapshot
        if cached_version == version and key in entries:
            return entries[key]
        with self._lock:
            cached_version, entries = self._snapshot
            if cached_version == version and key in entries:
                return entries[key]   # another thread built it while we waited
            value = build()
            # Publish only if the catalog is still the one we set out to describe.
            # A build can outlast a rebuild — it reads a connection opened before
            # the swap — and labelling its result with the version that has since
            # landed would serve the previous catalog until the *next* rebuild.
            # Skipping the write costs one rebuild; getting it wrong costs a day.
            # It also keeps a slow caller from replacing a newer snapshot with its
            # own older one, which would throw away every entry built since.
            if self._version() == version:
                base = entries if cached_version == version else {}
                self._snapshot = (version, {**base, key: value})
            return value

    def clear(self) -> None:
        """Forget everything. Used by the tests, which point the app at a
        different fixture catalog per case."""
        with self._lock:
            self._snapshot = (_UNSET, {})


class PinnedCache:
    """Several lookups that have to describe the *same* catalog.

    A value built out of another cached value must not be stored under a version
    its input never saw. Reading the version once per lookup allowed exactly that:
    fetch the merged author list, have a rebuild land, then file the buckets
    derived from it under the new version — where they would sit, wrong, until the
    rebuild after that. Pinning makes both lookups name one catalog, and the
    publish guard in :meth:`VersionedCache._get` drops the write if that catalog
    is gone by the time the build finishes.
    """

    def __init__(self, cache: VersionedCache, version: Any) -> None:
        self._cache = cache
        self._pinned_version = version

    def get(self, key: Hashable, build: Callable[[], Any]) -> Any:
        """As :meth:`VersionedCache.get`, but against the pinned version."""
        return self._cache._get(key, build, self._pinned_version)
