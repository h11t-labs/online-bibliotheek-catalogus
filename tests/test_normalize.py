"""Normalisation business logic against tmp raw data: publisher canon, author
split/alias, language validation, series detection, and list matching.

Exercises the streaming pipeline (``_prepass`` -> ``iter_records`` ->
``match_lists``) the same way :func:`obc.normalize.normalize` does."""

import json
from pathlib import Path

import pytest

from obc import db, normalize


def _write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def raw(tmp_path, monkeypatch):
    """A tmp ``data/raw`` with records + an ereader file; the side/list files
    are redirected at tmp so the test never touches the real catalog."""
    rec = tmp_path / "records"
    # three Querido spellings (most common = "Querido, Amsterdam") + isbn on #1
    _write(rec / "1.json", {"ppn": "1", "title": "Boek Een", "author": "Anna Vrij",
                            "format": "ebook", "language": "Nederlands",
                            "publisher": "Querido, Amsterdam", "isbn": "978 90 214 0001 7"})
    _write(rec / "2.json", {"ppn": "2", "title": "Boek Twee", "author": "Anna Vrij",
                            "format": "ebook", "language": "Nederlands",
                            "publisher": "querido, [Amsterdam]"})
    _write(rec / "3.json", {"ppn": "3", "title": "Boek Drie", "author": "Anna Vrij",
                            "format": "ebook", "language": "Nederlands",
                            "publisher": "Querido, Amsterdam"})
    # multi-author + alias (Bernlef -> J. Bernlef), junk language, series title
    _write(rec / "4.json", {"ppn": "4", "title": "Samen: deel 2",
                            "author": "Bob de Wit | Bernlef", "format": "ebook",
                            "language": "Fictie"})
    monkeypatch.setattr(normalize, "EREADER_FILE", tmp_path / "ereader.json")
    monkeypatch.setattr(normalize, "GENRES_FILE", tmp_path / "genres.json")
    monkeypatch.setattr(normalize, "RECENT_FILE", tmp_path / "recent.json")
    monkeypatch.setattr(normalize, "LISTS_DIR", tmp_path / "lists")
    _write(tmp_path / "ereader.json", ["1"])  # only book 1 is e-reader-available
    return tmp_path


def _enrich(raw):
    """Run the read-only half of the pipeline; return (records, by_isbn, by_key)."""
    paths = sorted((raw / "records").rglob("*.json"))
    aux = normalize._load_aux()
    canon, by_isbn, by_key, _ = normalize._prepass(paths)
    records = {r["ppn"]: r for r in normalize.iter_records(paths, aux, canon)}
    return records, by_isbn, by_key


def test_publishers_canonicalised_to_most_common(raw):
    records, _, _ = _enrich(raw)
    pubs = {ppn: records[ppn]["publisher"] for ppn in ("1", "2", "3")}
    assert pubs["1"] == pubs["2"] == pubs["3"] == "Querido, Amsterdam"


def test_authors_split_and_aliased(raw):
    r4 = _enrich(raw)[0]["4"]
    assert r4["authors"] == ["Bob de Wit", "J. Bernlef"]
    assert r4["author"] == "Bob de Wit, J. Bernlef"


def test_junk_language_dropped_and_series_detected(raw):
    r4 = _enrich(raw)[0]["4"]
    assert r4["language"] is None            # "Fictie" is not a real language
    assert r4["series"] == "Samen"
    assert r4["series_no"] == 2


def test_ereader_flag_from_side_file(raw):
    records = _enrich(raw)[0]
    assert records["1"]["ereader"] == 1
    assert records["2"]["ereader"] == 0


def test_match_lists_by_isbn_then_title(raw):
    _, by_isbn, by_key = _enrich(raw)
    _write(raw / "lists" / "t.json", {"slug": "t", "name": "T", "items": [
        {"position": 1, "isbn": "9789021400017", "title": "x", "author": "y"},
        {"position": 2, "title": "Boek Twee", "author": "Anna Vrij"},
        {"position": 3, "title": "Bestaat Niet", "author": "Niemand"},
    ]})
    items = normalize.match_lists(by_isbn, by_key)[0]["items"]
    assert items[0]["ppn"] == "1"   # matched on ISBN (punctuation stripped)
    assert items[1]["ppn"] == "2"   # matched on title + author surname
    assert items[2]["ppn"] is None  # no match -> stays in list_items, greyed out


def test_normalize_end_to_end_builds_db(raw, tmp_path):
    _write(raw / "lists" / "t.json", {"slug": "t", "name": "T",
           "items": [{"position": 1, "isbn": "9789021400017"}]})
    db_path = tmp_path / "out.db"
    stats = normalize.normalize(raw, db_path)
    assert stats["books"] == 4
    conn = db.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM book_lists").fetchone()[0] == 1  # isbn match
    assert conn.execute(
        "SELECT publisher FROM books WHERE ppn='2'").fetchone()[0] == "Querido, Amsterdam"
    conn.close()
