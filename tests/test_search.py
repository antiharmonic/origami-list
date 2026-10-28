"""Library search: reading every page (text layer or OCR) and searching it.

- A page's text comes from the PDF's own text layer when it has one, otherwise from OCR.
- The OCR worker reads the priority book first, then the newest; a changed file is read again; pausing stops it part way.
- A page that fails to read is retried, never searchable, and doesn't count as read; an unreadable file is an error, not "done".
- Search groups hits by book and collapses consecutive pages into runs, with highlighted snippets.
- Japanese works: spaces OCR puts between Japanese characters are removed, short words are found, and サイ doesn't match サイズ.
- Search includes glossary alternatives (rhinoceros -> サイ), the list's own models, and skips hidden books.
"""
import pytest

from app import books, ocr
from tests.conftest import make_pdf
from tests.helpers import INSECTS, NATURE, RHINO_TEXT, add_ocr_pages, needs_tesseract


def test_page_text_prefers_the_text_layer(dirs):
    make_pdf(dirs[0] / "Texty.pdf", pages=1, text=RHINO_TEXT)
    source, text = ocr.page_text(dirs[0] / "Texty.pdf", 1)
    assert source == "text" and "Rhinoceros" in text

@needs_tesseract
def test_page_text_falls_back_to_ocr(dirs):
    source, text = ocr.page_text(dirs[0] / "sub" / "Kato - Nature Study.pdf", 1)
    assert source == "ocr" and text.strip() == ""

@needs_tesseract
def test_worker_queue_priority_and_resync(dirs):
    conn = ocr.connect()
    make_pdf(dirs[0] / "Texty.pdf", pages=2, text=RHINO_TEXT)
    present = books.scan(force=True)
    ocr.sync(conn, ocr.sizes_and_mtimes(present))
    added = {NATURE: 3, INSECTS: 2, "Texty.pdf": 1}
    assert ocr.next_book(conn, present, added) == NATURE  # newest first
    ocr.set_control(conn, priority_path="Texty.pdf")
    assert ocr.next_book(conn, present, added) == "Texty.pdf"

    ocr.process_book(conn, "Texty.pdf", workers=1, log=lambda m: None)
    row = conn.execute("SELECT * FROM ocr_books WHERE path = 'Texty.pdf'").fetchone()
    assert (row["state"], row["pages"], row["done_pages"]) == ("done", 2, 2)
    assert ocr.get_control(conn)["priority_path"] is None
    assert ocr.search(conn, ["rhinoceros"], {"Texty.pdf"})[0]["runs"][0]["start"] == 1
    assert ocr.book_status(conn, NATURE, set(present), added)["position"] == 1
    assert ocr.progress(conn, present)["done"] == 1

    # A changed file is read again from scratch.
    make_pdf(dirs[0] / "Texty.pdf", pages=3, text=RHINO_TEXT)
    ocr.sync(conn, ocr.sizes_and_mtimes(present))
    row = conn.execute("SELECT * FROM ocr_books WHERE path = 'Texty.pdf'").fetchone()
    assert row["state"] == "queued" and conn.execute("SELECT COUNT(*) FROM ocr_pages").fetchone()[0] == 0

    # Pausing stops a book part way; it carries on later.
    ocr.set_control(conn, paused=1)
    ocr.process_book(conn, "Texty.pdf", workers=1, log=lambda m: None)
    assert conn.execute("SELECT state FROM ocr_books WHERE path = 'Texty.pdf'").fetchone()[0] == "queued"
    conn.close()


def test_collapse_runs_and_snippet():
    assert ocr.collapse_runs([12, 3, 4, 5, 9, 11]) == [(3, 5), (9, 9), (11, 12)]
    assert str(ocr.snippet("a <b> Rhinoceros here", ["rhinoceros"])) == "a &lt;b&gt; <mark>Rhinoceros</mark> here"


def test_search_page(client, conn):
    add_ocr_pages([
        (NATURE, 3, "Rhinoceros step 1"), (NATURE, 4, "Rhinoceros step 9"), (NATURE, 7, "the rhinoceros again"),
        (INSECTS, 2, "サイの折り方"), (INSECTS, 5, "Beetle"),
    ])
    page = client.get("/search", params={"q": "rhinoceros"}).text
    # The glossary adds the Japanese (single characters like 犀 are skipped: too noisy to search alone).
    assert "Also searching: サイ" in page and "犀" not in page.split("Also searching")[1].split("</p>")[0]
    assert "p.3–4" in page and "p.7" in page and "<mark>Rhinoceros</mark>" in page and "<mark>サイ</mark>" in page
    assert "page=3" in page and "name=Rhinoceros+%28%E3%82%B5%E3%82%A4%29" in page  # + Add "Rhinoceros (サイ)"
    assert "Text search:" in page

    # Two-character Japanese is matched by scanning, and hidden books drop out.
    assert "Origami Insects" in client.get("/search", params={"q": "サイ"}).text
    client.post("/library/hide", data={"path": INSECTS, "hidden": "1"})
    assert "Origami Insects" not in client.get("/search", params={"q": "サイ"}).text

    # Models in the list match too.
    client.post("/models", data={"name": "Rhinoceros", "book": NATURE, "page": "3"})
    assert "In your list (1)" in client.get("/search", params={"q": "rhino"}).text


