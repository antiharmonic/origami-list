import re
import shutil

import pytest

from app import books, db, import_csv, toc
from tests.conftest import make_pdf

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


def test_notes_save_conflict_and_render(client):
    assert client.get("/notes").json() == {"body": "", "rev": 0}
    assert client.post("/notes", data={"body": "- octopus", "rev": 0}).json() == {"rev": 1}
    assert client.get("/notes").json() == {"body": "- octopus", "rev": 1}

    # A window still on rev 0 can't overwrite, unless it forces.
    r = client.post("/notes", data={"body": "stale", "rev": 0})
    assert r.status_code == 409 and r.json()["rev"] == 1
    assert client.get("/notes").json()["body"] == "- octopus"
    assert client.post("/notes", data={"body": "mine", "rev": 0, "force": "true"}).json() == {"rev": 2}

    html = client.post("/notes/preview", data={"body": "- **Komatsu**\n- <script>x</script>"}).text
    assert "<li><strong>Komatsu</strong></li>" in html and "<script>" not in html and "&lt;script&gt;" in html

    for page in ("/", "/library"):
        assert 'id="notes-panel"' in client.get(page).text


def test_star_books_and_filter(client, conn):
    r = client.post("/library/star", data={"path": NATURE, "starred": "1"})
    assert 'class="star on"' in r.text and "★" in r.text
    book = db.get_book_by_path(conn, NATURE)
    assert (book["starred"], book["status"]) == (1, "unexplored")  # starring isn't triage

    all_statuses = {"filtered": 1, "status": db.BOOK_STATUSES}
    page = client.get("/library", params={**all_statuses, "starred": 1}).text
    assert "Showing 1 of 2" in page and "Kato - Nature Study" in page
    assert 'class="star on"' in client.get("/library/book", params={"path": NATURE}).text

    assert 'class="star"' in client.post("/library/star", data={"path": NATURE, "starred": "0"}).text
    assert "Showing 0 of 2" in client.get("/library", params={**all_statuses, "starred": 1}).text
    assert client.post("/library/star", data={"path": "../secret.pdf", "starred": "1"}).status_code == 404


HQ = "Lang - Insects HQ.pdf"


def test_relink_to_new_file_shifts_pages_and_hides_old(client, conn, dirs):
    make_pdf(dirs[0] / HQ)
    books.scan(force=True)
    client.post("/models", data={"name": "Mantis", "book": INSECTS, "page": "2", "offset": "4"})
    client.post("/models", data={"name": "Cicada", "book": INSECTS, "page": "1"})
    old_id = db.get_book_by_path(conn, INSECTS)["id"]

    r = client.post("/library/relink", data={"path": INSECTS, "book": HQ, "shift": "-1", "hide_old": "1"},
                    follow_redirects=False)
    assert r.status_code == 303 and "relinked=1" in r.headers["location"]
    book = db.get_book(conn, old_id)
    assert (book["path"], book["title"], book["page_offset"]) == (HQ, "Lang - Insects HQ", 3)
    assert sorted(m["page"] for m in db.list_models(conn)) == [1, 1]  # 2-1, and 1-1 clamped to 1

    # The old scan is hidden: out of the library and book search unless asked for.
    assert db.get_book_by_path(conn, INSECTS)["hidden"] == 1
    assert "Lang - Origami Insects" not in client.get("/library").text
    assert "1 hidden" in client.get("/library").text
    assert "Lang - Origami Insects" in client.get("/library", params={"hidden": 1}).text
    assert INSECTS not in client.get("/api/books", params={"book": "lang"}).text
    assert HQ in client.get("/api/books", params={"book": "lang"}).text
    assert "file missing" not in client.get("/").text


def test_relink_merges_into_existing_row(client, conn, dirs):
    make_pdf(dirs[0] / HQ)
    books.scan(force=True)
    client.post("/models", data={"name": "Mantis", "book": INSECTS, "page": "5"})
    client.post("/library/book", data={"path": INSECTS, "title": "Insects", "designers": "Robert Lang",
                                       "notes": "", "page_offset": "2", "cover_page": "1"})
    client.post("/library/star", data={"path": INSECTS, "starred": "1"})
    client.post("/library/status", data={"path": HQ, "status": "explored"})
    hq_id = db.get_book_by_path(conn, HQ)["id"]

    client.post("/library/relink", data={"path": INSECTS, "book": HQ, "shift": "2"})
    hq = db.get_book(conn, hq_id)
    assert (hq["designers"], hq["starred"], hq["status"], hq["page_offset"]) == ("Robert Lang", 1, "explored", 4)
    (m,) = db.list_models(conn)
    assert (m["book_id"], m["page"]) == (hq_id, 7)
    assert db.get_book_by_path(conn, INSECTS) is None  # merged away, and not hidden (no hide_old)


