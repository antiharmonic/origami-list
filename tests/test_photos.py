"""Fold photos.

- A photo pasted or dropped on the model form (a plain image upload) is saved with the model, and a new one replaces the old.
"""
from app import db


def test_pasted_photo_upload_and_replace(client, conn):
    # A pasted screenshot arrives as a plain multipart file named "image.png".
    from app import config
    client.post("/models", data={"name": "Rhino"}, files={"photo": ("image.png", b"\x89PNG fake", "image/png")})
    m = db.list_models(conn)[0]
    assert m["photo"] == f"{m['id']}.png"
    client.post(f"/models/{m['id']}", data={"name": "Rhino"},
                files={"photo": ("image.jpg", b"\xff\xd8 fake", "image/jpeg")})
    photos = config.DATA_DIR / "photos"
    assert db.get_model(conn, m["id"])["photo"] == f"{m['id']}.jpg"
    assert sorted(p.name for p in photos.iterdir()) == [f"{m['id']}.jpg"]
    assert "paste an image" in client.get(f"/models/{m['id']}/edit").text
