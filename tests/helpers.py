"""Shared test data and helpers (fixtures live in conftest.py)."""
import shutil

import pytest

from app import db, ocr

# The two books conftest's `dirs` fixture puts in the temp books folder, plus a third some tests add.
NATURE = "sub/Kato - Nature Study.pdf"
INSECTS = "Lang - Origami Insects (z-library.sk, 1lib.sk).pdf"
HQ = "Lang - Insects HQ.pdf"

# The user's original hand-made CSV format.
ORIGINAL_CSV = """model,author,complexity,suggested_paper,book,file,page,folded,notes
Simple Dragonfly,Shuki Kato,3,foil,Origami Nature Study,'/home/someone/Books/Kato - Nature Study.pdf,24,Yes,Nice.
Mystery,Someone,,,,'/nowhere/missing.pdf,5,No,
,No Name,,,,,,,
"""

RHINO_TEXT = ["Rhinoceros by Fumiaki Kawahata", "Fold the square in half and unfold.", "Several varieties of this animal."]

# An Engel-style contents page: models mixed with front and back matter.
ENGEL_TOC = """CONTENTS

ACKNOWLEDGMENTS vii
FOREWORD 1
SYMBOLS AND PROCEDURES 8
ANGELFISH ......... 20
BUTTERFLY FISH 22
| KANGAROO 25
GIRAFFE 29
PENGUIN 33
NOTES AND SOURCES 40
"""

# A fake article translation of pages 2-4, with the per-page headings Gemini is asked for.
ARTICLE = "### Page 2\nThe story begins.\n\n### Page 3\nIt continues.\n\n### Page 4\nThe end."

needs_tesseract = pytest.mark.skipif(not shutil.which("tesseract"), reason="tesseract not installed")


def order(conn):
    """Model names in list order."""
    return [m["name"] for m in db.list_models(conn)]


def study_ids(conn, model_id):
    return [sid for sid, _ in db.parse_studies(db.get_model(conn, model_id)["studies"])]


def add_ocr_pages(rows):
    """Put fake OCR'd pages into ocr.db: [(path, page, text)]."""
    conn = ocr.connect()
    with conn:
        conn.executemany("INSERT INTO ocr_pages (path, page, source, text) VALUES (?, ?, 'ocr', ?)", rows)
    conn.close()


def ocr_done(path, pages):
    """A book whose OCR has finished, with the given {page: text}."""
    add_ocr_pages([(path, p, t) for p, t in pages.items()])
    conn = ocr.connect()
    with conn:
        conn.execute("INSERT INTO ocr_books (path, size, mtime, pages, done_pages, state) VALUES (?, 1, 1, ?, ?, 'done')",
                     (path, len(pages), len(pages)))
    conn.close()


def toc_book(offset=4):
    """{page: text} for a book with ENGEL_TOC on page 3 and its models at printed page + offset."""
    pages = {3: ENGEL_TOC}
    for name, printed in [("ANGELFISH", 20), ("BUTTERFLY FISH", 22), ("KANGAROO", 25), ("GIRAFFE", 29)]:
        pages[printed + offset] = f"{name}\nFold in half."
    return pages
