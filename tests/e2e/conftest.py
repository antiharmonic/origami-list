"""Browser tests: the real app served by uvicorn in a thread, driven by Playwright's Chromium.

Each test gets the temp books/data folders from tests/conftest.py (`dirs`), a server started on a free
port, and pytest-playwright's `page`. Gemini is faked in-process (the `gemini` fixture), so nothing
leaves the machine.
"""
import threading
import time

import pytest
import uvicorn

from app import db
from tests.conftest import make_pdf

READER = "Reader.pdf"  # a 6-page book with a text layer, for the viewer


@pytest.fixture
def server(dirs):
    """The app on http://127.0.0.1:<free port>, with fresh data, for one test."""
    make_pdf(dirs[0] / READER, pages=6, text=["Origami diagrams", "Fold in half."])
    from app import books
    books.scan(force=True)
    from app.main import app
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not srv.started:
        if time.monotonic() > deadline:
            raise RuntimeError("server didn't start")
        time.sleep(0.02)
    port = srv.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(5)


@pytest.fixture
def conn(server):
    """A connection to the server's (already migrated) database, for setting up and checking data."""
    c = db.connect()
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _timeouts(page):
    page.set_default_timeout(10_000)


def open_book(page, base, path=READER, **params):
    """Open the book page and wait until the PDF.js viewer has the document."""
    from urllib.parse import urlencode
    page.goto(f"{base}/library/book?{urlencode({'path': path, **params})}")
    page.wait_for_function(
        "() => { const app = document.getElementById('viewer').contentWindow.PDFViewerApplication;"
        " return app && app.pagesCount > 0; }")


def go_to_page(page, n):
    """Move the embedded viewer to page n, as paging through the book would."""
    page.evaluate("n => document.getElementById('viewer').contentWindow.PDFViewerApplication.page = n", n)


def viewer_page(page):
    return page.evaluate("() => document.getElementById('viewer').contentWindow.PDFViewerApplication.page")
