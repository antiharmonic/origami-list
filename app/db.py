import sqlite3
from datetime import datetime, timezone

from . import config
from .books import clean_title

STATUSES = ["want", "attempted", "folded", "nailed"]
STATUS_LABELS = {"want": "Want", "attempted": "Attempted", "folded": "Folded", "nailed": "Nailed it"}
BOOK_STATUSES = ["unexplored", "browsing", "explored", "skip"]
BOOK_STATUS_LABELS = {"unexplored": "Unexplored", "browsing": "Browsing", "explored": "Explored", "skip": "Skip"}
BOOK_FIELDS = ["title", "page_offset", "status", "designers", "notes", "cover_page", "starred", "hidden"]
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
    """
    CREATE TABLE notes (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        body TEXT NOT NULL DEFAULT '',
        rev INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT
    );
    INSERT INTO notes (id) VALUES (1);
    """,
    """
    ALTER TABLE books ADD COLUMN starred INTEGER NOT NULL DEFAULT 0;
    """,
    """
    ALTER TABLE books ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0;
    """,
    """
    CREATE TABLE studies (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE COLLATE NOCASE,
        notes TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL
    );
    CREATE TABLE model_studies (
        model_id INTEGER NOT NULL REFERENCES models(id) ON DELETE CASCADE,
        study_id INTEGER NOT NULL REFERENCES studies(id) ON DELETE CASCADE,
        PRIMARY KEY (model_id, study_id)
    );
    CREATE INDEX model_studies_study ON model_studies(study_id);
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


def hidden_paths(conn):
    return {r[0] for r in conn.execute("SELECT path FROM books WHERE hidden = 1")}


def relink_book(conn, book_id, target, shift=0):
    """Point a book and its models at another file (a rename, or a better scan); returns the target's id.

    `shift` moves every model's page (and the offset) for scans with more/fewer front pages. If the
    target already has a row, the two are merged into it and this one is deleted.
    """
    old = get_book(conn, book_id)
    existing = get_book_by_path(conn, target)
    with conn:
        if shift:
            conn.execute("UPDATE models SET page = MAX(page + ?, 1) WHERE book_id = ? AND page IS NOT NULL",
                         (shift, book_id))
        offset = old["page_offset"] + shift
        if existing is None:
            # A title still at its filename-derived default follows the new file.
            title = clean_title(target) if old["title"] == clean_title(old["path"]) else old["title"]
            conn.execute("UPDATE books SET path = ?, title = ?, page_offset = ?, hidden = 0 WHERE id = ?",
                         (target, title, offset, book_id))
            return book_id
        conn.execute("UPDATE models SET book_id = ? WHERE book_id = ?", (existing["id"], book_id))
        conn.execute("""
            UPDATE books SET
                designers = CASE WHEN designers = '' THEN ? ELSE designers END,
                notes = CASE WHEN notes = '' THEN ? ELSE notes END,
                page_offset = CASE WHEN page_offset = 0 THEN ? ELSE page_offset END,
                status = CASE WHEN status = 'unexplored' THEN ? ELSE status END,
                starred = MAX(starred, ?),
                hidden = 0
            WHERE id = ?""",
            (old["designers"], old["notes"], offset, old["status"], old["starred"], existing["id"]))
        conn.execute("DELETE FROM books WHERE id = ?", (book_id,))
    return existing["id"]


def mark_browsing(conn, book_id):
    """Using a book for a model means it's being looked through."""
    with conn:
        conn.execute("UPDATE books SET status = 'browsing' WHERE id = ? AND status = 'unexplored'", (book_id,))


# --- models ---

# `studies` is "id:name" pairs joined by \x1f (see parse_studies), so every model query carries its studies.
_MODEL_SELECT = """
    SELECT m.*, b.path AS book_path, b.title AS book_title, b.page_offset AS book_offset,
           (SELECT group_concat(s.id || ':' || s.name, char(31))
            FROM model_studies ms JOIN studies s ON s.id = ms.study_id WHERE ms.model_id = m.id) AS studies
    FROM models m LEFT JOIN books b ON b.id = m.book_id
"""

