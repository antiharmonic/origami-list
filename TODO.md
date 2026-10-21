# TODO

## Setup
- [ ] Move `~/Books/Nicolas Terry Presents - Origami Nature Study … .pdf` into `~/Books/Origami/`
- [ ] Import `the-list.csv` (Books & import page, or `docker compose exec -T app python -m app.import_csv - < the-list.csv`)
- [ ] Click through the UI in Firefox: drag-to-reorder, book search, live preview, photo upload
- [ ] Confirm the container comes back after a reboot

## Ideas
- [ ] Embedded PDF.js viewer with an "add this page" button (records book + current page)
- [ ] Crop thumbnails to the model image instead of the whole page
- [ ] Pre-fill model entries from a PDF's outline/bookmarks where books have one
- [ ] Remove `books` rows that no model references any more (they currently linger on the Books page)
- [ ] Answer `HEAD` requests on `/books/...` (currently 405; GET with ranges works)
- [ ] Decide on the complexity scale (currently 1–5)
