import asyncio

# Must come first: it points the app at a temporary test database before app modules load.
from tests.test_api import client, guest, add  # noqa: F401  (shared fixture + helpers)

import pytest

from app import db, player as player_mod
from app.player import Player
from app.spotify import SpotifyError, spotify


def uri(n):
    return f"spotify:track:{n:022d}"


class FakeSpotify:
    """Minimal model of Spotify: a current track and an upcoming queue."""

    def __init__(self):
        self.playback_state = None
        self.upcoming: list[str] = []
        self.played, self.added, self.skips = [], [], 0
        self.device = None
        self.devices_list = [
            {"id": "stale", "name": "Old PC", "is_active": True},
            {"id": "real", "name": "Host PC", "is_active": False},
        ]

    def playing(self, track_uri, progress=30_000):
        self.playback_state = {
            "device": DEVICE, "is_playing": True, "progress_ms": progress,
            "item": {"uri": track_uri, "type": "track", "name": track_uri[-4:], "duration_ms": 200_000, "artists": []},
        }

    def stopped(self):
        self.playback_state = {"device": DEVICE, "is_playing": False, "progress_ms": 0, "item": None}

    async def playback(self):
        return self.playback_state

    async def devices(self):
        return self.devices_list

    async def queue(self):
        current = (self.playback_state or {}).get("item") or {}
        return current.get("uri"), list(self.upcoming)

    async def play_now(self, track_uri, device_id=None, position_ms=0):
        self.played.append(track_uri)
        self.device = device_id

    async def add_to_queue(self, track_uri):
        self.added.append(track_uri)
        self.upcoming.append(track_uri)

    async def skip(self):
        self.skips += 1
        if not self.upcoming:
            raise SpotifyError(403, "Restriction violated")
        self.playing(self.upcoming.pop(0), progress=0)


DEVICE = {"name": "PC"}


@pytest.fixture
def fake(monkeypatch):
    f = FakeSpotify()
    for name in ("playback", "play_now", "add_to_queue", "skip", "devices", "queue"):
        monkeypatch.setattr(spotify, name, getattr(f, name))
    monkeypatch.setattr(spotify, "is_connected", lambda: True)
    return f


@pytest.fixture
def no_grace(monkeypatch):
    monkeypatch.setattr(player_mod, "STOPPED_GRACE_SECONDS", 0)


def run(coro):
    return asyncio.run(coro)


def test_autostart_when_stopped(client, fake, no_grace):
    add(guest(client, "Eve"), 1)
    fake.stopped()
    p = Player()
    run(p.tick())
    assert fake.played == [uri(1)]
    run(p.tick())  # still looks stopped right after: no double start
    assert len(fake.played) == 1


def test_no_autostart_during_brief_stopped_blip(client, fake):
    # Skips/track changes briefly report "not playing at 0:00": that's not a stop.
    add(guest(client, "Eve"), 1)
    fake.stopped()
    run(Player().tick())
    assert fake.played == []


def test_no_autostart_when_paused_mid_song(client, fake, no_grace):
    add(guest(client, "Eve"), 1)
    fake.playing("spotify:track:x", progress=60_000)
    fake.playback_state["is_playing"] = False
    run(Player().tick())
    assert fake.played == []


def test_no_autostart_when_disabled(client, fake, no_grace):
    db.update_settings({"auto_play": False})
    add(guest(client, "Eve"), 1)
    fake.stopped()
    run(Player().tick())
    assert fake.played == []


def test_restart_mid_song_keeps_it_as_ours(client, fake):
    add(guest(client, "Eve"), 1)
    fake.playing("spotify:track:x", progress=190_000)
    p = Player()
    run(p.tick())                      # pushes song 1
    run(fake.skip())                   # song 1 starts
    run(p.tick())
    assert p.now_playing["added_by"] == "Eve"
    restarted = Player()               # a new player knows nothing about what was playing
    run(restarted.tick())
    assert restarted.now_playing["added_by"] == "Eve"
    assert not restarted.is_in_auto_mode()


