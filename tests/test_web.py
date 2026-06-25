"""Integration tests for the web UI (FastAPI TestClient over the live DB)."""

import sqlite3
import warnings

import pytest

warnings.filterwarnings("ignore")
from obc.web import app as appmod  # noqa: E402

DB = appmod.DB_PATH


def _has_db():
    try:
        sqlite3.connect(f"file:{DB}?mode=ro", uri=True).execute("SELECT 1 FROM books LIMIT 1")
        return True
    except sqlite3.Error:
        return False


pytestmark = pytest.mark.skipif(not _has_db(), reason="catalog.db not built")


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    return TestClient(appmod.app)


def _row(q):
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    r = c.execute(q).fetchone()
    c.close()
    return r


def test_home_and_filters(client):
    for path in ["/", "/?q=de", "/?format=ebook", "/?format=audiobook",
                 "/?language=Nederlands&language=Engels", "/?ereader=1",
                 "/?year_from=2000&year_to=2010", "/?sort=added",
                 "/?sort=title", "/?q=de&sort=relevance", "/?page=2"]:
        assert client.get(path).status_code == 200, path


def test_search_filter_is_applied(client):
    import re
    r = client.get("/?format=audiobook")
    ppns = re.findall(r"/book/([0-9xX]+)", r.text)[:10]
    if ppns:
        c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        bad = c.execute(
            "SELECT COUNT(*) FROM books WHERE ppn IN (%s) AND format<>'audiobook'"
            % ",".join("?" * len(ppns)), ppns).fetchone()[0]
        c.close()
        assert bad == 0


def test_suggest_shape(client):
    d = client.get("/suggest?q=de").json()
    for key in ("titles", "authors", "publishers", "genres", "languages", "lists"):
        assert key in d
    assert client.get("/suggest?q=").json()["titles"] == []


def test_facet_endpoint(client):
    assert "values" in client.get("/facet?type=author&q=a").json()
    assert "values" in client.get("/facet?type=publisher").json()
    assert client.get("/facet?type=bogus").json()["values"] == []


def test_book_detail_and_404(client):
    row = _row("SELECT ppn FROM books LIMIT 1")
    assert client.get(f"/book/{row[0]}").status_code == 200
    assert client.get("/book/zzznope").status_code == 404


def test_author_page(client):
    row = _row("SELECT a.name FROM authors a JOIN book_authors ba ON ba.author_id=a.id "
               "GROUP BY a.id ORDER BY COUNT(*) DESC LIMIT 1")
    if row:
        assert client.get(f"/author/{row[0]}").status_code == 200
    assert client.get("/author/Zzz Niemand").status_code == 404


def test_lists_pages(client):
    assert client.get("/lists").status_code == 200
    row = _row("SELECT slug FROM lists LIMIT 1")
    if row:
        assert client.get(f"/list/{row[0]}").status_code == 200
        assert client.get(f"/list/{row[0]}?show=available").status_code == 200
        assert client.get(f"/list/{row[0]}?show=unavailable").status_code == 200
    assert client.get("/list/zzznope").status_code == 404


def test_stats_and_static(client):
    assert client.get("/stats").status_code == 200
    assert client.get("/favicon.svg").status_code == 200


def test_series_404(client):
    assert client.get("/series/Zzz Geen Reeks").status_code == 404
