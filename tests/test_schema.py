"""Database migrations and settings.

- Migrations upgrade old databases without losing data (books used by models become "browsing"; translations gain page ranges).
- origami.db runs in WAL mode.
"""
from app import db


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


def test_translations_migration_keeps_rows(tmp_path):
    c = db.connect(tmp_path / "old.db")
    c.execute(f"PRAGMA user_version = {len(db.MIGRATIONS) - 1}")
    for sql in db.MIGRATIONS[:-1]:
        c.executescript(sql)
    c.execute("INSERT INTO books (path, title) VALUES ('a.pdf', 'A')")
    c.execute("INSERT INTO translations (book_id, page, model, body, created_at) VALUES (1, 7, 'm', 'Seven', 'now')")
    c.commit()
    db.migrate(c)
    assert db.get_translation(c, 1, 7)["body"] == "Seven"
    assert c.execute("SELECT page_end FROM translations").fetchone()[0] == 7
    c.close()


def test_origami_db_uses_wal(conn):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
