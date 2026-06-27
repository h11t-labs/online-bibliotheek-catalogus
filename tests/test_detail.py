"""Parser tests against captured detail-page fixtures."""

from pathlib import Path

from obc.detail import parse_detail

FIX = Path(__file__).parent / "fixtures"


def _parse(name):
    return parse_detail((FIX / name).read_text(encoding="utf-8"))


def test_ebook():
    r = _parse("ebook_460719149.html")
    assert r["ppn"] == "460719149"
    assert r["title"] == "De adoptie"
    assert r["author"] == "Marcella Kleine"
    assert r["format"] == "ebook"
    assert r["year"] == 2026
    assert r["pages"] == 320
    assert r["isbn"] == "9789465170596"
    assert "Thrillers" in r["subjects"]


def test_audiobook_alternate_canonical():
    # canonical here is the bibliotheek.nl /titel.{ppn}.html variant
    r = _parse("audiobook_431630879.html")
    assert r["ppn"] == "431630879"
    assert r["format"] == "audiobook"
    assert r["narrator"] == "Anne Tjerk Popkema"
    assert r["duration"]


def test_cover_and_ppn_present():
    r = _parse("ebook_416728413.html")
    assert r["ppn"] == "416728413"
    assert "leibniz.zbkb.nl" in r["cover_url"]
    assert r["pages"] == 728


def test_jeugd_detail_enrichment():
    # a children's book carries Leeftijd, an explicit Serie, Inhoud + keyword tags
    r = _parse("detail_jeugd_422516414.html")
    assert r["ppn"] == "422516414"
    assert r["age"] == "9-12 jaar"
    assert r["series"] == "De spannende avonturen met Dolfi"
    assert r["series_no"] == 7
    assert r["category"] == "fictie"
    # curated jeugd genres -> subjects; the plain "Onderwerpen" -> keywords
    assert "Natuur & Dieren" in r["subjects"]
    assert "Dolfijnen" in r["keywords"] and "Dolfijnen" not in r["subjects"]
    # genre facet codes reveal the hierarchy: 2.0 (parent) -> 2.6 (sub-genre)
    codes = {g["name"]: g["code"] for g in r["genres"]}
    assert codes["Natuur & Dieren"] == "2.0"
    assert codes["Wilde dieren"] == "2.6"
