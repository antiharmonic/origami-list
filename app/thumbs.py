import hashlib
import subprocess
import uuid

from . import config
from .books import resolve

WIDTH = 400


def thumbs_dir():
    return config.DATA_DIR / "thumbs"


def thumbnail(book_rel, page):
    """Path to a cached PNG of one PDF page, rendering it first if needed. None if it can't be rendered."""
    src = resolve(book_rel)
    if src is None or page < 1:
        return None
    key = hashlib.sha1(f"{book_rel}|{int(src.stat().st_mtime)}".encode()).hexdigest()[:16]
    out = thumbs_dir() / f"{key}-{page}.png"
    if out.exists():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    # Render to a temp name, then rename, so a half-written file is never served.
    tmp_prefix = out.with_name(f".{out.stem}.{uuid.uuid4().hex}")
    try:
        subprocess.run(
            ["pdftoppm", "-f", str(page), "-l", str(page), "-scale-to-x", str(WIDTH), "-scale-to-y", "-1", "-png", "-singlefile",
             str(src), str(tmp_prefix)],
            check=True, capture_output=True, timeout=60,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    tmp = tmp_prefix.with_name(tmp_prefix.name + ".png")
    if not tmp.exists():
        return None
    tmp.replace(out)
    return out
