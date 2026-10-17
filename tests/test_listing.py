"""Tests for the results-listing parser against captured fixtures."""

from pathlib import Path

import pytest

from obc.listing import NotAResultsPage, max_page, parse_listing, total_results

FIX = Path(__file__).parent / "fixtures"


def test_ebook_listing():
    html = (FIX / "listing_ebook.html").read_text(encoding="utf-8")
    recs, mx = parse_listing(html)
    assert len(recs) == 20
    assert mx == 50  # capped partition
    r = recs[0]
    assert r["ppn"] and r["slug"] and r["title"] and r["author"]
    assert r["format"] == "ebook"
    assert r["language"] == "Nederlands"
    assert r["summary"]
    assert all(r.get("year") for r in recs)


def test_ebook_size_decimal():
    recs, _ = parse_listing((FIX / "listing_ebook.html").read_text(encoding="utf-8"))
    sizes = [r.get("size") for r in recs if r.get("size")]
    # Dutch decimal comma must survive: "9,8 MB", not "8 MB"
    assert any("," in s for s in sizes)


def test_audiobook_listing():
    recs, _ = parse_listing((FIX / "listing_audiobook.html").read_text(encoding="utf-8"))
    assert recs and recs[0]["format"] == "audiobook"
    assert recs[0]["duration"]
    assert "uur" in recs[0]["duration"] or ":" in recs[0]["duration"]


def test_max_page_none():
    assert max_page("<html>no pager here</html>") == 1


def test_total_results():
    assert total_results(
        '<p class="totalresults">Resultaat 1 - 20\n'
        '<span class="additional">(van 3124)</span></p>') == 3124
    assert total_results('<p class="totalresults">Resultaat 1 - 7</p>') == 7
    assert total_results("<html>zegt niets</html>") is None


def test_total_results_from_fixture():
    assert total_results(
        (FIX / "listing_ebook.html").read_text(encoding="utf-8")) == 3124


def test_the_page_past_the_last_one_is_a_results_page():
    """Captured from the live site: page 3 of a 7-result query.

    It carries no ``ul.rich-list``, no ``p.totalresults`` and no sort form —
    only site chrome under <title>Zoekresultaten</title>. Requiring any of those
    markers rejected the ordinary end of pagination as a blocked page, so on the
    live catalog every cell of a full walk "failed" at the moment it finished,
    and the year-split that reaches the pre-1900 titles died on its first query.
    """
    html = (FIX / "listing_past_end.html").read_text(encoding="utf-8")
    recs, _ = parse_listing(html)          # must not raise
    assert recs == []


def test_parse_listing_rejects_a_non_results_page():
    # A soft-200 (maintenance page, block interstitial) must not read as "zero
    # results": that once terminated an enumeration cell as if it were complete,
    # and everything unseen was marked removed.
    with pytest.raises(NotAResultsPage):
        parse_listing("<html><body>Er is een storing. Probeer later.</body></html>")


def test_extent_first_line_is_not_a_language():
    # An item with no language starts the pipe-line with the extent; that used to
    # be captured as language="47 pagina's (ePub3, 9,8 MB)" and the page count lost.
    html = ('<ul class="rich-list"><li>'
            '<a class="image-link" href="/catalogus/1/x">t</a>'
            "<p class=\"additional\">47 pagina's (ePub3, 9,8 MB) | "
            "Uitgeverij X | 2020</p>"
            "</li></ul>")
    rec = parse_listing(html)[0][0]
    assert rec.get("language") is None
    assert rec["pages"] == 47
    assert rec["publisher"] == "Uitgeverij X"
    assert rec["year"] == 2020
