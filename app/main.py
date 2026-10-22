import csv
import io
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import books, config, db, import_csv, thumbs

APP_DIR = Path(__file__).parent
PHOTO_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}
DEFAULT_STATUSES = ["want", "attempted"]


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


def todo_count():
    """Models still to fold, for the header badge on every page."""
    conn = db.connect()
    try:
        return db.count_models(conn, DEFAULT_STATUSES)
    finally:
        conn.close()


templates.env.globals.update(STATUSES=db.STATUSES, STATUS_LABELS=db.STATUS_LABELS, urlencode=urlencode,
                             todo_count=todo_count)


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
        offset = _opt_int(form.get("offset"))
        if offset is not None:
            db.set_book_offset(conn, fields["book_id"], offset)
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
def book_search(request: Request, book: str = ""):
    return templates.TemplateResponse(request, "_book_results.html", {"results": books.search(book)})


@app.post("/api/rescan")
def rescan():
    books.scan(force=True)
    return _redirect("/books-admin")


@app.get("/api/preview", response_class=HTMLResponse)
def preview(request: Request, book: str = "", page: str = "", printed: str = "", offset: str = ""):
    pdf_page = _opt_int(page)
    offset = _opt_int(offset) or 0
    if pdf_page is not None and printed:
        pdf_page += offset
    ok = bool(book and books.resolve(book) and pdf_page and pdf_page > 0)
    return templates.TemplateResponse(request, "_preview.html", {
        "book": book, "page": pdf_page, "ok": ok, "offset": offset, "printed": bool(printed),
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
def books_admin(request: Request, conn=Depends(get_conn)):
    return templates.TemplateResponse(request, "books.html", {
        "books": db.list_books(conn), "file_count": len(books.scan()), "message": request.query_params.get("msg"),
        "base_url": str(request.base_url),
    })


@app.post("/books-admin/{book_id}")
def update_book(book_id: int, title: str = Form(...), page_offset: str = Form("0"), conn=Depends(get_conn)):
    db.update_book(conn, book_id, title.strip(), _opt_int(page_offset) or 0)
    return _redirect("/books-admin")


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
