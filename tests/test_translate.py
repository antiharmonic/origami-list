"""On-demand translation with Gemini (always faked in tests; never the network).

- A page is sent as an image with its OCR text and known designers as hints; an article (a page range) is sent as one request.
- Every translation is cached forever: viewing it again doesn't call Gemini; Retranslate replaces it.
- Article translations show on every page in their range, with per-page anchors; a page's own translation coexists with them.
- Errors (no key, busy, quota, refused) become readable messages and nothing is cached; a busy model is retried, then Flash-Lite is used.
- Gemini's reply is rendered with raw HTML escaped; a cut-off reply is marked as such.
"""
import shutil

import pytest

from app import books, db, translate
from tests.conftest import make_pdf
from tests.helpers import ARTICLE, INSECTS, NATURE, add_ocr_pages


def test_page_request_has_image_and_hints():
    body = translate.page_request(b"img", "Tanteidan 26", 218, "カドを折る 前川淳", [("前川淳", "Jun Maekawa")])
    image, hint = body["contents"][0]["parts"]
    assert image["inline_data"]["mime_type"] == "image/png" and image["inline_data"]["data"] == "aW1n"
    assert "PDF page 218" in hint["text"] and "前川淳 = Jun Maekawa" in hint["text"] and "カドを折る" in hint["text"]
    assert "Write English only" in body["systemInstruction"]["parts"][0]["text"]
    if shutil.which("pdftoppm"):
        assert translate.render_page(INSECTS, 1).startswith(b"\x89PNG")


def test_translate_page_is_cached(client, conn, gemini):
    db.seed_glossary(conn)
    add_ocr_pages([(INSECTS, 2, "カドを折る 前川淳")])
    page = client.get("/library/book", params={"path": INSECTS}).text
    assert 'id="translate"' in page and "Translate selection" in page
    assert "Translate this page" in client.get("/api/translation", params={"path": INSECTS, "page": 2}).text

    out = client.post("/api/translate", data={"path": INSECTS, "page": "2"}).text
    assert "<strong>14.</strong> Fold the corner." in out and "Retranslate" in out
    assert "前川淳 = Jun Maekawa" in gemini["calls"][0]["contents"][0]["parts"][1]["text"]
    # Served from the cache afterwards; Retranslate replaces it.
    assert "Fold the corner" in client.get("/api/translation", params={"path": INSECTS, "page": 2}).text
    assert len(gemini["calls"]) == 1
    gemini["reply"] = "**14.** Valley fold the corner."
    assert "Valley fold" in client.post("/api/translate", data={"path": INSECTS, "page": "2"}).text
    assert conn.execute("SELECT COUNT(*) FROM translations").fetchone()[0] == 1

    # Relinking with a shift moves the cached page.
    db.relink_book(conn, db.get_book_by_path(conn, INSECTS)["id"], NATURE, shift=2)
    assert "Valley fold" in client.get("/api/translation", params={"path": NATURE, "page": 4}).text


def test_translate_errors_and_escaping(client, conn, gemini):
    gemini["fail"] = "Gemini's free-tier limit was hit; try again in a minute."
    out = client.post("/api/translate", data={"path": INSECTS, "page": "1"}).text
    assert "free-tier limit" in out and "Translate this page" in out
    assert conn.execute("SELECT COUNT(*) FROM translations").fetchone()[0] == 0
    gemini["fail"] = None
    gemini["reply"] = "<script>alert(1)</script> hi"
    out = client.post("/api/translate", data={"path": INSECTS, "page": "1"}).text
    assert "<script>" not in out and "&lt;script&gt;" in out
    assert client.post("/api/translate", data={"path": "../secret.pdf", "page": "1"}).status_code == 404
    assert client.get("/api/translation", params={"path": "../secret.pdf", "page": 1}).status_code == 404


def test_translate_selection_cached(client, conn, gemini):
    gemini["reply"] = "Fold only the top layer."
    assert "Fold only" in client.post("/api/translate-text", data={"text": "上の 1 枚だけ 折る"}).text
    assert "Fold only" in client.post("/api/translate-text", data={"text": " 上の 1 枚だけ\n折る "}).text
    assert len(gemini["calls"]) == 1


