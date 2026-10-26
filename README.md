# Origami List

A to-fold list for origami models. It serves the PDFs in `~/Books/Origami` over HTTP so each entry opens at the right page, and shows a thumbnail of that page.

Open http://localhost:5748 (or `http://<this-pc>:5748` from another device on the LAN).

## Running
```sh
docker compose up -d --build   # first run, or after changing code
docker compose logs -f
```
`restart: unless-stopped` plus the enabled `docker.service` brings it back on every boot.

## Reading a book
A book's page (click it in the **Library**) shows the PDF on the right and a panel on the left. Scroll to a model, type its name in **Add a model** and press Enter: the page is filled in from the viewer, and the designer, status and studies stay for the next one. Click a model's `p.N` to jump back to it. PDF.js is downloaded when the image is built; bump `PDFJS_VERSION`/`PDFJS_SHA256` in the `Dockerfile` to upgrade it.

## Fold photos
To add a photo of your fold, pick a file on the model form, drop one onto the photo field, or just paste an image (Ctrl+V) anywhere on the form. It's saved with the model when you click Save.

## Studies
Group models you want to fold together, e.g. every rhinoceros, or a designer deep dive. Tick studies (or type a new one) on a model's form. To start one quickly, search the list ("rhino") and use **Add the shown models to a study…**. Each study has a page with a gallery (your fold photo if you uploaded one, otherwise the diagram page), a note, and folded/attempted progress. The list's **Study** filter shows only a study's models, or everything but them.

## Notes
The **Notes** button in the top nav (or Alt+N) opens a scratchpad that's the same on every page, for ideas like "find a good octopus model". It's Markdown (`- bullets`, `**bold**`, `# headings`), autosaves as you type, and Enter continues a list, Tab indents, and Ctrl+B / Ctrl+I bold and italicise.

## Data
Everything lives in `./data/`: `origami.db` (SQLite), `thumbs/` (a cache, safe to delete) and `photos/`. Back it up by copying that folder, or use **Books & import → Export CSV**.

## Importing a CSV
Use the upload on the **Books & import** page, or:
```sh
docker compose exec -T app python -m app.import_csv - < the-list.csv
```
Book files are matched by filename anywhere under `~/Books/Origami`.

## Tests
```sh
docker compose run --rm --no-deps app pytest -q
```
