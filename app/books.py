import re
import time
from pathlib import Path, PurePosixPath

from . import config

SCAN_TTL = 300
_cache = {"at": 0.0, "files": [], "set": frozenset(), "ctimes": {}}

# Download-site tags and similar noise that shows up in filenames.
_JUNK = re.compile(r"\s*\((?:[^)]*z-lib[^)]*|[^)]*1lib[^)]*)\)", re.IGNORECASE)


def scan(force=False):
    """Relative POSIX paths of every PDF under BOOKS_DIR, cached for SCAN_TTL seconds."""
    if force or time.monotonic() - _cache["at"] > SCAN_TTL:
        root = config.BOOKS_DIR
        ctimes = {}
        if root.is_dir():
            for p in root.rglob("*"):
                if p.suffix.lower() == ".pdf" and "__MACOSX" not in p.parts and p.is_file():
                    ctimes[p.relative_to(root).as_posix()] = p.stat().st_ctime
        _cache.update(at=time.monotonic(), files=sorted(ctimes, key=str.lower), set=frozenset(ctimes), ctimes=ctimes)
    return _cache["files"]


def exists(rel):
    """Whether the last scan saw this path (no disk access, so it's cheap per list row)."""
    scan()
    return rel in _cache["set"]


def ctimes():
    """{path: ctime} from the last scan. ctime is when the file landed in the folder (mtime is
    often years older for downloads), so it's what "date added" is first recorded from."""
    scan()
    return _cache["ctimes"]


def folder(rel):
    parent = PurePosixPath(rel).parent.as_posix()
    return "" if parent == "." else parent


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
