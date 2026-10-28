"""Studies: named groups of models.

- Studies are created and chosen on the model form (names are case-insensitive, a new one can be typed).
- The list filters by study (in / not in), and the shown models can be added to a study in one go.
- A study page shows its models, with fold photos preferred over diagram thumbnails.
"""
from app import db
from tests.helpers import INSECTS, study_ids


def test_studies_from_the_model_form(client, conn):
    r = client.post("/models", data={"name": "Rhino", "new_study": " rhino  study ", "then": "new"},
                    follow_redirects=False)
    (m,) = db.list_models(conn)
    (study,) = db.list_studies(conn)
    assert study["name"] == "rhino study" and study_ids(conn, m["id"]) == [study["id"]]
    assert f"study={study['id']}" in r.headers["location"]  # Save and new carries the study
    assert f'value="{study["id"]}" checked' in client.get(r.headers["location"]).text
    assert f'href="/studies/{study["id"]}">rhino study</a>' in client.get("/").text

    # Same name in another case is the same study; unticking it on edit removes the membership.
    client.post("/models", data={"name": "Rhino 2", "new_study": "Rhino Study"})
    assert len(db.list_studies(conn)) == 1
    client.post(f"/models/{m['id']}", data={"name": "Rhino"})
    assert study_ids(conn, m["id"]) == []


def test_study_filters_bulk_add_and_page(client, conn):
    for name in ("Black Rhino", "White Rhino", "Crane"):
        db.create_model(conn, {"name": name})
    show_all = {"filtered": 1, "status": db.STATUSES}
    r = client.post("/studies/bulk", data={**show_all, "q": "rhino", "new_study": "Rhinos"})
    sid = db.study_by_name(conn, "Rhinos", create=False)
    assert r.status_code == 204 and r.headers["HX-Redirect"] == f"/studies/{sid}"
    client.post("/studies/bulk", data={**show_all, "q": "rhino", "target_study": sid})  # no duplicates
    assert db.get_study(conn, sid)["model_count"] == 2

    assert "Showing 2 of 3" in client.get("/", params={**show_all, "study": f"in:{sid}"}).text
    page = client.get("/", params={**show_all, "study": f"out:{sid}"}).text
    assert "Showing 1 of 3" in page and "Crane" in page

    # The page prefers the fold photo, falling back to the diagram thumbnail.
    rhinos = db.list_models(conn, study=("in", sid))
    db.update_model(conn, rhinos[0]["id"], {"photo": "1.jpg", "status": "folded"})
    db.update_model(conn, rhinos[1]["id"], {"book_id": db.get_or_create_book(conn, INSECTS), "page": 2})
    page = client.get(f"/studies/{sid}").text
    assert 'src="/photos/1.jpg"' in page and "/thumb?" in page and "1 folded · 0 attempted · 1 to go" in page

    client.post(f"/studies/{sid}/remove", data={"model_id": rhinos[0]["id"]})
    assert db.get_study(conn, sid)["model_count"] == 1 and len(db.list_models(conn)) == 3
    client.post(f"/studies/{sid}", data={"name": "Rhinoceros", "notes": "horns"})
    assert dict(db.get_study(conn, sid))["notes"] == "horns"
    client.post(f"/models/{rhinos[1]['id']}/delete")  # cascades to the membership
    assert db.get_study(conn, sid)["model_count"] == 0
    client.post(f"/studies/{sid}/delete")
    assert db.list_studies(conn) == [] and len(db.list_models(conn)) == 2
    assert client.get(f"/studies/{sid}").status_code == 404
