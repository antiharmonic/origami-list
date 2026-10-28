import importlib.util

import pytest
from fastapi.testclient import TestClient

from app import books, config, db, translate

# The browser tests need Playwright, which only the e2e image has (docker compose --profile test run --rm e2e).
collect_ignore = [] if importlib.util.find_spec("playwright") else ["e2e"]


def make_pdf(path, pages=2, text=None):
    """Write a minimal valid PDF: blank pages, or with `text` (a list of lines) on every page as a real text layer."""
    kids = " ".join(f"{3 + i} 0 R" for i in range(pages))
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>"]
    if text:
        font, content = 3 + pages, 4 + pages
        objs += [f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 300] /Resources << /Font << /F1 {font} 0 R >> >> "
                 f"/Contents {content} 0 R >>"] * pages
        lines = " ".join(f"({line}) '" for line in text)
        stream = f"BT /F1 6 Tf 8 TL 5 290 Td {lines} ET"
        objs += ["<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
                 f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream"]
    else:
        objs += ["<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 300] >>"] * pages
    out, offsets = b"%PDF-1.4\n", []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{body}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(out)


@pytest.fixture(autouse=True)
def dirs(tmp_path, monkeypatch):
    books_dir, data_dir = tmp_path / "books", tmp_path / "data"
    make_pdf(books_dir / "Lang - Origami Insects (z-library.sk, 1lib.sk).pdf")
    make_pdf(books_dir / "sub" / "Kato - Nature Study.pdf")
    (tmp_path / "secret.pdf").write_bytes(b"%PDF-1.4 secret")
    data_dir.mkdir()
    monkeypatch.setattr(config, "BOOKS_DIR", books_dir)
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    books.scan(force=True)
    return books_dir, data_dir


@pytest.fixture
def conn(dirs):
    c = db.connect()
    db.migrate(c)
    yield c
    c.close()


@pytest.fixture
def client(dirs):
    from app.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture
def gemini(monkeypatch):
    """A fake Gemini: records request bodies, answers with `reply` (or raises `fail`). No network."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    fake = {"calls": [], "reply": "**14.** Fold the corner.\n→ only the top layer", "fail": None}

    def call(body, timeout=None):
        fake["calls"].append(body)
        if fake["fail"]:
            raise translate.TranslateError(fake["fail"])
        return fake["reply"], "fake-model"
    monkeypatch.setattr(translate, "call", call)
    monkeypatch.setattr(translate, "render_page", lambda path, page: b"\x89PNG fake")
    return fake
