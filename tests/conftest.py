"""Shared fixtures: a hermetic on-disk catalog built from ``sampledata``.

These let the db / queries / web tests run anywhere, with no dependency on the
real ~280 MB ``data/catalog.db``.
"""

from __future__ import annotations

import pytest
import sampledata

from obc import db


def _build_catalog(path) -> None:
    conn = db.connect(path)
    db.bulk_load(conn, sampledata.records(), sampledata.lists())
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # so mode=ro readers see it
    conn.close()


@pytest.fixture(scope="session")
def catalog_db(tmp_path_factory):
    """Path to a freshly built fixture catalog (one per test session)."""
    path = tmp_path_factory.mktemp("catalog") / "test.db"
    _build_catalog(path)
    return path


@pytest.fixture
def ro_conn(catalog_db):
    """A read-only connection to the fixture catalog (with the ``fold`` fn)."""
    from obc.web import queries
    conn = queries.connect_ro(catalog_db)
    yield conn
    conn.close()


@pytest.fixture
def client(catalog_db, monkeypatch):
    """A FastAPI TestClient wired to the fixture catalog, with the Wikipedia
    author-bio lookup stubbed out (no network in tests)."""
    from fastapi.testclient import TestClient

    from obc.web import app as appmod

    monkeypatch.setattr(appmod, "DB_PATH", catalog_db)
    monkeypatch.setattr(appmod, "author_bio", lambda name: None)
    appmod._facets_cache.update(key=None, data=None)
    # No `with`: skip the lifespan so the optional refresh scheduler never starts.
    yield TestClient(appmod.app)
    appmod._facets_cache.update(key=None, data=None)
