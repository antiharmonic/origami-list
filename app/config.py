import os
from pathlib import Path

# Read at call time by the other modules (config.BOOKS_DIR), so tests can override them.
BOOKS_DIR = Path(os.environ.get("BOOKS_DIR", "/books"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