def test_push_near_end_of_track_only_once(client, fake):
    add(guest(client, "Eve"), 1)
    fake.playing("spotify:track:x", progress=190_000)
    p = Player()
    run(p.tick())
    run(p.tick())
    assert fake.added == [uri(1)]
    assert db.items_with_status("sent")[0]["track_uri"] == uri(1)


def test_song_plays_once_when_track_ends_naturally(client, fake):
    eve = guest(client, "Eve")
    add(eve, 1)
    add(eve, 2)
    fake.playing("spotify:track:x", progress=190_000)
    p = Player()
    run(p.tick())                      # pushes song 1
    run(fake.skip())                   # track ends -> Spotify plays song 1
    run(p.tick())
    assert db.items_with_status("playing")[0]["track_uri"] == uri(1)
    run(p.skip())                      # admin skip -> song 2, not song 1 again
    run(p.tick())
    assert fake.playback_state["item"]["uri"] == uri(2)
    assert fake.added == [uri(1), uri(2)]


def test_skip_while_playing_lines_up_our_next_song(client, fake):
    add(guest(client, "Eve"), 1)
    fake.playing("spotify:track:x")
    fake.upcoming = ["spotify:track:album-next"]  # rest of the album/playlist
    run(Player().skip())
    assert fake.playback_state["item"]["uri"] == uri(1)
    assert fake.played == []  # no direct play command, which the desktop client ignores


def test_skip_passes_stale_entries_ahead_of_ours(client, fake):
    add(guest(client, "Eve"), 1)
    fake.playing("spotify:track:x")
    fake.upcoming = ["spotify:track:stale1", "spotify:track:stale2"]
    run(Player().skip())
    assert fake.playback_state["item"]["uri"] == uri(1)
    assert fake.skips == 3


def test_song_already_in_spotify_queue_is_not_added_twice(client, fake):
    add(guest(client, "Eve"), 1)
    fake.playing("spotify:track:x")
    fake.upcoming = [uri(1)]  # leftover from earlier
    run(Player().skip())
    assert fake.added == []
    assert fake.playback_state["item"]["uri"] == uri(1)


def test_starts_on_chosen_device_else_active(client, fake):
    add(guest(client, "Eve"), 1)
    add(guest(client, "Fay"), 2)
    run(Player().play_next_now())
    assert fake.device == "stale"  # nothing chosen: Spotify's active device
    db.update_settings({"device_id": "real"})
    fake.playback_state = None
    db.requeue([i["id"] for i in db.items_with_status("sent")])
    run(Player().play_next_now())
    assert fake.device == "real"


def test_unplayed_sent_songs_return_to_queue(client, fake, no_grace):
    add(guest(client, "Eve"), 1)
    item = db.items_with_status("queued")[0]
    db.mark_sent(item["id"])
    with db.connect() as conn:  # pretend it was sent a while ago
        conn.execute("UPDATE queue_items SET sent_at = sent_at - 60 WHERE id = ?", (item["id"],))
    db.update_settings({"auto_play": False})
    fake.stopped()
    p = Player()
    run(p.tick())
    assert db.get_item(item["id"])["status"] == "queued"
    assert p.warning


def test_restricted_device_gives_clear_message_and_no_queueing(client, fake):
    add(guest(client, "Eve"), 1)
    fake.playing("spotify:track:x", progress=190_000)
    fake.playback_state["device"] = {"name": "Kitchen Speaker", "is_restricted": True}
    p = Player()
    run(p.tick())
    assert fake.added == [] and "Kitchen Speaker" in p.warning
    with pytest.raises(SpotifyError) as exc:
        run(p.skip())
    assert exc.value.status == 409 and "Kitchen Speaker" in str(exc.value)
    with pytest.raises(SpotifyError):
        run(p.play_next_now())
    assert fake.added == [] and fake.skips == 0


def test_skip_with_empty_queue_gives_friendly_error(client, fake):
    with pytest.raises(SpotifyError) as exc:
        run(Player().skip())
    assert exc.value.status == 409
