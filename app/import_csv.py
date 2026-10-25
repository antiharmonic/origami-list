"""Import models from a CSV file.

Accepts both the original hand-made format
    model,author,complexity,suggested_paper,book,file,page,folded,notes
and this app's own /export.csv format (whose "studies" column is names joined with "; ").

Usage: python -m app.import_csv FILE   (use "-" to read stdin)
"""
import csv
import io
import sys

from . import books, db

# Our field name -> accepted CSV header names, first match wins.
ALIASES = {
    "name": ["name", "model"],
    "designer": ["designer", "author"],
    "complexity": ["complexity"],
    "paper": ["paper", "suggested_paper"],
    "book_title": ["book_title", "book"],
    "file": ["book_path", "file"],
    "page": ["page"],
    "url": ["url"],
    "status": ["status"],
    "folded": ["folded"],
    "notes": ["notes"],
    "studies": ["studies"],
}


def _get(row, field):
    for header in ALIASES[field]:
        value = row.get(header)
        if value is not None and value.strip():
            return value.strip()
    return ""


def _int(value):
    try:
        return int(value)
    except ValueError:
        return None


def import_text(conn, text):
    """Import every row of a CSV string. Returns (imported count, list of warnings)."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    reader.fieldnames = [h.strip().lower() for h in reader.fieldnames or []]
    imported, warnings = 0, []
    for line, row in enumerate(reader, start=2):
        name = _get(row, "name")
        if not name:
            warnings.append(f"line {line}: no model name, skipped")
            continue
        status = _get(row, "status").lower()
        if status not in db.STATUSES:
            status = "folded" if _get(row, "folded").lower() in ("yes", "y", "true", "1") else "want"
        fields = {
            "name": name,
            "designer": _get(row, "designer"),
            "complexity": _int(_get(row, "complexity")),
            "paper": _get(row, "paper"),
            "page": _int(_get(row, "page")),
            "url": _get(row, "url") or None,
            "status": status,
            "notes": _get(row, "notes"),
        }
        # Spreadsheets sometimes keep a leading apostrophe on paths.
        file = _get(row, "file").lstrip("'\"")
        if file:
            rel = file if books.resolve(file) else books.find_by_basename(file)
            if rel:
                fields["book_id"] = db.get_or_create_book(conn, rel)
                db.mark_browsing(conn, fields["book_id"])
            else:
                book_title = _get(row, "book_title") or file
                warnings.append(f"line {line}: book file not found for {name!r}: {file}")
                fields["notes"] = f"[Book: {book_title}] {fields['notes']}".strip()
        model_id = db.create_model(conn, fields)
        studies = [db.study_by_name(conn, s) for s in _get(row, "studies").split(";") if s.strip()]
        if studies:
            db.set_model_studies(conn, model_id, studies)
        imported += 1
    return imported, warnings


def main(argv):
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    text = sys.stdin.read() if argv[1] == "-" else open(argv[1], encoding="utf-8-sig").read()
    conn = db.connect()
    db.migrate(conn)
    imported, warnings = import_text(conn, text)
    for w in warnings:
        print("warning:", w, file=sys.stderr)
    print(f"imported {imported} model(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
