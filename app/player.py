"""Spotify Connect playback: watches Spotify and feeds it our queue one song at a time."""
import asyncio
import logging
import time

from . import db
from .queue_logic import order_queue
from .spotify import SpotifyError, spotify

log = logging.getLogger("jukebox.player")

# Skips and track changes briefly report "not playing at 0:00"; only treat Spotify as
# stopped once it has looked that way for this long.
STOPPED_GRACE_SECONDS = 6
# Most songs we'll skip past to reach ours (stale entries ahead of it in Spotify's queue).
MAX_SKIPS = 10


def upcoming() -> list[dict]:
    """Full upcoming queue: songs already sent to Spotify (locked), then our ordered queue."""
    s = db.get_settings()
    sent = db.items_with_status("sent")
    queued = db.items_with_status("queued")
    if sent:
        last_user = sent[-1]["user_id"]
    else:
        playing = db.items_with_status("playing")
        last_user = playing[-1]["user_id"] if playing else None
    ordered = order_queue(
        queued,
        fair_rotation=s["fair_rotation"],
        allow_consecutive=s["allow_consecutive"],
        last_user_id=last_user,
    )
    return sent + ordered


def next_queued() -> dict | None:
    return next((i for i in upcoming() if i["status"] == "queued"), None)


def restricted_message(pb: dict | None) -> str | None:
    """Spotify refuses all remote control of "restricted" devices (some speakers/TVs)."""
    device = (pb or {}).get("device") or {}
    if not device.get("is_restricted"):
        return None
    return (f"Spotify is playing on \"{device.get('name', 'a device')}\", which Spotify doesn't let apps "
            "control, so the jukebox can't queue or skip there. Play on your computer or phone instead "
            "(you can connect that to the speaker by cable or Bluetooth).")


