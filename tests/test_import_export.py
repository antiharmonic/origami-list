"""CSV import and export.

- The user's original hand-made CSV imports: columns are recognised by alias, books are matched by path then filename, and a missing book becomes a warning with the model still imported.
- The app's own export imports again with nothing lost that CSV can carry, including studies.
"""
from app import db, import_csv
from tests.helpers import ORIGINAL_CSV, order


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


def test_studies_survive_csv_round_trip(client, conn):
    client.post("/models", data={"name": "Rhino", "new_study": "Rhinos"})
    exported = client.get("/export.csv").text
    assert exported.splitlines()[1].endswith(",Rhinos")
    sid = db.study_by_name(conn, "Rhinos", create=False)
    db.delete_study(conn, sid)
    import_csv.import_text(conn, exported)
    (study,) = db.list_studies(conn)
    assert study["name"] == "Rhinos" and study["model_count"] == 1
