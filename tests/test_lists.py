"""Parser tests for the curated-list providers (no network: parse fixed text)."""

import json

import httpx
import pytest

import obc.lists
from obc.lists import bestseller60, nyt, wikiprize
from obc.log import logger


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
    item = lst["items"][0]
    assert item["title"] == "The Women"  # ALL-CAPS tidied
    assert item["author"] == "Kristin Hannah"
    assert item["isbn"] == "9781250178633"
    assert item["position"] == 1


def test_wikiprize_parses_years_and_orders_newest_first():
    wikitext = (
        "=== 2014 ===\n"
        "* [[Anna Vrij]], ''De Ontdekking''\n"
        "=== 2013 ===\n"
        "* [[Bob de Wit]], ''Thriller in de Nacht''\n"
    )
    items = wikiprize.parse_wikitext(wikitext)
    assert len(items) == 2
    assert items[0] == {"title": "De Ontdekking", "author": "Anna Vrij", "isbn": None,
                        "cover_url": None, "year": 2014, "position": 1, "won": 1}
    assert items[1]["year"] == 2013  # sorted newest first


def test_wikiprize_marks_nominees_vs_winners():
    wt = ("=== Genomineerden 2020 ===\n"
          "* [[Anna Vrij]], ''Genomineerd Boek''\n"
          "== Winnaars ==\n"
          "* [[Cara Licht]], ''Winnend Boek''\n")
    by_title = {it["title"]: it for it in wikiprize.parse_wikitext(wt)}
    assert by_title["Genomineerd Boek"]["won"] == 0  # under a nominee heading
    assert by_title["Winnend Boek"]["won"] == 1       # winner section -> won


def test_nyt_error_does_not_leak_api_key(monkeypatch):
    # raise_for_status() would embed the full URL (api-key included) in the
    # exception, which update() logs verbatim — the raised message must be clean.
    key = "SECRETKEY123"
    monkeypatch.setenv("NYT_API_KEY", key)

    def fake_get(url, params=None, **kw):
        return httpx.Response(429, request=httpx.Request("GET", url, params=params))

    monkeypatch.setattr(nyt.httpx, "get", fake_get)
    with pytest.raises(RuntimeError) as exc:
        nyt.fetch_all()
    assert key not in str(exc.value)
    assert "429" in str(exc.value)


def test_bestseller60_positionless_items_not_collapsed():
    # if a markup change loses the position element, items must not all dedupe
    # onto the shared key None — only true duplicates (same title+author) may.
    html = """
    <div class="card__position"></div>
    <div class="card__author"><a href="/a">Anna Vrij</a></div>
    <div class="card__title" title="Boek Een">Boek Een</div>
    <div class="card__position"></div>
    <div class="card__author"><a href="/b">Bob de Wit</a></div>
    <div class="card__title" title="Boek Twee">Boek Twee</div>
    <div class="card__position"></div>
    <div class="card__author"><a href="/a">Anna Vrij</a></div>
    <div class="card__title" title="Boek Een">Boek Een</div>
    """
    items = bestseller60.parse(html)
    assert [it["title"] for it in items] == ["Boek Een", "Boek Twee"]


def test_update_sanitizes_slug_and_writes_atomically(tmp_path, monkeypatch):
    provider = lambda: [  # noqa: E731 — needs a __module__ like a real provider
        {"slug": "../Evil/Slug!", "name": "x", "url": "u", "description": "d",
         "items": [{"position": 1, "title": "t", "author": "a",
                    "isbn": None, "cover_url": None}]},
    ]
    monkeypatch.setattr(obc.lists, "LISTS_DIR", tmp_path)
    monkeypatch.setattr(obc.lists, "PROVIDERS", [provider])
    obc.lists.update()
    # "/" and ".." stripped -> stays inside LISTS_DIR; no .tmp left behind
    assert [p.name for p in tmp_path.iterdir()] == ["evilslug.json"]
    data = json.loads((tmp_path / "evilslug.json").read_text(encoding="utf-8"))
    assert data["slug"] == "evilslug" and data["updated_at"]


def test_update_warns_when_provider_refreshes_fewer_lists(tmp_path, monkeypatch):
    # a previously written list this run doesn't refresh -> staleness warning
    # (the fake provider's module basename is "test_lists", hence the filename)
    (tmp_path / "test_lists-old.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(obc.lists, "LISTS_DIR", tmp_path)
    monkeypatch.setattr(obc.lists, "PROVIDERS", [lambda: []])
    msgs = []
    sink = logger.add(lambda m: msgs.append(str(m)), level="WARNING")
    try:
        obc.lists.update()
    finally:
        logger.remove(sink)
    assert any("not refreshed" in m for m in msgs)


def test_bestseller60_period_from_week():
    p = bestseller60.period("… Week 26 - 2026 …")
    assert p and p.startswith("week 26 · ") and "t/m" in p and "2026" in p
    assert bestseller60.period("no week here") is None


def test_bestseller60_period_handles_redesigned_year_week_order():
    # the site's redesign flipped "Week N - YYYY" to "YYYY week N" (e.g. the meta
    # description); a fetch after that redesign returned None here, so the "bijgewerkt
    # op" date kept advancing while the displayed week/range silently went stale.
    p = bestseller60.period('content="2026 week 27 van de officiële bestsellerlijst"')
    assert p and p.startswith("week 27 · ") and "2026" in p
