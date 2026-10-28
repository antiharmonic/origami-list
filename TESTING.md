# Testing

The tests are the list of what this app must keep doing. Each file in `tests/` starts with a docstring listing
the requirements it protects, and the test names spell them out. Before changing something, read the file for
that area; if a change breaks one of those requirements on purpose, update the test and its docstring with it.

| Layer | What it covers | Run it |
|---|---|---|
| Unit and route tests (`tests/test_*.py`) | Database, OCR, parsing, translation (Gemini faked), every route's HTML | `docker compose run --rm --no-deps app pytest -q` (about 20 s) |
| Browser tests (`tests/e2e/`) | The JavaScript you use: the reader and quick add, page text, suggestions, translation pane, drag-to-reorder, notes, photo paste, filters, library, search, studies | `docker compose --profile test run --rm --build e2e` (about 20 s once built; the first build downloads Chromium) |
| Real-data check (`tests/test_real_data.py`) | Migrations and every page on a *copy* of your real `origami.db` and `ocr.db` | `docker compose run --rm --no-deps -e REAL_DATA=1 app pytest -q tests/test_real_data.py` |

Code is baked into the images, so run `docker compose build` first (the browser command's `--build` does it for
its image).

## Before deploying a change

```sh
docker compose build                                                                  # new image, app not restarted yet
docker compose run --rm --no-deps app pytest -q                                       # unit tests
docker compose --profile test run --rm --build e2e                                    # browser tests
docker compose run --rm --no-deps -e REAL_DATA=1 app pytest -q tests/test_real_data.py  # your data, on a copy
docker compose up -d                                                                  # deploy
```

The real-data check only reads `data/` (through SQLite's backup API, safe while the app runs) and fails if the
real files changed during the run. If you were using the app at the same time, just run it again.

GitHub runs the unit and browser tests on every push (`.github/workflows/tests.yml`); the real-data check is
local only.

## Writing tests

- Fixtures (`tests/conftest.py`): `dirs` (temp books and data folders, used automatically), `conn`, `client`
  (FastAPI's TestClient), `gemini` (a fake translation engine; tests never reach the network).
- Shared data and helpers: `tests/helpers.py` (`NATURE`, `INSECTS`, `add_ocr_pages`, `ocr_done`, `toc_book`, …).
- Browser tests (`tests/e2e/conftest.py`): `server` (the app on a free port with fresh data), `page`
  (Playwright), and `open_book` / `go_to_page` for the PDF.js viewer. Prefer the ids and `data-*` attributes the
  templates already have as selectors.
