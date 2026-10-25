import csv
import io
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt

from . import books, config, db, import_csv, thumbs

APP_DIR = Path(__file__).parent
PHOTO_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}
DEFAULT_STATUSES = ["want", "attempted"]
LIBRARY_DEFAULT_STATUSES = ["unexplored", "browsing"]
# Notes markdown: raw HTML is escaped, and a single newline is a line break.
MARKDOWN = MarkdownIt("commonmark", {"html": False, "breaks": True}).enable(["table", "strikethrough"])


@asynccontextmanager
async def lifespan(app):
    for sub in ("thumbs", "photos"):
        (config.DATA_DIR / sub).mkdir(parents=True, exist_ok=True)
    conn = db.connect()
    db.migrate(conn)
    conn.close()
    yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
templates = Jinja2Templates(directory=APP_DIR / "templates")
templates.env.filters["urlpath"] = lambda p: quote(p or "")
templates.env.filters["date"] = lambda ts: datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else ""


def todo_count():
    """Models still to fold, for the header badge on every page."""
    conn = db.connect()
    try:
        return db.count_models(conn, DEFAULT_STATUSES)
    finally:
        conn.close()


def books_warning():
    """A banner message when the books folder looks empty or unmounted, else None."""
    files = books.scan()
    conn = db.connect()
    try:
        known = conn.execute("SELECT COUNT(*) FROM book_files").fetchone()[0]
        paths = [r[0] for r in conn.execute("SELECT path FROM books")]
    finally:
        conn.close()
    if not files and known:
        return ("The books folder looks empty. Is it mounted? Nothing has been deleted: "
                "books and models reconnect when the files are back.")
    missing = sum(not books.exists(p) for p in paths)
    if missing >= 3 and missing * 2 > len(paths):
        return (f"{missing} of {len(paths)} books you've used are missing from the books folder. "
                "Is it mounted? Nothing has been deleted.")
    return None


templates.env.globals.update(STATUSES=db.STATUSES, STATUS_LABELS=db.STATUS_LABELS, urlencode=urlencode,
                             todo_count=todo_count, BOOK_STATUSES=db.BOOK_STATUSES,
                             BOOK_STATUS_LABELS=db.BOOK_STATUS_LABELS, books_warning=books_warning,
                             book_missing=lambda path: bool(path) and not books.exists(path))


def get_conn():
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


def _redirect(url="/"):
    return RedirectResponse(url, status_code=303)


def _opt_int(value):
    value = (value or "").strip()
    return int(value) if value.lstrip("-").isdigit() else None


# --- list ---

@app.get("/", response_class=HTMLResponse)
def index(request: Request, conn=Depends(get_conn)):
    params = request.query_params
    statuses = [s for s in params.getlist("status") if s in db.STATUSES]
    if "filtered" not in params:
        statuses = DEFAULT_STATUSES
    q = params.get("q", "")
    sort = params.get("sort", "mine")
    if sort not in db.SORTS:
        sort = "mine"
    models = db.list_models(conn, statuses, q, sort)
    return templates.TemplateResponse(request, "list.html", {
        "models": models, "statuses": statuses, "q": q, "sort": sort, "total": db.count_models(conn),
    })


@app.post("/models/{model_id}/status", response_class=HTMLResponse)
def set_status(request: Request, model_id: int, status: str = Form(...), conn=Depends(get_conn)):
    if status not in db.STATUSES:
        raise HTTPException(400, "bad status")
    db.update_model(conn, model_id, {"status": status})
    # The row, plus an out-of-band swap so the header's "to fold" badge updates too.
    row = templates.get_template("_row.html").render(m=db.get_model(conn, model_id))
    badge = templates.get_template("_todo_count.html").render(oob=True)
    return HTMLResponse(row + badge)


@app.post("/models/{model_id}/move", status_code=204)
def move(model_id: int, prev: str = Form(""), next: str = Form(""), conn=Depends(get_conn)):
    db.move_model(conn, model_id, _opt_int(prev), _opt_int(next))
    return Response(status_code=204)


