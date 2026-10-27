"""Full-text search over every book page: text layers where PDFs have them, Tesseract OCR otherwise.

Lives in its own database (data/ocr.db): the `ocr` service (app/ocr_worker.py) writes it, the app
reads it. It's all regenerable, so the backup leaves it out.
"""
import os
import re
import sqlite3
import subprocess
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed

from markupsafe import Markup, escape

from . import books, config

LANGS = "eng+jpn"
# Cap the rendered page size: some scans have huge pages, and a fixed dpi made 200-megapixel images.
RENDER_LONG_SIDE = 3500
PAGE_TIMEOUT = 120
MIN_TEXT_CHARS = 50  # a page with less real text than this gets OCR'd instead
MAX_ATTEMPTS = 3  # a page that keeps failing is given up on, so its book can still finish
_JA = "\u3040-\u30ff\u3400-\u9fff\uff66-\uff9f"
# Tesseract puts spaces between Japanese characters ("シロ サイ"); Japanese doesn't use them.
_JA_GAP = re.compile(f"(?<=[{_JA}])[ \t\u3000]+(?=[{_JA}])")
_KATAKANA = re.compile("^[\u30a0-\u30ff]+$")


def normalize(text):
    return _JA_GAP.sub("", text)


def _renormalize(conn):
    for rowid, text in conn.execute("SELECT rowid, text FROM ocr_pages").fetchall():
        if normalize(text) != text:
            conn.execute("UPDATE ocr_pages SET text = ? WHERE rowid = ?", (normalize(text), rowid))


SCHEMA = [
    """
    CREATE TABLE ocr_books (
        path TEXT PRIMARY KEY,
        size INTEGER NOT NULL,
        mtime REAL NOT NULL,
        pages INTEGER,
        done_pages INTEGER NOT NULL DEFAULT 0,
        state TEXT NOT NULL DEFAULT 'queued',
        error TEXT,
        updated_at REAL
    );
    CREATE TABLE ocr_pages (
        path TEXT NOT NULL,
        page INTEGER NOT NULL,
        source TEXT NOT NULL,
        text TEXT NOT NULL,
        PRIMARY KEY (path, page)
    );
    CREATE VIRTUAL TABLE ocr_fts USING fts5(text, content='ocr_pages', content_rowid='rowid', tokenize='trigram');
    CREATE TRIGGER ocr_pages_ai AFTER INSERT ON ocr_pages BEGIN
        INSERT INTO ocr_fts (rowid, text) VALUES (new.rowid, new.text);
    END;
    CREATE TRIGGER ocr_pages_ad AFTER DELETE ON ocr_pages BEGIN
        INSERT INTO ocr_fts (ocr_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
    END;
    CREATE TRIGGER ocr_pages_au AFTER UPDATE ON ocr_pages BEGIN
        INSERT INTO ocr_fts (ocr_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
        INSERT INTO ocr_fts (rowid, text) VALUES (new.rowid, new.text);
    END;
    CREATE TABLE ocr_control (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        paused INTEGER NOT NULL DEFAULT 0,
        priority_path TEXT,
        rate REAL
    );
    INSERT INTO ocr_control (id) VALUES (1);
    """,
    _renormalize,
    # Throughput for the ETA: totals, since a short average swings wildly between text-layer and scanned books.
    """
    ALTER TABLE ocr_control ADD COLUMN busy_secs REAL NOT NULL DEFAULT 0;
    ALTER TABLE ocr_control ADD COLUMN busy_pages INTEGER NOT NULL DEFAULT 0;
    """,
    # A page that failed keeps its error out of `text` (so search can't match it) and is retried.
    """
    ALTER TABLE ocr_pages ADD COLUMN error TEXT;
    ALTER TABLE ocr_pages ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
    UPDATE ocr_pages SET error = text, text = '', attempts = 1 WHERE source = 'error';
    """,
]


