"""Work identity (obc.work): title normalisation, the key, cross-links, overrides.

The grouping is the one piece of this model that can be *wrong* about the data
(a false merge hides a book), so every rule gets its own case here rather than
being covered incidentally through the build.
"""

from __future__ import annotations

import pytest

from obc.work import (
    EditionMeta,
    group_editions,
    load_overrides,
    stamp_work_ids,
    strip_format_noise,
    work_key,
)


@pytest.mark.parametrize("title, expected", [
    ("De adoptie - luisterboek", "De adoptie"),
    ("De adoptie – luisterboek", "De adoptie"),
    ("Titel (e-book)", "Titel"),
    ("Titel [ebook]", "Titel"),
    ("Titel luisterboek (digitaal)", "Titel"),
    ("Titel - digitaal luisterboek", "Titel"),
    ("Titel: gesproken versie", "Titel"),
    ("Titel - EPUB3", "Titel"),
    ("Deel 2: digitaal", "Deel 2"),
    ("Luisterboek", "Luisterboek"),       # stripping would empty it
    ("e-book", "e-book"),
    ("1984", "1984"),
    ("Catch-22", "Catch-22"),
    ("", ""),
    (None, ""),
])
def test_strip_format_noise(title, expected):
    assert strip_format_noise(title) == expected


def test_strip_format_noise_only_takes_one_marker():
    # one marker, not a greedy tail-eating loop
    assert strip_format_noise("Titel - luisterboek - mp3") == "Titel - luisterboek"


def test_work_key_uses_first_author_surname():
    # "Anna Vrij" and "A. Vrij ; Jan Stem" are the same book to the key
    assert work_key("De Ontdekking", "Anna Vrij", "Nederlands") == \
        work_key("De ontdekking - luisterboek", "A. Vrij ; Jan Stem", "Nederlands")
    # a different surname is a different work
    assert work_key("De Ontdekking", "Anna Vrij", "Nederlands") != \
        work_key("De Ontdekking", "Bob de Wit", "Nederlands")
    # no author at all still keys on the title
    assert work_key("De Ontdekking", None, None) == ("de ontdekking", "")


def _meta(**kw):
    """EditionMeta with the fixture defaults (Dutch e-book, no cross-links)."""
    return EditionMeta(title=kw.get("title"), author=kw.get("author", "Anna Vrij"),
                       language=kw.get("language", "Nederlands"),
                       format=kw.get("format", "ebook"),
                       related_ppns=tuple(kw.get("related_ppns", ())))


def test_key_groups_same_title_author_language():
    groups = group_editions({
        "1": _meta(title="De Ontdekking"),
        "2": _meta(title="De ontdekking - luisterboek", format="audiobook"),
        "3": _meta(title="Iets anders"),
    })
    assert groups["1"] == groups["2"] == "1"
    assert groups["3"] == "3"


def test_different_language_splits_the_key():
    groups = group_editions({
        "1": _meta(title="De Ontdekking", language="Nederlands"),
        "2": _meta(title="De Ontdekking", language="Engels"),
    })
    assert groups["1"] != groups["2"]


def test_missing_language_joins_a_single_language_bucket():
    groups = group_editions({
        "1": _meta(title="De Ontdekking", language="Nederlands"),
        "2": _meta(title="De Ontdekking", language=None, format="audiobook"),
    })
    assert groups["1"] == groups["2"]


def test_missing_language_stays_alone_when_the_bucket_is_ambiguous():
    groups = group_editions({
        "1": _meta(title="De Ontdekking", language="Nederlands"),
        "2": _meta(title="De Ontdekking", language="Engels"),
        "3": _meta(title="De Ontdekking", language=None),
    })
    assert groups["3"] == "3"
    assert len({groups["1"], groups["2"], groups["3"]}) == 3


def test_empty_key_editions_never_unify():
    # no title to fold (non-Latin script, junk row): each stays its own work
    groups = group_editions({
        "1": _meta(title="Λήδα"),
        "2": _meta(title="Βάρβαρούση"),
        "3": _meta(title=None),
    })
    assert groups == {"1": "1", "2": "2", "3": "3"}


def test_related_ppns_bridge_two_different_titles():
    groups = group_editions({
        "1": _meta(title="Het grote mysterie"),
        "2": _meta(title="Het grote mysterie, tweede deel", format="audiobook",
                   related_ppns=["1"]),
    })
    assert groups["1"] == groups["2"] == "1"


def test_related_ppn_absent_from_meta_is_ignored():
    groups = group_editions({"1": _meta(title="Solo", related_ppns=["999"])})
    assert groups == {"1": "1"}


def test_overrides_merge_joins_and_split_detaches_the_second_ppn():
    meta = {
        "1": _meta(title="Eén"),
        "2": _meta(title="Twee"),
    }
    assert group_editions(meta, {"merge": [["1", "2"]]}) == {"1": "1", "2": "1"}

    twins = {
        "1": _meta(title="De Ontdekking"),
        "2": _meta(title="De Ontdekking", format="audiobook"),
    }
    split = group_editions(twins, {"split": [["1", "2"]]})
    assert split == {"1": "1", "2": "2"}      # the *second* is forced out
    # merge then split cancel out to two singletons
    both = group_editions({"1": _meta(title="Eén"), "2": _meta(title="Twee")},
                          {"merge": [["1", "2"]], "split": [["1", "2"]]})
    assert both == {"1": "1", "2": "2"}


def test_representative_prefers_the_ebook_then_the_lowest_ppn():
    # an audiobook with a lower PPN does not outrank the e-book
    groups = group_editions({
        "007": _meta(title="De Ontdekking", format="audiobook"),
        "009": _meta(title="De Ontdekking", format="ebook"),
    })
    assert set(groups.values()) == {"009"}
    # no e-book in the group -> lowest ppn (string order), as set_primary_editions did
    audio_only = group_editions({
        "020": _meta(title="Alleen audio", format="audiobook"),
        "003": _meta(title="Alleen audio", format="audiobook"),
    })
    assert set(audio_only.values()) == {"003"}
    # a group of one is its own representative
    assert group_editions({"5": _meta(title="Solo")}) == {"5": "5"}


def test_stamp_work_ids_fills_only_missing_ids():
    records = [
        {"ppn": "1", "title": "De Ontdekking", "author": "Anna Vrij",
         "language": "Nederlands", "format": "ebook"},
        {"ppn": "2", "title": "De Ontdekking", "author": "Anna Vrij",
         "language": "Nederlands", "format": "audiobook"},
        {"ppn": "3", "title": "Al gestempeld", "work_id": "keep-me"},
    ]
    out = stamp_work_ids(records)
    assert out is records                     # same list, stamped in place
    assert records[0]["work_id"] == records[1]["work_id"] == "1"
    assert records[2]["work_id"] == "keep-me"


def test_load_overrides_missing_file_is_empty(tmp_path):
    assert load_overrides(tmp_path / "nope.json") == {}
    path = tmp_path / "work_overrides.json"
    path.write_text('{"merge": [["1", "2"]]}', encoding="utf-8")
    assert load_overrides(path) == {"merge": [["1", "2"]]}