# --- add / edit ---

def _form_page(request, model=None, prefill=None):
    return templates.TemplateResponse(request, "form.html", {"m": model, "prefill": prefill or {}})


@app.get("/models/new", response_class=HTMLResponse)
def new_model(request: Request, conn=Depends(get_conn)):
    prefill = dict(request.query_params)
    book = db.get_book_by_path(conn, prefill.get("book", ""))
    prefill["offset"] = book["page_offset"] if book else 0
    return _form_page(request, prefill=prefill)


@app.get("/add", response_class=HTMLResponse)
def bookmarklet_add(request: Request):
    """Target for a bookmarklet: /add?url=...&title=..."""
    p = request.query_params
    return _form_page(request, prefill={"url": p.get("url", ""), "name": p.get("title", "")})


@app.get("/models/{model_id}/edit", response_class=HTMLResponse)
def edit_model(request: Request, model_id: int, conn=Depends(get_conn)):
    model = db.get_model(conn, model_id)
    if not model:
        raise HTTPException(404)
    return _form_page(request, model=model)


async def _save_photo(model_id, photo: UploadFile | None):
    if not photo or not photo.filename:
        return None
    ext = PHOTO_TYPES.get(photo.content_type)
    if not ext:
        raise HTTPException(400, "photo must be a JPEG, PNG, WebP or GIF image")
    photos = config.DATA_DIR / "photos"
    for old in photos.glob(f"{model_id}.*"):
        old.unlink()
    name = f"{model_id}{ext}"
    (photos / name).write_bytes(await photo.read())
    return name


