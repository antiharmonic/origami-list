import sqlite3
from datetime import datetime, timezone

from . import config
from .books import clean_title

STATUSES = ["want", "attempted", "folded", "nailed"]
STATUS_LABELS = {"want": "Want", "attempted": "Attempted", "folded": "Folded", "nailed": "Nailed it"}
BOOK_STATUSES = ["unexplored", "browsing", "explored", "skip"]
BOOK_STATUS_LABELS = {"unexplored": "Unexplored", "browsing": "Browsing", "explored": "Explored", "skip": "Skip"}
BOOK_FIELDS = ["title", "page_offset", "status", "designers", "notes", "cover_page"]
MODEL_FIELDS = ["name", "designer", "book_id", "page", "url", "complexity", "paper", "status", "notes", "photo"]

# Each entry upgrades the schema by one version (tracked in PRAGMA user_version).
MIGRATIONS = [
    """
    CREATE TABLE books (
        id INTEGER PRIMARY KEY,
        path TEXT NOT NULL UNIQUE,
        title TEXT NOT NULL,
        page_offset INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE models (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        designer TEXT NOT NULL DEFAULT '',
        book_id INTEGER REFERENCES books(id) ON DELETE SET NULL,
        page INTEGER,
        url TEXT,
        complexity INTEGER,
        paper TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'want',
        position REAL NOT NULL,
        notes TEXT NOT NULL DEFAULT '',
        photo TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX models_position ON models(position);
    """,
    """
    ALTER TABLE books ADD COLUMN status TEXT NOT NULL DEFAULT 'unexplored';
    ALTER TABLE books ADD COLUMN designers TEXT NOT NULL DEFAULT '';
    ALTER TABLE books ADD COLUMN notes TEXT NOT NULL DEFAULT '';
    ALTER TABLE books ADD COLUMN cover_page INTEGER NOT NULL DEFAULT 1;
    UPDATE books SET status = 'browsing' WHERE id IN (SELECT book_id FROM models WHERE book_id IS NOT NULL);
    """,
    """
    CREATE TABLE book_files (
        path TEXT PRIMARY KEY,
        added_at REAL NOT NULL
    );
    """,
]

# Below this gap between neighbours, positions are renumbered 1..N.
MIN_GAP = 1e-6


