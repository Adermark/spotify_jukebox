import asyncio

# Must come first: it points the app at a temporary test database before app modules load.
from tests.test_api import client, guest, add  # noqa: F401  (shared fixture + helpers)

import pytest

from app import db, player as player_mod
from app.sonos import SonosError, SonosStatus, spotify_uri
from app.sonos_player import SonosPlayer


def uri(n):
    return f"spotify:track:{n:022d}"


class FakeSonos:
    """Minimal model of a Sonos group: a queue, a position and a transport state."""

    def __init__(self):
        self.queue: list[str] = []
        self.position = 0           # 1-based, 0 = nothing
        self.state = "STOPPED"
        self.from_queue = True
        self.progress_ms = 0
        self.grouped = 0
        self.taken_over = 0
        self.group_volume = 30

    async def volume(self):
        return self.group_volume

    async def set_volume(self, volume):
        self.group_volume = volume

    async def status(self):
        current = self.queue[self.position - 1] if self.position else None
        return SonosStatus(
            state=self.state, from_queue=self.from_queue, queue_position=self.position,
            uri=current if self.from_queue else None, title="T" if current or not self.from_queue else None,
            artist="A", album_art=None, position_ms=self.progress_ms, duration_ms=200_000, room="Kitchen",
        )

    async def upcoming(self, queue_position):
        return [(i, u) for i, u in enumerate(self.queue) if i >= queue_position]

    async def add(self, track_uri):
        self.queue.append(track_uri)
        return len(self.queue) - 1

    async def play_index(self, index):
        self.position, self.state, self.progress_ms, self.from_queue = index + 1, "PLAYING", 0, True

    async def take_over(self, track_uri):
        self.taken_over += 1
        self.queue = []
        await self.play_index(await self.add(track_uri))

    async def next(self):
        if self.position >= len(self.queue):
            raise SonosError("UPnP Error 711")
        self.position += 1
        self.progress_ms = 0

    async def ensure_group(self):
        self.grouped += 1

    async def pause(self):
        if self.state == "PLAYING":
            self.state = "PAUSED_PLAYBACK"

    async def seek(self, position_ms):
        self.progress_ms = position_ms

    def track_ends(self):
        """Sonos moves to the next queue item by itself, or stops at the end."""
        if self.position < len(self.queue):
            self.position += 1
            self.progress_ms = 0
        else:
            self.state = "STOPPED"


@pytest.fixture
def sonos(client, monkeypatch):
    monkeypatch.setattr(player_mod, "STOPPED_GRACE_SECONDS", 0)
    import app.sonos_player as sp
    monkeypatch.setattr(sp, "STOPPED_GRACE_SECONDS", 0)
    db.update_settings({"playback_mode": "sonos", "sonos_rooms": "RINCON_1"})
    fake = FakeSonos()
    return fake, SonosPlayer(fake)


def run(coro):
    return asyncio.run(coro)


def test_spotify_uri_from_sonos_uri():
    assert spotify_uri("x-sonos-spotify:spotify%3atrack%3a7sA2SKTo1QbTSSYn5YvJC4?sid=9&flags=8224&sn=1") \
        == "spotify:track:7sA2SKTo1QbTSSYn5YvJC4"
    assert spotify_uri("x-rincon-stream:RINCON_123") is None


def test_autostart_takes_over_and_plays_through_queue(client, sonos):
    fake, p = sonos
    eve = guest(client, "Eve")
    add(eve, 1)
    add(eve, 2)
    run(p.tick())                                   # stopped + songs waiting -> start
    assert fake.queue == [uri(1)] and fake.taken_over == 1 and fake.grouped == 1
    run(p.tick())
    assert db.items_with_status("playing")[0]["track_uri"] == uri(1)
    assert p.now_playing["added_by"] == "Eve"

    fake.progress_ms = 190_000                      # near the end -> next song handed over once
    run(p.tick())
    run(p.tick())
    assert fake.queue == [uri(1), uri(2)]

    fake.track_ends()                               # Sonos continues by itself
    run(p.tick())
    assert db.items_with_status("playing")[0]["track_uri"] == uri(2)
    assert [i["status"] for i in db.history()] == ["playing", "played"]


def test_skip_jumps_to_our_next_song(client, sonos):
    fake, p = sonos
    eve = guest(client, "Eve")
    add(eve, 1)
    add(eve, 2)
    run(p.play_next_now())
    run(p.tick())
    run(p.skip())
    run(p.tick())
    assert fake.queue[fake.position - 1] == uri(2)
    assert fake.taken_over == 1  # second song was appended, not a fresh queue