def test_page_text_and_ocr_controls(client):
    add_ocr_pages([(NATURE, 2, "Crane <diagram>")])
    assert "Crane &lt;diagram&gt;" in client.get("/api/page-text", params={"path": NATURE, "page": 2}).text
    assert "hasn't been read yet" in client.get("/api/page-text", params={"path": NATURE, "page": 1}).text
    assert client.get("/api/page-text", params={"path": "../secret.pdf", "page": 1}).status_code == 404

    client.post("/ocr/priority", data={"path": NATURE})
    client.post("/ocr/pause", data={"paused": "1"})
    conn = ocr.connect()
    control = ocr.get_control(conn)
    assert (control["paused"], control["priority_path"]) == (1, NATURE)
    conn.close()
    assert "paused" in client.get("/library").text and "Resume" in client.get("/library").text


def test_japanese_spacing_and_short_katakana(client):
    assert ocr.normalize("シロ サイ の 折り 方\nRhino ceros 2 枚") == "シロサイの折り方\nRhino ceros 2 枚"
    add_ocr_pages([(NATURE, 3, ocr.normalize("シロ サイ")), (INSECTS, 4, ocr.normalize("A4 サイ ズ の 紙"))])
    page = client.get("/search", params={"q": "サイ"}).text
    assert "Nature Study" in page and "Origami Insects" not in page  # サイズ ("size") isn't a rhino


def test_failed_ocr_page_is_retried_not_indexed(dirs, monkeypatch):
    conn = ocr.connect()
    make_pdf(dirs[0] / "Texty.pdf", pages=2, text=RHINO_TEXT)
    present = books.scan(force=True)
    ocr.sync(conn, ocr.sizes_and_mtimes(present))
    real = ocr.page_text

    def flaky(full, page):
        if page == 2:
            raise RuntimeError("tesseract crashed")
        return real(full, page)
    monkeypatch.setattr(ocr, "page_text", flaky)

    ocr.process_book(conn, "Texty.pdf", workers=1, log=lambda m: None)
    row = conn.execute("SELECT * FROM ocr_books WHERE path = 'Texty.pdf'").fetchone()
    assert (row["state"], row["done_pages"]) == ("queued", 1)  # not finished: page 2 will be retried
    page = ocr.get_page(conn, "Texty.pdf", 2)
    assert (page["source"], page["text"], page["attempts"]) == ("error", "", 1) and "crashed" in page["error"]
    assert not ocr.search(conn, ["crashed"], {"Texty.pdf"})

    # It keeps failing: after MAX_ATTEMPTS the book is done, with the page reported as failed.
    for _ in range(ocr.MAX_ATTEMPTS - 1):
        ocr.process_book(conn, "Texty.pdf", workers=1, log=lambda m: None)
    status = ocr.book_status(conn, "Texty.pdf", set(present), {})
    assert (status["state"], status["done_pages"], status["failed"]) == ("done", 1, 1)

    # A later success (after the file is re-queued) clears the error and is counted once.
    monkeypatch.setattr(ocr, "page_text", real)
    conn.execute("UPDATE ocr_pages SET attempts = 0 WHERE page = 2")
    conn.commit()
    ocr.process_book(conn, "Texty.pdf", workers=1, log=lambda m: None)
    status = ocr.book_status(conn, "Texty.pdf", set(present), {})
    assert (status["state"], status["done_pages"], status["failed"]) == ("done", 2, 0)
    assert ocr.get_page(conn, "Texty.pdf", 2)["error"] is None
    conn.close()


def test_unreadable_pdf_is_an_error_not_done(dirs):
    conn = ocr.connect()
    (dirs[0] / "Broken.pdf").write_bytes(b"not a pdf")
    present = books.scan(force=True)
    ocr.sync(conn, ocr.sizes_and_mtimes(present))
    with pytest.raises(Exception):
        ocr.page_count(dirs[0] / "Broken.pdf")
    ocr.process_book(conn, "Broken.pdf", workers=1, log=lambda m: None)
    assert conn.execute("SELECT state FROM ocr_books WHERE path = 'Broken.pdf'").fetchone()[0] == "error"
    conn.close()
