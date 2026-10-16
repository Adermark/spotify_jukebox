"""Playback loop for Sonos mode: feeds our queue into the Sonos queue one song at a time.

Same idea as the Spotify player: the real queue lives in our database, and the next song
is handed to the speakers shortly before the current one ends.
"""
import asyncio
import logging
import time

from . import db
from .player import STOPPED_GRACE_SECONDS, next_queued, upcoming
from .sonos import SonosController, SonosError
from .spotify import spotify
from .volume import AutoModeVolume

log = logging.getLogger("jukebox.sonos")


class SonosPlayer:
    def __init__(self, controller: SonosController):
        self.c = controller
        self.now_playing: dict | None = None
        self.error: str | None = None
        self.warning: str | None = None
        self._last_key: tuple | None = None
        self._pushed_for_current = False
        self._stopped_since: float | None = None
        self._autostart_after = 0.0
        self._hold_until = 0.0  # ignore the speakers' state until then (regrouping in progress)
        self._lock = asyncio.Lock()
        self.volume = AutoModeVolume("sonos", lambda: self.c.volume(), lambda v: self.c.set_volume(v),
                                     lambda: self.c.pause(), lambda: self.c.resume())

    def hold(self, seconds: float = 8) -> None:
        """Regrouping makes Sonos report in-between states (and can move group leadership);
        don't react to them."""
        self._hold_until = time.time() + seconds
        self._last_key = None

    def is_playing_ours(self) -> bool:
        """True if a jukebox-queued song is currently loaded or playing."""
        np = self.now_playing
        if not np or not np.get("added_by"):
            return False
        elapsed = (time.time() - np.get("fetched_at", 0)) * 1000
        if np.get("duration_ms") and (np.get("progress_ms", 0) + elapsed >= np["duration_ms"]):
            return False
        return True

    def is_in_auto_mode(self) -> bool:
        """True if playback is active but playing non-jukebox music (auto-mode fallback / radio)."""
        np = self.now_playing
        if not np or not np.get("is_playing"):
            return False
        return not bool(np.get("added_by"))

    async def tick(self) -> None:
        if time.time() < self._hold_until:
            return
        st = await self.c.status()
        if st is None:
            self.now_playing = None
            self.warning = "Pick the Sonos room(s) to play on under Settings."
            return
        self.warning = None
        now = time.time()
        if st.state == "TRANSITIONING":
            return  # loading the next track; read again next tick

        # Stopped = the queue ran out (or nothing was started). Paused, TV, line-in or
        # Spotify Connect playing on the speakers are left alone.
        self._stopped_since = (self._stopped_since or now) if st.state == "STOPPED" else None
        if self._stopped_since and now - self._stopped_since >= STOPPED_GRACE_SECONDS:
            await self._requeue_lost(st)
            if db.get_settings()["auto_play"] and now >= self._autostart_after:
                if await self.play_next_now() is not None:
                    return

        key = (st.queue_position, st.uri, st.from_queue)
        if key != self._last_key:
            db.mark_now_playing(st.uri if st.from_queue else None)
            self._pushed_for_current = False
            self._last_key = key
            if st.from_queue:
                await self._requeue_lost(st)

        if not st.title or st.state == "STOPPED":
            self.now_playing = None
        else:
            playing = db.items_with_status("playing")
            ours = playing[-1] if playing and playing[-1]["track_uri"] == st.uri else None
            self.now_playing = {
                "uri": st.uri,
                "title": ours["title"] if ours else st.title,
                "artists": ours["artists"] if ours else (st.artist or ""),
                "album_art": (ours and ours["album_art"]) or st.album_art,
                "duration_ms": st.duration_ms or (ours["duration_ms"] if ours else 0),
                "progress_ms": st.position_ms,
                "is_playing": st.state in ("PLAYING", "TRANSITIONING"),
                "added_by": ours["added_by"] if ours else None,
                "fetched_at": now,
                "device": st.room,
            }

        # Only Spotify tracks count as fallback music: TV sound and line-in keep their volume.
        if st.state == "PLAYING":
            playing = ("ours" if self.is_playing_ours() else "fallback") if st.uri else None
            try:
                current = await self.c.volume()
            except SonosError as e:
                log.warning("Couldn't read the volume: %s", e)
                current = None
            await self.volume.update(playing, current)

        # Hand the next song to the speakers shortly before this one ends.
        push_ms = db.get_settings()["push_seconds_before_end"] * 1000
        if (st.from_queue and st.state == "PLAYING" and not self._pushed_for_current
                and st.duration_ms and st.duration_ms - st.position_ms <= push_ms):
            async with self._lock:
                if db.items_with_status("sent"):
                    self._pushed_for_current = True  # already lined up
                    return
                nxt = next_queued()
                if nxt:
                    await self.c.add(nxt["track_uri"])
                    db.mark_sent(nxt["id"])
                    self._pushed_for_current = True
                    log.info("Queued on Sonos: %s (added by %s)", nxt["title"], nxt["added_by"])

    async def play_next_now(self) -> dict | None:
        """Jump to the next song in our queue right away. Returns it, or None if the queue is empty."""
        async with self._lock:
            target = next(iter(upcoming()), None)
            if target is None:
                return None
            self._autostart_after = time.time() + 10
            await self.c.ensure_group()
            st = await self.c.status()
            if st is not None and st.from_queue and st.state != "STOPPED":
                # Already playing our queue: jump to the song if it's lined up, else append it.
                after = await self.c.upcoming(st.queue_position)
                index = next((i for i, uri in after if uri == target["track_uri"]), None)
                if index is None:
                    index = await self.c.add(target["track_uri"])
                await self.c.play_index(index)
            else:
                # Stopped, TV, Spotify Connect...: start a fresh queue (clears leftovers and
                # resets repeat/shuffle).
                await self._pause_spotify_elsewhere()
                await self.c.take_over(target["track_uri"])
            if target["status"] == "queued":
                db.mark_sent(target["id"])
            self._pushed_for_current = True
            log.info("Playing on Sonos: %s (added by %s)", target["title"], target["added_by"])
            return target

    async def apply_rooms(self, uids: list[str]) -> None:
        """Save the ticked rooms, group them, and start the jukebox there if it wasn't playing.

        Whether to start is decided from the state *before* regrouping: right after a join the
        speakers report in-between states, and acting on those skipped songs or split the group.
        """
        async with self._lock:
            try:
                before = await self.c.status()
            except SonosError:
                before = None
            db.update_settings({"sonos_rooms": ",".join(uids)})
            if not uids:
                return
            if before is None:
                # No rooms were picked before, so nothing is being regrouped yet: the new
                # room's own state is reliable (it may already be playing the jukebox).
                before = await self.c.status()
            was_playing = bool(before and before.from_queue and before.state in ("PLAYING", "TRANSITIONING"))
            await self.c.ensure_group()
            self.hold()
        if not was_playing:
            await self.play_next_now()

    async def resume(self, track_uri: str, position_ms: int) -> None:
        """Continue a song that was playing elsewhere (switching from Spotify to Sonos)."""
        async with self._lock:
            await self.c.ensure_group()
            await self.c.take_over(track_uri)
            if position_ms > 5000:
                await self.c.seek(position_ms)
            self._autostart_after = time.time() + 10
            self.hold(4)

    async def interrupt_auto_mode(self, play) -> dict | None:
        """Fade the fallback music out and run play() to start the jukebox queue."""
        return await self.volume.interrupt(play)

    async def snapshot(self) -> tuple[str, int] | None:
        """(track uri, position) if the speakers are playing a Spotify track."""
        st = await self.c.status()
        if st and st.state == "PLAYING" and st.uri:
            return st.uri, st.position_ms
        return None

    async def stop(self) -> None:
        await self.c.pause()

    async def _pause_spotify_elsewhere(self) -> None:
        """When the speakers take over, stop Spotify on the computer/phone so only one plays."""
        try:
            if not spotify.is_connected():
                return
            pb = await spotify.playback()
            if pb and pb.get("is_playing") and not (pb.get("device") or {}).get("is_restricted"):
                await spotify.pause()
                log.info("Paused Spotify on %s", pb["device"].get("name"))
        except Exception as e:  # never block the Sonos takeover on this
            log.warning("Couldn't pause Spotify: %s", e)

    async def skip(self) -> None:
        if await self.play_next_now() is not None:
            return
        try:
            await self.c.next()
        except SonosError:
            raise SonosError("Nothing to skip to: the queue is empty", 409)

    async def _requeue_lost(self, st) -> None:
        """Songs marked as sent that aren't waiting in the Sonos queue go back into ours."""
        stale = [i for i in db.items_with_status("sent") if time.time() - i["sent_at"] >= 15]
        if not stale:
            return
        waiting = {uri for _, uri in await self.c.upcoming(st.queue_position)} if st.from_queue else set()
        lost = [i["id"] for i in stale if i["track_uri"] not in waiting]
        if lost:
            db.requeue(lost)
            log.warning("Requeued %d song(s) that weren't in the Sonos queue", len(lost))