def connect(path=None):
    # One connection per request; async routes may use it from a different thread than created it.
    conn = sqlite3.connect(path or config.DATA_DIR / "origami.db", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def migrate(conn):
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for i, sql in enumerate(MIGRATIONS[version:], start=version + 1):
        with conn:
            conn.executescript(sql)
            conn.execute(f"PRAGMA user_version = {i}")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- books ---

def get_or_create_book(conn, path):
    row = conn.execute("SELECT id FROM books WHERE path = ?", (path,)).fetchone()
    if row:
        return row["id"]
    with conn:
        cur = conn.execute("INSERT INTO books (path, title) VALUES (?, ?)", (path, clean_title(path)))
    return cur.lastrowid


def get_book(conn, book_id):
    return conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()


def get_book_by_path(conn, path):
    return conn.execute("SELECT * FROM books WHERE path = ?", (path,)).fetchone()


def library_rows(conn, path=None):
    """Book rows with their model counts, keyed by path (optionally just one path)."""
    sql = """SELECT b.*, COUNT(m.id) AS model_count
             FROM books b LEFT JOIN models m ON m.book_id = b.id"""
    params = []
    if path is not None:
        sql += " WHERE b.path = ?"
        params.append(path)
    sql += " GROUP BY b.id"
    return {r["path"]: r for r in conn.execute(sql, params)}


def record_added(conn, ctimes):
    """Remember when each file was first seen (its ctime then); later ctime changes don't move it."""
    with conn:
        conn.executemany("INSERT OR IGNORE INTO book_files (path, added_at) VALUES (?, ?)", ctimes.items())
    return dict(conn.execute("SELECT path, added_at FROM book_files").fetchall())


def update_book_meta(conn, book_id, fields):
    data = {k: fields[k] for k in BOOK_FIELDS if k in fields}
    if not data:
        return
    sets = ", ".join(f"{k} = ?" for k in data)
    with conn:
        conn.execute(f"UPDATE books SET {sets} WHERE id = ?", [*data.values(), book_id])


def mark_browsing(conn, book_id):
    """Using a book for a model means it's being looked through."""
    with conn:
        conn.execute("UPDATE books SET status = 'browsing' WHERE id = ? AND status = 'unexplored'", (book_id,))


# --- models ---

_MODEL_SELECT = """
    SELECT m.*, b.path AS book_path, b.title AS book_title, b.page_offset AS book_offset
    FROM models m LEFT JOIN books b ON b.id = m.book_id
"""

SORTS = {
    "mine": "m.position",
    "complexity": "m.complexity IS NULL, m.complexity, m.position",
    "book": "b.title IS NULL, b.title COLLATE NOCASE, m.page, m.position",
}


def list_models(conn, statuses=None, q="", sort="mine", book_id=None):
    where, params = [], []
    if book_id is not None:
        where.append("m.book_id = ?")
        params.append(book_id)
    if statuses:
        where.append(f"m.status IN ({','.join('?' * len(statuses))})")
        params += statuses
    for term in q.split():
        where.append("(m.name || ' ' || m.designer || ' ' || m.notes || ' ' || m.paper || ' ' || COALESCE(b.title, '')) LIKE ?")
        params.append(f"%{term}%")
    sql = _MODEL_SELECT
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY " + SORTS.get(sort, SORTS["mine"])
    return conn.execute(sql, params).fetchall()


def count_models(conn, statuses=None):
    sql, params = "SELECT COUNT(*) FROM models", []
    if statuses:
        sql += f" WHERE status IN ({','.join('?' * len(statuses))})"
        params = list(statuses)
    return conn.execute(sql, params).fetchone()[0]


def get_model(conn, model_id):
    return conn.execute(_MODEL_SELECT + " WHERE m.id = ?", (model_id,)).fetchone()


def create_model(conn, fields, at_top=False):
    agg = "MIN(position) - 1" if at_top else "MAX(position) + 1"
    position = conn.execute(f"SELECT COALESCE({agg}, 1) FROM models").fetchone()[0]
    data = {k: fields.get(k) for k in MODEL_FIELDS if k in fields}
    data.update(position=position, created_at=_now(), updated_at=_now())
    cols = ", ".join(data)
    with conn:
        cur = conn.execute(f"INSERT INTO models ({cols}) VALUES ({', '.join('?' * len(data))})", list(data.values()))
    return cur.lastrowid


def update_model(conn, model_id, fields):
    data = {k: fields[k] for k in MODEL_FIELDS if k in fields}
    data["updated_at"] = _now()
    sets = ", ".join(f"{k} = ?" for k in data)
    with conn:
        conn.execute(f"UPDATE models SET {sets} WHERE id = ?", [*data.values(), model_id])


def delete_model(conn, model_id):
    with conn:
        conn.execute("DELETE FROM models WHERE id = ?", (model_id,))


def _position(conn, model_id):
    if model_id is None:
        return None
    row = conn.execute("SELECT position FROM models WHERE id = ?", (model_id,)).fetchone()
    return row["position"] if row else None


def renormalize(conn):
    ids = [r["id"] for r in conn.execute("SELECT id FROM models ORDER BY position, id")]
    with conn:
        conn.executemany("UPDATE models SET position = ? WHERE id = ?", [(i, mid) for i, mid in enumerate(ids, 1)])


def move_model(conn, model_id, prev_id=None, next_id=None):
    """Place model_id between prev_id (the row above it) and next_id (the row below it)."""
    for attempt in range(2):
        prev_pos, next_pos = _position(conn, prev_id), _position(conn, next_id)
        if prev_pos is None and next_pos is None:
            return
        if prev_pos is None:
            pos = next_pos - 1
        elif next_pos is None:
            pos = prev_pos + 1
        elif next_pos - prev_pos > MIN_GAP or attempt:
            pos = (prev_pos + next_pos) / 2
        else:
            renormalize(conn)
            continue
        with conn:
            conn.execute("UPDATE models SET position = ?, updated_at = ? WHERE id = ?", (pos, _now(), model_id))
        return
