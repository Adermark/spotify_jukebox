"""Volume for auto-mode: fallback music plays quieter, and fades out when a song interrupts it.

There are two volumes. The jukebox volume is what jukebox songs play at; it's simply the
device's volume while they play. The pause volume (a setting) is what fallback music plays
at, but never louder than the jukebox volume.

Each player (Spotify, Sonos) gives us a way to read and set its volume. While fallback music
plays we remember the jukebox volume ("normal"), so it can be put back when the next jukebox
song starts. It's kept in the database so a restart doesn't forget it.
"""
import asyncio
import logging
import time
from typing import Awaitable, Callable

from . import db

log = logging.getLogger("jukebox.volume")

FADE_DOWN_SECONDS = 3   # lowering the volume when fallback music starts
FADE_OUT_SECONDS = 2.5  # fading fallback music out before an interrupting song
STEP_SECONDS = 0.15     # target time between volume changes (each Spotify call takes about as long)
# After an interrupt the player can still report the old fallback track for a moment;
# don't lower the volume again because of that.
SETTLE_SECONDS = 10
# Players report a volume we set a moment late; until then a different reading isn't a
# change made by hand.
QUIET_SECONDS = 5


class AutoModeVolume:
    def __init__(self, name: str, get_volume: Callable[[], Awaitable[int | None]],
                 set_volume: Callable[[int], Awaitable[None]],
                 pause: Callable[[], Awaitable[None]], resume: Callable[[], Awaitable[None]]):
        """get_volume returns None when the device's volume can't be controlled."""
        self._key = f"normal_volume_{name}"
        self._get = get_volume
        self._set_device = set_volume
        self._pause = pause
        self._resume = resume
        self._lock = asyncio.Lock()
        self._lower_after = 0.0
        self._quiet_until = 0.0
        self._expected: int | None = None  # the volume we last set
        self.current: int | None = None    # last volume seen on the device (None = can't control)

    @property
    def normal(self) -> int | None:
        return db.get_state(self._key)

    @normal.setter
    def normal(self, volume: int | None) -> None:
        db.set_state(self._key, volume)

    @staticmethod
    def pause_volume() -> int:
        return db.get_settings()["pause_volume"]

    def status(self) -> dict:
        """For the admin page's sliders. volume is the jukebox volume; pause_volume is what
        fallback music plays at (never above the jukebox volume). None = can't be controlled."""
        if self.current is None:
            return {"mode": None, "volume": None, "pause_volume": None}
        normal = self.normal
        jukebox = normal if normal is not None else self.current
        return {"mode": "pause" if normal is not None else "jukebox", "volume": jukebox,
                "pause_volume": min(jukebox, self.pause_volume())}

    async def update(self, playing: str | None, current: int | None = None) -> None:
        """Called on every player tick. playing is "ours" (a jukebox song), "fallback"
        (other music), or None (paused, stopped or unknown: leave the volume alone).
        current is the device's volume if the player already read it."""
        if current is not None and time.time() >= self._quiet_until:
            self.current = current  # right after we set it, the player may still report the old value
        if self._lock.locked():
            return  # a fade is running
        try:
            async with self._lock:
                normal = self.normal
                if playing == "ours" and normal is not None:
                    await self._set(normal)
                    log.info("Volume back to %s for jukebox songs", normal)
                    self.normal = None
                elif playing == "fallback" and normal is None and time.time() >= self._lower_after:
                    if current is None:
                        current = self.current = await self._get()
                    if current is None:
                        return
                    self.normal = current
                    target = min(current, self.pause_volume())
                    if target < current:
                        await self._fade(current, target, FADE_DOWN_SECONDS)
                        log.info("Fallback music: volume %s -> %s", current, target)
                elif playing == "fallback" and normal is not None and current is not None:
                    self._adopt_manual_change(current, normal)
        except Exception as e:
            log.warning("Couldn't change the volume: %s", e)

    def _adopt_manual_change(self, current: int, normal: int) -> None:
        """Someone changed the volume on the device while fallback music played: that's the
        new pause volume, like moving the slider. Turning it above the jukebox volume raises
        that too, since fallback music is never louder."""
        if self._expected is None:
            self._expected = current  # first reading after a restart
        if current == self._expected or time.time() < self._quiet_until:
            return
        self._expected = current
        db.update_settings({"pause_volume": current})
        if current > normal:
            self.normal = current
        log.info("Volume changed on the device: pause volume is now %s", current)

    async def set_from_admin(self, volume: int | None = None, pause_volume: int | None = None) -> dict:
        """The admin's sliders: the jukebox volume and the pause volume (at most the jukebox
        volume). The device follows whichever one is playing right now."""
        async with self._lock:
            normal = self.normal
            jukebox = normal if normal is not None else self.current
            if volume is not None:
                jukebox = max(0, min(100, int(volume)))
                if normal is None:
                    await self._set(jukebox)
                else:
                    self.normal = jukebox
            if pause_volume is not None:
                limit = jukebox if jukebox is not None else 100
                db.update_settings({"pause_volume": max(0, min(limit, int(pause_volume)))})
            if normal is not None:
                await self._set(min(jukebox, self.pause_volume()))
        return self.status()

    async def interrupt(self, play: Callable[[], Awaitable]):
        """Fade the fallback music out and run play() to start the jukebox song.

        Once the fade reaches 0 the player is paused and the normal volume put back while it's
        silent, so the song starts from its first second at full volume. Raising the volume
        after the skip instead loses the start of the song: each player call takes a moment.
        Playback is resumed whether play() works or not.
        """
        async with self._lock:
            faded = False
            try:
                current = await self._get()
                if current is not None:
                    restore = self.normal if self.normal is not None else current
                    await self._fade(current, 0, FADE_OUT_SECONDS)
                    faded = True
                    await self._pause()
                    await self._set(restore)
                    self.normal = None
            except Exception as e:
                log.warning("Couldn't fade out: %s", e)
            try:
                return await play()
            finally:
                self._lower_after = time.time() + SETTLE_SECONDS
                if faded:
                    try:
                        await self._resume()
                    except Exception as e:
                        log.warning("Couldn't resume playback: %s", e)

    async def _set(self, volume: int) -> None:
        await self._set_device(volume)
        self.current = self._expected = volume
        self._quiet_until = time.time() + QUIET_SECONDS

    async def _fade(self, start: int, end: int, seconds: float) -> None:
        steps = max(1, min(abs(end - start), round(seconds / STEP_SECONDS)))
        began = time.monotonic()
        for i in range(1, steps + 1):
            await self._set(round(start + (end - start) * i / steps))
            # Sleep until this step's slot, so slow calls don't stretch the fade.
            await asyncio.sleep(max(0.0, began + seconds * i / steps - time.monotonic()))
