import os
from pathlib import Path

# Read at call time by the other modules (config.BOOKS_DIR), so tests can override them.
BOOKS_DIR = Path(os.environ.get("BOOKS_DIR", "/books"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
# The PDF.js viewer, downloaded at image build (see Dockerfile); absent in tests.
PDFJS_DIR = Path(os.environ.get("PDFJS_DIR", "/srv/pdfjs"))


def gemini_key():
    """The Gemini API key from .env (see README, "Translating pages"); empty when translation isn't set up."""
    return os.environ.get("GEMINI_API_KEY", "").strip()


def gemini_models():
    """Models to try in order: GEMINI_MODEL (default: the current stable Flash), then Flash-Lite when that's busy."""
    first = os.environ.get("GEMINI_MODEL", "").strip() or "gemini-flash-latest"
    return list(dict.fromkeys([first, "gemini-flash-lite-latest"]))
