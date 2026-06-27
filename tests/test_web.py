"""End-to-end route tests over the hermetic fixture catalog (see conftest)."""


def test_home_and_filters(client):
    for path in ["/", "/?q=ontdekking", "/?format=ebook", "/?format=audiobook",
                 "/?language=Nederlands&language=Engels", "/?ereader=1",
                 "/?year_from=2018&year_to=2021", "/?sort=added", "/?sort=title",
                 "/?q=thriller&sort=relevance", "/?page=2", "/?list=test-top"]:
        assert client.get(path).status_code == 200, path


def test_format_filter_renders_only_matches(client):
    body = client.get("/?format=audiobook").text
    assert "/book/002" in body  # the audiobook edition is shown


def test_suggest(client):
    data = client.get("/suggest?q=ontdek").json()
    assert any(t["ppn"] == "001" for t in data["titles"])
    for key in ("titles", "authors", "publishers", "genres", "languages", "lists"):
        assert key in data
    assert client.get("/suggest?q=").json()["titles"] == []


def test_facet_endpoint(client):
    assert "Anna Vrij" in client.get("/facet?type=author").json()["values"]
    assert client.get("/facet?type=bogus").json()["values"] == []


def test_book_detail_and_404(client):
    assert client.get("/book/001").status_code == 200
    assert client.get("/book/zzznope").status_code == 404


def test_author_page(client):
    assert client.get("/author/Anna Vrij").status_code == 200
    assert client.get("/author/Zzz Niemand").status_code == 404


def test_series_page(client):
    assert client.get("/series/Het Mysterie").status_code == 200
    assert client.get("/series/Zzz Geen Reeks").status_code == 404


def test_lists_pages(client):
    assert client.get("/lists").status_code == 200
    assert client.get("/list/test-top").status_code == 200
    assert client.get("/list/test-top?show=available").status_code == 200
    assert client.get("/list/test-top?show=unavailable").status_code == 200
    assert client.get("/list/zzznope").status_code == 404


def test_stats_health_static(client):
    assert client.get("/stats").status_code == 200
    assert client.get("/favicon.svg").status_code == 200
    assert client.get("/healthz").json() == {"status": "ok"}


def test_about_page(client):
    r = client.get("/over")
    assert r.status_code == 200
    assert "Over deze catalogus" in r.text


def test_theme_switcher_present(client):
    # the switcher lives in the shared header, so it ships on every page
    body = client.get("/").text
    assert 'id="theme-toggle"' in body
    assert "localStorage.getItem('theme')" in body


def test_admin_refresh_requires_token(client):
    # No OBC_REFRESH_TOKEN configured in tests -> always unauthorized.
    assert client.post("/admin/refresh").status_code == 401
    assert client.post("/admin/refresh",
                       headers={"Authorization": "Bearer nope"}).status_code == 401