class Player:
    def __init__(self):
        self.now_playing: dict | None = None
        self.error: str | None = None
        self.warning: str | None = None  # sticky until something plays (error resets every tick)
        self._last_uri: str | None = None
        self._last_progress = 0
        self._pushed_for_current = False
        self._stopped_since: float | None = None
        self._autostart_after = 0.0  # don't auto-start before this time (gives Spotify time to react)
        self._lock = asyncio.Lock()  # serialises Spotify queue changes from the loop and admin actions

    async def tick(self) -> None:
        if not spotify.is_connected():
            self.now_playing = None
            return

        pb = await spotify.playback()
        item = pb.get("item") if pb else None
        progress = (pb or {}).get("progress_ms") or 0
        now = time.time()

        # Stopped = a device is there but nothing is loaded, or a finished track sits at 0:00.
        # A pause mid-song is left alone.
        looks_stopped = bool(pb and pb.get("device")) and (not item or (not pb.get("is_playing") and progress == 0))
        self._stopped_since = (self._stopped_since or now) if looks_stopped else None
        if self._stopped_since and now - self._stopped_since >= STOPPED_GRACE_SECONDS:
            await self._requeue_unplayed(spotify_stopped=True)
            if db.get_settings()["auto_play"] and now >= self._autostart_after:
                if await self.play_next_now() is not None:
                    return
        if pb and pb.get("is_playing") and not restricted_message(pb):
            self.warning = None

        if not item or item.get("type") != "track":
            if self._last_uri is not None:
                db.mark_now_playing(None)
                self._last_uri = None
            self.now_playing = None
            return

        uri = item["uri"]
        duration = item["duration_ms"]

        # New track started (or the same track restarted from the beginning).
        if uri != self._last_uri or progress + 5000 < self._last_progress:
            db.mark_now_playing(uri)
            self._pushed_for_current = False
            await self._requeue_unplayed(spotify_stopped=False)
        self._last_uri = uri
        self._last_progress = progress

        playing_rows = db.items_with_status("playing")
        ours = playing_rows[-1] if playing_rows and playing_rows[-1]["track_uri"] == uri else None
        images = item.get("album", {}).get("images") or []
        self.now_playing = {
            "uri": uri,
            "title": item["name"],
            "artists": ", ".join(a["name"] for a in item.get("artists", [])),
            "album_art": images[0]["url"] if images else None,
            "duration_ms": duration,
            "progress_ms": progress,
            "is_playing": bool(pb.get("is_playing")),
            "added_by": ours["added_by"] if ours else None,
            "fetched_at": now,
            "device": (pb.get("device") or {}).get("name"),
        }

        if restricted_message(pb):
            self.warning = restricted_message(pb)
            return  # Spotify won't take queue commands for this device

        # Hand the next song to Spotify shortly before this one ends.
        # If our queue is empty we keep checking on each tick until the track ends.
        push_ms = db.get_settings()["push_seconds_before_end"] * 1000
        if pb.get("is_playing") and not self._pushed_for_current and duration - progress <= push_ms:
            async with self._lock:
                if db.items_with_status("sent"):
                    self._pushed_for_current = True  # already lined up (e.g. by a skip)
                    return
                nxt = next_queued()
                if nxt:
                    await self._send(nxt)
                    self._pushed_for_current = True

    # --- moving to our next song ---------------------------------------------------------

    async def play_next_now(self) -> dict | None:
        """Jump to the next song in our queue right away. Returns it, or None if the queue is empty."""
        async with self._lock:
            target = next(iter(upcoming()), None)
            if target is None:
                return None
            # Whether it works or fails, wait a bit before auto-starting again.
            self._autostart_after = time.time() + 10
            pb = await spotify.playback()
            if restricted_message(pb):
                raise SpotifyError(409, restricted_message(pb))
            disallows = ((pb or {}).get("actions") or {}).get("disallows") or {}
            if pb and pb.get("item") and not disallows.get("skipping_next"):
                # Something is loaded: line our song up and skip to it. This keeps Spotify's
                # queue intact, so nothing we sent earlier is left behind to replay later.
                await self._skip_to(target)
            else:
                await spotify.play_now(target["track_uri"], await self._device_id())
                if target["status"] == "queued":
                    db.mark_sent(target["id"])
            log.info("Jumped to: %s (added by %s)", target["title"], target["added_by"])
            return target

    async def snapshot(self) -> tuple[str, int] | None:
        """(track uri, position) if Spotify is playing a track on a device we can control."""
        pb = await spotify.playback()
        item = (pb or {}).get("item") or {}
        if pb and pb.get("is_playing") and item.get("type") == "track":
            return item["uri"], pb.get("progress_ms") or 0
        return None

    async def stop(self) -> None:
        pb = await spotify.playback()
        if pb and pb.get("is_playing") and not restricted_message(pb):
            await spotify.pause()

    async def resume(self, track_uri: str, position_ms: int) -> None:
        """Continue a song that was playing elsewhere (switching from Sonos to Spotify)."""
        async with self._lock:
            self._autostart_after = time.time() + 10
            await spotify.play_now(track_uri, await self._device_id(), position_ms)

    async def skip(self) -> None:
        """Admin action: skip to the next song in our queue, or Spotify's next if ours is empty."""
        if restricted_message(pb := await spotify.playback()):
            raise SpotifyError(409, restricted_message(pb))
        if await self.play_next_now() is not None:
            return
        try:
            await spotify.skip()
        except SpotifyError as e:
            if e.status == 403:
                raise SpotifyError(409, "Nothing to skip to: the queue is empty")
            raise

    async def _send(self, item: dict) -> None:
        """Add an item to Spotify's queue, unless it's already waiting there."""
        _, spotify_upcoming = await spotify.queue()
        if item["track_uri"] not in spotify_upcoming:
            await spotify.add_to_queue(item["track_uri"])
        db.mark_sent(item["id"])
        log.info("Queued on Spotify: %s (added by %s)", item["title"], item["added_by"])

    async def _skip_to(self, target: dict) -> None:
        """Make sure target is in Spotify's queue, then skip forward until it plays."""
        if target["status"] == "queued":
            await self._send(target)
        _, spotify_upcoming = await spotify.queue()
        uri = target["track_uri"]
        skips = spotify_upcoming.index(uri) + 1 if uri in spotify_upcoming else 1
        for _ in range(min(skips, MAX_SKIPS)):
            await spotify.skip()
        self._pushed_for_current = True

    # --- helpers -------------------------------------------------------------------------

    async def _device_id(self) -> str | None:
        """Device to play on: the one picked in settings if it's online, else Spotify's active one.

        Naming the device matters: Spotify can report a stale device as "active", and a play
        command without a device then returns 204 but plays nothing.
        """
        devices = await spotify.devices()
        preferred = db.get_settings()["device_id"]
        if preferred and any(d["id"] == preferred for d in devices):
            return preferred
        active = next((d for d in devices if d["is_active"]), None)
        return (active or (devices[0] if devices else {})).get("id")

    async def _requeue_unplayed(self, spotify_stopped: bool) -> None:
        """Songs marked as sent that aren't in Spotify's queue go back into our queue.

        That happens when a play command was accepted but nothing played, or when
        Spotify's queue was replaced (e.g. the host started something else).
        """
        stale = [i for i in db.items_with_status("sent") if time.time() - i["sent_at"] >= 15]
        if not stale:
            return
        current, spotify_upcoming = await spotify.queue()
        present = set(spotify_upcoming) | {current}
        lost = [i["id"] for i in stale if i["track_uri"] not in present]
        if not lost:
            return
        db.requeue(lost)
        log.warning("Requeued %d song(s) that weren't in Spotify's queue", len(lost))
        if spotify_stopped:
            self.warning = ("Spotify accepted the play command but nothing started playing. "
                            "Pick the playback device under Settings.")
            self._autostart_after = time.time() + 60