def test_relink_rejects_bad_targets(client):
    client.post("/library/star", data={"path": INSECTS, "starred": "1"})
    for target in ("../secret.pdf", "nope.pdf", INSECTS):
        assert client.post("/library/relink", data={"path": INSECTS, "book": target}).status_code == 400
    assert client.post("/library/relink", data={"path": "unknown.pdf", "book": NATURE}).status_code == 404


def test_missing_file_is_flagged_and_relinkable(client, conn, dirs):
    client.post("/models", data={"name": "Dragonfly", "book": NATURE, "page": "2"})
    (dirs[0] / NATURE).unlink()
    books.scan(force=True)

    assert "file missing" in client.get("/").text
    library = client.get("/library").text
    assert "Missing files (1)" in library and "looks empty" not in library
    page = client.get("/library/book", params={"path": NATURE})
    assert page.status_code == 200 and "File missing" in page.text and "Relink to another file" in page.text
    assert client.get("/library/book", params={"path": "never-seen.pdf"}).status_code == 404


def test_empty_books_folder_warns(client, dirs):
    client.get("/library")  # records the files as seen
    backup = dirs[0].with_name("books-away")
    dirs[0].rename(backup)
    books.scan(force=True)
    page = client.get("/").text
    assert "looks empty" in page and "Missing files" not in client.get("/library").text
    backup.rename(dirs[0])
    books.scan(force=True)
    assert "looks empty" not in client.get("/").text


def test_preview_shows_cover_until_a_page_is_entered(client):
    r = client.get("/api/preview", params={"book": INSECTS})
    assert "Cover of the selected book" in r.text and "page=1" in r.text
    client.post("/library/book", data={"path": INSECTS, "title": "Insects", "designers": "", "notes": "",
                                       "page_offset": "0", "cover_page": "2"})
    assert "page=2" in client.get("/api/preview", params={"book": INSECTS}).text
    r = client.get("/api/preview", params={"book": INSECTS, "page": "1"})
    assert "Cover of" not in r.text and "PDF page 1" in r.text


def study_ids(conn, model_id):
    return [sid for sid, _ in db.parse_studies(db.get_model(conn, model_id)["studies"])]


def test_studies_from_the_model_form(client, conn):
    r = client.post("/models", data={"name": "Rhino", "new_study": " rhino  study ", "then": "new"},
                    follow_redirects=False)
    (m,) = db.list_models(conn)
    (study,) = db.list_studies(conn)
    assert study["name"] == "rhino study" and study_ids(conn, m["id"]) == [study["id"]]
    assert f"study={study['id']}" in r.headers["location"]  # Save and new carries the study
    assert f'value="{study["id"]}" checked' in client.get(r.headers["location"]).text
    assert f'href="/studies/{study["id"]}">rhino study</a>' in client.get("/").text

    # Same name in another case is the same study; unticking it on edit removes the membership.
    client.post("/models", data={"name": "Rhino 2", "new_study": "Rhino Study"})
    assert len(db.list_studies(conn)) == 1
    client.post(f"/models/{m['id']}", data={"name": "Rhino"})
    assert study_ids(conn, m["id"]) == []


