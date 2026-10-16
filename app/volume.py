"""Volume for auto-mode: fallback music plays quieter, and fades out when a song interrupts it.

Each player (Spotify, Sonos) gives us a way to read and set its volume. While fallback music
plays we remember the volume jukebox songs played at ("normal"), so it can be put back when
the next jukebox song starts. It's kept in the database so a restart doesn't forget it.
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


class AutoModeVolume:
    def __init__(self, name: str, get_volume: Callable[[], Awaitable[int | None]],
                 set_volume: Callable[[int], Awaitable[None]],
                 pause: Callable[[], Awaitable[None]], resume: Callable[[], Awaitable[None]]):
        """get_volume returns None when the device's volume can't be controlled."""
        self._key = f"normal_volume_{name}"
        self._get = get_volume
        self._set = set_volume
        self._pause = pause
        self._resume = resume
        self._lock = asyncio.Lock()
        self._lower_after = 0.0

    @property
    def normal(self) -> int | None:
        return db.get_state(self._key)

    @normal.setter
    def normal(self, volume: int | None) -> None:
        db.set_state(self._key, volume)

    async def update(self, playing: str | None) -> None:
        """Called on every player tick. playing is "ours" (a jukebox song), "fallback"
        (other music), or None (paused, stopped or unknown: leave the volume alone)."""
        if self._lock.locked():
            return  # a fade is running
        try:
            async with self._lock:
                if playing == "ours" and self.normal is not None:
                    await self._set(self.normal)
                    log.info("Volume back to %s for jukebox songs", self.normal)
                    self.normal = None
                elif playing == "fallback" and self.normal is None and time.time() >= self._lower_after:
                    level = db.get_settings()["auto_mode_volume"]
                    if level >= 100:
                        return
                    current = await self._get()
                    if current is None:
                        return
                    self.normal = current
                    target = round(current * level / 100)
                    await self._fade(current, target, FADE_DOWN_SECONDS)
                    log.info("Fallback music: volume %s -> %s", current, target)
        except Exception as e:
            log.warning("Couldn't change the volume: %s", e)

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

    async def _fade(self, start: int, end: int, seconds: float) -> None:
        steps = max(1, min(abs(end - start), round(seconds / STEP_SECONDS)))
        began = time.monotonic()
        for i in range(1, steps + 1):
            await self._set(round(start + (end - start) * i / steps))
            # Sleep until this step's slot, so slow calls don't stretch the fade.
            await asyncio.sleep(max(0.0, began + seconds * i / steps - time.monotonic()))
