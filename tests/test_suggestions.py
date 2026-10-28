"""Suggested models from a book's contents page, and "fully indexed" books.

- Contents pages are recognised and parsed (front/back matter dropped, Japanese entries and designers handled); other pages are not.
- The printed -> PDF page offset is detected from where the names reappear, unless the book has its own; a wrong detection can be ignored.
- Added and dismissed suggestions stay that way when the contents are read again.
- A suggestion disappears once a model with the same name (in either language) is in the book.
- Add / dismiss / add all verified work from the book page; "Mark fully indexed" hides the suggestions.
"""
from app import db, toc
from tests.helpers import INSECTS, NATURE, ocr_done, toc_book


def test_toc_parses_contents_and_detects_offset():
    entries, info = toc.extract(toc_book())
    assert info["toc_pages"] == [3] and info["offset"] == 4 and info["detected"]
    names = [e["name"] for e in entries]
    assert names == ["Angelfish", "Butterfly Fish", "Kangaroo", "Giraffe", "Penguin"]  # no front/back matter
    by = {e["name"]: e for e in entries}
    assert by["Angelfish"]["page"] == 24 and by["Angelfish"]["verified"]
    assert by["Penguin"]["page"] == 37 and not by["Penguin"]["verified"]
    # The book's own offset wins over detection.
    entries, info = toc.extract(toc_book(), offset=2)
    assert info["offset"] == 2 and not info["detected"] and entries[0]["page"] == 22


def test_toc_ignores_non_contents_and_reads_japanese():
    assert toc.contents_entries("Step 1 fold\nStep 2 fold\nsome text 3") == []
    # A numbered list repeating the same numbers isn't a contents page.
    assert toc.contents_entries("\n".join(f"idea number {n}" for n in [1, 1, 3, 3, 3, 3, 3])) == []
    jp = "目次\nうさぎ / 鶴田芳理 10\nフクロウ前川淳 14\nユニコーン 18\nUnicorn Tetsuya Gotani\n"
    entries = toc.contents_entries(jp)
    assert ("うさぎ", 10, "鶴田芳理") in entries
    assert ("Unicorn Tetsuya Gotani (ユニコーン)", 18, "") in entries
    # "page 74" with the name on the line above.
    city = "Contents\nEiffel Tower\npage 74\nTokyo Tower\npage 80\nTorii page 90\n"
    assert [e[0] for e in toc.contents_entries(city)] == ["Eiffel Tower", "Tokyo Tower", "Torii"]


def test_store_suggestions_keeps_decisions(conn):
    db.seed_glossary(conn)
    book_id = db.get_or_create_book(conn, NATURE)
    entries = [{"name": n, "designer": d, "printed": p, "page": p, "verified": True}
               for n, d, p in [("フクロウ前川淳", "", 10), ("Crane", "", 12), ("Frog", "", 14), ("ウサギ", "鶴田芳理", 16)]]
    db.store_suggestions(conn, book_id, entries, 0, 1)
    shown = {s["name"]: s for s in db.list_suggestions(conn, book_id)}
    assert shown["Owl (フクロウ)"]["designer"] == "Jun Maekawa"  # designer split off, name from the glossary
    assert shown["Rabbit (ウサギ)"]["designer"] == "鶴田芳理"
    db.set_suggestion_state(conn, [shown["Crane"]["id"]], "dismissed")
    db.store_suggestions(conn, book_id, entries, 0, 2)  # a re-extraction doesn't bring it back
    assert "Crane" not in {s["name"] for s in db.list_suggestions(conn, book_id)}
    # Models already on the list (by name or page) hide their suggestion.
    db.create_model(conn, {"name": "frog", "designer": "", "book_id": book_id, "page": None, "status": "want"})
    assert "Frog" not in {s["name"] for s in db.list_suggestions(conn, book_id)}
    # Relinking with a shift moves suggested pages along.
    db.relink_book(conn, book_id, INSECTS, shift=3)
    assert {s["page"] for s in db.list_suggestions(conn, book_id)} == {13, 19}


