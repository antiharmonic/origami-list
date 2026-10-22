# Origami List

A to-fold list for origami models. It serves the PDFs in `~/Books/Origami` over HTTP so each entry opens at the right page, and shows a thumbnail of that page.

Open http://localhost:5748 (or `http://<this-pc>:5748` from another device on the LAN).

## Running
```sh
docker compose up -d --build   # first run, or after changing code
docker compose logs -f
```
`restart: unless-stopped` plus the enabled `docker.service` brings it back on every boot.

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