def test_play_next_takes_over_from_tv_or_spotify_connect(client, sonos):
    fake, p = sonos
    fake.from_queue, fake.state = False, "PLAYING"  # e.g. the Arc playing TV sound
    add(guest(client, "Eve"), 1)
    run(p.tick())
    assert fake.taken_over == 0                     # auto-play leaves non-queue playback alone
    run(p.play_next_now())                          # but the admin can take over
    assert fake.taken_over == 1 and fake.queue == [uri(1)]


def test_pause_is_left_alone(client, sonos):
    fake, p = sonos
    add(guest(client, "Eve"), 1)
    fake.queue, fake.position, fake.state = ["spotify:track:x"], 1, "PAUSED_PLAYBACK"
    run(p.tick())
    assert fake.taken_over == 0 and fake.queue == ["spotify:track:x"]


def test_skip_with_nothing_queued(client, sonos):
    fake, p = sonos
    with pytest.raises(SonosError) as exc:
        run(p.skip())
    assert exc.value.status == 409


def test_first_room_ticked_starts_music(client, sonos):
    fake, p = sonos
    db.update_settings({"sonos_rooms": ""})
    add(guest(client, "Eve"), 1)
    run(p.apply_rooms(["RINCON_1"]))
    assert fake.taken_over == 1 and fake.state == "PLAYING"


def test_adding_a_room_mid_song_does_not_skip(client, sonos):
    fake, p = sonos
    eve = guest(client, "Eve")
    add(eve, 1)
    add(eve, 2)
    run(p.play_next_now())
    run(p.tick())

    async def regrouping_status():  # Sonos reports in-between states while regrouping
        return SonosStatus("TRANSITIONING", False, 0, None, None, None, None, 0, 0, "Kitchen")
    real_status = fake.status
    fake.status = regrouping_status
    run(p.tick())                     # in-between state: ignored
    fake.status = real_status
    run(p.apply_rooms(["RINCON_1", "RINCON_2"]))
    assert fake.queue == [uri(1)] and fake.position == 1 and fake.taken_over == 1
    run(p.tick())                     # held while the group settles: no reaction
    assert db.items_with_status("playing")[0]["track_uri"] == uri(1)


def test_takeover_pauses_spotify_on_the_computer(client, sonos, monkeypatch):
    from app.spotify import spotify
    fake, p = sonos
    paused = []

    async def playback():
        return {"is_playing": True, "device": {"name": "PC", "is_restricted": False}}

    async def pause():
        paused.append(True)
    monkeypatch.setattr(spotify, "is_connected", lambda: True)
    monkeypatch.setattr(spotify, "playback", playback)
    monkeypatch.setattr(spotify, "pause", pause)
    add(guest(client, "Eve"), 1)
    run(p.play_next_now())
    assert paused == [True] and fake.taken_over == 1


def test_unresponsive_room_gives_clear_error(client):
    from app.sonos import SonosController
    db.update_settings({"sonos_rooms": "RINCON_1"})
    c = SonosController()
    c._names = {"RINCON_1": "Kitchen"}
    c._discover = lambda force=False: {}
    with pytest.raises(SonosError) as exc:
        run(c.status())
    assert "Kitchen isn't responding" in str(exc.value)


def test_no_rooms_selected_gives_a_hint(client, sonos):
    fake, p = sonos

    async def no_status():
        return None
    fake.status = no_status
    run(p.tick())
    assert "Sonos room" in p.warning


def test_switching_modes_takes_the_song_along(client, sonos, monkeypatch):
    from app.playback import Playback
    from app.spotify import spotify
    from tests.test_player import FakeSpotify
    fake_sonos, sonos_player = sonos
    fake_spotify = FakeSpotify()
    for name in ("playback", "play_now", "devices", "queue"):
        monkeypatch.setattr(spotify, name, getattr(fake_spotify, name))
    paused = []

    async def pause():
        paused.append(True)
        fake_spotify.playback_state["is_playing"] = False
    monkeypatch.setattr(spotify, "pause", pause)
    monkeypatch.setattr(spotify, "is_connected", lambda: True)

    pb = Playback()
    pb.sonos = sonos_player
    db.update_settings({"playback_mode": "sonos"})
    add(guest(client, "Eve"), 1)
    run(sonos_player.play_next_now())
    run(sonos_player.tick())
    fake_sonos.progress_ms = 61_000

    run(pb.switch_mode("spotify"))       # Sonos -> Spotify: same song, same place
    assert fake_sonos.state == "PAUSED_PLAYBACK"
    assert fake_spotify.played == [uri(1)]
    assert db.get_settings()["playback_mode"] == "spotify"

    fake_spotify.playing(uri(1), progress=90_000)
    run(pb.switch_mode("sonos"))         # Spotify -> Sonos: back again
    assert paused == [True]
    assert fake_sonos.queue == [uri(1)] and fake_sonos.progress_ms == 90_000 and fake_sonos.state == "PLAYING"
