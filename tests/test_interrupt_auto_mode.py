from tests.test_api import client, guest, add  # noqa: F401
import pytest

from app import db
from app.playback import playback


def test_interrupt_auto_mode_when_queue_empty(client, monkeypatch):
    played_calls = []

    async def mock_play_next_now():
        played_calls.append(True)
        return {"title": "Song 1"}

    monkeypatch.setattr(playback, "play_next_now", mock_play_next_now)
    monkeypatch.setattr(playback, "can_play", lambda: True)

    # Simulate auto-mode: music is playing, but added_by is None (fallback playlist)
    playback.active.now_playing = {
        "uri": "spotify:track:fallback",
        "title": "Fallback Song",
        "artists": "Fallback Artist",
        "is_playing": True,
        "added_by": None,
    }

    assert playback.is_in_auto_mode() is True

    alice = guest(client, "Alice")
    r = add(alice, 1)
    assert r.status_code == 200
    assert len(played_calls) == 1, "Expected auto-mode to be interrupted and play_next_now() called"


def test_do_not_interrupt_when_jukebox_song_is_playing(client, monkeypatch):
    played_calls = []

    async def mock_play_next_now():
        played_calls.append(True)
        return {"title": "Song 2"}

    monkeypatch.setattr(playback, "play_next_now", mock_play_next_now)
    monkeypatch.setattr(playback, "can_play", lambda: True)

    # Simulate jukebox song playing (added_by is set)
    playback.active.now_playing = {
        "uri": "spotify:track:0000000000000000000001",
        "title": "Song 1",
        "artists": "Artist",
        "is_playing": True,
        "added_by": "Alice",
    }

    assert playback.is_in_auto_mode() is False

    bob = guest(client, "Bob")
    r = add(bob, 2)
    assert r.status_code == 200
    assert len(played_calls) == 0, "Should not interrupt when a jukebox song is already playing"


def test_do_not_interrupt_when_queue_not_empty(client, monkeypatch):
    played_calls = []

    async def mock_play_next_now():
        played_calls.append(True)
        return {"title": "Song 1"}

    monkeypatch.setattr(playback, "play_next_now", mock_play_next_now)
    monkeypatch.setattr(playback, "can_play", lambda: True)

    # Queue already has a song
    alice = guest(client, "Alice")
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO queue_items (user_id, track_uri, title, artists, duration_ms, status, added_at) "
            "VALUES (1, 'spotify:track:existing', 'Existing', 'Artist', 100000, 'queued', 1000)"
        )

    playback.active.now_playing = {
        "uri": "spotify:track:fallback",
        "is_playing": True,
        "added_by": None,
    }

    bob = guest(client, "Bob")
    r = add(bob, 2)
    assert r.status_code == 200
    assert len(played_calls) == 0, "Should not interrupt if queue was not empty"


def test_do_not_interrupt_when_auto_play_disabled(client, monkeypatch):
    played_calls = []

    async def mock_play_next_now():
        played_calls.append(True)
        return {"title": "Song 1"}

    monkeypatch.setattr(playback, "play_next_now", mock_play_next_now)
    monkeypatch.setattr(playback, "can_play", lambda: True)

    db.update_settings({"auto_play": False})

    playback.active.now_playing = {
        "uri": "spotify:track:fallback",
        "is_playing": True,
        "added_by": None,
    }

    alice = guest(client, "Alice")
    r = add(alice, 1)
    assert r.status_code == 200
    assert len(played_calls) == 0, "Should not interrupt if auto_play setting is False"
