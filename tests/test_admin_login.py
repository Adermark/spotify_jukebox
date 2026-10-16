# Must come first: it points the app at a temporary test database before app modules load.
from tests.test_api import client  # noqa: F401  (shared fixture)

from fastapi.testclient import TestClient

from app import db, main


def login(remember):
    c = TestClient(main.app)
    r = c.post("/api/admin/login", json={"password": "secret", "remember": remember})
    assert r.status_code == 200
    return c, r


def restart():
    main.admin_sessions.clear()  # what a server restart does to logins kept in memory


def test_remembered_login_survives_a_restart(client):
    c, r = login(remember=True)
    assert "max-age=2592000" in r.headers["set-cookie"].lower()
    restart()
    assert c.get("/api/admin/settings").status_code == 200


def test_plain_login_is_a_browser_session_and_ends_on_restart(client):
    c, r = login(remember=False)
    assert "max-age" not in r.headers["set-cookie"].lower()
    assert c.get("/api/admin/settings").status_code == 200
    restart()
    assert c.get("/api/admin/settings").status_code == 401


def test_logout_forgets_a_remembered_login(client):
    c, _ = login(remember=True)
    token = c.cookies.get(main.ADMIN_COOKIE)
    c.post("/api/admin/logout")
    assert not db.is_remembered_admin(token)
    other = TestClient(main.app)
    other.cookies.set(main.ADMIN_COOKIE, token)
    assert other.get("/api/admin/settings").status_code == 401


def test_only_a_hash_of_the_token_is_stored(client):
    c, _ = login(remember=True)
    token = c.cookies.get(main.ADMIN_COOKIE)
    with db.connect() as conn:
        stored = [r["token_hash"] for r in conn.execute("SELECT token_hash FROM admin_logins")]
    assert token not in stored and len(stored) >= 1


def test_expired_login_is_refused(client):
    c, _ = login(remember=True)
    with db.connect() as conn:
        conn.execute("UPDATE admin_logins SET expires_at = 0")
    restart()
    assert c.get("/api/admin/settings").status_code == 401
