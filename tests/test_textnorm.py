"""Tests for text normalisation + the bestseller60 parser."""

from obc.textnorm import split_authors, publisher_key, match_key, canonical_publisher
from obc.lists import bestseller60, nyt


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


def test_match_key_uses_title_and_surname():
    assert match_key("De Hobbit", "J.R.R. Tolkien") == match_key("de hobbit", "Tolkien")


def test_bestseller60_parser():
    html = """
    <div class="card__position card__position--up">3</div>
    <div class="card__author"><a href="/zoeken/Ilja+Gort">Ilja Gort</a></div>
    <div class="card__title heading-2" title="Grand Café du Malheur">Grand Café du Malheur</div>
    <div class="card__tags__tag">ISBN 9789083425542</div>
    """
    items = bestseller60.parse(html)
    assert items and items[0]["position"] == 3
    assert items[0]["title"] == "Grand Café du Malheur"
    assert items[0]["author"] == "Ilja Gort"
    assert items[0]["isbn"] == "9789083425542"


def test_prometheus_subimprint_kept_separate():
    assert canonical_publisher("Prometheus, Amsterdam", "Prometheus, Amsterdam") == "Prometheus, Amsterdam"
    assert canonical_publisher("Bert Bakker, Amsterdam", "x") == "Prometheus Bert Bakker, Amsterdam"
    assert canonical_publisher("Prometheus-Bert Bakker, Amsterdam", "x") == "Prometheus Bert Bakker, Amsterdam"


def test_nyt_parse_overview():
    data = {"results": {"lists": [
        {"list_name_encoded": "combined-print-and-e-book-fiction",
         "display_name": "Combined Print & E-Book Fiction",
         "books": [{"rank": 1, "title": "THE WOMEN", "author": "Kristin Hannah",
                    "primary_isbn13": "9781250178633", "book_image": "https://x/c.jpg"}]}]}}
    out = nyt.parse_overview(data)
    assert len(out) == 1
    lst = out[0]
    assert lst["slug"] == "nyt-combined-print-and-e-book-fiction"
    assert lst["name"] == "NYT — Combined Print & E-Book Fiction"
    it = lst["items"][0]
    assert it["title"] == "The Women"  # ALL-CAPS tidied
    assert it["author"] == "Kristin Hannah"
    assert it["isbn"] == "9781250178633"
    assert it["position"] == 1