def test_study_filters_bulk_add_and_page(client, conn):
    for name in ("Black Rhino", "White Rhino", "Crane"):
        db.create_model(conn, {"name": name})
    show_all = {"filtered": 1, "status": db.STATUSES}
    r = client.post("/studies/bulk", data={**show_all, "q": "rhino", "new_study": "Rhinos"})
    sid = db.study_by_name(conn, "Rhinos", create=False)
    assert r.status_code == 204 and r.headers["HX-Redirect"] == f"/studies/{sid}"
    client.post("/studies/bulk", data={**show_all, "q": "rhino", "target_study": sid})  # no duplicates
    assert db.get_study(conn, sid)["model_count"] == 2

    assert "Showing 2 of 3" in client.get("/", params={**show_all, "study": f"in:{sid}"}).text
    page = client.get("/", params={**show_all, "study": f"out:{sid}"}).text
    assert "Showing 1 of 3" in page and "Crane" in page

    # The page prefers the fold photo, falling back to the diagram thumbnail.
    rhinos = db.list_models(conn, study=("in", sid))
    db.update_model(conn, rhinos[0]["id"], {"photo": "1.jpg", "status": "folded"})
    db.update_model(conn, rhinos[1]["id"], {"book_id": db.get_or_create_book(conn, INSECTS), "page": 2})
    page = client.get(f"/studies/{sid}").text
    assert 'src="/photos/1.jpg"' in page and "/thumb?" in page and "1 folded · 0 attempted · 1 to go" in page

    client.post(f"/studies/{sid}/remove", data={"model_id": rhinos[0]["id"]})
    assert db.get_study(conn, sid)["model_count"] == 1 and len(db.list_models(conn)) == 3
    client.post(f"/studies/{sid}", data={"name": "Rhinoceros", "notes": "horns"})
    assert dict(db.get_study(conn, sid))["notes"] == "horns"
    client.post(f"/models/{rhinos[1]['id']}/delete")  # cascades to the membership
    assert db.get_study(conn, sid)["model_count"] == 0
    client.post(f"/studies/{sid}/delete")
    assert db.list_studies(conn) == [] and len(db.list_models(conn)) == 2
    assert client.get(f"/studies/{sid}").status_code == 404


def test_studies_survive_csv_round_trip(client, conn):
    client.post("/models", data={"name": "Rhino", "new_study": "Rhinos"})
    exported = client.get("/export.csv").text
    assert exported.splitlines()[1].endswith(",Rhinos")
    sid = db.study_by_name(conn, "Rhinos", create=False)
    db.delete_study(conn, sid)
    import_csv.import_text(conn, exported)
    (study,) = db.list_studies(conn)
    assert study["name"] == "Rhinos" and study["model_count"] == 1


def test_pasted_photo_upload_and_replace(client, conn):
    # A pasted screenshot arrives as a plain multipart file named "image.png".
    from app import config
    client.post("/models", data={"name": "Rhino"}, files={"photo": ("image.png", b"\x89PNG fake", "image/png")})
    m = db.list_models(conn)[0]
    assert m["photo"] == f"{m['id']}.png"
    client.post(f"/models/{m['id']}", data={"name": "Rhino"},
                files={"photo": ("image.jpg", b"\xff\xd8 fake", "image/jpeg")})
    photos = config.DATA_DIR / "photos"
    assert db.get_model(conn, m["id"])["photo"] == f"{m['id']}.jpg"
    assert sorted(p.name for p in photos.iterdir()) == [f"{m['id']}.jpg"]
    assert "paste an image" in client.get(f"/models/{m['id']}/edit").text


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


# --- library search (OCR) and glossary ---

from app import ocr  # noqa: E402

needs_tesseract = pytest.mark.skipif(not shutil.which("tesseract"), reason="tesseract not installed")
RHINO_TEXT = ["Rhinoceros by Fumiaki Kawahata", "Fold the square in half and unfold.", "Several varieties of this animal."]


def add_ocr_pages(rows):
    """Put fake OCR'd pages into ocr.db: [(path, page, text)]."""
    conn = ocr.connect()
    with conn:
        conn.executemany("INSERT INTO ocr_pages (path, page, source, text) VALUES (?, ?, 'ocr', ?)", rows)
    conn.close()


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


def test_glossary_seed_edit_delete(conn):
    db.seed_glossary(conn)
    count = conn.execute("SELECT COUNT(*) FROM glossary").fetchone()[0]
    db.seed_glossary(conn)
    assert conn.execute("SELECT COUNT(*) FROM glossary").fetchone()[0] == count > 300

    db.save_glossary(conn, "subject", "サイ", "Rhino")  # a user edit beats the seed
    gone = conn.execute("SELECT id FROM glossary WHERE ja = 'ゾウ'").fetchone()[0]
    db.delete_glossary(conn, gone)
    db.seed_glossary(conn)
    assert db.suggest(conn, "subject", "サイ") == "Rhino (サイ)"
    assert db.suggest(conn, "subject", "ゾウ") is None


