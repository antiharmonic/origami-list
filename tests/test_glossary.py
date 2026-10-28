"""The Japanese -> English glossary.

- Seeded entries load once; the user's edits win and deleted seeds stay deleted.
- Suggestions: サイ -> "Rhinoceros (サイ)"; 川畑文昭 or "KAWAHATA Fumiaki" -> "Fumiaki Kawahata".
- Saving a model named "English (日本語)" teaches the glossary.
- The glossary page lists, adds, edits and deletes entries.
"""
from app import db
from tests.helpers import NATURE


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


def test_glossary_page(client, conn):
    client.post("/glossary", data={"kind": "designer", "ja": "加々美均", "en": "Hitoshi Kakami"})
    page = client.get("/glossary", params={"kind": "designer", "q": "Kakami"}).text
    assert 'value="加々美均"' in page and 'value="Fumiaki Kawahata"' not in page
    entry = conn.execute("SELECT id FROM glossary WHERE ja = '加々美均'").fetchone()[0]
    client.post("/glossary", data={"kind": "designer", "ja": "加々美均", "en": "Hitoshi KAKAMI", "entry_id": entry})
    assert db.suggest(conn, "designer", "加々美均") == "Hitoshi KAKAMI"
    client.post(f"/glossary/{entry}/delete", data={"kind": "designer"})
    assert db.suggest(conn, "designer", "加々美均") is None
