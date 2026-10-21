import re
import time
from pathlib import Path, PurePosixPath

from . import config

SCAN_TTL = 300
_cache = {"at": 0.0, "files": []}

# Download-site tags and similar noise that shows up in filenames.
_JUNK = re.compile(r"\s*\((?:[^)]*z-lib[^)]*|[^)]*1lib[^)]*)\)", re.IGNORECASE)


def scan(force=False):
    """Relative POSIX paths of every PDF under BOOKS_DIR, cached for SCAN_TTL seconds."""
    if force or time.monotonic() - _cache["at"] > SCAN_TTL:
        root = config.BOOKS_DIR
        files = []
        if root.is_dir():
            for p in root.rglob("*"):
                if p.suffix.lower() == ".pdf" and "__MACOSX" not in p.parts and p.is_file():
                    files.append(p.relative_to(root).as_posix())
        files.sort(key=str.lower)
        _cache.update(at=time.monotonic(), files=files)
    return _cache["files"]


def search(q, limit=25):
    terms = q.lower().split()
    if not terms:
        return []
    hits = [f for f in scan() if all(t in f.lower() for t in terms)]
    return hits[:limit]


def resolve(rel):
    """Absolute path for a book's relative path, or None if it's outside BOOKS_DIR or missing."""
    if not rel:
        return None
    root = config.BOOKS_DIR.resolve()
    p = (root / rel).resolve()
    if not p.is_relative_to(root) or not p.is_file():
        return None
    return p


def find_by_basename(name):
    name = PurePosixPath(name.replace("\\", "/")).name.lower()
    for f in scan():
        if PurePosixPath(f).name.lower() == name:
            return f
    return None


def clean_title(rel):
    stem = Path(rel).stem
    stem = _JUNK.sub("", stem).replace("_", " ")
    return re.sub(r"\s+", " ", stem).strip() or Path(rel).name
