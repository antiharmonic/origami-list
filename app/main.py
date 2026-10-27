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

from . import books, config, db, import_csv, ocr, thumbs, toc

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
    db.seed_glossary(conn)
    conn.close()
    ocr.connect().close()  # creates data/ocr.db, so pages can read it before the worker's first pass
    yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
app.mount("/pdfjs", StaticFiles(directory=config.PDFJS_DIR, check_dir=False), name="pdfjs")
templates = Jinja2Templates(directory=APP_DIR / "templates")
templates.env.filters["urlpath"] = lambda p: quote(p or "")
templates.env.filters["studies"] = db.parse_studies
templates.env.filters["hours"] = lambda h: f"{round(h * 60)} min" if h < 1 else f"{h:.1f} h" if h < 10 else f"{round(h)} h"
templates.env.filters["date"] = lambda ts: datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else ""


def todo_count():
    """Models still to fold, for the header badge on every page."""
    conn = db.connect()
    try:
        return db.count_models(conn, DEFAULT_STATUSES)
    finally:
        conn.close()


def all_studies():
    """Every study, for the list filter and the model form's checkboxes."""
    conn = db.connect()
    try:
        return db.list_studies(conn)
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
                             BOOK_STATUS_LABELS=db.BOOK_STATUS_LABELS, books_warning=books_warning, all_studies=all_studies,
                             book_missing=lambda path: bool(path) and not books.exists(path))


def get_conn():
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


def get_ocr():
    conn = ocr.connect()
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

def _list_filter(params):
    """The main list's filters from query params (or the same fields posted as a form)."""
    statuses = [s for s in params.getlist("status") if s in db.STATUSES]
    if "filtered" not in params:
        statuses = DEFAULT_STATUSES
    sort = params.get("sort", "mine")
    if sort not in db.SORTS:
        sort = "mine"
    # "in:3" / "out:3": only, or all but, study 3's models.
    mode, _, study_id = (params.get("study") or "").partition(":")
    study = (mode, int(study_id)) if mode in ("in", "out") and study_id.isdigit() else None
    return {"statuses": statuses, "q": params.get("q", ""), "sort": sort, "study": study}


