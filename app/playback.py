"""Picks the playback backend (Spotify Connect or Sonos) from settings and runs its loop."""
import asyncio
import logging

from . import db
from .config import config
from .player import Player
from .sonos import SonosController, SonosError
from .sonos_player import SonosPlayer
from .spotify import SpotifyError

log = logging.getLogger("jukebox.playback")


class Playback:
    def __init__(self):
        self.spotify = Player()
        self.sonos_controller = SonosController()
        self.sonos = SonosPlayer(self.sonos_controller)

    @property
    def active(self):
        return self.sonos if db.get_settings()["playback_mode"] == "sonos" else self.spotify

    @property
    def now_playing(self):
        return self.active.now_playing

    @property
    def error(self):
        return self.active.error or self.active.warning

    async def run(self) -> None:
        while True:
            backend = self.active
            try:
                await backend.tick()
                backend.error = None
            except (SpotifyError, SonosError) as e:
                backend.error = str(e)
                if getattr(e, "status", None) != 503:
                    log.warning("%s", e)
            except Exception as e:  # keep the loop alive no matter what
                backend.error = str(e)
                log.exception("Playback tick failed")
            await asyncio.sleep(config.poll_interval)

    async def switch_mode(self, mode: str) -> None:
        """Switch between Spotify and Sonos, taking the current song along at the same position."""
        old = db.get_settings()["playback_mode"]
        if mode == old:
            return
        source = self.active
        try:
            snapshot = await source.snapshot()
        except Exception as e:
            log.warning("Couldn't read what's playing before switching: %s", e)
            snapshot = None
        try:
            await source.stop()
        except Exception as e:
            log.warning("Couldn't stop the old player: %s", e)

        # Songs handed to the old player go back into our queue; the current one is handed
        # over so the new player still knows who added it.
        db.requeue([i["id"] for i in db.items_with_status("sent")])
        for item in db.items_with_status("playing"):
            if snapshot and item["track_uri"] == snapshot[0]:
                db.mark_sent(item["id"])
        db.update_settings({"playback_mode": mode})

        if snapshot:
            await self.active.resume(*snapshot)
            log.info("Moved playback to %s at %ss", mode, snapshot[1] // 1000)

    async def play_next_now(self):
        return await self.active.play_next_now()

    async def skip(self):
        await self.active.skip()


playback = Playback()
