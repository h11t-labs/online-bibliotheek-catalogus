"""Unit tests for the text-normalisation helpers."""

from obc.textnorm import (
    canonical_author,
    canonical_publisher,
    detect_series,
    fold,
    match_key,
    publisher_key,
    split_authors,
    valid_language,
)


def test_split_authors_pipe_semicolon():
    assert split_authors("Marianne Busser | Ron Schröder") == ["Marianne Busser", "Ron Schröder"]
    assert split_authors("A ; B ; A") == ["A", "B"]  # dedupe


def test_split_authors_keeps_commas_in_names():
    # commas are name quirks here, not separators
    assert split_authors("Buren, van") == ["Buren, van"]
    assert split_authors("") == []


def test_publisher_key_groups_variants():
    keys = {publisher_key(p) for p in
            ["De Correspondent, Amsterdam", "de Correspondent, [Amsterdam]",
             "De Correspondent, Amsterdam"]}
    assert len(keys) == 1


def test_prometheus_subimprint_kept_separate():
    assert canonical_publisher("Prometheus, Amsterdam", "Prometheus, Amsterdam") == "Prometheus, Amsterdam"
    assert canonical_publisher("Bert Bakker, Amsterdam", "x") == "Prometheus Bert Bakker, Amsterdam"
    assert canonical_publisher("Prometheus-Bert Bakker, Amsterdam", "x") == "Prometheus Bert Bakker, Amsterdam"


def test_canonical_author_alias():
    assert canonical_author("Bernlef") == "J. Bernlef"
    assert canonical_author("Anna Vrij") == "Anna Vrij"  # unknown -> unchanged


def test_fold_strips_case_and_diacritics():
    assert fold("Klöpping") == "klopping"
    assert fold("España!") == "espana"
    assert fold(None) == ""


def test_match_key_uses_title_and_surname():
    assert match_key("De Hobbit", "J.R.R. Tolkien") == match_key("de hobbit", "Tolkien")


def test_valid_language_filters_junk():
    assert valid_language("Nederlands") == "Nederlands"
    assert valid_language("Fictie") is None
    assert valid_language("") is None


def test_detect_series_explicit_marker_only():
    assert detect_series("Het Mysterie: deel 2") == ("Het Mysterie", 2)
    # no false positives on numbers that aren't series markers
    assert detect_series("1984") == (None, None)
    assert detect_series("Catch-22") == (None, None)