def connect():
    conn = sqlite3.connect(config.DATA_DIR / "ocr.db", timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for i, step in enumerate(SCHEMA[version:], start=version + 1):
        with conn:
            step(conn) if callable(step) else conn.executescript(step)
            conn.execute(f"PRAGMA user_version = {i}")
    return conn


# --- reading a page ---

def page_count(full):
    """The PDF's page count; raises if pdfinfo can't read it (rather than reporting 0 pages)."""
    out = subprocess.run(["pdfinfo", str(full)], capture_output=True, text=True, timeout=60, check=True).stdout
    count = next((int(line.split()[1]) for line in out.splitlines() if line.startswith("Pages:")), None)
    if not count:
        raise ValueError("pdfinfo reported no pages")
    return count


def page_text(full, page):
    """(source, text) for one page: its text layer if it has a real one, else Tesseract OCR."""
    text = subprocess.run(["pdftotext", "-q", "-f", str(page), "-l", str(page), str(full), "-"],
                          capture_output=True, text=True, timeout=PAGE_TIMEOUT).stdout
    if len("".join(text.split())) >= MIN_TEXT_CHARS:
        return "text", normalize(text)
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["pdftoppm", "-q", "-scale-to", str(RENDER_LONG_SIDE), "-gray", "-png", "-singlefile",
                        "-f", str(page), "-l", str(page), str(full), f"{tmp}/p"],
                       check=True, capture_output=True, timeout=PAGE_TIMEOUT)
        out = subprocess.run(["tesseract", f"{tmp}/p.png", "-", "-l", LANGS, "--psm", "3"],
                             capture_output=True, text=True, timeout=PAGE_TIMEOUT, check=True,
                             env={**os.environ, "OMP_THREAD_LIMIT": "1"})
    return "ocr", normalize(out.stdout)


def _page_job(full, page):
    """(page, source, text, error); a failure is an "error" page with no text, retried on a later pass."""
    try:
        return page, *page_text(full, page), None
    except Exception as e:  # one bad page shouldn't stop the book
        return page, "error", "", f"{type(e).__name__}: {e}"


def _pages_left(conn, path, pages):
    """Pages still to read: never tried, or failed fewer than MAX_ATTEMPTS times."""
    settled = {r[0] for r in conn.execute(
        "SELECT page FROM ocr_pages WHERE path = ? AND (source != 'error' OR attempts >= ?)", (path, MAX_ATTEMPTS))}
    return [p for p in range(1, pages + 1) if p not in settled]


# --- the queue ---

def sync(conn, stats):
    """Queue new files and re-queue changed ones. `stats` is {path: (size, mtime)}."""
    known = {r["path"]: r for r in conn.execute("SELECT path, size, mtime FROM ocr_books")}
    with conn:
        for path, (size, mtime) in stats.items():
            row = known.get(path)
            if row is None:
                conn.execute("INSERT INTO ocr_books (path, size, mtime) VALUES (?, ?, ?)", (path, size, mtime))
            elif (row["size"], row["mtime"]) != (size, mtime):
                conn.execute("DELETE FROM ocr_pages WHERE path = ?", (path,))
                conn.execute("""UPDATE ocr_books SET size = ?, mtime = ?, pages = NULL, done_pages = 0,
                                state = 'queued', error = NULL WHERE path = ?""", (size, mtime, path))


def next_book(conn, present, added):
    """The priority book if one is set, else the most recently added unfinished book."""
    control = get_control(conn)
    todo = {r[0] for r in conn.execute("SELECT path FROM ocr_books WHERE state IN ('queued', 'running')")} & set(present)
    if control["priority_path"] in todo:
        return control["priority_path"]
    return max(todo, key=lambda p: (added.get(p, 0), p), default=None)


def get_control(conn):
    return conn.execute("SELECT * FROM ocr_control WHERE id = 1").fetchone()


def set_control(conn, **fields):
    sets = ", ".join(f"{k} = ?" for k in fields)
    with conn:
        conn.execute(f"UPDATE ocr_control SET {sets} WHERE id = 1", list(fields.values()))


