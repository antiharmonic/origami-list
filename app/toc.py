"""Suggested models from a book's contents page(s), read from its OCR'd text.

Pure functions over {page: text}: find the pages that look like a table of contents, parse their
"Name ..... 42" lines, work out how printed page numbers map to PDF pages, and mark the entries
whose name really appears on the page they point at. It's a suggestion list for the user to
accept or dismiss, not an importer: OCR'd contents pages are messy.
"""
import re
from collections import Counter

from .ocr import normalize

VERSION = 1  # bump to re-extract every book (earlier added/dismissed decisions are kept)
SCAN_PAGES = 20  # contents pages are near the front
MIN_LINES = 6
MIN_LINES_TITLED = 3  # on a page that says "Contents" / 目次
MIN_ORDERED = 0.7
MIN_DISTINCT = 0.6
MIN_OFFSET_HITS = 3
OFFSET_SHARE = 0.3
NEAR = 2  # a name found this close to its target page counts as verified

_JA = "぀-ヿ㐀-鿿ｦ-ﾟ"
_HAS_JA = re.compile(f"[{_JA}]")
# A page number: 1-3 digits standing alone, possibly after leader dots ("Angelfish ..... 90").
_NUMBER = re.compile(r"(?:^|(?<=[\s.·…_\-]))(\d{1,3})(?=\s|$)")
_JUNK = " \t.,:;·…_-—–|/\\*•'\"“”‘’()[]{}<>~=+。、・ー"
_PAGE_WORD = re.compile(r"\s*\b(pages?|pp?|pg)\.?$", re.I)  # "Statue of Liberty page 74"
_TITLED = re.compile(r"contents|目次|もくじ", re.I)
# Front and back matter that contents pages list alongside the models.
_STOP = re.compile(
    r"^(the )?(table of )?contents?\b|foreword|introduction|preface|acknowledg|symbols?\b|procedures|"
    r"\bbases?\b|basic folds|folding (tips|techniques)|\bindex\b|^notes?\b|sources|credits|about the (author|designer)|"
    r"bibliography|glossary|^history|materials|^how to|^chapter\b|^part\b|^section\b|^appendix|^resources|"
    r"折り図|記号|基本形|目次|もくじ|はじめに|あとがき|索引|奥付",
    re.I)


def _clean(name):
    name = " ".join(name.split()).strip(_JUNK)
    # OCR leaves stray one-letter tokens at either end ("| MEET THE MASTER", "Horse ば").
    parts = name.split()
    while len(parts) > 1 and len(parts[-1]) == 1:
        parts.pop()
    while len(parts) > 1 and len(parts[0]) == 1:
        parts.pop(0)
    return " ".join(parts).strip(_JUNK)


_SMALL = {"a", "an", "and", "at", "by", "for", "in", "of", "on", "or", "the", "to", "with"}


def _tidy_case(name):
    """"KNIGHT ON HORSEBACK" -> "Knight on Horseback": contents pages love capitals; the list doesn't."""
    if _HAS_JA.search(name) or not name.isupper():
        return name
    words = name.lower().split()
    return " ".join(w if i and w in _SMALL else "-".join(p[:1].upper() + p[1:] for p in w.split("-"))
                    for i, w in enumerate(words))


