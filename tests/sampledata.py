"""A tiny, deterministic catalog shared by the hermetic tests.

Six books chosen to exercise the interesting paths:

* 001 + 002 — an e-book and an audiobook of the *same work* (shared title +
  author) so edition-linking / formats_map have something to join on.
* 003 — English, two authors, ereader=0.
* 004 — a series volume (``Het Mysterie: deel 2``), Dutch.
* 005 — ereader-available cookbook.
* 006 — Spanish, diacritics in the title.

``records()`` / ``lists()`` return fresh copies so a test may mutate them.
"""

from __future__ import annotations


def records() -> list[dict]:
    return [
        {"ppn": "001", "title": "De Ontdekking", "author": "Anna Vrij",
         "authors": ["Anna Vrij"], "format": "ebook", "language": "Nederlands",
         "publisher": "Querido, Amsterdam", "year": 2020, "isbn": "9789021400001",
         "subjects": ["Literatuur & Romans"], "ereader": 1,
         "summary": "Een verhaal over España en ontdekking.",
         "cover_url": "https://leibniz.zbkb.nl/assets/id/PPN:001?width=320"},
        {"ppn": "002", "title": "De Ontdekking", "author": "Anna Vrij",
         "authors": ["Anna Vrij"], "format": "audiobook", "language": "Nederlands",
         "publisher": "Querido, Amsterdam", "year": 2021, "isbn": "9789021400002",
         "subjects": ["Literatuur & Romans"], "narrator": "Jan Stem",
         "duration": "6 uur", "summary": "Audio-editie van De Ontdekking."},
        {"ppn": "003", "title": "Thriller in de Nacht", "author": "Bob de Wit, Cara Licht",
         "authors": ["Bob de Wit", "Cara Licht"], "format": "ebook", "language": "Engels",
         "publisher": "Spanning BV", "year": 2015, "isbn": "9789021400003",
         "subjects": ["Spanning & Thrillers"], "ereader": 0,
         "summary": "A thriller in the night."},
        {"ppn": "004", "title": "Het Mysterie: deel 2", "author": "Bob de Wit",
         "authors": ["Bob de Wit"], "format": "ebook", "language": "Nederlands",
         "publisher": "Spanning BV", "year": 2018,
         "subjects": ["Spanning & Thrillers"], "series": "Het Mysterie", "series_no": 2},
        {"ppn": "005", "title": "Koken met Liefde", "author": "Dirk Kok",
         "authors": ["Dirk Kok"], "format": "ebook", "language": "Nederlands",
         "publisher": "Keuken Pers", "year": 2022, "isbn": "9789021400005",
         "subjects": ["Gezin & Gezondheid"], "ereader": 1},
        {"ppn": "006", "title": "Poesía Española", "author": "Elena Sol",
         "authors": ["Elena Sol"], "format": "ebook", "language": "Spaans",
         "publisher": "Sol Editorial", "year": 2019,
         "subjects": ["Literatuur & Romans"], "summary": "Poesía en español."},
    ]


def lists() -> list[dict]:
    """One curated list: two matched books (001, 003) + one unmatched slot."""
    return [{
        "slug": "test-top", "name": "Test Top", "url": "https://example.test",
        "description": "Een testlijst.", "updated_at": "2024-01-01",
        "items": [
            {"position": 1, "ppn": "001", "title": "De Ontdekking",
             "isbn": "9789021400001", "won": 1},
            {"position": 2, "ppn": None, "title": "Onbekend Boek", "author": "Niemand",
             "won": 0},
            {"position": 3, "ppn": "003", "title": "Thriller in de Nacht", "won": 0},
        ],
    }]
