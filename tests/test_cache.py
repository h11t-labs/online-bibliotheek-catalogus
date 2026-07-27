"""The memoisation primitive (obc.web.cache), and the two races it has to survive.

Both were found by review, not by the suite: they need a catalog rebuild to land
inside a window of a few instructions, which no end-to-end test will hit by
chance. Driving the version by hand makes them deterministic.
"""

from obc.web import cache


def test_a_value_is_built_once_per_version():
    version, builds = [1], []

    def build():
        builds.append(version[0])
        return f"v{version[0]}"

    c = cache.VersionedCache(lambda: version[0])
    assert [c.get("k", build), c.get("k", build)] == ["v1", "v1"]
    version[0] = 2
    assert c.get("k", build) == "v2"
    assert builds == [1, 2]


def test_a_late_build_cannot_roll_back_a_newer_snapshot():
    """A caller that read an old version must not publish over a newer snapshot.

    The version is read before the lock is taken, so a caller can be overtaken
    while it waits: by the time it writes, a newer catalog has already filled the
    cache. Publishing regardless replaced that whole snapshot with the older one,
    throwing away every entry built since and forcing all of them to be rebuilt.

    Threads because the window is between reading the version and taking the lock
    — a single-threaded test cannot get inside it, and one that pretends to would
    pass against the bug.
    """
    import threading

    version = [1]
    read_v1 = threading.Event()
    newer_published = threading.Event()

    def version_fn():
        v = version[0]
        if threading.current_thread().name == "slow" and not read_v1.is_set():
            read_v1.set()            # v1 in hand, now let the newer writer past
            newer_published.wait(5)
        return v

    c = cache.VersionedCache(version_fn)
    slow = threading.Thread(target=lambda: c.get("facets", lambda: "facets-v1"),
                            name="slow")
    slow.start()
    assert read_v1.wait(5)
    version[0] = 2                   # a rebuild lands
    assert c.get("genres", lambda: "genres-v2") == "genres-v2"
    newer_published.set()            # the slow caller wakes up and writes
    slow.join(5)

    assert c.get("genres", lambda: "REBUILT") == "genres-v2", "snapshot was rolled back"


def test_pinned_lookups_all_describe_one_catalog():
    """A value derived from another cached value must share its version.

    `authors_by_letter` fetches the merged author list and then buckets it. Read
    separately, a rebuild between the two filed version-1 authors under version 2.
    """
    version = [1]
    c = cache.VersionedCache(lambda: version[0])
    pinned = c.pinned()

    base = pinned.get("authors", lambda: "authors-v1")
    version[0] = 2              # a rebuild lands between the two lookups
    derived = pinned.get(("authors", "achternaam"), lambda: f"buckets-of-{base}")

    assert derived == "buckets-of-authors-v1"    # consistent with its own input
    # and it is not sitting in the cache pretending to be version 2
    assert c.get(("authors", "achternaam"),
                 lambda: "buckets-of-authors-v2") == "buckets-of-authors-v2"


def test_clear_forgets_every_version():
    c = cache.VersionedCache(lambda: 1)
    c.get("k", lambda: "first")
    c.clear()
    assert c.get("k", lambda: "second") == "second"
