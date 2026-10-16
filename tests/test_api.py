import os
import tempfile

# Configure before the app is imported.
_tmp = tempfile.mkdtemp()
os.environ["CONFIG_FILE"] = os.path.join(_tmp, "test.env")  # never read or write the real .env
os.environ["OPEN_BROWSER"] = "0"
os.environ["ADMIN_PASSWORD"] = "secret"
os.environ["SPOTIFY_CLIENT_ID"] = "0" * 32
os.environ["SPOTIFY_CLIENT_SECRET"] = "1" * 32
os.environ["DATABASE_PATH"] = os.path.join(_tmp, "test.db")
os.environ["POLL_INTERVAL"] = "3600"
os.environ["MDNS_NAME"] = ""  # no network advertising from tests

import pytest
from fastapi.testclient import TestClient

from app import db, main
from app.spotify import spotify

FAKE_TRACKS = {
    f"{n:022d}": {
        "uri": f"spotify:track:{n:022d}", "title": f"Song {n}", "artists": "Artist",
        "album_art": None, "duration_ms": 200_000, "explicit": n == 9,
    }
    for n in range(1, 10)
}


@pytest.fixture
def client(monkeypatch):
    async def fake_track(track_id):
        return FAKE_TRACKS[track_id]
    monkeypatch.setattr(spotify, "track", fake_track)
    with TestClient(main.app) as c:
        with db.connect() as conn:
            conn.execute("DELETE FROM queue_items")
            conn.execute("DELETE FROM users")
            conn.execute("DELETE FROM settings")
        yield c


def guest(client_app, name):
    c = TestClient(main.app)
    assert c.post("/api/join", json={"name": name}).status_code == 200
    return c


def add(c, n):
    return c.post("/api/queue", json={"uri": f"spotify:track:{n:022d}"})


def test_favicon(client):
    assert client.get("/favicon.ico").headers["content-type"] == "image/x-icon"
    for path in ("/static/icon-192.png", "/static/apple-touch-icon.png"):
        r = client.get(path)
        assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    page = client.get("/").text
    assert 'href="/favicon.ico"' in page and 'rel="apple-touch-icon"' in page


def test_queue_flow_with_limits_and_fairness(client):
    alice, bob = guest(client, "Alice"), guest(client, "Bob")
    for n in (1, 2, 3):
        assert add(alice, n).status_code == 200
    r = add(alice, 4)
    assert r.status_code == 403 and "3 songs" in r.json()["detail"]
    assert add(bob, 5).status_code == 200
    assert add(bob, 1).status_code == 409  # duplicate

    state = alice.get("/api/state").json()
    assert [(i["title"], i["added_by"]) for i in state["queue"]] == [
        ("Song 1", "Alice"), ("Song 5", "Bob"), ("Song 2", "Alice"), ("Song 3", "Alice"),
    ]
    assert state["my_count"] == 3
    assert [i["mine"] for i in state["queue"]] == [True, False, True, True]

    # Bob can't remove Alice's song; Alice can.
    first_id = state["queue"][0]["id"]
    assert bob.delete(f"/api/queue/{first_id}").status_code == 404
    assert alice.delete(f"/api/queue/{first_id}").status_code == 200


def test_admin_settings_and_moderation(client):
    admin = TestClient(main.app)
    assert admin.get("/api/admin/settings").status_code == 401
    assert admin.post("/api/admin/login", json={"password": "nope"}).status_code == 401
    assert admin.post("/api/admin/login", json={"password": "secret"}).status_code == 200

    s = admin.put("/api/admin/settings", json={"allow_explicit": False, "max_songs_per_user": 0}).json()
    assert s["allow_explicit"] is False and s["max_songs_per_user"] == 0

    carol = guest(client, "Carol")
    assert add(carol, 9).status_code == 403  # explicit
    for n in (1, 2, 3, 4):
        assert add(carol, n).status_code == 200  # unlimited now

    queue = admin.get("/api/admin/queue").json()["queue"]
    last = queue[-1]["id"]
    assert admin.post(f"/api/admin/queue/{last}/top").status_code == 200
    assert admin.get("/api/admin/queue").json()["queue"][0]["id"] == last

    users = admin.get("/api/admin/users").json()["users"]
    carol_id = next(u["id"] for u in users if u["name"] == "Carol")
    admin.post(f"/api/admin/users/{carol_id}/block", json={"blocked": True})
    assert add(carol, 5).status_code == 403
    assert admin.get("/api/admin/queue").json()["queue"] == []


def test_now_playing_bookkeeping(client):
    dave = guest(client, "Dave")
    add(dave, 1)
    add(dave, 2)
    first, second = db.items_with_status("queued")
    db.mark_sent(first["id"])
    db.mark_now_playing(first["track_uri"])
    assert db.get_item(first["id"])["status"] == "playing"
    db.mark_sent(second["id"])
    db.mark_now_playing(second["track_uri"])
    assert db.get_item(first["id"])["status"] == "played"
    assert db.get_item(second["id"])["status"] == "playing"
    assert len(db.history()) == 2
