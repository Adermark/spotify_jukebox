"""Direct control of Sonos speakers on the local network (via SoCo).

Spotify's Web API refuses to control Sonos ("restricted device"), so in Sonos mode we
talk to the speakers ourselves: Spotify tracks are put in the Sonos queue as share links
(this needs Spotify linked in the Sonos app), and playback is controlled over UPnP.

SoCo is blocking, so every call runs in a worker thread.
"""
import asyncio
import re
import threading
import time
from dataclasses import dataclass
from urllib.parse import unquote

import soco
import soco.config
from soco.plugins.sharelink import ShareLinkPlugin

from . import db

# SoCo waits 20 s by default; a hung speaker would then stall every other call.
soco.config.REQUEST_TIMEOUT = 5

SPOTIFY_TRACK = re.compile(r"spotify:track:([A-Za-z0-9]{22})")
DISCOVERY_TTL = 300   # seconds before speakers are looked up again
REDISCOVER_MIN = 30   # at most one forced re-discovery per this many seconds
NOT_RESPONDING = ("{names} isn't responding. Unplug it for 10 seconds and plug it back in, "
                  "or untick it under Settings.")


class SonosError(Exception):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


@dataclass
class SonosStatus:
    state: str              # PLAYING, PAUSED_PLAYBACK, STOPPED, TRANSITIONING
    from_queue: bool        # playing from the Sonos queue (not TV, line-in, Spotify Connect...)
    queue_position: int     # 1-based position in the Sonos queue, 0 if none
    uri: str | None         # spotify:track:... if the current track is a Spotify track
    title: str | None
    artist: str | None
    album_art: str | None
    position_ms: int
    duration_ms: int
    room: str


def spotify_uri(sonos_uri: str | None) -> str | None:
    """'x-sonos-spotify:spotify%3atrack%3aID?sid=..' -> 'spotify:track:ID'."""
    m = SPOTIFY_TRACK.search(unquote(sonos_uri or ""))
    return f"spotify:track:{m.group(1)}" if m else None


def _ms(hms: str | None) -> int:
    try:
        h, m, s = (int(x) for x in (hms or "").split(":"))
        return (h * 3600 + m * 60 + s) * 1000
    except ValueError:
        return 0  # "NOT_IMPLEMENTED" or empty


