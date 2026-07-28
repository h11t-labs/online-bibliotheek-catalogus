"""A tiny, deterministic catalog shared by the hermetic tests.

Nine *editions* forming five *works*, chosen to exercise the interesting paths:

* 001 + 002 + 007 — one work in three editions: an e-book and **two** audiobooks
  (the case that killed /luisterboeken, where editions were counted as titles).
  007's summary carries a word ("walvisexpeditie") that exists nowhere else, so
  a work whose text only lives on a non-representative edition must still be
  findable.
* 003 — English, two authors, ereader=0.
* 004 + 009 — a series volume (``Het Mysterie: deel 2``) plus an audiobook whose
  title matches no key; they are one work **only** via the library's own
  ``related_ppns`` cross-link. 009 also spells its author "Bob De Wit", so the
  person grain has a spelling variant to merge (majority "Bob de Wit", 2 vs 1).
* 005 + 008 — one work **only** via ``strip_format_noise``: 008's title carries a
  "- luisterboek" suffix. 005 has keywords not present in the title/subjects (so
  full-text/suggest matching on keywords has something to exercise).
* 006 — Spanish, diacritics in the title.

Truth the tests assert against: 9 editions, 5 works; work 001 = {001, 002, 007}
(audiobook_ppn "002"), 003 = {003}, 004 = {004, 009}, 005 = {005, 008}, 006 = {006}.

Every record carries a ``url``: the borrow link is per edition (you borrow an
edition, never a work), so the book page needs two distinct ones to render.

``records()`` / ``lists()`` return fresh copies so a test may mutate them.
"""

from __future__ import annotations


def records() -> list[dict]:
    return [
        {"ppn": "001",
         "url": "https://www.onlinebibliotheek.nl/catalogus/001/de-ontdekking",
         "title": "De Ontdekking", "author": "Anna Vrij",
         "authors": ["Anna Vrij"], "format": "ebook", "language": "Nederlands",
         "publisher": "Querido, Amsterdam", "year": 2020, "isbn": "9789021400001",
         "subjects": ["Literatuur & Romans"], "ereader": 1,
         "summary": "Een verhaal over España en ontdekking.",
         "cover_url": "https://leibniz.zbkb.nl/assets/id/PPN:001?width=320"},
        {"ppn": "002",
         "url": "https://www.onlinebibliotheek.nl/catalogus/002/de-ontdekking-luisterboek",
         "title": "De Ontdekking", "author": "Anna Vrij",
         "authors": ["Anna Vrij"], "format": "audiobook", "language": "Nederlands",
         "publisher": "Querido, Amsterdam", "year": 2021, "isbn": "9789021400002",
         "subjects": ["Literatuur & Romans"], "narrator": "Jan Stem",
         "duration": "6 uur", "summary": "Audio-editie van De Ontdekking."},
        {"ppn": "003",
         "url": "https://www.onlinebibliotheek.nl/catalogus/003/thriller-in-de-nacht",
         "title": "Thriller in de Nacht", "author": "Bob de Wit, Cara Licht",
         "authors": ["Bob de Wit", "Cara Licht"], "format": "ebook", "language": "Engels",
         "publisher": "Spanning BV", "year": 2015, "isbn": "9789021400003",
         "subjects": ["Spanning & Thrillers"], "ereader": 0,
         "summary": "A thriller in the night."},
        {"ppn": "004",
         "url": "https://www.onlinebibliotheek.nl/catalogus/004/het-mysterie-deel-2",
         "title": "Het Mysterie: deel 2", "author": "Bob de Wit",
         "authors": ["Bob de Wit"], "format": "ebook", "language": "Nederlands",
         "publisher": "Spanning BV", "year": 2018,
         "subjects": ["Spanning & Thrillers"], "series": "Het Mysterie", "series_no": 2},
        {"ppn": "005",
         "url": "https://www.onlinebibliotheek.nl/catalogus/005/koken-met-liefde",
         "title": "Koken met Liefde", "author": "Dirk Kok",
         "authors": ["Dirk Kok"], "format": "ebook", "language": "Nederlands",
         "publisher": "Keuken Pers", "year": 2022, "isbn": "9789021400005",
         "subjects": ["Gezin & Gezondheid"], "keywords": "pasta, italiaans",
         "ereader": 1},
        {"ppn": "006",
         "url": "https://www.onlinebibliotheek.nl/catalogus/006/poesia-espanola",
         "title": "Poesía Española", "author": "Elena Sol",
         "authors": ["Elena Sol"], "format": "ebook", "language": "Spaans",
         "publisher": "Sol Editorial", "year": 2019,
         "subjects": ["Literatuur & Romans"], "summary": "Poesía en español."},
        # a second audiobook of work 001 — merged by the key, and the only place
        # the word "walvisexpeditie" appears in the whole fixture catalog
        {"ppn": "007",
         "url": "https://www.onlinebibliotheek.nl/catalogus/007/de-ontdekking-luisterboek-2",
         "title": "De Ontdekking", "author": "Anna Vrij",
         "authors": ["Anna Vrij"], "format": "audiobook", "language": "Nederlands",
         "publisher": "Querido, Amsterdam", "year": 2023, "isbn": "9789021400007",
         "subjects": ["Literatuur & Romans"], "narrator": "Piet Stem",
         "summary": "Volledige walvisexpeditie editie."},
        # merges with 005 only because strip_format_noise drops the suffix
        {"ppn": "008",
         "url": "https://www.onlinebibliotheek.nl/catalogus/008/koken-met-liefde-luisterboek",
         "title": "Koken met Liefde - luisterboek", "author": "Dirk Kok",
         "authors": ["Dirk Kok"], "format": "audiobook", "language": "Nederlands",
         "publisher": "Keuken Pers", "year": 2022, "isbn": "9789021400008",
         "subjects": ["Gezin & Gezondheid"], "narrator": "Kees Stem"},
        # merges with 004 only because the library links the two itself; the
        # capital-D spelling folds to the same person as 003/004's "Bob de Wit"
        {"ppn": "009",
         "url": "https://www.onlinebibliotheek.nl/catalogus/009/het-grote-mysterie-tweede-deel",
         "title": "Het grote mysterie, tweede deel", "author": "Bob De Wit",
         "authors": ["Bob De Wit"], "format": "audiobook", "language": "Nederlands",
         "publisher": "Spanning BV", "year": 2019,
         "subjects": ["Spanning & Thrillers"], "narrator": "Ria Stem",
         "related_ppns": ["004"]},
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
