"""On-demand translation of Japanese pages and passages with Gemini (free tier).

A page is sent as an image: the OCR text of a diagram page has its captions out of order, while a
vision model can match each caption to its step number. The OCR text goes along as a hint.
Results are cached forever in origami.db by the caller (see db.save_translation).
"""
import base64
import json
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import books, config

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
TIMEOUT = 75
RANGE_TIMEOUT = 180  # several page images take a while
MAX_RANGE = 8  # pages per article translation
RANGE_MAX_TOKENS = 16384
RETRY_WAITS = (3, 8)  # Gemini answers 503 ("high demand") now and then; it usually passes quickly
IMAGE_LONG_SIDE = 1600  # captions stay legible; bigger only costs tokens

SYSTEM = """You translate Japanese origami publications (diagram books, Origami Tanteidan magazines \
and convention books) into English for a folder who can't read Japanese.

Write English only. Never copy Japanese sentences or the page's own English captions into your answer; \
the only Japanese allowed is a name in brackets: "Twisted Box (ねじれ箱)".

- A diagram page: one Markdown list item per step that has text, in step-number order: \
"- **14.** <your English translation of that step's Japanese>". Translate the Japanese even where the page \
also prints English (that English is often shorter or looser). Skip steps without text; never output an \
empty step. After a step, add an indented line "→ <note>" only when the text says something the arrows \
and symbols don't (which layer or flap, "don't flatten yet", "repeat behind", what the fold is for).
- An article or essay: translate it as prose, keeping its headings.
- Model names as "English (日本語)"; people given name first ("Jun Maekawa").
- Use standard origami terms: valley/mountain fold, squash, petal fold, sink (open/closed), inside/outside \
reverse fold, crimp, rabbit ear, swivel, preliminary base, bird base, waterbomb base, crease pattern.
- Don't describe the pictures beyond what's needed, and don't invent text that isn't there.
- Reply with the Markdown only, no preamble.

Example of a diagram page answer:
- **3.** Fold only the top layer to the center line.
  → Just one layer; the layers underneath stay put.
- **4.** Unfold."""

RANGE = """These are consecutive pages of one article or chapter, PDF pages {start}–{end}, in order. Translate \
them as one continuous text, keeping names and terms consistent throughout. Start each page's content with \
the heading "### Page N" (its PDF page number), for every page, even one with little text. A sentence that \
runs across a page break goes under the page where it starts. Diagram pages inside the range use the \
step-list format."""

TRUNCATED = "\n\n*Translation stopped early: the reply hit its length limit. Retranslate a smaller range.*"

PASSAGE = "This is a passage selected from an origami publication, not a whole page. Translate it; add a short → note only if something needs explaining."


class TranslateError(Exception):
    """A failure to show the user as is."""


def enabled():
    return bool(config.gemini_key())


def render_page(path, page):
    """PNG bytes of one PDF page, long side IMAGE_LONG_SIDE."""
    src = books.resolve(path)
    if src is None:
        raise TranslateError("The book's file isn't available.")
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["pdftoppm", "-q", "-f", str(page), "-l", str(page), "-scale-to", str(IMAGE_LONG_SIDE),
                        "-png", "-singlefile", str(src), f"{tmp}/p"], check=True, capture_output=True, timeout=60)
        with open(f"{tmp}/p.png", "rb") as f:
            return f.read()


def render_pages(path, start, end):
    """[JPEG bytes] for PDF pages start..end (JPEG keeps a multi-page request small)."""
    src = books.resolve(path)
    if src is None:
        raise TranslateError("The book's file isn't available.")
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["pdftoppm", "-q", "-f", str(start), "-l", str(end), "-scale-to", str(IMAGE_LONG_SIDE),
                        "-jpeg", "-jpegopt", "quality=80", str(src), f"{tmp}/p"],
                       check=True, capture_output=True, timeout=60 * (end - start + 1))
        files = sorted(Path(tmp).glob("p-*.jpg"), key=lambda f: int(f.stem.rsplit("-", 1)[1]))
        return [f.read_bytes() for f in files]


