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
    # an e-book and its audiobook: one work in two editions, and the audiobook is
    # the one carrying an ISBN a curated list can match on
    _write(rec / "5.json", {"ppn": "5", "title": "Boek Vijf", "author": "Els Vijf",
                            "format": "ebook", "language": "Nederlands"})
    _write(rec / "6.json", {"ppn": "6", "title": "Boek Vijf", "author": "Els Vijf",
                            "format": "audiobook", "language": "Nederlands",
                            "isbn": "9789021400060"})
    monkeypatch.setattr(normalize, "EREADER_FILE", tmp_path / "ereader.json")
    monkeypatch.setattr(normalize, "GENRES_FILE", tmp_path / "genres.json")
    monkeypatch.setattr(normalize, "RECENT_FILE", tmp_path / "recent.json")
    monkeypatch.setattr(normalize, "LISTS_DIR", tmp_path / "lists")
    _write(tmp_path / "ereader.json", ["1"])  # only book 1 is e-reader-available
    return tmp_path


def test_normalize_publishes_recommendations_with_the_swap(tmp_path, monkeypatch):
    """``work_similar`` is derived from the finished catalog, so it is not in the base
    schema and a rebuild never carries it over — and normalize swaps a freshly built
    temp DB over the live file. It must therefore build the recommendations *before*
    that swap: otherwise every nightly refresh would silently publish a catalog whose
    "meer zoals dit" strip is gone (the queries fall back to empty), and it would only
    come back if someone ran `obc similar` by hand.
    """
    pytest.importorskip("sklearn")
    rec = tmp_path / "records"
    # two thrillers + two cookbooks: pairs share subjects/summary words, so min_df=2
    # keeps real vocabulary and the reduced space has an actual neighbour to find
    books = [
        ("Nacht in Parijs", "Spanning & Thrillers",
         "Een rechercheur jaagt in Parijs op een moordenaar"),
        ("Schaduw in Parijs", "Spanning & Thrillers",
         "Een rechercheur zoekt in Parijs een moordenaar"),
        ("Koken met vuur", "Koken & Eten", "Recepten voor de barbecue met vuur en rook"),
        ("Vuur en rook", "Koken & Eten", "Barbecue recepten met rook en vuur"),
    ]
    for i, (title, subject, summary) in enumerate(books, start=1):
        _write(rec / f"{i}.json",
               {"ppn": str(i), "title": title, "author": f"Auteur {i}",
                "format": "ebook", "language": "Nederlands",
                "subjects": [subject], "summary": summary})
    for name in ("EREADER_FILE", "GENRES_FILE", "RECENT_FILE"):
        monkeypatch.setattr(normalize, name, tmp_path / f"{name.lower()}.json")
    monkeypatch.setattr(normalize, "LISTS_DIR", tmp_path / "lists")

    db_path = tmp_path / "catalog.db"
    normalize.normalize(raw_dir=tmp_path, db_path=db_path)

    conn = db.connect(db_path)
    try:
        rows = conn.execute("SELECT COUNT(*) FROM work_similar").fetchone()[0]
    finally:
        conn.close()
    assert rows > 0, "normalize must publish work_similar together with the catalog"


def _enrich(raw):
    """Run the read-only half of the pipeline; return (records, by_isbn, by_key,
    work_of)."""
    paths = sorted((raw / "records").rglob("*.json"))
    aux = normalize._load_aux()
    canon, by_isbn, by_key, _, work_of = normalize._prepass(paths)
    records = {r["ppn"]: r
               for r in normalize.iter_records(paths, aux, canon, work_of)}
    return records, by_isbn, by_key, work_of


def test_reclaim_disk_checkpoints_live_wal_instead_of_deleting(tmp_path):
    """_reclaim_disk must not unlink a live DB's -wal/-shm — that can corrupt an
    open reader. It folds the WAL back in via a TRUNCATE checkpoint instead, so the
    DB stays intact and its -wal shrinks to nothing."""
    import sqlite3

    db_path = tmp_path / "catalog.db"
    conn = db.connect(db_path)  # WAL mode
    db.init_db(conn)
    conn.execute("INSERT INTO editions(ppn, work_id, title) VALUES ('1', '1', 'Een')")
    conn.execute("INSERT INTO editions(ppn, work_id, title) VALUES ('2', '2', 'Twee')")
    conn.commit()
    # Keep the writer connection open (idle) so the -wal isn't auto-checkpointed
    # away on close — this is the "live DB with an active WAL" state we're testing.
    wal = tmp_path / "catalog.db-wal"
    assert wal.exists() and wal.stat().st_size > 0, "expected a non-empty WAL to reclaim"

    normalize._reclaim_disk(db_path, tmp_path)

    # WAL folded back in and truncated (never deleted out from under the reader).
    assert (not wal.exists()) or wal.stat().st_size == 0
    # DB still opens and the rows are all there.
    assert conn.execute("SELECT COUNT(*) FROM editions").fetchone()[0] == 2
    conn.close()
    ro = sqlite3.connect(db_path)
    assert ro.execute("SELECT COUNT(*) FROM editions").fetchone()[0] == 2
    ro.close()


def test_publishers_canonicalised_to_most_common(raw):
    records, _, _, _ = _enrich(raw)
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


