import asyncio

# Must come first: it points the app at a temporary test database before app modules load.
from tests.test_api import client  # noqa: F401  (shared fixture)

import pytest

from app import db, volume as volume_mod
from app.volume import AutoModeVolume


class FakeDevice:
    def __init__(self, volume=80, controllable=True):
        self.volume = volume
        self.controllable = controllable
        self.history: list[int] = []
        self.playing = True
        self.calls: list[str] = []

    async def get(self):
        return self.volume if self.controllable else None

    async def set(self, v):
        self.volume = v
        self.history.append(v)

    async def pause(self):
        self.playing = False
        self.calls.append("pause")

    async def resume(self):
        self.playing = True
        self.calls.append("resume")


@pytest.fixture
def fast(monkeypatch):
    async def no_sleep(_):
        pass
    monkeypatch.setattr(volume_mod.asyncio, "sleep", no_sleep)


def make(device):
    return AutoModeVolume("test", device.get, device.set, device.pause, device.resume)


def run(coro):
    return asyncio.run(coro)


def test_fallback_is_faded_down_and_restored_for_jukebox_song(client, fast):
    db.update_settings({"pause_volume": 40})
    dev = FakeDevice(80)
    v = make(dev)

    run(v.update("fallback"))
    assert dev.volume == 40
    assert len(dev.history) > 1, "should fade in steps, not jump"
    assert dev.history == sorted(dev.history, reverse=True)

    run(v.update("fallback"))  # already lowered: left alone
    assert dev.volume == 40

    run(v.update("ours"))
    assert dev.volume == 80


def test_manual_change_during_fallback_becomes_the_pause_volume(client, fast, monkeypatch):
    monkeypatch.setattr(volume_mod, "QUIET_SECONDS", -1)
    db.update_settings({"pause_volume": 40})
    dev = FakeDevice(80)
    v = make(dev)
    run(v.update("fallback"))
    run(v.update("fallback", 30))  # host turns it down by hand
    assert dev.volume == 40, "the device already is at 30; nothing to set"
    assert db.get_settings()["pause_volume"] == 30
    run(v.update("ours"))
    assert dev.volume == 80


def test_turning_fallback_above_jukebox_volume_raises_both(client, fast, monkeypatch):
    monkeypatch.setattr(volume_mod, "QUIET_SECONDS", -1)
    db.update_settings({"pause_volume": 40})
    dev = FakeDevice(80)
    v = make(dev)
    run(v.update("fallback"))
    run(v.update("fallback", 90))
    assert db.get_settings()["pause_volume"] == 90
    run(v.update("ours"))
    assert dev.volume == 90


def test_our_own_volume_changes_are_not_taken_as_manual(client, fast):
    db.update_settings({"pause_volume": 40})
    dev = FakeDevice(80)
    v = make(dev)
    run(v.update("fallback"))
    run(v.update("fallback", 52))  # a stale reading from mid-fade
    assert db.get_settings()["pause_volume"] == 40


def test_paused_or_unknown_leaves_volume(client, fast):
    db.update_settings({"pause_volume": 40})
    dev = FakeDevice(80)
    v = make(dev)
    run(v.update(None))
    assert dev.history == []


def test_pause_volume_above_jukebox_volume_means_no_change(client, fast):
    db.update_settings({"pause_volume": 90})
    dev = FakeDevice(80)
    v = make(dev)
    run(v.update("fallback"))
    assert dev.history == []
    assert v.status() == {"mode": "pause", "volume": 80, "max": 80}


def test_slider_sets_jukebox_volume_during_jukebox_songs(client, fast):
    db.update_settings({"pause_volume": 40})
    dev = FakeDevice(80)
    v = make(dev)
    assert run(v.set_from_admin(60)) == {"mode": "jukebox", "volume": 60, "max": 100}
    assert dev.volume == 60
    assert db.get_settings()["pause_volume"] == 40


def test_slider_sets_pause_volume_during_fallback_clamped_to_jukebox_volume(client, fast):
    db.update_settings({"pause_volume": 40})
    dev = FakeDevice(80)
    v = make(dev)
    run(v.update("fallback"))
    assert run(v.set_from_admin(25)) == {"mode": "pause", "volume": 25, "max": 80}
    assert db.get_settings()["pause_volume"] == 25
    run(v.set_from_admin(95))
    assert dev.volume == 80 and db.get_settings()["pause_volume"] == 80
    run(v.update("ours"))
    assert dev.volume == 80


def test_lower_jukebox_volume_clamps_next_fallback(client, fast):
    db.update_settings({"pause_volume": 40})
    dev = FakeDevice(80)
    v = make(dev)
    run(v.set_from_admin(30))
    run(v.update("fallback"))
    assert dev.volume == 30, "fallback is never louder than the jukebox volume"
    assert db.get_settings()["pause_volume"] == 40, "the pause volume is kept for later"


def test_device_without_volume_control_is_left_alone(client, fast):
    db.update_settings({"pause_volume": 40})
    dev = FakeDevice(80, controllable=False)
    v = make(dev)
    run(v.update("fallback"))
    assert dev.history == []
    run(v.interrupt(lambda: asyncio.sleep(0)))
    assert dev.history == [] and dev.calls == []


def test_normal_volume_survives_restart(client, fast):
    db.update_settings({"pause_volume": 40})
    dev = FakeDevice(80)
    run(make(dev).update("fallback"))
    run(make(dev).update("ours"))  # a new instance, as after a restart
    assert dev.volume == 80


def test_interrupt_fades_out_then_starts_song_at_normal_volume(client, fast):
    db.update_settings({"pause_volume": 40})
    dev = FakeDevice(80)
    v = make(dev)
    run(v.update("fallback"))
    when_played = []

    async def play():
        when_played.append((dev.volume, dev.playing))
        return "song"

    assert run(v.interrupt(play)) == "song"
    assert 0 in dev.history, "should fade all the way out"
    # Paused and back at normal volume before the song starts, so none of it is quiet.
    assert when_played == [(80, False)]
    assert dev.calls == ["pause", "resume"]
    assert dev.playing and dev.volume == 80

    # The player can still report the old fallback track right after the skip.
    run(v.update("fallback"))
    assert dev.volume == 80


def test_interrupt_restores_volume_when_play_fails(client, fast):
    dev = FakeDevice(70)
    v = make(dev)

    async def play():
        raise RuntimeError("Spotify said no")

    with pytest.raises(RuntimeError):
        run(v.interrupt(play))
    assert dev.volume == 70
    assert dev.playing, "the fallback music should keep playing"


def test_setting_is_clamped_to_100(client):
    assert db.update_settings({"pause_volume": 250})["pause_volume"] == 100
    assert "_normal_volume_test" not in db.get_settings()