def process_book(conn, path, workers=1, log=print):
    """OCR a book's remaining pages, committing each one; stops early (resumably) when paused."""
    full = books.resolve(path)
    if not full:
        return
    try:
        pages = page_count(full)
    except Exception as e:
        with conn:
            conn.execute("UPDATE ocr_books SET state = 'error', error = ?, updated_at = ? WHERE path = ?",
                         (str(e), time.time(), path))
        return
    todo = _pages_left(conn, path, pages)
    with conn:
        conn.execute("UPDATE ocr_books SET pages = ?, state = 'running', updated_at = ? WHERE path = ?",
                     (pages, time.time(), path))
    log(f"{path}: {len(todo)} of {pages} pages to read")
    start, finished = time.monotonic(), 0
    # One worker runs in-process (no point forking for it; tests rely on that too).
    with (ProcessPoolExecutor(workers) if workers > 1 else ThreadPoolExecutor(1)) as pool:
        futures = [pool.submit(_page_job, full, p) for p in todo]
        for f in as_completed(futures):
            page, source, text, error = f.result()
            finished += 1
            failed = int(source == "error")
            with conn:
                conn.execute("""INSERT INTO ocr_pages (path, page, source, text, error, attempts) VALUES (?, ?, ?, ?, ?, ?)
                                ON CONFLICT (path, page) DO UPDATE SET source = excluded.source, text = excluded.text,
                                error = excluded.error, attempts = attempts + excluded.attempts""",
                             (path, page, source, text, error, failed))
                # Counted, not incremented, so a page that failed before and now succeeds isn't counted twice.
                conn.execute("""UPDATE ocr_books SET updated_at = ?, done_pages = (SELECT COUNT(*) FROM ocr_pages
                                WHERE path = ocr_books.path AND source != 'error') WHERE path = ?""", (time.time(), path))
            control = get_control(conn)
            if control["paused"] or (control["priority_path"] not in (None, path)):
                for other in futures:
                    other.cancel()
                break
    if finished:
        with conn:
            conn.execute("UPDATE ocr_control SET busy_secs = busy_secs + ?, busy_pages = busy_pages + ? WHERE id = 1",
                         (time.monotonic() - start, finished))
    left = len(_pages_left(conn, path, pages))
    with conn:
        conn.execute("UPDATE ocr_books SET state = ? WHERE path = ?", ("queued" if left > 0 else "done", path))
        conn.execute("UPDATE ocr_control SET priority_path = NULL WHERE id = 1 AND priority_path = ? AND ? = 0", (path, left))
    log(f"{path}: {'paused/preempted with ' + str(left) + ' pages left' if left else 'done'}")


# --- status ---

def progress(conn, present):
    present = set(present)
    rows = [r for r in conn.execute("SELECT * FROM ocr_books") if r["path"] in present]
    done = sum(r["state"] == "done" for r in rows)
    known = [r["pages"] for r in rows if r["pages"]]
    avg_pages = sum(known) / len(known) if known else 80
    pages_left = sum((r["pages"] or avg_pages) - r["done_pages"] for r in rows if r["state"] != "done")
    pages_left += avg_pages * (len(present) - len(rows))  # not even queued yet
    control = get_control(conn)
    running = next((r for r in rows if r["state"] == "running"), None)
    return {
        "books": len(present), "done": done, "percent": round(100 * done / len(present)) if present else 100,
        "pages_done": sum(r["done_pages"] for r in rows),
        # Pages per second so far; only trusted after a few minutes of work.
        "hours_left": pages_left * control["busy_secs"] / control["busy_pages"] / 3600
                      if control["busy_secs"] > 300 and pages_left else None,
        "paused": bool(control["paused"]), "running": running,
        "errors": sum(r["state"] == "error" for r in rows),
    }


def book_status(conn, path, present, added):
    """Where one book stands: done, running (with page counts), or its place in the queue."""
    row = conn.execute("SELECT * FROM ocr_books WHERE path = ?", (path,)).fetchone()
    if row is None:
        return {"state": "queued", "position": None}
    status = dict(row)
    status["failed"] = conn.execute("SELECT COUNT(*) FROM ocr_pages WHERE path = ? AND source = 'error'",
                                    (path,)).fetchone()[0]
    if row["state"] == "queued":
        control = get_control(conn)
        todo = [r[0] for r in conn.execute("SELECT path FROM ocr_books WHERE state IN ('queued', 'running')")
                if r[0] in present]
        order = sorted(todo, key=lambda p: (p != control["priority_path"], -added.get(p, 0)))
        status["position"] = order.index(path) + 1 if path in order else None
    return status