def test_book_page_suggestions_flow(client, conn):
    ocr_done(INSECTS, toc_book())
    page = client.get("/library/book", params={"path": INSECTS}).text
    assert "Suggested from contents (5)" in page and "offset +4" in page and 'data-goto="24"' in page
    row = db.get_book_by_path(conn, INSECTS)
    ids = {s["name"]: s["id"] for s in db.list_suggestions(conn, row["id"])}

    # Add one (it becomes a model), dismiss another.
    out = client.post("/library/book/models", data={"path": INSECTS, "name": "Angelfish", "page": "24",
                                                    "suggestion_id": ids["Angelfish"]}).text
    assert "Angelfish" in out and "Suggested from contents (4)" in out
    out = client.post("/library/book/suggestions/dismiss", data={"path": INSECTS, "suggestion_id": ids["Penguin"]}).text
    assert "Suggested from contents (3)" in out

    # Add all verified: the remaining three are all found on their pages.
    out = client.post("/library/book/suggestions/add-verified", data={"path": INSECTS, "status": "want"}).text
    assert "Giraffe" in out and 'id="suggestions"' in out and "Suggested from contents" not in out
    assert len(db.list_models(conn, book_id=row["id"])) == 4

    # The detected offset can become the book's own.
    client.post("/library/book/offset", data={"path": INSECTS})
    assert db.get_book_by_path(conn, INSECTS)["page_offset"] == 4


def test_fully_indexed_hides_suggestions(client, conn):
    ocr_done(INSECTS, toc_book())
    assert "Mark fully indexed" in client.get("/library/book", params={"path": INSECTS}).text
    out = client.post("/library/book/indexed", data={"path": INSECTS, "indexed": "1"}).text
    assert "Fully indexed ✓" in out and "Suggested from contents" not in out
    assert "Suggested from contents" not in client.get("/library/book", params={"path": INSECTS}).text
    client.post("/library/book/indexed", data={"path": INSECTS, "indexed": "0"})
    assert "Suggested from contents (5)" in client.get("/library/book", params={"path": INSECTS}).text
    # A book with no contents page gets no row (and no section).
    ocr_done(NATURE, {1: "Just a cover"})
    assert "Suggested from contents" not in client.get("/library/book", params={"path": NATURE}).text
    assert db.get_book_by_path(conn, NATURE) is None


def test_suggestions_hidden_only_by_matching_names(conn):
    book_id = db.get_or_create_book(conn, NATURE)
    entries = [{"name": n, "designer": "", "printed": p, "page": p, "verified": True}
               for n, p in [("Owl", 10), ("Mouse", 10), ("Fox Terrier", 12)]]
    db.store_suggestions(conn, book_id, entries, 0, 1)
    for name in ["Owl (フクロウ)", "Fox"]:
        db.create_model(conn, {"name": name, "designer": "", "book_id": book_id, "page": 10, "status": "want"})
    # "Owl (フクロウ)" covers "Owl"; a different model on page 10 doesn't hide "Mouse"; "Fox" isn't "Fox Terrier".
    assert {s["name"] for s in db.list_suggestions(conn, book_id)} == {"Mouse", "Fox Terrier"}


def test_ignore_detected_offset(client, conn):
    ocr_done(INSECTS, toc_book())
    client.get("/library/book", params={"path": INSECTS})
    book_id = db.get_book_by_path(conn, INSECTS)["id"]
    assert db.list_suggestions(conn, book_id)[0]["page"] == 24  # printed 20 + detected 4
    out = client.post("/library/book/offset", data={"path": INSECTS, "action": "ignore"}).text
    assert "unverified" in out and db.get_book_by_path(conn, INSECTS)["toc_offset"] == 0
    assert db.list_suggestions(conn, book_id)[0]["page"] == 20