def test_suggestions(conn):
    db.seed_glossary(conn)
    s = lambda kind, text: db.suggest(conn, kind, text)
    assert s("subject", "サイ") == "Rhinoceros (サイ)"
    assert s("subject", "rhinoceros") == "Rhinoceros (サイ)"
    assert s("subject", "Rhinoceros (サイ)") is None and s("subject", "Gizmo") is None
    assert s("designer", "川畑文昭") == s("designer", "川畑 文昭") == "Fumiaki Kawahata"
    assert s("designer", "Kawahata Fumiaki") == s("designer", "KAWAHATA Fumiaki") == "Fumiaki Kawahata"
    assert s("designer", "SUZUKI Taro") == "Taro Suzuki"  # unknown, but family name in capitals
    assert s("designer", "John Montroll") is None and s("designer", "Fumiaki Kawahata") is None


def test_quick_add_learns_and_suggests(client, conn):
    client.post("/library/book/models", data={"path": NATURE, "name": "Gazelle (ガゼル)", "page": "4"})
    assert db.suggest(conn, "subject", "ガゼル") == "Gazelle (ガゼル)"
    chip = client.get("/api/suggest", params={"kind": "subject", "target": "qa-name", "name": "ガゼル"}).text
    assert 'data-value="Gazelle (ガゼル)"' in chip and 'data-target="qa-name"' in chip
    assert client.get("/api/suggest", params={"kind": "designer", "designer": "John Montroll"}).text == ""

    page = client.get("/library/book", params={"path": NATURE, "page": "4", "name": "サイ"}).text
    assert 'value="サイ"' in page and 'data-value="Rhinoceros (サイ)"' in page
    assert "Text search: queued" in page and "Read this book next" in page


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


def test_glossary_page(client, conn):
    client.post("/glossary", data={"kind": "designer", "ja": "加々美均", "en": "Hitoshi Kakami"})
    page = client.get("/glossary", params={"kind": "designer", "q": "Kakami"}).text
    assert 'value="加々美均"' in page and 'value="Fumiaki Kawahata"' not in page
    entry = conn.execute("SELECT id FROM glossary WHERE ja = '加々美均'").fetchone()[0]
    client.post("/glossary", data={"kind": "designer", "ja": "加々美均", "en": "Hitoshi KAKAMI", "entry_id": entry})
    assert db.suggest(conn, "designer", "加々美均") == "Hitoshi KAKAMI"
    client.post(f"/glossary/{entry}/delete", data={"kind": "designer"})
    assert db.suggest(conn, "designer", "加々美均") is None


def test_japanese_spacing_and_short_katakana(client):
    assert ocr.normalize("シロ サイ の 折り 方\nRhino ceros 2 枚") == "シロサイの折り方\nRhino ceros 2 枚"
    add_ocr_pages([(NATURE, 3, ocr.normalize("シロ サイ")), (INSECTS, 4, ocr.normalize("A4 サイ ズ の 紙"))])
    page = client.get("/search", params={"q": "サイ"}).text
    assert "Nature Study" in page and "Origami Insects" not in page  # サイズ ("size") isn't a rhino


# --- suggested models from contents pages ---

ENGEL_TOC = """CONTENTS

ACKNOWLEDGMENTS vii
FOREWORD 1
SYMBOLS AND PROCEDURES 8
ANGELFISH ......... 20
BUTTERFLY FISH 22
| KANGAROO 25
GIRAFFE 29
PENGUIN 33
NOTES AND SOURCES 40
"""


def toc_book(offset=4):
    """{page: text} for a book whose models sit at printed page + offset."""
    pages = {3: ENGEL_TOC}
    for name, printed in [("ANGELFISH", 20), ("BUTTERFLY FISH", 22), ("KANGAROO", 25), ("GIRAFFE", 29)]:
        pages[printed + offset] = f"{name}\nFold in half."
    return pages


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


def ocr_done(path, pages):
    add_ocr_pages([(path, p, t) for p, t in pages.items()])
    conn = ocr.connect()
    with conn:
        conn.execute("INSERT INTO ocr_books (path, size, mtime, pages, done_pages, state) VALUES (?, 1, 1, ?, ?, 'done')",
                     (path, len(pages), len(pages)))
    conn.close()


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