# --- search ---

def collapse_runs(pages):
    """[3, 4, 5, 9, 11, 12] -> [(3, 5), (9, 9), (11, 12)]: a model's diagrams span consecutive pages."""
    runs = []
    for p in sorted(set(pages)):
        if runs and p - runs[-1][1] <= 1:
            runs[-1][1] = p
        else:
            runs.append([p, p])
    return [tuple(r) for r in runs]


def snippet(text, terms, width=70):
    """A bit of text around the first matching term, HTML-escaped, with the match in <mark>."""
    flat = " ".join(text.split())
    low = flat.lower()
    hits = [(low.find(t.lower()), t) for t in terms if low.find(t.lower()) >= 0]
    if not hits:
        return escape(flat[: width * 2])
    at, term = min(hits)
    start, end = max(at - width, 0), at + len(term)
    out = ("…" if start else "") + escape(flat[start:at]) + Markup("<mark>") + escape(flat[at:end]) + Markup("</mark>")
    return Markup(out + escape(flat[end:end + width]) + ("…" if end + width < len(flat) else ""))


def search(conn, terms, allowed, max_books=40, max_runs=5, max_pages=3000):
    """Pages matching any of `terms` in the `allowed` paths, grouped by book (best first) and collapsed into runs."""
    terms = [t for t in dict.fromkeys(" ".join(t.split()) for t in terms) if t]
    scores = {}
    for term in terms:
        if len(term) >= 3:
            rows = conn.execute("""SELECT p.path, p.page, bm25(ocr_fts) AS score FROM ocr_fts
                                   JOIN ocr_pages p ON p.rowid = ocr_fts.rowid
                                   WHERE ocr_fts MATCH ? ORDER BY score LIMIT ?""",
                                ('"' + term.replace('"', '""') + '"', max_pages))
        else:
            # Trigrams can't match one- or two-character terms (most Japanese words), so scan instead.
            rows = conn.execute("SELECT path, page, text FROM ocr_pages WHERE text LIKE ? LIMIT ?",
                                (f"%{term}%", max_pages)).fetchall()
            if _KATAKANA.match(term):
                # A short katakana word inside a longer one isn't a match: サイ (rhino) in サイズ (size).
                whole = re.compile(re.escape(term) + "(?![\u30a0-\u30ff])")
                rows = [r for r in rows if whole.search(r["text"])]
            rows = [(path, page, 0.0) for path, page, _ in rows]
        for path, page, score in rows:
            if path in allowed:
                key = (path, page)
                scores[key] = min(scores.get(key, 0.0), score)
    by_book = {}
    for (path, page), score in scores.items():
        by_book.setdefault(path, {})[page] = score
    results = []
    for path, pages in by_book.items():
        runs = []
        for start, end in collapse_runs(pages):
            best = min(pages[p] for p in range(start, end + 1) if p in pages)
            runs.append({"start": start, "end": end, "score": best})
        runs.sort(key=lambda r: (r["score"], r["start"]))
        runs = sorted(runs[:max_runs], key=lambda r: r["start"])
        for run in runs:
            text = conn.execute("SELECT text FROM ocr_pages WHERE path = ? AND page = ?", (path, run["start"])).fetchone()[0]
            run["snippet"] = snippet(text, terms)
        results.append({"path": path, "runs": runs, "pages": len(pages), "score": min(pages.values())})
    results.sort(key=lambda b: (b["score"], -b["pages"], b["path"].lower()))
    return results[:max_books]


def get_page(conn, path, page):
    return conn.execute("SELECT source, text, error, attempts FROM ocr_pages WHERE path = ? AND page = ?",
                        (path, page)).fetchone()


def sizes_and_mtimes(paths):
    """{path: (size, mtime)} for the given scanned paths, skipping any that vanished meanwhile."""
    out = {}
    for rel in paths:
        try:
            st = (config.BOOKS_DIR / rel).stat()
        except OSError:
            continue
        out[rel] = (st.st_size, st.st_mtime)
    return out

