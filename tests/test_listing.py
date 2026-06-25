"""Tests for the results-listing parser against captured fixtures."""

from pathlib import Path

from obc.listing import max_page, parse_listing

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
