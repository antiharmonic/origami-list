"""The to-fold list, notes, the model form, library, search and studies in a real browser.

- Dragging a model reorders the list, and the order survives a reload.
- Changing a status inline updates the row and the "to fold" badge.
- Status chips and the search box filter the list and update "Showing x of y".
- Notes autosave and survive a reload; an edit from another window shows a conflict instead of overwriting.
- The model form: picking a book shows its cover; a printed page converts through the offset; a pasted photo is saved.
- Library: stars toggle and filter; a book's status changes from its card.
- Search: "+ Add" opens the reader at that page with the name filled in.
- "Add the shown models to a study" creates the study from the filtered list and opens it.
"""
import re

from playwright.sync_api import expect

from app import db
from tests.helpers import INSECTS, NATURE, add_ocr_pages


def make_models(conn, *names, status="want"):
    return [db.create_model(conn, {"name": n, "status": status}) for n in names]


def test_drag_reorder_survives_reload(page, server, conn):
    a, b, c = make_models(conn, "Alpha", "Beta", "Gamma")
    page.goto(server + "/")
    # SortableJS uses native drag and drop; drop Gamma onto the top edge of Alpha.
    page.locator(f'li[data-id="{c}"] .handle').drag_to(page.locator(f'li[data-id="{a}"]'),
                                                       target_position={"x": 40, "y": 3})
    page.wait_for_function("() => document.querySelector('#list li').dataset.id === '%d'" % c)
    page.wait_for_timeout(300)  # the move is posted without waiting
    page.reload()
    expect(page.locator("#list li .title")).to_have_text([re.compile("Gamma"), re.compile("Alpha"), re.compile("Beta")])


def test_inline_status_change_updates_row_and_badge(page, server, conn):
    (crane,) = make_models(conn, "Crane")
    page.goto(server + "/")
    expect(page.locator("#todo-count")).to_have_text("1 to fold")
    page.locator(f'li[data-id="{crane}"] select.status-select').select_option("folded")
    expect(page.locator(f'li[data-id="{crane}"]')).to_have_class(re.compile("status-folded"))
    expect(page.locator("#todo-count")).to_have_text("0 to fold")
    assert db.get_model(conn, crane)["status"] == "folded"


def test_filters_update_list_and_count(page, server, conn):
    make_models(conn, "Crane", "Frog")
    make_models(conn, "Lily", status="attempted")
    page.goto(server + "/")
    count = page.locator("#list-count")
    expect(count).to_have_text("Showing 3 of 3")
    page.locator('.filters input[name="status"][value="want"]').uncheck()
    expect(count).to_have_text("Showing 1 of 3")
    expect(page.locator("#list li .title")).to_have_text([re.compile("Lily")])
    page.locator('.filters input[name="status"][value="want"]').check()
    page.locator('.filters input[name="q"]').fill("fro")
    expect(count).to_have_text("Showing 1 of 3")
    expect(page.locator("#list li .title")).to_have_text([re.compile("Frog")])


def test_notes_autosave_and_conflict(page, server, context):
    page.goto(server + "/")
    page.locator("#notes-toggle").click()
    page.locator("#notes-text").fill("- find a great octopus")
    expect(page.locator("#notes-status")).to_contain_text("Saved")
    page.reload()
    page.locator("#notes-toggle").click()
    expect(page.locator("#notes-text")).to_have_value("- find a great octopus")

    # Another window saves first; this one then gets a conflict, not a silent overwrite.
    other = context.new_page()
    other.goto(server + "/")
    other.locator("#notes-toggle").click()
    other.locator("#notes-text").fill("- theirs")
    expect(other.locator("#notes-status")).to_contain_text("Saved")
    page.locator("#notes-text").fill("- mine")
    expect(page.locator("#notes-status")).to_contain_text("Changed in another window")
    page.get_by_role("button", name="Load theirs").click()
    expect(page.locator("#notes-text")).to_have_value("- theirs")


def test_model_form_cover_printed_page_and_pasted_photo(page, server, conn):
    page.goto(server + "/models/new")
    page.locator('input[name="name"]').fill("Mantis")
    page.locator("#book").fill("insects")
    page.locator(f'#book-results [data-path="{INSECTS}"]').click()
    expect(page.locator("#book")).to_have_value(INSECTS)
    expect(page.locator("#preview img")).to_have_count(1)

    page.locator("#page").fill("10")
    page.locator("#offset").fill("6")
    page.locator("#printed").check()

    # Paste an image, as Ctrl+V with a screenshot on the clipboard would.
    page.evaluate("""() => {
        const png = Uint8Array.from(atob('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=='), c => c.charCodeAt(0));
        const dt = new DataTransfer();
        dt.items.add(new File([png], 'image.png', {type: 'image/png'}));
        document.dispatchEvent(new ClipboardEvent('paste', {clipboardData: dt, bubbles: true}));
    }""")
    expect(page.locator("#photo-staged")).to_be_visible()
    page.get_by_role("button", name="Save", exact=True).click()
    page.wait_for_url(server + "/")
    (m,) = db.list_models(conn)
    assert (m["name"], m["page"], m["photo"] is not None) == ("Mantis", 16, True)
    assert db.get_book_by_path(conn, INSECTS)["page_offset"] == 6
    expect(page.locator("#list li .thumb.photo img")).to_have_count(1)


def test_library_star_filter_and_status(page, server, conn):
    page.goto(server + "/library")
    count = page.locator("#library-count")
    expect(count).to_contain_text("of 3")
    card = page.locator("article.book-card").filter(has=page.locator(f'[title="{INSECTS}"]'))
    card.locator("button.star").click()
    expect(card.locator("button.star")).to_have_text("★")
    page.locator('.filters input[name="starred"]').check()
    expect(count).to_contain_text("Showing 1 of 3")
    card = page.locator("article.book-card").filter(has=page.locator(f'[title="{INSECTS}"]'))
    card.locator('select[name="status"]').select_option("explored")
    expect(page.locator("article.book-card").first).to_have_class(re.compile("bstatus-explored"))
    assert db.get_book_by_path(conn, INSECTS)["status"] == "explored"


def test_search_add_opens_reader_with_name(page, server):
    add_ocr_pages([(NATURE, 2, "Rhinoceros by Fumiaki Kawahata")])
    page.goto(server + "/search?q=rhinoceros")
    page.locator("main").get_by_role("link", name=re.compile("^\\+ Add “")).first.click()
    page.wait_for_url(re.compile("/library/book\\?"))
    assert "page=2" in page.url
    expect(page.locator("#qa-name")).to_have_value("Rhinoceros (サイ)")


def test_bulk_add_shown_models_to_a_new_study(page, server, conn):
    make_models(conn, "Crane", "Flapping Crane", "Frog")
    page.goto(server + "/")
    page.locator('.filters input[name="q"]').fill("crane")
    expect(page.locator("#list-count")).to_have_text("Showing 2 of 3")
    page.locator(".bulk-study summary").click()
    page.locator('.bulk-row input[name="new_study"]').fill("Cranes")
    page.locator(".bulk-row").get_by_role("button", name="Add").click()
    page.wait_for_url(re.compile("/studies/\\d+$"))
    expect(page.locator("main")).to_contain_text("Flapping Crane")
    expect(page.locator("main")).not_to_contain_text("Frog")