@app.get("/", response_class=HTMLResponse)
def index(request: Request, conn=Depends(get_conn)):
    f = _list_filter(request.query_params)
    models = db.list_models(conn, **f)
    return templates.TemplateResponse(request, "list.html", {
        **f, "models": models, "total": db.count_models(conn),
        "study_value": request.query_params.get("study", "") if f["study"] else "",
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
    prefill["study_ids"] = [int(s) for s in request.query_params.getlist("study") if s.isdigit()]
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


def _save_studies(conn, model_id, form):
    """The form's ticked studies plus an optional new one; returns the model's study ids."""
    ids = [int(s) for s in form.getlist("study") if s.isdigit()]
    new = db.study_by_name(conn, form.get("new_study") or "")
    if new:
        ids.append(new)
    db.set_model_studies(conn, model_id, ids)
    return ids


def _after_save(form, fields, study_ids):
    """Back to the list, or for "Save and new" a fresh form with the designer, book and studies carried over."""
    if form.get("then") != "new":
        return _redirect()
    params = {
        "designer": fields["designer"],
        "book": (form.get("book") or "").strip() if fields["book_id"] else "",
        "position": "top" if form.get("position") == "top" else "",
        "saved": fields["name"],
        "study": study_ids,
    }
    return _redirect("/models/new?" + urlencode({k: v for k, v in params.items() if v}, doseq=True))


@app.post("/models")
async def create_model(request: Request, conn=Depends(get_conn)):
    form, fields = await _fields_from_form(request, conn)
    model_id = db.create_model(conn, fields, at_top=form.get("position") == "top")
    db.learn_from_name(conn, fields["name"])
    photo = await _save_photo(model_id, form.get("photo"))
    if photo:
        db.update_model(conn, model_id, {"photo": photo})
    return _after_save(form, fields, _save_studies(conn, model_id, form))


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
    db.learn_from_name(conn, fields["name"])
    return _after_save(form, fields, _save_studies(conn, model_id, form))


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
        "indexed": bool(row["indexed"]) if row else False,
        "toc_version": row["toc_version"] if row else 0,
        "toc_offset": row["toc_offset"] if row else 0,
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
def library(request: Request, conn=Depends(get_conn), oconn=Depends(get_ocr)):
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
        "sort": sort, "ocr_progress": ocr.progress(oconn, books.scan()),
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
def library_book(request: Request, path: str, conn=Depends(get_conn), oconn=Depends(get_ocr)):
    b = _book_entry_or_404(conn, path)
    if _ensure_suggestions(conn, oconn, b):
        b = _book_entry_or_404(conn, path)
    models = db.list_models(conn, book_id=b["id"], sort="book") if b["id"] else []
    params = request.query_params
    name = params.get("name", "")
    return templates.TemplateResponse(request, "book.html", {
        "b": b, "models": models, "saved": bool(params.get("saved")), "relinked": bool(params.get("relinked")),
        "page": _opt_int(params.get("page", "")), "name": name,
        "name_suggestion": db.suggest(conn, "subject", name) if name else None,
        "ocr": _ocr_book_status(conn, oconn, path),
        **_suggestions_ctx(conn, b),
    })


def _ensure_suggestions(conn, oconn, b):
    """Read the book's contents page into suggestions, once its OCR is done (and again when toc.VERSION changes)."""
    if b["missing"] or b["indexed"] or b["toc_version"] >= toc.VERSION:
        return False
    row = oconn.execute("SELECT state FROM ocr_books WHERE path = ?", (b["path"],)).fetchone()
    if not row or row["state"] != "done":
        return False
    pages = dict(oconn.execute("SELECT page, text FROM ocr_pages WHERE path = ? AND source != 'error'", (b["path"],)))
    entries, info = toc.extract(pages, b["page_offset"])
    if not entries and not b["id"]:
        return False  # nothing to suggest; don't create a row just to remember that
    book_id = b["id"] or db.get_or_create_book(conn, b["path"])
    db.store_suggestions(conn, book_id, entries, info["offset"] if info["detected"] else 0, toc.VERSION)
    return True


def _suggestions_ctx(conn, b):
    """What _suggestions.html needs: the pending suggestions and how their pages were worked out."""
    pending = db.list_suggestions(conn, b["id"]) if b["id"] and not b["indexed"] and not b["missing"] else []
    return {"suggestions": pending, "detected_offset": b["toc_offset"] if not b["page_offset"] else 0}


def _render_suggestions(conn, path, oob=False):
    b = _book_entry_or_404(conn, path)
    return templates.get_template("_suggestions.html").render(b=b, oob=oob, **_suggestions_ctx(conn, b))


def _ocr_book_status(conn, oconn, path):
    added = db.record_added(conn, books.ctimes())
    return ocr.book_status(oconn, path, set(books.scan()), added)


@app.post("/library/book/models", response_class=HTMLResponse)
async def quick_add_model(request: Request, conn=Depends(get_conn)):
    """The book page's quick add: create a model on the viewer's current page, then re-render the book's model list."""
    form = await request.form()
    path = form.get("path") or ""
    if _book_entry_or_404(conn, path)["missing"]:
        raise HTTPException(400, "the book's file is missing")
    name = (form.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name is required")
    status = form.get("status") or "want"
    book_id = db.get_or_create_book(conn, path)
    db.mark_browsing(conn, book_id)
    page = _opt_int(form.get("page") or "")
    model_id = db.create_model(conn, {
        "name": name,
        "designer": (form.get("designer") or "").strip(),
        "complexity": _opt_int(form.get("complexity") or ""),
        "status": status if status in db.STATUSES else "want",
        "book_id": book_id,
        "page": page if page and page > 0 else None,
    })
    db.learn_from_name(conn, name)
    study_ids = _save_studies(conn, model_id, form)
    suggestion = _opt_int(form.get("suggestion_id") or "")
    if suggestion and (row := db.get_suggestion(conn, suggestion)) and row["book_id"] == book_id:
        db.set_suggestion_state(conn, [suggestion], "added")
    return _book_models_response(conn, path, book_id, study_ids, added={"name": name, "page": page})


def _book_models_response(conn, path, book_id, study_ids, added):
    """The book's model list, plus everything else in the panel an add changes (as out-of-band swaps)."""
    models = db.list_models(conn, book_id=book_id, sort="book")
    return HTMLResponse(
        templates.get_template("_book_models.html").render(models=models, added=added)
        + templates.get_template("_todo_count.html").render(oob=True)
        + templates.get_template("_study_chips.html").render(member=study_ids, oob=True)
        + templates.get_template("_book_status.html").render(b=_book_entry_or_404(conn, path), oob=True)
        + _render_suggestions(conn, path, oob=True)
    )


def _book_row_or_404(conn, path):
    row = db.get_book_by_path(conn, path)
    if not row:
        raise HTTPException(404)
    return row


@app.post("/library/book/suggestions/dismiss", response_class=HTMLResponse)
async def dismiss_suggestions(request: Request, conn=Depends(get_conn)):
    """Dismiss one suggestion (`suggestion_id`) or, with `all`, every one still shown."""
    form = await request.form()
    path = form.get("path") or ""
    row = _book_row_or_404(conn, path)
    if form.get("all"):
        ids = [s["id"] for s in db.list_suggestions(conn, row["id"])]
    else:
        ids = [i for i in [_opt_int(form.get("suggestion_id") or "")] if i]
    db.set_suggestion_state(conn, [i for i in ids if (s := db.get_suggestion(conn, i)) and s["book_id"] == row["id"]],
                            "dismissed")
    return HTMLResponse(_render_suggestions(conn, path))


@app.post("/library/book/suggestions/add-verified", response_class=HTMLResponse)
async def add_verified_suggestions(request: Request, conn=Depends(get_conn)):
    """Add every verified suggestion as a model, with the quick-add form's status and studies."""
    form = await request.form()
    path = form.get("path") or ""
    row = _book_row_or_404(conn, path)
    status = form.get("status") or "want"
    picked = [s for s in db.list_suggestions(conn, row["id"]) if s["verified"]]
    ids = [int(s) for s in form.getlist("study") if s.isdigit()]
    for s in sorted(picked, key=lambda s: s["page"]):
        model_id = db.create_model(conn, {
            "name": s["name"], "designer": s["designer"], "complexity": None,
            "status": status if status in db.STATUSES else "want", "book_id": row["id"], "page": s["page"],
        })
        db.learn_from_name(conn, s["name"])
        db.set_model_studies(conn, model_id, ids)
    db.set_suggestion_state(conn, [s["id"] for s in picked], "added")
    if picked:
        db.mark_browsing(conn, row["id"])
    return _book_models_response(conn, path, row["id"], ids, added={"name": f"{len(picked)} models", "page": None})


@app.post("/library/book/offset", response_class=HTMLResponse)
def use_detected_offset(path: str = Form(...), conn=Depends(get_conn)):
    """Make the offset detected from the contents page the book's own (so the model form's printed pages use it)."""
    row = _book_row_or_404(conn, path)
    if not row["page_offset"] and row["toc_offset"]:
        db.update_book_meta(conn, row["id"], {"page_offset": row["toc_offset"]})
    return HTMLResponse(_render_suggestions(conn, path))


@app.post("/library/book/indexed", response_class=HTMLResponse)
def library_indexed(path: str = Form(...), indexed: bool = Form(...), conn=Depends(get_conn)):
    """Mark a book fully indexed (every model in it is on the list or deliberately skipped), or undo that."""
    b = _book_entry_or_404(conn, path)
    db.set_indexed(conn, db.get_or_create_book(conn, path), indexed)
    b = _book_entry_or_404(conn, path)
    return HTMLResponse(templates.get_template("_book_indexed.html").render(b=b)
                        + _render_suggestions(conn, path, oob=True))


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
    w.writerow(["name", "designer", "book_title", "book_path", "page", "url", "complexity", "paper", "status", "notes",
                "studies"])
    for m in db.list_models(conn):
        w.writerow([m["name"], m["designer"], m["book_title"] or "", m["book_path"] or "", m["page"] or "",
                    m["url"] or "", m["complexity"] or "", m["paper"], m["status"], m["notes"],
                    "; ".join(name for _, name in db.parse_studies(m["studies"]))])
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


# --- studies ---

@app.get("/studies", response_class=HTMLResponse)
def studies(request: Request, conn=Depends(get_conn)):
    return templates.TemplateResponse(request, "studies.html", {"studies": db.list_studies(conn)})


@app.post("/studies")
def create_study(name: str = Form(...), conn=Depends(get_conn)):
    study_id = db.study_by_name(conn, name)
    if not study_id:
        raise HTTPException(400, "name is required")
    return _redirect(f"/studies/{study_id}")


@app.post("/studies/bulk")
async def bulk_add_to_study(request: Request, conn=Depends(get_conn)):
    """Add every model the list currently shows (its filters are posted along) to a study."""
    form = await request.form()
    study_id = db.study_by_name(conn, form.get("new_study") or "") or _opt_int(form.get("target_study"))
    if not study_id or not db.get_study(conn, study_id):
        raise HTTPException(400, "pick a study or enter a new name")
    db.add_to_study(conn, study_id, [m["id"] for m in db.list_models(conn, **_list_filter(form))])
    return Response(status_code=204, headers={"HX-Redirect": f"/studies/{study_id}"})


def _study_or_404(conn, study_id):
    study = db.get_study(conn, study_id)
    if not study:
        raise HTTPException(404)
    return study


@app.get("/studies/{study_id}", response_class=HTMLResponse)
def study_page(request: Request, study_id: int, conn=Depends(get_conn)):
    study = _study_or_404(conn, study_id)
    models = db.list_models(conn, study=("in", study_id))
    return templates.TemplateResponse(request, "study.html", {
        "study": study, "models": models, "saved": bool(request.query_params.get("saved")),
    })


@app.post("/studies/{study_id}")
def save_study(study_id: int, name: str = Form(...), notes: str = Form(""), conn=Depends(get_conn)):
    _study_or_404(conn, study_id)
    other = db.study_by_name(conn, name, create=False)
    if not name.strip() or (other and other != study_id):
        raise HTTPException(400, "name is empty or used by another study")
    db.update_study(conn, study_id, name, notes.strip())
    return _redirect(f"/studies/{study_id}?saved=1")


@app.post("/studies/{study_id}/delete")
def delete_study(study_id: int, conn=Depends(get_conn)):
    _study_or_404(conn, study_id)
    db.delete_study(conn, study_id)
    return _redirect("/studies")


@app.post("/studies/{study_id}/remove")
def remove_from_study(study_id: int, model_id: int = Form(...), conn=Depends(get_conn)):
    _study_or_404(conn, study_id)
    db.remove_from_study(conn, study_id, model_id)
    return _redirect(f"/studies/{study_id}")


# --- library search (OCR), glossary ---

@app.get("/search", response_class=HTMLResponse)
def search(request: Request, q: str = "", conn=Depends(get_conn), oconn=Depends(get_ocr)):
    q = " ".join(q.split())
    present = books.scan()
    progress = ocr.progress(oconn, present)
    if not q:
        return templates.TemplateResponse(request, "search.html", {"q": q, "progress": progress})
    alternatives = [a for a in db.glossary_alternatives(conn, q) if len(a) >= 2]
    allowed = set(present) - db.hidden_paths(conn)
    rows = db.library_rows(conn)
    results = ocr.search(oconn, [q, *alternatives], allowed)
    for r in results:
        r["title"] = rows[r["path"]]["title"] if r["path"] in rows else books.clean_title(r["path"])
    return templates.TemplateResponse(request, "search.html", {
        "q": q, "progress": progress, "alternatives": alternatives, "results": results,
        "models": db.list_models(conn, q=q, sort="book")[:20],
        # "+ Add" prefills the name; a Japanese query gets its English form ("Rhinoceros (サイ)").
        "add_name": db.suggest(conn, "subject", q) or q,
    })


@app.get("/api/suggest", response_class=HTMLResponse)
def suggest(request: Request, kind: str, target: str = "", conn=Depends(get_conn)):
    """A suggestion chip for a quick-add field; htmx sends the field's own value (`name` or `designer`)."""
    if kind not in ("subject", "designer"):
        raise HTTPException(400)
    params = request.query_params
    value = db.suggest(conn, kind, params.get("text") or params.get("name") or params.get("designer") or "")
    if not value:
        return HTMLResponse("")
    return HTMLResponse(templates.get_template("_suggestion.html").render(value=value, target=target))


@app.get("/api/page-text", response_class=HTMLResponse)
def page_text(request: Request, path: str, page: int, oconn=Depends(get_ocr)):
    if not books.resolve(path):
        raise HTTPException(404)
    return templates.TemplateResponse(request, "_page_text.html", {"row": ocr.get_page(oconn, path, page), "page": page})


@app.post("/ocr/pause")
def ocr_pause(paused: bool = Form(...), back: str = Form("/library"), oconn=Depends(get_ocr)):
    ocr.set_control(oconn, paused=int(paused))
    return _redirect(back if back.startswith("/") else "/library")


@app.post("/ocr/priority")
def ocr_priority(path: str = Form(...), oconn=Depends(get_ocr)):
    if not books.resolve(path):
        raise HTTPException(404)
    ocr.sync(oconn, ocr.sizes_and_mtimes([path]))
    ocr.set_control(oconn, priority_path=path)
    return _redirect("/library/book?" + urlencode({"path": path}))


@app.get("/glossary", response_class=HTMLResponse)
def glossary(request: Request, kind: str = "subject", q: str = "", conn=Depends(get_conn)):
    if kind not in ("subject", "designer"):
        kind = "subject"
    return templates.TemplateResponse(request, "glossary.html", {
        "kind": kind, "q": q, "entries": db.list_glossary(conn, kind, q),
    })


@app.post("/glossary")
def save_glossary(kind: str = Form(...), ja: str = Form(...), en: str = Form(...), entry_id: str = Form(""),
                  conn=Depends(get_conn)):
    if kind not in ("subject", "designer"):
        raise HTTPException(400)
    db.save_glossary(conn, kind, ja, en, _opt_int(entry_id))
    return _redirect("/glossary?" + urlencode({"kind": kind}))


@app.post("/glossary/{entry_id}/delete")
def delete_glossary(entry_id: int, kind: str = Form("subject"), conn=Depends(get_conn)):
    db.delete_glossary(conn, entry_id)
    return _redirect("/glossary?" + urlencode({"kind": kind}))
