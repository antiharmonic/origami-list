"""The book page in a real browser: viewer, quick add, page text, suggestions, translation.

- Paging the viewer moves quick add's page and highlights that page's models; a p.N button jumps the viewer.
- Quick add (Enter) adds the model, clears the name, keeps designer and studies, and updates the "to fold" badge.
- Page text follows the viewer; "Use as name" takes the selection, and a glossary word offers its English.
- Suggestions: Add files the model (studies still ticked), ✕ dismisses, "Mark fully indexed" hides them.
- Translate: a page, then an article; the enlarged pane follows the viewer to that page's part; Esc closes it.
"""
from playwright.sync_api import expect

from app import db, translate
from tests.e2e.conftest import READER, go_to_page, open_book, viewer_page
from tests.helpers import add_ocr_pages, ocr_done, toc_book

# Long enough that each page's part has to be scrolled to.
LONG_ARTICLE = "\n\n".join(f"### Page {n}\n" + "Fold the flap and tuck it inside. " * 300 for n in (2, 3, 4))


def test_viewer_drives_quick_add_page_and_jumps(page, server, conn):
    book_id = db.get_or_create_book(conn, READER)
    db.create_model(conn, {"name": "Crane", "book_id": book_id, "page": 3, "status": "want"})
    open_book(page, server)

    go_to_page(page, 3)
    expect(page.locator("#qa-page")).to_have_value("3")
    expect(page.locator("#book-models li.here")).to_contain_text("Crane")

    go_to_page(page, 5)
    expect(page.locator("#qa-page")).to_have_value("5")
    expect(page.locator("#book-models li.here")).to_have_count(0)
    page.locator('#book-models [data-goto="3"]').click()
    page.wait_for_function("() => document.getElementById('viewer').contentWindow.PDFViewerApplication.page === 3")
    assert viewer_page(page) == 3


def test_quick_add_keeps_designer_and_studies(page, server, conn):
    study = db.study_by_name(conn, "Birds")
    open_book(page, server)
    go_to_page(page, 2)
    expect(page.locator("#todo-count")).to_have_text("0 to fold")

    page.locator(f'#study-chips input[value="{study}"]').check()
    page.locator("#qa-designer").fill("Akira Yoshizawa")
    page.locator("#qa-name").fill("Crane")
    page.locator("#qa-name").press("Enter")

    expect(page.locator("#book-models")).to_contain_text("Crane")
    expect(page.locator("#qa-name")).to_have_value("")
    expect(page.locator("#qa-designer")).to_have_value("Akira Yoshizawa")
    expect(page.locator(f'#study-chips input[value="{study}"]')).to_be_checked()
    expect(page.locator("#todo-count")).to_have_text("1 to fold")
    (m,) = db.list_models(conn)
    assert (m["name"], m["designer"], m["page"]) == ("Crane", "Akira Yoshizawa", 2)
    assert db.list_models(conn, study=("in", study))[0]["name"] == "Crane"


def test_page_text_use_as_name_and_glossary_chip(page, server):
    add_ocr_pages([(READER, 1, "Contents"), (READER, 2, "サイ 川畑文昭")])
    open_book(page, server)
    page.locator("#page-text summary").click()
    go_to_page(page, 2)
    body = page.locator("#page-text-body")
    expect(body).to_contain_text("サイ")

    # Select "サイ" in the page text, then "Use as name".
    page.evaluate("""() => {
        const node = document.querySelector('#page-text-body pre').firstChild;
        const range = document.createRange();
        range.setStart(node, node.textContent.indexOf('サイ'));
        range.setEnd(node, node.textContent.indexOf('サイ') + 2);
        getSelection().removeAllRanges();
        getSelection().addRange(range);
    }""")
    page.locator('[data-use="qa-name"]').click()
    expect(page.locator("#qa-name")).to_have_value("サイ")
    chip = page.locator("#qa-name-suggest .suggestion")
    expect(chip).to_have_text("→ Rhinoceros (サイ)")
    chip.click()
    expect(page.locator("#qa-name")).to_have_value("Rhinoceros (サイ)")


def test_suggestions_add_dismiss_and_mark_indexed(page, server, conn, dirs):
    from tests.conftest import make_pdf
    from app import books
    make_pdf(dirs[0] / "Engel.pdf", pages=40)
    books.scan(force=True)
    ocr_done("Engel.pdf", toc_book())
    study = db.study_by_name(conn, "Fish")
    open_book(page, server, "Engel.pdf")
    heading = page.locator("#suggestions h2")
    expect(heading).to_have_text("Suggested from contents (5)")

    page.locator(f'#study-chips input[value="{study}"]').check()
    row = page.locator("#suggestions li").filter(has=page.locator('input[name="name"][value="Angelfish"]'))
    row.get_by_role("button", name="Add").click()
    expect(page.locator("#book-models")).to_contain_text("Angelfish")
    expect(heading).to_have_text("Suggested from contents (4)")
    expect(page.locator(f'#study-chips input[value="{study}"]')).to_be_checked()
    assert db.list_models(conn, study=("in", study))[0]["page"] == 24

    row = page.locator("#suggestions li").filter(has=page.locator('input[name="name"][value="Penguin"]'))
    row.locator('button[title^="Not a model"]').click()
    expect(heading).to_have_text("Suggested from contents (3)")

    page.get_by_role("button", name="Mark fully indexed").click()
    expect(page.locator("#book-indexed")).to_contain_text("Fully indexed ✓")
    expect(page.locator("#suggestions details")).to_have_count(0)


def test_translate_page_article_and_enlarged_pane(page, server, gemini, monkeypatch):
    monkeypatch.setattr(translate, "render_pages", lambda path, start, end: [b"jpg"] * (end - start + 1))
    open_book(page, server)
    page.locator("#translate summary").click()
    go_to_page(page, 2)
    page.get_by_role("button", name="Translate this page").click()
    expect(page.locator("#translate-body .translation")).to_contain_text("Fold the corner.")

    # An article over pages 2-4.
    gemini["reply"] = LONG_ARTICLE
    form = page.locator(".tr-range-form")
    form.locator('input[name="start"]').fill("2")
    form.locator('input[name="end"]').fill("4")
    form.get_by_role("button", name="Translate pages").click()
    article = page.locator(".tr-article")
    expect(article.locator("#tp-3")).to_have_text("Page 3")

    # Enlarge, then page the viewer: the pane stays open and scrolls to that page's part.
    page.locator(".tr-view:not([hidden]) [data-tr-expand]").click()
    expect(page.locator("#translate")).to_have_class("book-section tr-expanded")
    pane = page.locator(".tr-view:not([hidden]) .tr-box")
    assert pane.bounding_box()["width"] > 400
    go_to_page(page, 4)
    expect(page.locator(".tr-article #tp-4")).to_be_visible()
    page.wait_for_function("""() => {
        const box = document.querySelector('.tr-article'), h = box.querySelector('#tp-4');
        return Math.abs(h.getBoundingClientRect().top - box.getBoundingClientRect().top) < 30;
    }""")
    expect(page.locator("#translate")).to_have_class("book-section tr-expanded")

    page.keyboard.press("Escape")
    expect(page.locator("#translate")).to_have_class("book-section")
