import re
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
    db.update_book_meta(conn, m["book_id"], {"page_offset": 1})
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


def test_save_and_new_carries_designer_and_book(client, conn):
    r = client.post("/models", data={"name": "Beetle", "designer": "Lang", "book": "sub/Kato - Nature Study.pdf",
                                     "page": "2", "position": "top", "then": "new"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/models/new?")
    form = client.get(r.headers["location"]).text
    assert "Saved “Beetle”" in form
    assert 'value="Lang"' in form and 'value="sub/Kato - Nature Study.pdf"' in form
    assert 'value="top" checked' in form

    r = client.post("/models", data={"name": "Ant"}, follow_redirects=False)
    assert r.headers["location"] == "/"
    assert "New from" in client.get("/").text


def test_offset_on_form_is_saved_to_book(client, conn):
    book = "sub/Kato - Nature Study.pdf"
    assert client.get("/api/book-offset", params={"book": book}).text == "0"
    client.post("/models", data={"name": "Frog", "book": book, "page": "1", "printed": "1", "offset": "1"})
    m = db.list_models(conn)[0]
    assert m["page"] == 2 and m["book_offset"] == 1
    assert client.get("/api/book-offset", params={"book": book}).text == "1"

    # The next form for this book starts with the saved offset.
    form = client.get("/models/new", params={"book": book}).text
    assert re.search(r'name="offset"[^>]*value="1"', form)
    assert re.search(r'name="printed"[^>]*checked', form)
    # Books without an offset, and edits (page is already a PDF page), start unticked.
    assert not re.search(r'name="printed"[^>]*checked', client.get("/models/new").text)
    assert not re.search(r'name="printed"[^>]*checked', client.get(f"/models/{m['id']}/edit").text)

    r = client.get("/api/preview", params={"book": book, "page": "1", "printed": "1", "offset": "1"})
    assert "PDF page 2" in r.text


def test_counts(client, conn):
    for name, status in [("A", "want"), ("B", "attempted"), ("C", "folded"), ("D", "nailed"), ("Crab", "want")]:
        db.create_model(conn, {"name": name, "status": status})
    assert db.count_models(conn) == 5
    assert db.count_models(conn, ["want", "attempted"]) == 3

    page = client.get("/").text
    assert "3 to fold" in page and "Showing 3 of 5" in page
    page = client.get("/?filtered=1&status=want&q=crab").text
    assert "3 to fold" in page and "Showing 1 of 5" in page

    a = db.list_models(conn)[0]
    r = client.post(f"/models/{a['id']}/status", data={"status": "folded"})
    assert re.search(r'id="todo-count"[^>]*hx-swap-oob="true"[^>]*>2 to fold', r.text)
    assert f'data-id="{a["id"]}"' in r.text


def test_migration_marks_used_books_browsing(dirs, tmp_path):
    c = db.connect(tmp_path / "old.db")
    c.executescript(db.MIGRATIONS[0])
    c.execute("PRAGMA user_version = 1")
    c.execute("INSERT INTO books (path, title) VALUES ('a.pdf', 'A'), ('b.pdf', 'B')")
    c.execute("INSERT INTO models (name, book_id, position, created_at, updated_at) VALUES ('M', 1, 1, '', '')")
    c.commit()
    db.migrate(c)
    statuses = dict(c.execute("SELECT path, status FROM books").fetchall())
    assert statuses == {"a.pdf": "browsing", "b.pdf": "unexplored"}


NATURE = "sub/Kato - Nature Study.pdf"
INSECTS = "Lang - Origami Insects (z-library.sk, 1lib.sk).pdf"


def test_library_lists_all_pdfs_and_filters(client, conn):
    page = client.get("/library").text
    assert "Showing 2 of 2" in page and "Lang - Origami Insects" in page and "Kato - Nature Study" in page
    assert "2 unexplored · 0 browsing" in page

    # Setting a status on a book with no row creates the row.
    r = client.post("/library/status", data={"path": INSECTS, "status": "skip"})
    assert 'class="book-card bstatus-skip"' in r.text
    assert 'id="library-summary"' in r.text and 'hx-swap-oob="true"' in r.text
    assert db.get_book_by_path(conn, INSECTS)["status"] == "skip"

    page = client.get("/library").text  # default filter: unexplored + browsing
    assert "Showing 1 of 2" in page and "Origami Insects" not in page
    page = client.get("/library", params={"filtered": 1, "status": "skip", "q": "insects"}).text
    assert "Showing 1 of 2" in page
    page = client.get("/library", params={"filtered": 1, "status": db.BOOK_STATUSES, "has_models": 1}).text
    assert "Showing 0 of 2" in page


def test_adding_model_bumps_book_to_browsing(client, conn):
    client.post("/models", data={"name": "Owl", "book": NATURE, "page": "1"})
    assert db.get_book_by_path(conn, NATURE)["status"] == "browsing"
    # An explicit status is not overridden by later models.
    client.post("/library/status", data={"path": NATURE, "status": "explored"})
    client.post("/models", data={"name": "Bat", "book": NATURE, "page": "2"})
    assert db.get_book_by_path(conn, NATURE)["status"] == "explored"


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


def test_added_date_is_frozen_and_drives_recent_sort(client, conn):
    assert db.record_added(conn, {"x.pdf": 100.0})["x.pdf"] == 100.0
    assert db.record_added(conn, {"x.pdf": 999.0})["x.pdf"] == 100.0  # first sighting wins

    # Insects arrived after Nature Study: recent sort (the default) puts it first, title sort doesn't.
    db.record_added(conn, {NATURE: 1_000_000.0, INSECTS: 2_000_000_000.0})
    page = client.get("/library").text
    assert page.index("Lang - Origami Insects") < page.index("Kato - Nature Study")
    assert "Added 1970-01-1" in page  # 1,000,000s after the epoch, local time
    page = client.get("/library", params={"filtered": 1, "status": db.BOOK_STATUSES, "sort": "title"}).text
    assert page.index("Kato - Nature Study") < page.index("Lang - Origami Insects")
