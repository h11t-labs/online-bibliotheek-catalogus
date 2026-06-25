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