async def _fields_from_form(request: Request, conn):
    form = await request.form()
    name = (form.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name is required")
    status = form.get("status") or "want"
    fields = {
        "name": name,
        "designer": (form.get("designer") or "").strip(),
        "url": (form.get("url") or "").strip() or None,
        "complexity": _opt_int(form.get("complexity")),
        "paper": (form.get("paper") or "").strip(),
        "status": status if status in db.STATUSES else "want",
        "notes": (form.get("notes") or "").strip(),
        "book_id": None,
        "page": None,
    }
    book_path = (form.get("book") or "").strip()
    if book_path:
        if not books.resolve(book_path):
            raise HTTPException(400, f"book not found: {book_path}")
        fields["book_id"] = db.get_or_create_book(conn, book_path)
        db.mark_browsing(conn, fields["book_id"])
        offset = _opt_int(form.get("offset"))
        if offset is not None:
            db.update_book_meta(conn, fields["book_id"], {"page_offset": offset})
        else:
            offset = db.get_book(conn, fields["book_id"])["page_offset"]
        page = _opt_int(form.get("page"))
        if page is not None and form.get("printed"):
            page += offset
        fields["page"] = page
    return form, fields


def _after_save(form, fields):
    """Back to the list, or for "Save and new" a fresh form with the designer and book carried over."""
    if form.get("then") != "new":
        return _redirect()
    params = {
        "designer": fields["designer"],
        "book": (form.get("book") or "").strip() if fields["book_id"] else "",
        "position": "top" if form.get("position") == "top" else "",
        "saved": fields["name"],
    }
    return _redirect("/models/new?" + urlencode({k: v for k, v in params.items() if v}))


@app.post("/models")
async def create_model(request: Request, conn=Depends(get_conn)):
    form, fields = await _fields_from_form(request, conn)
    model_id = db.create_model(conn, fields, at_top=form.get("position") == "top")
    photo = await _save_photo(model_id, form.get("photo"))
    if photo:
        db.update_model(conn, model_id, {"photo": photo})
    return _after_save(form, fields)


@app.post("/models/{model_id}")
async def update_model(request: Request, model_id: int, conn=Depends(get_conn)):
    if not db.get_model(conn, model_id):
        raise HTTPException(404)
    form, fields = await _fields_from_form(request, conn)
    photo = await _save_photo(model_id, form.get("photo"))
    if photo:
        fields["photo"] = photo
    elif form.get("remove_photo"):
        for old in (config.DATA_DIR / "photos").glob(f"{model_id}.*"):
            old.unlink()
        fields["photo"] = None
    db.update_model(conn, model_id, fields)
    return _after_save(form, fields)


@app.post("/models/{model_id}/delete")
def delete_model(model_id: int, conn=Depends(get_conn)):
    for old in (config.DATA_DIR / "photos").glob(f"{model_id}.*"):
        old.unlink()
    db.delete_model(conn, model_id)
    return _redirect()


# --- books, thumbnails, photos ---

@app.get("/api/books", response_class=HTMLResponse)
def book_search(request: Request, book: str = "", conn=Depends(get_conn)):
    hidden = db.hidden_paths(conn)
    results = [p for p in books.search(book, limit=50) if p not in hidden][:25]
    return templates.TemplateResponse(request, "_book_results.html", {"results": results})


@app.post("/api/rescan")
def rescan():
    books.scan(force=True)
    return _redirect("/books-admin")


@app.get("/api/preview", response_class=HTMLResponse)
def preview(request: Request, book: str = "", page: str = "", printed: str = "", offset: str = "",
            conn=Depends(get_conn)):
    pdf_page = _opt_int(page)
    offset = _opt_int(offset) or 0
    if pdf_page is not None and printed:
        pdf_page += offset
    found = bool(book and books.resolve(book))
    # No page yet: show the cover, to confirm the right book was picked.
    cover = found and pdf_page is None
    if cover:
        row = db.get_book_by_path(conn, book)
        pdf_page = row["cover_page"] if row else 1
    ok = bool(found and pdf_page and pdf_page > 0)
    return templates.TemplateResponse(request, "_preview.html", {
        "book": book, "page": pdf_page, "ok": ok, "cover": cover, "offset": offset, "printed": bool(printed),
    })


@app.get("/api/book-offset", response_class=PlainTextResponse)
def book_offset(book: str = "", conn=Depends(get_conn)):
    existing = db.get_book_by_path(conn, book) if book else None
    return str(existing["page_offset"] if existing else 0)


@app.get("/books/{path:path}")
def serve_book(path: str):
    full = books.resolve(path)
    if not full or full.suffix.lower() != ".pdf":
        raise HTTPException(404)
    return FileResponse(full, media_type="application/pdf", content_disposition_type="inline", filename=full.name)


@app.get("/thumb")
def thumb(book: str, page: int):
    out = thumbs.thumbnail(book, page)
    if not out:
        raise HTTPException(404)
    return FileResponse(out, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/photos/{name}")
def photo(name: str):
    full = config.DATA_DIR / "photos" / Path(name).name
    if not full.is_file():
        raise HTTPException(404)
    return FileResponse(full)


@app.get("/books-admin", response_class=HTMLResponse)
def books_admin(request: Request):
    return templates.TemplateResponse(request, "books.html", {
        "file_count": len(books.scan()), "message": request.query_params.get("msg"),
        "base_url": str(request.base_url),
    })


# --- library ---

LIBRARY_SORTS = {
    "title": lambda b: b["title"].lower(),
    "recent": lambda b: (-b["added_at"], b["title"].lower()),
    "models": lambda b: (-b["model_count"], b["title"].lower()),
    "folder": lambda b: (b["folder"].lower(), b["title"].lower()),
}


def _library_entry(path, row, added_at=0):
    """A scanned PDF merged with its books row; files with no row get the defaults."""
    return {
        "path": path,
        "folder": books.folder(path),
        "title": row["title"] if row else books.clean_title(path),
        "status": row["status"] if row else "unexplored",
        "starred": bool(row["starred"]) if row else False,
        "hidden": bool(row["hidden"]) if row else False,
        "missing": False,
        "model_count": row["model_count"] if row else 0,
        "designers": row["designers"] if row else "",
        "notes": row["notes"] if row else "",
        "page_offset": row["page_offset"] if row else 0,
        "cover_page": row["cover_page"] if row else 1,
        "id": row["id"] if row else None,
        "added_at": added_at,
    }


def _book_entry_or_404(conn, path):
    """A scanned file, or a known book whose file has gone missing (so it can be relinked)."""
    row = db.library_rows(conn, path).get(path)
    present = books.resolve(path) is not None
    if not present and not row:
        raise HTTPException(404)
    added = db.record_added(conn, books.ctimes())
    return {**_library_entry(path, row, added.get(path, 0)), "missing": not present}


def _library_summary(entries):
    counts = {s: 0 for s in db.BOOK_STATUSES} | {"hidden": 0}
    for b in entries:
        counts["hidden" if b["hidden"] else b["status"]] += 1
    return counts


@app.get("/library", response_class=HTMLResponse)
def library(request: Request, conn=Depends(get_conn)):
    params = request.query_params
    statuses = [s for s in params.getlist("status") if s in db.BOOK_STATUSES]
    if "filtered" not in params:
        statuses = LIBRARY_DEFAULT_STATUSES
    q = params.get("q", "")
    has_models = bool(params.get("has_models"))
    starred = bool(params.get("starred"))
    show_hidden = bool(params.get("hidden"))
    sort = params.get("sort", "recent")
    if sort not in LIBRARY_SORTS:
        sort = "recent"

    rows = db.library_rows(conn)
    added = db.record_added(conn, books.ctimes())
    entries = [_library_entry(path, rows.get(path), added.get(path, 0)) for path in books.scan()]
    universe = [b for b in entries if show_hidden or not b["hidden"]]
    terms = q.lower().split()
    shown = [
        b for b in universe
        if b["status"] in statuses
        and (not has_models or b["model_count"])
        and (not starred or b["starred"])
        and all(t in f"{b['title']} {b['path']} {b['designers']} {b['notes']}".lower() for t in terms)
    ]
    shown.sort(key=LIBRARY_SORTS[sort])
    # Known books whose file is gone (renamed, moved, deleted); not listed while the whole folder looks gone.
    missing = [] if books_warning() else sorted(
        (r for p, r in rows.items() if not books.exists(p)), key=lambda r: r["title"].lower())
    return templates.TemplateResponse(request, "library.html", {
        "entries": shown, "total": len(universe), "summary": _library_summary(entries), "missing": missing,
        "statuses": statuses, "q": q, "has_models": has_models, "starred": starred, "show_hidden": show_hidden,
        "sort": sort,
    })


@app.post("/library/status", response_class=HTMLResponse)
def library_status(path: str = Form(...), status: str = Form(...), back: str = Form(""), conn=Depends(get_conn)):
    if status not in db.BOOK_STATUSES:
        raise HTTPException(400, "bad status")
    _book_entry_or_404(conn, path)
    db.update_book_meta(conn, db.get_or_create_book(conn, path), {"status": status})
    if back == "book":
        return _redirect("/library/book?" + urlencode({"path": path}))
    # The card, plus the status summary out-of-band.
    rows = db.library_rows(conn)
    added = db.record_added(conn, books.ctimes())
    card = templates.get_template("_book_card.html").render(b=_library_entry(path, rows.get(path), added.get(path, 0)))
    summary = templates.get_template("_library_summary.html").render(
        summary=_library_summary([_library_entry(p, rows.get(p)) for p in books.scan()]), oob=True)
    return HTMLResponse(card + summary)


@app.post("/library/star", response_class=HTMLResponse)
def library_star(path: str = Form(...), starred: bool = Form(...), conn=Depends(get_conn)):
    b = _book_entry_or_404(conn, path)
    db.update_book_meta(conn, db.get_or_create_book(conn, path), {"starred": int(starred)})
    return templates.get_template("_star.html").render(b={**b, "starred": starred})


@app.post("/library/hide")
def library_hide(path: str = Form(...), hidden: bool = Form(...), conn=Depends(get_conn)):
    _book_entry_or_404(conn, path)
    db.update_book_meta(conn, db.get_or_create_book(conn, path), {"hidden": int(hidden)})
    return _redirect("/library/book?" + urlencode({"path": path}))


@app.post("/library/relink")
def library_relink(path: str = Form(...), book: str = Form(...), shift: str = Form("0"),
                   hide_old: bool = Form(False), conn=Depends(get_conn)):
    """Move a book row (and its models) to another file; `book` is the new file's path."""
    row = db.get_book_by_path(conn, path)
    if not row:
        raise HTTPException(404)
    book = book.strip()
    if book == path or not books.resolve(book):
        raise HTTPException(400, f"pick a different file that exists: {book}")
    db.relink_book(conn, row["id"], book, _opt_int(shift) or 0)
    if hide_old and books.resolve(path):
        db.update_book_meta(conn, db.get_or_create_book(conn, path), {"hidden": 1})
    return _redirect("/library/book?" + urlencode({"path": book, "relinked": 1}))


@app.get("/library/book", response_class=HTMLResponse)
def library_book(request: Request, path: str, conn=Depends(get_conn)):
    b = _book_entry_or_404(conn, path)
    models = db.list_models(conn, book_id=b["id"], sort="book") if b["id"] else []
    return templates.TemplateResponse(request, "book.html", {
        "b": b, "models": models, "saved": bool(request.query_params.get("saved")),
        "relinked": bool(request.query_params.get("relinked")),
    })


@app.post("/library/book")
def save_library_book(path: str = Form(...), title: str = Form(""), designers: str = Form(""),
                      notes: str = Form(""), page_offset: str = Form("0"), cover_page: str = Form("1"),
                      conn=Depends(get_conn)):
    _book_entry_or_404(conn, path)
    db.update_book_meta(conn, db.get_or_create_book(conn, path), {
        "title": title.strip() or books.clean_title(path),
        "designers": designers.strip(),
        "notes": notes.strip(),
        "page_offset": _opt_int(page_offset) or 0,
        "cover_page": max(_opt_int(cover_page) or 1, 1),
    })
    return _redirect("/library/book?" + urlencode({"path": path, "saved": 1}))


# --- import / export ---

@app.post("/import")
async def import_upload(file: UploadFile, conn=Depends(get_conn)):
    text = (await file.read()).decode("utf-8", errors="replace")
    imported, warnings = import_csv.import_text(conn, text)
    msg = f"Imported {imported} model(s)." + "".join(f"\n{w}" for w in warnings)
    return _redirect("/books-admin?" + urlencode({"msg": msg}))


@app.get("/export.csv")
def export(conn=Depends(get_conn)):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["name", "designer", "book_title", "book_path", "page", "url", "complexity", "paper", "status", "notes"])
    for m in db.list_models(conn):
        w.writerow([m["name"], m["designer"], m["book_title"] or "", m["book_path"] or "", m["page"] or "",
                    m["url"] or "", m["complexity"] or "", m["paper"], m["status"], m["notes"]])
    return Response(out.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="origami-list.csv"'})


# --- notes ---

@app.get("/notes")
def notes(conn=Depends(get_conn)):
    row = db.get_notes(conn)
    return {"body": row["body"], "rev": row["rev"]}


@app.post("/notes")
def save_notes(body: str = Form(""), rev: int | None = Form(None), force: bool = Form(False), conn=Depends(get_conn)):
    new_rev = db.save_notes(conn, body, None if force else rev)
    if new_rev is None:
        return JSONResponse({"conflict": True, "rev": db.get_notes(conn)["rev"]}, status_code=409)
    return {"rev": new_rev}


@app.post("/notes/preview", response_class=HTMLResponse)
def notes_preview(body: str = Form("")):
    return MARKDOWN.render(body)