class SonosController:
    def __init__(self):
        self._zones: dict[str, soco.SoCo] = {}
        self._names: dict[str, str] = {}   # uid -> room name, remembered for error messages
        self._models: dict[str, str] = {}
        self._discovered_at = 0.0
        self._lock = threading.Lock()  # one UPnP conversation at a time

    async def _run(self, fn, *args):
        def call():
            with self._lock:
                return fn(*args)
        try:
            return await asyncio.to_thread(call)
        except SonosError:
            raise
        except Exception as e:
            raise SonosError(f"Sonos: {e}") from e

    # --- speakers -------------------------------------------------------------------

    def _discover(self, force: bool = False) -> dict[str, soco.SoCo]:
        age = time.time() - self._discovered_at
        if (force and age > REDISCOVER_MIN) or not self._zones or age > DISCOVERY_TTL:
            found = soco.discover(timeout=3) or set()
            self._zones = {z.uid: z for z in found}
            self._discovered_at = time.time()
            for z in found:
                try:
                    self._names[z.uid] = z.player_name
                except Exception:
                    pass
        return self._zones

    def _selected(self) -> list[soco.SoCo]:
        """Rooms ticked in settings, in settings order. The first one leads the group."""
        uids = [u for u in db.get_settings()["sonos_rooms"].split(",") if u]
        if not uids:
            return []
        zones = self._discover()
        if any(u not in zones for u in uids):
            zones = self._discover(force=True)
        missing = [self._names.get(u, "A Sonos room") for u in uids if u not in zones]
        if missing:
            raise SonosError(NOT_RESPONDING.format(names=" and ".join(missing)), 503)
        return [zones[u] for u in uids]

    def _coordinator(self) -> soco.SoCo | None:
        rooms = self._selected()
        return rooms[0].group.coordinator if rooms else None

    async def rooms(self) -> list[dict]:
        def list_rooms():
            selected = [u for u in db.get_settings()["sonos_rooms"].split(",") if u]
            out = []
            zones = self._discover(force=True)
            for z in zones.values():
                room = {"uid": z.uid, "name": self._names.get(z.uid, z.ip_address),
                        "selected": z.uid in selected, "responding": True, "grouped_with": []}
                try:
                    if z.uid not in self._models:
                        self._models[z.uid] = z.get_speaker_info().get("model_name")
                    group = z.group
                    room["grouped_with"] = sorted({m.player_name for m in group.members
                                                   if m.is_visible and m.uid != z.uid}) if group else []
                except Exception:
                    room["responding"] = False
                room["model"] = self._models.get(z.uid)
                out.append(room)
            # Ticked rooms that didn't answer discovery at all still need to be listed.
            for uid in selected:
                if uid not in zones:
                    out.append({"uid": uid, "name": self._names.get(uid, "Unknown room"), "model": self._models.get(uid),
                                "selected": True, "responding": False, "grouped_with": []})
            return sorted(out, key=lambda r: r["name"])
        return await self._run(list_rooms)

    async def ensure_group(self) -> None:
        """Make the ticked rooms one group led by the first, with nothing else in it."""
        def group():
            rooms = self._selected()
            if not rooms:
                raise SonosError("Pick the Sonos room(s) to play on under Settings.", 409)
            leader, wanted = rooms[0], {r.uid for r in rooms}
            if leader.group.coordinator.uid != leader.uid:
                leader.unjoin()
            for member in leader.group.members:
                if member.is_visible and member.uid not in wanted:
                    member.unjoin()
            for room in rooms[1:]:
                if room.group.coordinator.uid != leader.uid:
                    room.join(leader)
        await self._run(group)

    # --- playback -----------------------------------------------------------------------

    async def status(self) -> SonosStatus | None:
        def read():
            k = self._coordinator()
            if k is None:
                return None
            state = k.get_current_transport_info()["current_transport_state"]
            track = k.get_current_track_info()
            media = k.get_current_media_info()
            return SonosStatus(
                state=state,
                from_queue=(media.get("uri") or "").startswith("x-rincon-queue:"),
                queue_position=int(track.get("playlist_position") or 0),
                uri=spotify_uri(track.get("uri")),
                title=track.get("title") or None,
                artist=track.get("artist") or None,
                album_art=track.get("album_art") or None,
                position_ms=_ms(track.get("position")),
                duration_ms=_ms(track.get("duration")),
                room=k.player_name,
            )
        return await self._run(read)

    async def upcoming(self, queue_position: int) -> list[tuple[int, str | None]]:
        """(0-based index, spotify uri) for the Sonos queue items after queue_position."""
        def read():
            k = self._coordinator()
            if k is None:
                return []
            items = k.get_queue(start=queue_position, max_items=200)
            return [(queue_position + i, spotify_uri(it.resources[0].uri if it.resources else None))
                    for i, it in enumerate(items)]
        return await self._run(read)

    async def add(self, track_uri: str) -> int:
        """Append a Spotify track to the Sonos queue; returns its 0-based index."""
        track_id = track_uri.rsplit(":", 1)[-1]
        def add():
            k = self._coordinator()
            if k is None:
                raise SonosError("Pick the Sonos room(s) to play on under Settings.", 409)
            position = ShareLinkPlugin(k).add_share_link_to_queue(f"https://open.spotify.com/track/{track_id}")
            return position - 1
        return await self._run(add)

    async def play_index(self, index: int) -> None:
        await self._run(lambda: self._coordinator().play_from_queue(index))

    async def take_over(self, track_uri: str) -> None:
        """Switch the speakers to a fresh queue (from TV, Spotify Connect, ...) and play track_uri."""
        def clear():
            k = self._coordinator()
            if k is None:
                raise SonosError("Pick the Sonos room(s) to play on under Settings.", 409)
            k.clear_queue()
            k.play_mode = "NORMAL"  # no repeat/shuffle: the queue must play in our order, once
        await self._run(clear)
        await self.play_index(await self.add(track_uri))

    async def next(self) -> None:
        await self._run(lambda: self._coordinator().next())

    async def pause(self) -> None:
        def pause():
            k = self._coordinator()
            if k and k.get_current_transport_info()["current_transport_state"] == "PLAYING":
                k.pause()
        await self._run(pause)

    async def volume(self) -> int | None:
        """Volume of the whole group (0-100)."""
        def read():
            k = self._coordinator()
            return k.group.volume if k else None
        return await self._run(read)

    async def set_volume(self, volume: int) -> None:
        """Set the group's volume; Sonos keeps the rooms' levels relative to each other."""
        def write():
            k = self._coordinator()
            if k:
                k.group.volume = volume
        await self._run(write)

    async def resume(self) -> None:
        def resume():
            k = self._coordinator()
            if k and k.get_current_transport_info()["current_transport_state"] != "PLAYING":
                k.play()
        await self._run(resume)

    async def seek(self, position_ms: int) -> None:
        s = position_ms // 1000
        await self._run(lambda: self._coordinator().seek(f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}"))