SORTS = {
    "mine": "m.position",
    "complexity": "m.complexity IS NULL, m.complexity, m.position",
    "book": "b.title IS NULL, b.title COLLATE NOCASE, m.page, m.position",
}


def list_models(conn, statuses=None, q="", sort="mine", book_id=None, study=None):
    """`study` is ("in" | "out", study_id) to keep only, or drop, a study's models."""
    where, params = [], []
    if study:
        mode, study_id = study
        exists = "EXISTS (SELECT 1 FROM model_studies ms WHERE ms.model_id = m.id AND ms.study_id = ?)"
        where.append(exists if mode == "in" else "NOT " + exists)
        params.append(study_id)
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


# --- notes (a single global document) ---

def get_notes(conn):
    return conn.execute("SELECT body, rev, updated_at FROM notes WHERE id = 1").fetchone()


def save_notes(conn, body, rev=None):
    """Save and return the new rev, or None if rev is given and another save got there first."""
    sql = "UPDATE notes SET body = ?, rev = rev + 1, updated_at = ? WHERE id = 1"
    params = [body, _now()]
    if rev is not None:
        sql += " AND rev = ?"
        params.append(rev)
    with conn:
        if conn.execute(sql, params).rowcount == 0:
            return None
    return get_notes(conn)["rev"]


# --- studies (named groups of models) ---

def parse_studies(value):
    """The `studies` column of a model row -> [(id, name)]."""
    pairs = (item.split(":", 1) for item in (value or "").split("\x1f") if item)
    return sorted(((int(i), name) for i, name in pairs), key=lambda p: p[1].lower())


def list_studies(conn):
    return conn.execute("""
        SELECT s.*, COUNT(m.id) AS model_count,
               COUNT(CASE WHEN m.status IN ('folded', 'nailed') THEN 1 END) AS done_count,
               COUNT(CASE WHEN m.status = 'attempted' THEN 1 END) AS attempted_count
        FROM studies s
        LEFT JOIN model_studies ms ON ms.study_id = s.id
        LEFT JOIN models m ON m.id = ms.model_id
        GROUP BY s.id ORDER BY s.created_at DESC, s.id DESC""").fetchall()


def get_study(conn, study_id):
    return next((s for s in list_studies(conn) if s["id"] == study_id), None)


def study_by_name(conn, name, create=True):
    """Case-insensitive lookup ("rhino" is "Rhino"); returns the id, creating the study if asked."""
    name = " ".join(name.split())
    if not name:
        return None
    row = conn.execute("SELECT id FROM studies WHERE name = ?", (name,)).fetchone()
    if row or not create:
        return row["id"] if row else None
    with conn:
        return conn.execute("INSERT INTO studies (name, created_at) VALUES (?, ?)", (name, _now())).lastrowid


def update_study(conn, study_id, name, notes):
    with conn:
        conn.execute("UPDATE studies SET name = ?, notes = ? WHERE id = ?", (" ".join(name.split()), notes, study_id))


def delete_study(conn, study_id):
    """Removes the study and its memberships; the models stay."""
    with conn:
        conn.execute("DELETE FROM studies WHERE id = ?", (study_id,))


def set_model_studies(conn, model_id, study_ids):
    with conn:
        conn.execute("DELETE FROM model_studies WHERE model_id = ?", (model_id,))
        conn.executemany("INSERT OR IGNORE INTO model_studies (model_id, study_id) VALUES (?, ?)",
                         [(model_id, s) for s in study_ids])


def add_to_study(conn, study_id, model_ids):
    with conn:
        conn.executemany("INSERT OR IGNORE INTO model_studies (model_id, study_id) VALUES (?, ?)",
                         [(m, study_id) for m in model_ids])


def remove_from_study(conn, study_id, model_id):
    with conn:
        conn.execute("DELETE FROM model_studies WHERE study_id = ? AND model_id = ?", (study_id, model_id))
