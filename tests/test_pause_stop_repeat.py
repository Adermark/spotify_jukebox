import asyncio

# Must come first: it points the app at a temporary test database before app modules load.
from tests.test_api import FAKE_TRACKS, client, guest, add  # noqa: F401
from tests.test_player import FakeSpotify, fake, no_grace, uri  # noqa: F401
from tests.test_sonos import sonos  # noqa: F401

from fastapi.testclient import TestClient

from app import db, main, player as player_mod
from app.player import Player, is_paused
from app.queue_logic import song_key
from app.spotify import spotify


def run(coro):
    return asyncio.run(coro)


def admin():
    c = TestClient(main.app)
    assert c.post("/api/admin/login", json={"password": "secret"}).status_code == 200
    return c


def play(item_ids):
    """Pretend these queue items played, oldest first."""
    for n in item_ids:
        item = next(i for i in db.items_with_status("queued") if i["track_uri"] == uri(n))
        db.mark_sent(item["id"])
        db.mark_now_playing(item["track_uri"])


# --- songs between repeats --------------------------------------------------------------

def test_versions_of_a_song_count_as_the_same_song():
    same = [("Hey Jude - Remastered 2015", "The Beatles"), ("Hey Jude (Live)", "The Beatles, Billy Preston"),
            ("Hey Jude [Radio Edit]", "The Beatles"), ("hey jude!", "the beatles")]
    assert len({song_key(t, a) for t, a in same}) == 1
    assert song_key("Hey Jude", "Wilson Pickett") != song_key("Hey Jude", "The Beatles")
    assert song_key("(Untitled)", "X") == "untitled|x"


def test_repeat_gap_off_by_default(client):
    db.update_settings({"no_duplicates": False})
    eve = guest(client, "Eve")
    assert add(eve, 1).status_code == 200
    play([1])
    assert add(eve, 1).status_code == 200


def test_recently_played_song_is_refused_with_reason(client):
    db.update_settings({"repeat_gap_songs": 2, "max_songs_per_user": 0})
    eve = guest(client, "Eve")
    for n in (1, 2, 3):
        add(eve, n)
    play([1])
    r = add(eve, 1)
    assert r.status_code == 409 and "playing right now" in r.json()["detail"] and "2 more songs" in r.json()["detail"]
    play([2])
    r = add(eve, 1)
    assert "played recently" in r.json()["detail"] and "1 more song." in r.json()["detail"]
    play([3])
    assert add(eve, 1).status_code == 200  # two other songs have played since


def test_other_version_already_in_queue_is_refused(client, monkeypatch):
    db.update_settings({"repeat_gap_songs": 5})
    live = {**FAKE_TRACKS[f"{1:022d}"], "uri": uri(8), "title": "Song 1 - Live"}
    monkeypatch.setitem(FAKE_TRACKS, f"{8:022d}", live)
    eve = guest(client, "Eve")
    add(eve, 1)
    r = add(eve, 8)
    assert r.status_code == 409 and "already in the queue" in r.json()["detail"]


# --- pause ------------------------------------------------------------------------------

def test_pause_stops_auto_play_until_resumed(client, fake, no_grace, monkeypatch):
    paused, resumed = [], []

    async def pause():
        paused.append(True)
        fake.playback_state["is_playing"] = False

    async def resume(device_id=None):
        resumed.append(device_id)
    monkeypatch.setattr(spotify, "pause", pause)
    monkeypatch.setattr(spotify, "resume", resume)

    add(guest(client, "Eve"), 1)
    fake.playing("spotify:track:x", progress=60_000)
    a = admin()
    assert a.post("/api/admin/pause").status_code == 200
    assert paused == [True] and is_paused()
    assert a.get("/api/admin/status").json()["paused"] is True

    fake.stopped()                               # the paused song ends up at 0:00
    p = Player()
    run(p.tick())
    assert fake.played == []                     # auto-play waits while paused

    assert a.post("/api/admin/resume").json() == {"playing": "Song 1"}  # nothing loaded: start the queue
    assert not is_paused() and fake.played == [uri(1)]


def test_resume_continues_the_paused_song(client, fake, monkeypatch):
    resumed = []

    async def resume(device_id=None):
        resumed.append(device_id)
    monkeypatch.setattr(spotify, "resume", resume)
    fake.playing("spotify:track:x", progress=60_000)
    fake.playback_state["is_playing"] = False
    fake.playback_state["device"] = {"id": "pc", "name": "PC"}
    player_mod.set_paused(True)
    assert admin().post("/api/admin/resume").json() == {"playing": None}
    assert resumed == ["pc"] and fake.played == []


def test_pressing_play_elsewhere_ends_the_pause(client, fake, monkeypatch):
    player_mod.set_paused(True)
    monkeypatch.setattr(player_mod, "_paused_at", 0)  # the pause was a while ago
    fake.playing("spotify:track:x")
    run(Player().tick())
    assert not is_paused()


def test_sonos_pause_and_resume(client, sonos):
    fake_sonos, p = sonos
    add(guest(client, "Eve"), 1)
    run(p.play_next_now())
    run(p.pause_music())
    assert fake_sonos.state == "PAUSED_PLAYBACK" and is_paused()
    run(p.tick())
    assert fake_sonos.taken_over == 1

    async def resume():
        fake_sonos.state = "PLAYING"
    fake_sonos.resume = resume
    assert run(p.resume_music()) is None
    assert fake_sonos.state == "PLAYING" and not is_paused()


# --- stop jukebox -----------------------------------------------------------------------

def test_shutdown_pauses_and_stops_the_server(client, fake, monkeypatch):
    class FakeServer:
        should_exit = False

    async def pause():
        fake.playback_state["is_playing"] = False
    monkeypatch.setattr(spotify, "pause", pause)
    monkeypatch.setattr(main, "server", FakeServer())
    fake.playing("spotify:track:x")

    assert TestClient(main.app).post("/api/admin/shutdown").status_code == 401
    assert admin().post("/api/admin/shutdown").status_code == 200
    assert main.server.should_exit and is_paused()
    assert fake.playback_state["is_playing"] is False
