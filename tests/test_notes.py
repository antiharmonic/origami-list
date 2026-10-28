"""The global notes panel.

- Notes autosave with a revision number; a save from a stale copy (another window) is refused instead of overwriting.
- Markdown is rendered with raw HTML escaped.
"""


def test_notes_save_conflict_and_render(client):
    assert client.get("/notes").json() == {"body": "", "rev": 0}
    assert client.post("/notes", data={"body": "- octopus", "rev": 0}).json() == {"rev": 1}
    assert client.get("/notes").json() == {"body": "- octopus", "rev": 1}

    # A window still on rev 0 can't overwrite, unless it forces.
    r = client.post("/notes", data={"body": "stale", "rev": 0})
    assert r.status_code == 409 and r.json()["rev"] == 1
    assert client.get("/notes").json()["body"] == "- octopus"
    assert client.post("/notes", data={"body": "mine", "rev": 0, "force": "true"}).json() == {"rev": 2}

    html = client.post("/notes/preview", data={"body": "- **Komatsu**\n- <script>x</script>"}).text
    assert "<li><strong>Komatsu</strong></li>" in html and "<script>" not in html and "&lt;script&gt;" in html

    for page in ("/", "/library"):
        assert 'id="notes-panel"' in client.get(page).text
