import pytest
from fastapi.testclient import TestClient

from app import books, config, db


def make_pdf(path, pages=2):
    """Write a minimal valid PDF with blank pages."""
    kids = " ".join(f"{3 + i} 0 R" for i in range(pages))
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>"]
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
