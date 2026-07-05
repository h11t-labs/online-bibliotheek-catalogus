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
    # the devicetypes strip carries a .ereader span -> works on an e-reader
    assert r["ereader"] == 1


def test_ereader_flag_app_only():
    # an e-book whose devicetypes strip lists app + laptop but NOT e-reader
    html = """<html><head>
      <link rel="canonical" href="/catalogus/999/test-boek"/>
      <title>Test Boek - Auteur | e-book | de online Bibliotheek</title>
    </head><body>
      <span class="title">Test Boek</span>
      <p class="additional devicetypes"><span class="materialtype"> E-book </span>
        <span class="devicewrapper">| voor&nbsp;
          <span class="app" title="geschikt voor telefoon of tablet">telefoon of tablet</span>
          <span class="laptop" title="geschikt voor pc of laptop">pc of laptop</span>
        </span></p>
    </body></html>"""
    r = parse_detail(html)
    assert r["format"] == "ebook"
    assert r["ereader"] == 0


def test_no_ereader_flag_for_audiobook():
    # audiobooks have no e-reader concept -> the flag is left unset (NULL in DB)
    r = _parse("audiobook_431630879.html")
    assert "ereader" not in r


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