def test_translation_not_set_up(client, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    out = client.get("/api/translation", params={"path": INSECTS, "page": 1}).text
    assert "isn't set up" in out and "Translate this page" not in out
    assert "Translate selection" not in client.get("/library/book", params={"path": INSECTS}).text
    with pytest.raises(translate.TranslateError, match="GEMINI_API_KEY"):
        translate.call(translate.text_request("テスト"))


def test_gemini_call_retries_busy_and_reports_errors(monkeypatch):
    import io
    import json
    import urllib.error
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(translate, "RETRY_WAITS", (0, 0))
    answers = []

    def urlopen(req, timeout):
        code = answers.pop(0)
        if code != 200:
            raise urllib.error.HTTPError(req.full_url, code, "x", {}, io.BytesIO(b'{"error": {"message": "nope"}}'))
        return io.BytesIO(json.dumps({"candidates": [{"content": {"parts": [{"text": "Fold."}]}}]}).encode())
    monkeypatch.setattr(translate.urllib.request, "urlopen", urlopen)

    answers[:] = [503, 503, 200]
    assert translate.call(translate.text_request("折る")) == ("Fold.", "gemini-flash-latest")
    # Still busy after the retries: Flash-Lite takes over.
    answers[:] = [503, 503, 503, 200]
    assert translate.call(translate.text_request("折る")) == ("Fold.", "gemini-flash-lite-latest")
    answers[:] = [429, 200]
    assert translate.call(translate.text_request("折る")) == ("Fold.", "gemini-flash-lite-latest")
    answers[:] = [503] * 6
    with pytest.raises(translate.TranslateError, match="busy"):
        translate.call(translate.text_request("折る"))
    answers[:] = [403]
    with pytest.raises(translate.TranslateError, match="refused.*nope"):
        translate.call(translate.text_request("折る"))


def test_range_request_has_each_page_and_hint():
    body = translate.range_request([b"a", b"b", b"c"], "Tanteidan 56", 16, ["一", "二", "三"])
    parts = body["contents"][0]["parts"]
    assert [p.get("inline_data", {}).get("mime_type") for p in parts[::2]] == ["image/jpeg"] * 3
    assert "PDF page 17" in parts[3]["text"] and "二" in parts[3]["text"]
    assert "PDF pages 16–18" in body["systemInstruction"]["parts"][0]["text"]
    assert body["generationConfig"]["maxOutputTokens"] == translate.RANGE_MAX_TOKENS


def test_render_pages(dirs):
    if not shutil.which("pdftoppm"):
        pytest.skip("pdftoppm not installed")
    make_pdf(dirs[0] / "Long.pdf", pages=5)
    images = translate.render_pages("Long.pdf", 2, 4)
    assert len(images) == 3 and all(i.startswith(b"\xff\xd8") for i in images)


def test_translate_range(client, conn, gemini, dirs, monkeypatch):
    make_pdf(dirs[0] / "Long.pdf", pages=5)
    books.scan(force=True)
    monkeypatch.setattr(translate, "render_pages", lambda path, start, end: [b"jpg"] * (end - start + 1))
    gemini["reply"] = ARTICLE
    out = client.post("/api/translate-range", data={"path": "Long.pdf", "start": "2", "end": "4", "page": "3"}).text
    assert 'id="tp-3"' in out and "It continues." in out and "Article translation, pp.2–4" in out
    assert len(gemini["calls"]) == 1 and len(gemini["calls"][0]["contents"][0]["parts"]) == 6
    assert conn.execute("SELECT COUNT(*) FROM translations").fetchone()[0] == 1

    # Every page in the range shows it; the page after doesn't.
    assert "It continues." in client.get("/api/translation", params={"path": "Long.pdf", "page": 4}).text
    assert "It continues." not in client.get("/api/translation", params={"path": "Long.pdf", "page": 5}).text

    # A page's own translation and the article coexist, with a switch between them.
    gemini["reply"] = "Just page three."
    out = client.post("/api/translate", data={"path": "Long.pdf", "page": "3"}).text
    assert "Just page three." in out and "It continues." in out and 'data-tr-view="article"' in out
    assert out.count("data-tr-expand") == 2  # an enlarge button on each translation box
    assert conn.execute("SELECT COUNT(*) FROM translations").fetchone()[0] == 2

    # Limits: too many pages, backwards, past the end, outside the books folder.
    for start, end, message in [("1", "9", "Pick 2 to 8"), ("4", "2", "Pick 2 to 8"), ("4", "6", "has 5 pages")]:
        out = client.post("/api/translate-range", data={"path": "Long.pdf", "start": start, "end": end}).text
        assert message in out
    assert len(gemini["calls"]) == 2
    assert client.post("/api/translate-range", data={"path": "../secret.pdf", "start": "1", "end": "2"}).status_code == 404

    # Relinking with a shift moves both ends.
    db.relink_book(conn, db.get_book_by_path(conn, "Long.pdf")["id"], INSECTS, shift=1)
    assert "It continues." in client.get("/api/translation", params={"path": INSECTS, "page": 5}).text


def test_truncated_reply_is_marked():
    reply = {"candidates": [{"content": {"parts": [{"text": "### Page 1\nPart"}]}, "finishReason": "MAX_TOKENS"}]}
    assert translate._reply_text(reply).endswith(translate.TRUNCATED)
