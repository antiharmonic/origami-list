"""The library: every PDF in the books folder, and the book records behind them.

- Every PDF is listed, with its title cleaned from the filename; book search finds files by words.
- Books have a triage status (unexplored, browsing, explored, skip); adding a model from a book moves it to browsing.
- A book's "added" date is frozen the first time it's seen and drives the "Recently added" sort.
- Books can be starred and filtered by star.
- Relinking moves a book (models, status, star, details) to another file, shifting pages for a scan with more or fewer front pages, merging into an existing record, and refusing bad targets.
- A book whose file disappears is never deleted: it's flagged as missing and can be relinked.
- An empty books folder (an unmounted drive) shows a warning instead of looking like everything was deleted.
"""
from app import books, db
from tests.conftest import make_pdf
from tests.helpers import HQ, INSECTS, NATURE


def test_search_and_clean_title():
    assert books.search("kato nature") == ["sub/Kato - Nature Study.pdf"]
    assert books.clean_title("Lang - Origami Insects (z-library.sk, 1lib.sk).pdf") == "Lang - Origami Insects"


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
