from dataclasses import asdict

# Must come first: it points the app at a temporary config/database before app modules load.
from tests.test_api import client  # noqa: F401

import pytest
from fastapi.testclient import TestClient

from app import config as config_module
from app import main
from app.config import check_password, config
from app.spotify import spotify

APP_ID, APP_SECRET = "a" * 32, "b" * 32


@pytest.fixture
def fresh_install(client, monkeypatch, tmp_path):
    """Pretend nothing is configured yet; restore everything afterwards."""
    for key, value in asdict(config).items():
        monkeypatch.setattr(config, key, value)
    for key in ("ADMIN_PASSWORD", "ADMIN_PASSWORD_HASH", "SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET"):
        monkeypatch.setenv(key, "")
    monkeypatch.setattr(config, "admin_password", "")
    monkeypatch.setattr(config, "admin_password_hash", "")
    monkeypatch.setattr(config, "client_id", "")
    monkeypatch.setattr(config, "client_secret", "")
    env_file = tmp_path / ".env"
    env_file.write_text("# my notes\nPORT=8000\n", encoding="utf-8")
    monkeypatch.setattr(config_module, "CONFIG_FILE", env_file)

    async def accept(client_id, client_secret):
        return None if (client_id, client_secret) == (APP_ID, APP_SECRET) else "Spotify doesn't accept that"
    monkeypatch.setattr(spotify, "check_app_credentials", accept)
    return env_file


def test_first_start_picks_port_80_or_falls_back_and_saves_it(fresh_install, monkeypatch):
    from app.config import ensure_port
    monkeypatch.delenv("PORT", raising=False)
    fresh_install.write_text("", encoding="utf-8")
    assert ensure_port(is_free=lambda p: p == 80) == 80
    assert "PORT=80" in fresh_install.read_text(encoding="utf-8")
    assert config.redirect_uri == "http://127.0.0.1/callback"

    monkeypatch.delenv("PORT", raising=False)
    fresh_install.write_text("", encoding="utf-8")
    assert ensure_port(is_free=lambda p: p != 80) == 8000  # 80 taken or not allowed
    assert config.redirect_uri == "http://127.0.0.1:8000/callback"


def test_saved_port_is_kept(fresh_install, monkeypatch):
    from app.config import ensure_port
    monkeypatch.setenv("PORT", "8000")
    monkeypatch.setattr(config, "port", 8000)
    assert ensure_port(is_free=lambda p: True) == 8000


def test_existing_install_without_port_keeps_8000(fresh_install, monkeypatch):
    from app.config import ensure_port
    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.setattr(config, "admin_password", "x")
    monkeypatch.setattr(config, "client_id", APP_ID)
    monkeypatch.setattr(config, "client_secret", APP_SECRET)
    assert ensure_port(is_free=lambda p: True) == 8000  # its Spotify redirect URI uses 8000


def test_pages_send_you_to_setup_until_done(fresh_install):
    c = TestClient(main.app)
    for path in ("/", "/admin"):
        r = c.get(path, follow_redirects=False)
        assert r.status_code == 307 and r.headers["location"] == "/setup"


def test_setup_needs_the_one_time_token(fresh_install):
    c = TestClient(main.app)
    assert c.get("/api/setup/status").json()["allowed"] is False
    assert c.post("/api/setup/password", json={"password": "hunter22"}).status_code == 403
    bad = {"X-Setup-Token": "wrong"}
    assert c.post("/api/setup/password", json={"password": "hunter22"}, headers=bad).status_code == 403


def test_full_setup_flow(fresh_install):
    c = TestClient(main.app)
    token = {"X-Setup-Token": main.setup_token}
    status = c.get("/api/setup/status", headers=token).json()
    assert status["allowed"] and not status["configured"]
    assert status["redirect_uri"] == "http://127.0.0.1:8000/callback"

    assert c.post("/api/setup/password", json={"password": "short"}, headers=token).status_code == 422
    assert c.post("/api/setup/password", json={"password": "hunter22"}, headers=token).status_code == 200
    assert check_password("hunter22") and not check_password("hunter23")

    r = c.post("/api/setup/spotify-app", json={"client_id": "nope", "client_secret": APP_SECRET}, headers=token)
    assert r.status_code == 400 and "32 characters" in r.json()["detail"]
    r = c.post("/api/setup/spotify-app", json={"client_id": "c" * 32, "client_secret": APP_SECRET}, headers=token)
    assert r.status_code == 400 and "doesn't accept" in r.json()["detail"]
    assert c.post("/api/setup/spotify-app", json={"client_id": APP_ID, "client_secret": APP_SECRET}).status_code == 200

    assert config.configured
    saved = fresh_install.read_text(encoding="utf-8")
    assert "# my notes" in saved and "PORT=8000" in saved
    assert f"SPOTIFY_CLIENT_ID={APP_ID}" in saved and "ADMIN_PASSWORD_HASH=pbkdf2_sha256:" in saved
    assert "hunter22" not in saved

    # Setup is done: the token no longer works, but the logged-in admin can still change things.
    assert TestClient(main.app).post("/api/setup/password", json={"password": "x" * 8}, headers=token).status_code == 403
    assert c.get("/api/setup/status").json()["allowed"] is True
    assert c.get("/", follow_redirects=False).status_code == 200


def test_new_password_logs_out_remembered_admins(fresh_install):
    token = {"X-Setup-Token": main.setup_token}
    c = TestClient(main.app)
    assert c.post("/api/setup/password", json={"password": "hunter22"}, headers=token).status_code == 200
    remembered = TestClient(main.app)
    assert remembered.post("/api/admin/login", json={"password": "hunter22", "remember": True}).status_code == 200

    assert c.post("/api/setup/password", json={"password": "hunter33"}, headers=token).status_code == 200
    assert remembered.get("/api/admin/settings").status_code == 401
    assert c.get("/api/admin/settings").status_code == 200, "whoever changed it stays logged in"
