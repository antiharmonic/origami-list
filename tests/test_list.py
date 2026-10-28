"""The to-fold list and the model form.

- The list keeps the user's own order: drag moves a model between its neighbours, and positions are renumbered when they get too close.
- New models go to the bottom (or the top when asked).
- Models are added, edited and given a status; filters (status, search, book) and sorts narrow the list, and "Showing x of y" / the "N to fold" badge count correctly.
- A model's book must be a real file in the books folder.
- "Save and new" carries the designer, book and studies over to the next model.
- A model's page is always the PDF page; a printed page converts through the book's offset, which the form saves back to the book.
- The form's preview shows the book's cover until a page is entered, then that page.
"""
import re

from app import db
from tests.helpers import INSECTS, order


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


def test_preview_shows_cover_until_a_page_is_entered(client):
    r = client.get("/api/preview", params={"book": INSECTS})
    assert "Cover of the selected book" in r.text and "page=1" in r.text
    client.post("/library/book", data={"path": INSECTS, "title": "Insects", "designers": "", "notes": "",
                                       "page_offset": "0", "cover_page": "2"})
    assert "page=2" in client.get("/api/preview", params={"book": INSECTS}).text
    r = client.get("/api/preview", params={"book": INSECTS, "page": "1"})
    assert "Cover of" not in r.text and "PDF page 1" in r.text
