"""The book page (reader) and serving the PDFs.

- Paths from the browser can never reach outside the books folder.
- Books are served over HTTP with page deep links, and pages render as thumbnails.
- The book page embeds the PDF.js viewer (not for a missing file) beside the panel.
- Quick add creates a model on the viewer's page and refreshes the panel's model list, counts and status.
"""
import shutil

import pytest

from app import books, db
from tests.helpers import NATURE


def test_resolve_blocks_traversal():
    assert books.resolve("sub/Kato - Nature Study.pdf")
    assert books.resolve("../secret.pdf") is None
    assert books.resolve("/etc/passwd") is None
    assert books.resolve("sub/../../secret.pdf") is None


def test_pages_and_book_serving(client):
    assert client.get("/").status_code == 200
    assert client.get("/models/new").status_code == 200
    assert client.get("/books-admin").status_code == 200
    r = client.get("/books/sub/Kato%20-%20Nature%20Study.pdf")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert client.get("/books/..%2Fsecret.pdf").status_code == 404
    assert client.get("/books/sub/..%2F..%2Fsecret.pdf").status_code == 404


@pytest.mark.skipif(not shutil.which("pdftoppm"), reason="pdftoppm not installed")
def test_thumbnail(client):
    r = client.get("/thumb", params={"book": "sub/Kato - Nature Study.pdf", "page": 1})
    assert r.status_code == 200 and r.content.startswith(b"\x89PNG")
    assert client.get("/thumb", params={"book": "sub/Kato - Nature Study.pdf", "page": 99}).status_code == 404
    assert client.get("/thumb", params={"book": "../secret.pdf", "page": 1}).status_code == 404


def test_book_page(client, conn):
    assert client.get("/library/book", params={"path": "../secret.pdf"}).status_code == 404
    assert client.get("/library/book", params={"path": "missing.pdf"}).status_code == 404

    page = client.get("/library/book", params={"path": NATURE}).text
    assert "Models from this book (0)" in page

    r = client.post("/library/book", data={"path": NATURE, "title": "Nature Study", "designers": "Shuki Kato",
                                           "notes": "yacht!", "page_offset": "6", "cover_page": "2"},
                    follow_redirects=False)
    assert r.status_code == 303
    book = db.get_book_by_path(conn, NATURE)
    assert (book["title"], book["designers"], book["page_offset"], book["cover_page"]) == ("Nature Study", "Shuki Kato", 6, 2)

    client.post("/models", data={"name": "Dragonfly", "book": NATURE, "page": "2"})
    page = client.get("/library/book", params={"path": NATURE}).text
    assert "Models from this book (1)" in page and "Dragonfly" in page
    # Designers search in the library.
    assert "Showing 1 of 2" in client.get("/library", params={"filtered": 1, "status": db.BOOK_STATUSES, "q": "kato"}).text


def test_book_page_embeds_viewer(client):
    page = client.get("/library/book", params={"path": NATURE}).text
    assert "/pdfjs/web/viewer.html?file=%2Fbooks%2Fsub%2FKato%2520-%2520Nature%2520Study.pdf" in page
    assert 'id="quick-add"' in page and "Relink to another file" in page and "Save details" in page
    page = client.get("/library/book", params={"path": NATURE, "page": "5"}).text
    assert 'Study.pdf#page=5"' in page and 'name="page" type="number" min="1" value="5"' in page


def test_missing_book_page_has_no_viewer(client, dirs):
    client.post("/models", data={"name": "Dragonfly", "book": NATURE, "page": "2"})
    (dirs[0] / NATURE).unlink()
    books.scan(force=True)
    page = client.get("/library/book", params={"path": NATURE}).text
    assert "/pdfjs/" not in page and 'id="quick-add"' not in page
    assert '<details class="relink book-section" open>' in page
    assert client.post("/library/book/models", data={"path": NATURE, "name": "Crane"}).status_code == 400


def test_quick_add_from_viewer(client, conn):
    r = client.post("/library/book/models", data={"path": NATURE, "name": "Rhino", "designer": "Kato",
                                                  "page": "7", "complexity": "4", "status": "want",
                                                  "new_study": "Rhinoceros"})
    assert r.status_code == 200
    m = db.list_models(conn)[0]
    book = db.get_book_by_path(conn, NATURE)
    assert (m["name"], m["designer"], m["page"], m["complexity"], m["book_id"]) == ("Rhino", "Kato", 7, 4, book["id"])
    assert book["status"] == "browsing"
    study = db.study_by_name(conn, "rhinoceros", create=False)
    assert db.parse_studies(m["studies"]) == [(study, "Rhinoceros")]
    # The book's model list, plus the header badge, study chips (new study ticked) and book status out of band.
    assert 'id="book-models"' in r.text and "Models from this book (1)" in r.text and "Added “Rhino” (p.7)" in r.text
    assert 'id="todo-count"' in r.text and 'id="study-chips" class="chips" hx-swap-oob="true"' in r.text
    assert f'value="{study}" checked' in r.text
    assert 'id="book-status"' in r.text and 'value="browsing" class="primary"' in r.text

    assert client.post("/library/book/models", data={"path": NATURE, "name": " "}).status_code == 400
    assert client.post("/library/book/models", data={"path": "nope.pdf", "name": "X"}).status_code == 404
    assert client.post("/library/book/models", data={"path": "../secret.pdf", "name": "X"}).status_code == 404