def test_ereader_precedence_per_record_then_side_file_then_prior():
    """The flag is resolved detail-flag > side-file > last-known DB value."""
    T = normalize._transform
    # per-title detail flag wins even when the side-file disagrees (or lacks it)
    assert T({"ppn": "9", "format": "ebook", "ereader": 1}, set(), True,
             {}, {}, {}, {})["ereader"] == 1
    assert T({"ppn": "9", "format": "ebook", "ereader": 0}, {"9"}, True,
             {}, {}, {}, {})["ereader"] == 0
    # no detail flag -> fall back to the side-file membership set
    assert T({"ppn": "9", "format": "ebook"}, {"9"}, True, {}, {}, {}, {})["ereader"] == 1
    assert T({"ppn": "8", "format": "ebook"}, {"9"}, True, {}, {}, {}, {})["ereader"] == 0
    # side-file missing (have_ereader False) -> preserve the live DB's prior value,
    # and leave genuinely-unknown titles unset rather than forcing them to 0
    assert T({"ppn": "9", "format": "ebook"}, set(), False,
             {}, {}, {}, {"9": 1})["ereader"] == 1
    assert T({"ppn": "x", "format": "ebook"}, set(), False,
             {}, {}, {}, {}).get("ereader") is None


def test_normalize_preserves_ereader_when_side_file_vanishes(raw, tmp_path):
    """A rebuild with the ereader side-file missing must not blank the facet — it
    keeps each e-book's last-known flag from the live DB (the bug behind the 0)."""
    db_path = tmp_path / "out.db"
    normalize.normalize(raw, db_path)                      # side-file has ["1"]
    conn = db.connect(db_path)
    assert conn.execute("SELECT ereader FROM editions WHERE ppn='1'").fetchone()[0] == 1
    assert conn.execute("SELECT ereader FROM editions WHERE ppn='2'").fetchone()[0] == 0
    conn.close()

    (raw / "ereader.json").unlink()                        # side-file lost
    normalize.normalize(raw, db_path)
    conn = db.connect(db_path)
    assert conn.execute("SELECT ereader FROM editions WHERE ppn='1'").fetchone()[0] == 1
    assert conn.execute("SELECT ereader FROM editions WHERE ppn='2'").fetchone()[0] == 0
    conn.close()


def test_match_lists_by_isbn_then_title(raw):
    _, by_isbn, by_key, work_of = _enrich(raw)
    _write(raw / "lists" / "t.json", {"slug": "t", "name": "T", "items": [
        {"position": 1, "isbn": "9789021400017", "title": "x", "author": "y"},
        {"position": 2, "title": "Boek Twee", "author": "Anna Vrij"},
        {"position": 3, "title": "Bestaat Niet", "author": "Niemand"},
    ]})
    items = normalize.match_lists(by_isbn, by_key, work_of)[0]["items"]
    assert items[0]["ppn"] == "1"   # matched on ISBN (punctuation stripped)
    assert items[1]["ppn"] == "2"   # matched on title + author surname
    assert items[2]["ppn"] is None  # no match -> stays in list_items, greyed out


def test_normalize_end_to_end_builds_db(raw, tmp_path):
    _write(raw / "lists" / "t.json", {"slug": "t", "name": "T",
           "items": [{"position": 1, "isbn": "9789021400017"}]})
    db_path = tmp_path / "out.db"
    stats = normalize.normalize(raw, db_path)
    assert stats["editions"] == 6
    assert stats["works"] == 5          # 5 + 6 are one book in two editions
    conn = db.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM work_lists").fetchone()[0] == 1  # isbn match
    assert conn.execute(
        "SELECT publisher FROM works WHERE work_id='2'").fetchone()[0] == "Querido, Amsterdam"
    conn.close()


def test_editions_of_one_work_share_a_work_id(raw):
    """The grouping happens in the prepass, so every record is written already
    carrying it — and the representative is the e-book, whose PPN becomes the
    work_id (so its existing /book/{ppn} URL keeps its meaning)."""
    records, _, _, work_of = _enrich(raw)
    assert records["5"]["work_id"] == records["6"]["work_id"] == "5"
    assert work_of["6"] == "5"
    # the unrelated records stay their own work
    assert [records[p]["work_id"] for p in ("1", "2", "3", "4")] == ["1", "2", "3", "4"]


def test_list_slot_matched_on_the_audiobooks_isbn_lands_on_the_work(raw):
    """A curated-list slot used to match exactly one PPN — whichever edition's ISBN
    was seen first — and the other edition silently lost the ribbon."""
    _, by_isbn, by_key, work_of = _enrich(raw)
    _write(raw / "lists" / "t.json", {"slug": "t", "name": "T", "items": [
        {"position": 1, "isbn": "9789021400060", "title": "Boek Vijf"},
    ]})
    items = normalize.match_lists(by_isbn, by_key, work_of)[0]["items"]
    assert items[0]["ppn"] == "5"       # the audiobook's ISBN, the work's id


def test_list_slots_are_deduped_per_work(raw):
    """Two slots naming the two editions of one book are one book — the second must
    not be matched a second time."""
    _, by_isbn, by_key, work_of = _enrich(raw)
    _write(raw / "lists" / "t.json", {"slug": "t", "name": "T", "items": [
        {"position": 1, "title": "Boek Vijf", "author": "Els Vijf"},
        {"position": 2, "isbn": "9789021400060", "title": "Boek Vijf"},
    ]})
    items = normalize.match_lists(by_isbn, by_key, work_of)[0]["items"]
    assert items[0]["ppn"] == "5"
    assert items[1]["ppn"] is None


def test_overrides_split_forces_two_works(raw, monkeypatch, tmp_path):
    """When the data is wrong in a way no rule should guess, curate the exception:
    a split pair forces the *second* ppn out into its own work."""
    monkeypatch.setattr(normalize, "RAW_DIR", tmp_path)
    _write(tmp_path / "work_overrides.json", {"split": [["5", "6"]]})
    _, _, _, work_of = _enrich(raw)
    assert work_of["5"] == "5" and work_of["6"] == "6"
