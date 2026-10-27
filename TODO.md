# TODO

## Setup
- [x] Move `~/Books/Nicolas Terry Presents - Origami Nature Study … .pdf` into `~/Books/Origami/`
- [x] Import `the-list.csv` (Import / export page, or `docker compose exec -T app python -m app.import_csv - < the-list.csv`)
- [x] Click through the UI in Firefox: drag-to-reorder, book search, live preview, photo upload
- [x] Confirm the container comes back after a reboot

## Ideas
- [ ] Viewer v2: "use selection as name" (when the PDF has a text layer), "use this page as cover", open the list's Diagrams links in the viewer
- [ ] Crop thumbnails to the model image instead of the whole page
- [ ] Pre-fill model entries from a PDF's outline/bookmarks where books have one
- [ ] Translation: re-OCR vertical (tategaki) pages with `jpn_vert` if image translation struggles; optional Claude engine behind `translate.call`
- [ ] Library: filter for fully indexed / not yet indexed books (once indexing is under way)
- [ ] Contents suggestions: picture-grid layouts ("Page 29 Page 33" under a row of names, e.g. Montroll), per-entry designers in anthologies, two-column Japanese contents
- [ ] Search: let a result be marked "not a model" (or hide noisy books) if results get cluttered
- [ ] Glossary: review the seeded designer kanji; grow the subject list as needed
- [ ] Library: editor field and structured tags (designers/notes are free text for now)
- [ ] Library: overall complexity per book
- [ ] Single diagram PDFs (kept as one-model books in `~/Books/Origami/diagrams/`, named `Designer - Model Name.pdf`):
  - library folder filter (or a "Books / Diagrams" toggle) so they don't crowd out the books
  - one-click "Add as model" on short PDFs: model at page 1, name and designer from the filename, and mark the PDF fully indexed
- [ ] External integration with Gilad's Origami Database (giladorigami.com):
  - links from designers/authors and books to their Gilad pages
  - a search bar that jumps straight to Gilad's model search
  - design it alongside our own search needs (one box that searches our models/library and offers "search Gilad" as an option?)
- [ ] Answer `HEAD` requests on `/books/...` (currently 405; GET with ranges works)
- [ ] Notes: linkify bare URLs (needs `linkify-it-py`)
- [ ] Favicon: an origami crane
- [ ] Book page: reopen a book at the last page the viewer showed (when no `?page=` is given)
- [ ] Fold date: record when a model first becomes Folded or Nailed (`updated_at` moves on every edit), with a sort or a small "recently folded" view; it can't be backfilled, so sooner is better