def _usable(name):
    letters = sum(ch.isalpha() for ch in name)
    return len(name) >= 3 and letters >= max(2, len(name) // 2) and not _STOP.search(name)


def _segments(line):
    """[(name, number), ...] for one line; two-column layouts give two."""
    out, start = [], 0
    for m in _NUMBER.finditer(line):
        name = line[start:m.start()]
        if name.strip(_JUNK):
            out.append((name, int(m.group(1))))
        start = m.end()
    return out, line[start:].strip(_JUNK)


def _ordered(numbers):
    """Page numbers in a contents list mostly go up, and differ (a numbered list repeats "1", "3")."""
    if len(numbers) < 2:
        return True
    rising = sum(b >= a for a, b in zip(numbers, numbers[1:])) / (len(numbers) - 1)
    return rising >= MIN_ORDERED and len(set(numbers)) >= MIN_DISTINCT * len(numbers)


def contents_entries(text):
    """The (name, printed page, designer) entries on a page, or [] if it doesn't look like a contents page."""
    lines = [l for l in text.splitlines() if l.strip()]
    parsed = []
    for i, line in enumerate(lines):
        segs, tail = _segments(line)
        if not segs or tail:
            parsed.append(None)  # a line that doesn't end in a page number isn't an entry
            continue
        parsed.append(segs)
    rows = [p for p in parsed if p]
    columns = {}
    for segs in rows:
        for col, (_, n) in enumerate(segs):
            columns.setdefault(col, []).append(n)
    need = MIN_LINES_TITLED if _TITLED.search(text) else MIN_LINES
    if len(rows) < need or not all(_ordered(nums) for nums in columns.values() if len(nums) >= 3):
        return []
    entries = []
    for i, segs in enumerate(parsed):
        if not segs:
            continue
        for name, page in segs:
            name = _clean(_PAGE_WORD.sub("", _clean(name)))
            if not name and len(segs) == 1 and i and parsed[i - 1] is None:
                name = _clean(lines[i - 1])  # the name on its own line above "page 74"
            designer = ""
            # Japanese contents often credit the designer after a slash: "うさぎ / 鶴田芳理".
            if _HAS_JA.search(name) and "/" in name:
                parts = [_clean(x) for x in name.split("/")]
                parts = [x for x in parts if x]
                if len(parts) >= 2:
                    name, designer = parts[0], parts[-1]
            # Japanese-first books put the English on the next line ("ユニコーン 147" / "Unicorn Tetsuya Gotani").
            if len(segs) == 1 and _HAS_JA.search(name) and i + 1 < len(lines) and parsed[i + 1] is None:
                alt = _clean(lines[i + 1])
                if alt and not _HAS_JA.search(alt) and sum(c.isalpha() for c in alt) >= 3:
                    name = f"{alt} ({name})"
            if _usable(name):
                entries.append((_tidy_case(name), page, designer))
    return entries


def _key(text):
    return " ".join(normalize(text).lower().split())


def _found_on(name, pages):
    """The PDF pages whose text contains `name` (or, for "English (日本語)", either part)."""
    m = re.match(r"^(.*?) \((.*)\)$", name)
    keys = [_key(k) for k in ((m.group(1), m.group(2)) if m else (name,))]
    keys = [k for k in keys if len(k) >= 3 or _HAS_JA.search(k) and len(k) >= 2]
    return {p for p, text in pages.items() if any(k in text for k in keys)}


def extract(pages, offset=0):
    """Suggestions from {pdf page: text} -> (entries, info).

    entries: dicts with name, designer, printed, page (PDF page), verified. info: contents pages, the offset
    used and whether it was detected. A non-zero `offset` (the book's own) is used as given.
    """
    front = {p: t for p, t in pages.items() if p <= SCAN_PAGES}
    found = {}
    toc_pages = []
    for p in sorted(front):
        entries = contents_entries(front[p])
        if entries:
            toc_pages.append(p)
            for name, printed, designer in entries:
                found.setdefault((name.lower(), printed), (name, printed, designer))
    info = {"toc_pages": toc_pages, "offset": offset, "detected": False}
    if not found:
        return [], info
    body = {p: _key(t) for p, t in pages.items() if p not in toc_pages}
    hits = {k: _found_on(name, body) for k, (name, *_) in found.items()}
    if not offset:
        diffs = Counter(p - printed for (_, printed), ps in hits.items() for p in ps if p - printed >= -5)
        if diffs:
            best, count = diffs.most_common(1)[0]
            if count >= max(MIN_OFFSET_HITS, OFFSET_SHARE * len(found)):
                offset, info["detected"] = best, True
        info["offset"] = offset
    entries = []
    for k, (name, printed, designer) in found.items():
        page = printed + offset
        if page < 1:
            continue
        verified = any(abs(p - page) <= NEAR for p in hits[k])
        entries.append({"name": name, "designer": designer, "printed": printed, "page": page,
                        "verified": verified})
    entries.sort(key=lambda e: (e["page"], e["name"].lower()))
    return entries, info
