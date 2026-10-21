import shutil

import pytest

from app import books, db, import_csv

ORIGINAL_CSV = """model,author,complexity,suggested_paper,book,file,page,folded,notes
Simple Dragonfly,Shuki Kato,3,foil,Origami Nature Study,'/home/someone/Books/Kato - Nature Study.pdf,24,Yes,Nice.
Mystery,Someone,,,,'/nowhere/missing.pdf,5,No,
,No Name,,,,,,,
"""


def order(conn):
    return [m["name"] for m in db.list_models(conn)]


def test_move_between_neighbours(conn):
    ids = [db.create_model(conn, {"name": n}) for n in "ABCD"]
    db.move_model(conn, ids[3], prev_id=ids[0], next_id=ids[1])
    assert order(conn) == ["A", "D", "B", "C"]
    db.move_model(conn, ids[0], prev_id=ids[2], next_id=None)
    assert order(conn) == ["D", "B", "C", "A"]
    db.move_model(conn, ids[0], prev_id=None, next_id=ids[3])
    assert order(conn) == ["A", "D", "B", "C"]


def test_move_renormalizes_when_gap_is_tiny(conn):
    a, b, c = (db.create_model(conn, {"name": n}) for n in "ABC")
    for _ in range(60):  # repeatedly halving the gap between A and B
        db.move_model(conn, c, prev_id=a, next_id=b)
        db.move_model(conn, b, prev_id=a, next_id=c)
    assert order(conn)[0] == "A"
    positions = [m["position"] for m in db.list_models(conn)]
    assert len(set(positions)) == 3


def test_create_at_top(conn):
    db.create_model(conn, {"name": "A"})
    db.create_model(conn, {"name": "B"}, at_top=True)
    assert order(conn) == ["B", "A"]


def test_resolve_blocks_traversal():
    assert books.resolve("sub/Kato - Nature Study.pdf")
    assert books.resolve("../secret.pdf") is None
    assert books.resolve("/etc/passwd") is None
    assert books.resolve("sub/../../secret.pdf") is None


def test_search_and_clean_title():
    assert books.search("kato nature") == ["sub/Kato - Nature Study.pdf"]
    assert books.clean_title("Lang - Origami Insects (z-library.sk, 1lib.sk).pdf") == "Lang - Origami Insects"


def test_import_original_format(conn):
    imported, warnings = import_csv.import_text(conn, ORIGINAL_CSV)
    assert imported == 2
    assert len(warnings) == 2
    dragonfly, mystery = db.list_models(conn)
    assert dragonfly["book_path"] == "sub/Kato - Nature Study.pdf"
    assert (dragonfly["page"], dragonfly["status"], dragonfly["paper"], dragonfly["complexity"]) == (24, "folded", "foil", 3)
    assert mystery["book_id"] is None and mystery["status"] == "want"
    assert mystery["notes"].startswith("[Book: ")


def test_export_round_trip(client, conn):
    import_csv.import_text(conn, ORIGINAL_CSV)
    exported = client.get("/export.csv").text
    import_csv.import_text(conn, exported)
    names = order(conn)
    assert names.count("Simple Dragonfly") == 2
    second = db.list_models(conn)[2]
    assert second["book_path"] == "sub/Kato - Nature Study.pdf" and second["status"] == "folded"


def test_pages_and_book_serving(client):
    assert client.get("/").status_code == 200
    assert client.get("/models/new").status_code == 200
    assert client.get("/books-admin").status_code == 200
    r = client.get("/books/sub/Kato%20-%20Nature%20Study.pdf")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert client.get("/books/..%2Fsecret.pdf").status_code == 404
    assert client.get("/books/sub/..%2F..%2Fsecret.pdf").status_code == 404


def test_add_edit_status_and_filters(client, conn):
    r = client.post("/models", data={"name": "Crane", "book": "sub/Kato - Nature Study.pdf", "page": "2",
                                     "complexity": "2", "status": "want"}, follow_redirects=False)
    assert r.status_code == 303
    m = db.list_models(conn)[0]
    assert (m["name"], m["page"], m["complexity"]) == ("Crane", 2, 2)

    # Printed page numbers get the book's offset added.
    db.update_book(conn, m["book_id"], "Nature Study", 1)
    client.post(f"/models/{m['id']}", data={"name": "Crane", "book": m["book_path"], "page": "1", "printed": "1",
                                            "status": "want"})
    assert db.get_model(conn, m["id"])["page"] == 2

    assert "Crane" in client.get("/").text
    client.post(f"/models/{m['id']}/status", data={"status": "folded"})
    assert "Crane" not in client.get("/").text  # default filter hides folded
    assert "Crane" in client.get("/?filtered=1&status=folded").text


def test_bad_book_rejected(client):
    r = client.post("/models", data={"name": "X", "book": "../secret.pdf"})
    assert r.status_code == 400


@pytest.mark.skipif(not shutil.which("pdftoppm"), reason="pdftoppm not installed")
def test_thumbnail(client):
    r = client.get("/thumb", params={"book": "sub/Kato - Nature Study.pdf", "page": 1})
    assert r.status_code == 200 and r.content.startswith(b"\x89PNG")
    assert client.get("/thumb", params={"book": "sub/Kato - Nature Study.pdf", "page": 99}).status_code == 404
    assert client.get("/thumb", params={"book": "../secret.pdf", "page": 1}).status_code == 404
