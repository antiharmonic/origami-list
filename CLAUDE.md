# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A self-hosted to-fold list for origami models. FastAPI with Jinja2 templates and htmx, backed by SQLite. It runs as one Docker Compose service that serves the user's PDF library over HTTP. The point of serving the PDFs is that entries deep-link to exact pages (`/books/<path>#page=N` opens in Firefox's PDF viewer), and pages can be rendered as thumbnails.

## Commands

Everything runs in Docker; there is no local venv.

```sh
docker compose up -d --build                               # rebuild + (re)start; code is baked into the image, so rebuild after any change
docker compose logs -f
docker compose run --rm --no-deps app pytest -q            # full test suite (needs a rebuild first to pick up changes)
docker compose run --rm --no-deps app pytest -q tests/test_app.py::test_thumbnail   # single test
docker compose exec -T app python -m app.import_csv - < the-list.csv               # CSV import from stdin
```

There is no linter configured.

## Runtime layout

- `compose.yaml` mounts `~/Books/Origami` read-only at `/books` and `./data` at `/data`. It runs as uid 1000 with `restart: unless-stopped`, which makes it start on boot because `docker.service` is enabled. Port 5748 is exposed on all interfaces (LAN access, no auth, by design).
- `./data/` holds `origami.db`, `thumbs/` (a disposable cache) and `photos/` (user uploads). It is gitignored and is the only state.
- htmx and SortableJS are vendored in `app/static/`. No CDN is used and there is no build step.

## Architecture notes

- **Config:** `app/config.py` holds `BOOKS_DIR` and `DATA_DIR`. Other modules must read them as `config.X` at call time, never `from config import X`, because tests monkeypatch these attributes (`tests/conftest.py`).
- **Book identity:** a book is its path relative to `BOOKS_DIR`. `books.scan()` keeps an in-memory list of PDFs, refreshed after 5 minutes or by `/api/rescan`. Rows in the `books` table are created lazily by `db.get_or_create_book` the first time a book is used by a model, triaged, or edited. A scanned file with no row is treated as status `unexplored` with default metadata (`_library_entry` in `main.py` merges the scan with `db.library_rows`). Book triage statuses are `db.BOOK_STATUSES`. `db.mark_browsing` bumps `unexplored` to `browsing` whenever a model (form or CSV import) uses the book. The library page and the book page (`/library/book?path=`, path in the query because it contains `/`) are the only book UIs. `/books-admin` is now just import/export and rescan. A book's "added" date lives in `book_files` (one row per scanned path, independent of `books`). `db.record_added` inserts each newly seen path with its ctime at that moment (`INSERT OR IGNORE`, so it's frozen). That date drives the library's default "Recently added" sort. mtime isn't used, because downloads keep years-old mtimes. Any user-supplied path must go through `books.resolve()`, which is the path-traversal guard. It returns None for anything outside `BOOKS_DIR`.
- **Pages:** `models.page` is always the PDF page index, the same number `#page=` and `pdftoppm` use. The per-book `page_offset` only converts a printed page on input, when the form's `printed` checkbox is ticked: PDF page = printed page + offset. The model form has an editable offset field. It is loaded from `/api/book-offset` when the book changes, and saving the model writes it back to the book. The preview uses the form's value, not the DB.
- **Ordering:** `models.position` is a REAL. `db.move_model(id, prev_id, next_id)` puts a model at the midpoint between its neighbours and calls `renormalize()` to 1..N when the gap falls below `MIN_GAP`. The drag handler in `templates/base.html` posts the neighbour ids. Dragging is only enabled when the list is sorted by `mine` (the `.sortable` class on `#list`).
- **Schema changes:** append a new SQL string to `db.MIGRATIONS`. These are tracked with `PRAGMA user_version` and run at app startup (the lifespan in `main.py`). Never edit an existing entry.
- **SQLite connections:** there is one connection per request (the `get_conn` dependency), opened with `check_same_thread=False` because async routes use it from a different thread.
- **htmx partials:** `_row.html` is shared by the list and the inline status-change response. `_book_results.html` is the book search dropdown and `_preview.html` is the live thumbnail on the form. The filter form on `/` swaps `#list` via `hx-select`, and updates the "Showing x of y" label (`#list-count`) via `hx-select-oob`. The header's "N to fold" badge (`_todo_count.html`, Jinja global `todo_count()`, counting `DEFAULT_STATUSES`) is on every page, and the inline status route appends it as an out-of-band swap. The hidden `filtered=1` field tells "no statuses ticked" apart from "first visit", which falls back to `DEFAULT_STATUSES`.
- **Notes:** the "Notes" button in the nav and its slide-over panel (`_notes.html`, included in `base.html`, with logic in `static/notes.js`) edit one global Markdown document. It lives in the single-row `notes` table (`id = 1`). `POST /notes` autosaves with the client's `rev`, and `db.save_notes` refuses a stale rev (409) so two Firefox windows can't silently overwrite each other; `force` overrides. The panel reloads `GET /notes` (JSON) on each open. Preview is rendered server-side by `markdown-it-py` with raw HTML disabled.
- **Thumbnails:** `thumbs.thumbnail()` shells out to `pdftoppm` (poppler-utils, installed in the image). The cache key includes the PDF's mtime. It renders to a temp file and then renames it.
- **CSV:** `import_csv.ALIASES` maps both the user's original hand-made columns (`model, author, suggested_paper, file, folded, …`) and the app's own `/export.csv` columns, so an export can be imported again. Files are matched by exact relative path first, then by basename. A file that isn't found produces a warning, and the model is still imported, with `[Book: …]` added to its notes.

## Tests

`tests/conftest.py` builds a temporary books dir containing hand-written minimal PDFs (`make_pdf`) and a `secret.pdf` outside it, used for the traversal tests. It also points `config` at temp dirs. `test_thumbnail` is skipped when `pdftoppm` isn't installed, so run the tests inside the container.