def _hint(title, page, ocr_text, designers, limit=6000):
    hint = f"From “{title}”, PDF page {page}."
    if designers:
        hint += "\nDesigners on this page (use these English names): " + "; ".join(f"{ja} = {en}" for ja, en in designers)
    if ocr_text.strip():
        hint += "\n\nOCR text of the page (a hint only; it may be garbled and out of order):\n" + ocr_text.strip()[:limit]
    return hint


def range_request(images, title, start, ocr_texts, designers=()):
    """The generateContent body for pages start..start+len(images)-1: each image followed by its own hint."""
    end = start + len(images) - 1
    parts = []
    for i, image in enumerate(images):
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(image).decode()}})
        parts.append({"text": _hint(title, start + i, ocr_texts[i] if i < len(ocr_texts) else "", designers, 3000)})
    return {
        "systemInstruction": {"parts": [{"text": SYSTEM + "\n\n" + RANGE.format(start=start, end=end)}]},
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"maxOutputTokens": RANGE_MAX_TOKENS},
    }


def page_request(image, title, page, ocr_text="", designers=()):
    """The generateContent body for one page."""
    hint = _hint(title, page, ocr_text, designers)
    return {
        "systemInstruction": {"parts": [{"text": SYSTEM}]},
        "contents": [{"role": "user", "parts": [
            {"inline_data": {"mime_type": "image/png", "data": base64.b64encode(image).decode()}},
            {"text": hint},
        ]}],
    }


def text_request(text):
    return {
        "systemInstruction": {"parts": [{"text": SYSTEM + "\n\n" + PASSAGE}]},
        "contents": [{"role": "user", "parts": [{"text": text}]}],
    }


def call(body, timeout=TIMEOUT):
    """POST to Gemini; returns (Markdown, model used). Failures become TranslateError with a readable message.

    A busy (503) model is retried, then the next model in config.gemini_models() is tried, as on a
    timeout or a used-up quota (429).
    """
    key = config.gemini_key()
    if not key:
        raise TranslateError("Translation isn't set up: add GEMINI_API_KEY to .env (see the README).")
    error = None
    for model in config.gemini_models():
        try:
            return _call_model(model, key, body, timeout), model
        except _Busy as e:
            error = e.error
    raise error


class _Busy(Exception):
    """This model is overloaded or too slow right now; another one may do."""
    def __init__(self, error):
        self.error = error


def _call_model(model, key, body, timeout):
    req = urllib.request.Request(API.format(model=model), data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "x-goog-api-key": key})
    for wait in (*RETRY_WAITS, None):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return _reply_text(json.load(resp))
        except urllib.error.HTTPError as e:
            if e.code == 429:  # each model has its own free-tier quota
                raise _Busy(_http_error(e)) from e
            if e.code != 503:
                raise _http_error(e) from e
            if wait is None:
                raise _Busy(_http_error(e)) from e
        except (TimeoutError, urllib.error.URLError, OSError) as e:
            if isinstance(e, TimeoutError) or isinstance(getattr(e, "reason", None), TimeoutError):
                raise _Busy(TranslateError("Gemini took too long to answer; try again in a few minutes.")) from e
            raise TranslateError(f"Couldn't reach Gemini: {e}") from e
        time.sleep(wait)


def _http_error(e):
    if e.code == 429:
        return TranslateError("Gemini's free-tier limit was hit; try again in a minute.")
    if e.code == 503:
        return TranslateError("Gemini is busy right now (high demand); try again in a few minutes.")
    if e.code in (400, 401, 403, 404):
        return TranslateError(f"Gemini refused the request ({e.code}): {_error_message(e)}")
    return TranslateError(f"Gemini had a problem ({e.code}); try again later.")


def _reply_text(reply):
    parts = [p.get("text", "") for c in reply.get("candidates", [])[:1] for p in c.get("content", {}).get("parts", [])]
    text = "".join(parts).strip()
    if not text:
        reason = (reply.get("candidates") or [{}])[0].get("finishReason") or reply.get("promptFeedback", {}).get("blockReason")
        raise TranslateError(f"Gemini returned no translation ({reason or 'empty reply'}).")
    if reply["candidates"][0].get("finishReason") == "MAX_TOKENS":
        text += TRUNCATED
    return text


def _error_message(e):
    try:
        return json.load(e)["error"]["message"]
    except Exception:
        return "no details"
