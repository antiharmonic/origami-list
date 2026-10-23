# TODO

## Setup
- [ ] Move `~/Books/Nicolas Terry Presents - Origami Nature Study … .pdf` into `~/Books/Origami/`
- [ ] Import `the-list.csv` (Import / export page, or `docker compose exec -T app python -m app.import_csv - < the-list.csv`)
- [ ] Click through the UI in Firefox: drag-to-reorder, book search, live preview, photo upload
- [ ] Confirm the container comes back after a reboot

## Ideas
- [ ] Embedded PDF.js viewer with an "add this page" button (records book + current page)
- [ ] Crop thumbnails to the model image instead of the whole page
- [ ] Pre-fill model entries from a PDF's outline/bookmarks where books have one
- [ ] Library: model-name index per book (typed list, or extracted from PDF outlines/TOC) so models are searchable before they're added
- [ ] Library: editor field and structured tags (designers/notes are free text for now)
- [ ] Library: overall complexity per book
- [ ] Library: show `books` rows whose file has moved or been renamed (path no longer in the scan), and let them be re-linked
- [ ] External integration with Gilad's Origami Database (giladorigami.com):
  - links from designers/authors and books to their Gilad pages
  - a search bar that jumps straight to Gilad's model search
  - design it alongside our own search needs (one box that searches our models/library and offers "search Gilad" as an option?)
- [ ] Answer `HEAD` requests on `/books/...` (currently 405; GET with ranges works)
- [ ] Notes: linkify bare URLs (needs `linkify-it-py`)
- [ ] Decide on the complexity scale (currently 1–5)
