import os
from pathlib import Path

# Read at call time by the other modules (config.BOOKS_DIR), so tests can override them.
BOOKS_DIR = Path(os.environ.get("BOOKS_DIR", "/books"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
# The PDF.js viewer, downloaded at image build (see Dockerfile); absent in tests.
PDFJS_DIR = Path(os.environ.get("PDFJS_DIR", "/srv/pdfjs"))
