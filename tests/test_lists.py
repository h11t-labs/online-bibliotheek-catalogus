"""Parser tests for the curated-list providers (no network: parse fixed text)."""

from obc.lists import bestseller60, nyt, wikiprize


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


def test_bestseller60_period_from_week():
    p = bestseller60.period("… Week 26 - 2026 …")
    assert p and p.startswith("week 26 · ") and "t/m" in p and "2026" in p
    assert bestseller60.period("no week here") is None
