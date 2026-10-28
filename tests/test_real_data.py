"""The app against a copy of the real data (opt-in; see TESTING.md).

    docker compose run --rm --no-deps -e REAL_DATA=1 app pytest -q tests/test_real_data.py

Run it with a freshly built image before `docker compose up -d`, so new migrations and template changes are
tried on the real list first. It copies /data/origami.db and /data/ocr.db with SQLite's backup API (safe
while the app runs) and only ever touches the copies; the real books folder is read-only.

- Migrations bring the real database to the latest version without losing models, books, studies, glossary
  entries or translations.
- Every page the user relies on renders on the real data: the list (with the right count), every model's
  edit page, the library, every book with models or suggestions (including missing files), every study,
  search, the glossary, import/export and the CSV export.
"""
import csv
import io
import os
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import books, config, db

pytestmark = pytest.mark.skipif(os.environ.get("REAL_DATA") != "1", reason="opt-in: REAL_DATA=1 (see TESTING.md)")

REAL_DATA = Path(os.environ.get("REAL_DATA_DIR", "/data"))
REAL_BOOKS = config.BOOKS_DIR  # read at import, before any test fixture points config elsewhere
COUNTED = ["models", "books", "studies", "model_studies", "glossary", "translations", "suggestions", "notes"]


@pytest.fixture
def dirs():
    """Replaces conftest's temp folders: this module sets up its own (a copy of the real data)."""


def _copy(src, dest):
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    target = sqlite3.connect(dest)
    with target:
        source.backup(target)
    source.close()
    target.close()


def _counts(path):
    c = sqlite3.connect(path)
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    out = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in COUNTED if t in tables}
    c.close()
    return out


@pytest.fixture(scope="module")
def real(tmp_path_factory):
    if not (REAL_DATA / "origami.db").exists():
        pytest.skip(f"no database at {REAL_DATA}")
    before = {f: (REAL_DATA / f).stat().st_mtime for f in ("origami.db", "ocr.db") if (REAL_DATA / f).exists()}
    copy = tmp_path_factory.mktemp("real-data")
    _copy(REAL_DATA / "origami.db", copy / "origami.db")
    if (REAL_DATA / "ocr.db").exists():
        _copy(REAL_DATA / "ocr.db", copy / "ocr.db")
    counts = _counts(copy / "origami.db")

    mp = pytest.MonkeyPatch()
    mp.setattr(config, "DATA_DIR", copy)
    mp.setattr(config, "BOOKS_DIR", REAL_BOOKS)
    books.scan(force=True)
    from app.main import app
    with TestClient(app) as client:  # startup runs the migrations on the copy
        conn = db.connect()
        yield {"client": client, "conn": conn, "counts": counts}
        conn.close()
    mp.undo()

    after = {f: (REAL_DATA / f).stat().st_mtime for f in before}
    assert after == before, "the real data files changed (were you using the app during the test? then rerun)"


def test_migrations_keep_everything(real):
    conn = real["conn"]
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
    now = _counts(config.DATA_DIR / "origami.db")
    for table, n in real["counts"].items():
        if table == "glossary":
            assert now[table] >= n  # startup may seed new entries, never removes any
        else:
            assert now[table] == n, table


def test_list_and_every_model(real):
    client, conn = real["client"], real["conn"]
    total = conn.execute("SELECT COUNT(*) FROM models").fetchone()[0]
    page = client.get("/", params={"filtered": 1, "status": db.STATUSES})
    assert page.status_code == 200 and f"Showing {total} of {total}" in page.text
    for (model_id,) in conn.execute("SELECT id FROM models"):
        assert client.get(f"/models/{model_id}/edit").status_code == 200, model_id


def test_library_and_book_pages(real):
    client, conn = real["client"], real["conn"]
    assert client.get("/library").status_code == 200
    paths = [r[0] for r in conn.execute("""SELECT path FROM books WHERE id IN (SELECT book_id FROM models)
                                           OR id IN (SELECT book_id FROM suggestions)""")]
    assert paths
    for path in paths:
        assert client.get("/library/book", params={"path": path}).status_code == 200, path


def test_studies_search_glossary_and_export(real):
    client, conn = real["client"], real["conn"]
    assert client.get("/studies").status_code == 200
    for (study_id,) in conn.execute("SELECT id FROM studies"):
        assert client.get(f"/studies/{study_id}").status_code == 200, study_id
    assert client.get("/glossary").status_code == 200
    assert client.get("/books-admin").status_code == 200
    search = client.get("/search", params={"q": "crane"})
    assert search.status_code == 200 and "In your books" in search.text
    rows = list(csv.DictReader(io.StringIO(client.get("/export.csv").text)))
    assert len(rows) == conn.execute("SELECT COUNT(*) FROM models").fetchone()[0]
