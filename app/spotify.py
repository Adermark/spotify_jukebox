import asyncio
import base64
import time
from urllib.parse import urlencode

import httpx

from . import db
from .config import config

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API_URL = "https://api.spotify.com/v1"
SCOPES = "user-read-playback-state user-modify-playback-state user-read-currently-playing"


class SpotifyError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class NotConnected(SpotifyError):
    def __init__(self):
        super().__init__(503, "Spotify is not connected yet. The host needs to log in on the admin page.")


def simplify_track(t: dict) -> dict:
    images = t.get("album", {}).get("images") or []
    return {
        "uri": t["uri"],
        "title": t["name"],
        "artists": ", ".join(a["name"] for a in t.get("artists", [])),
        # Images come largest first; pick a small one for lists.
        "album_art": images[-2]["url"] if len(images) > 1 else (images[0]["url"] if images else None),
        "duration_ms": t["duration_ms"],
        "explicit": bool(t.get("explicit")),
    }


class Spotify:
    def __init__(self):
        self.http = httpx.AsyncClient(timeout=10)
        self._refresh_lock = asyncio.Lock()

    async def close(self):
        await self.http.aclose()

    # --- OAuth ------------------------------------------------------------------

    def auth_url(self, state: str) -> str:
        return AUTH_URL + "?" + urlencode({
            "client_id": config.client_id,
            "response_type": "code",
            "redirect_uri": config.redirect_uri,
            "scope": SCOPES,
            "state": state,
        })

    def _basic_auth(self) -> dict:
        raw = f"{config.client_id}:{config.client_secret}".encode()
        return {"Authorization": "Basic " + base64.b64encode(raw).decode()}

    async def check_app_credentials(self, client_id: str, client_secret: str) -> str | None:
        """Ask Spotify whether a client id/secret pair is valid. Returns an error message or None."""
        raw = f"{client_id}:{client_secret}".encode()
        try:
            r = await self.http.post(
                TOKEN_URL,
                headers={"Authorization": "Basic " + base64.b64encode(raw).decode()},
                data={"grant_type": "client_credentials"},
            )
        except httpx.HTTPError as e:
            return f"Couldn't reach Spotify to check them: {e}"
        if r.status_code == 200:
            return None
        if r.status_code in (400, 401):
            return "Spotify doesn't accept that Client ID and Client secret. Check that both were copied completely."
        return f"Spotify answered {r.status_code}: {r.text[:200]}"

    async def exchange_code(self, code: str) -> None:
        r = await self.http.post(TOKEN_URL, headers=self._basic_auth(), data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": config.redirect_uri,
        })
        if r.status_code != 200:
            raise SpotifyError(r.status_code, f"Token exchange failed: {r.text}")
        data = r.json()
        db.save_auth(data["access_token"], data["refresh_token"], time.time() + data["expires_in"])

    async def _refresh(self, auth: dict) -> str:
        r = await self.http.post(TOKEN_URL, headers=self._basic_auth(), data={
            "grant_type": "refresh_token",
            "refresh_token": auth["refresh_token"],
        })
        if r.status_code != 200:
            if r.status_code in (400, 401):
                db.clear_auth()  # refresh token revoked; host must log in again
            raise SpotifyError(r.status_code, f"Token refresh failed: {r.text}")
        data = r.json()
        refresh_token = data.get("refresh_token") or auth["refresh_token"]
        db.save_auth(data["access_token"], refresh_token, time.time() + data["expires_in"])
        return data["access_token"]

    async def _access_token(self, force_refresh: bool = False) -> str:
        auth = db.get_auth()
        if auth is None:
            raise NotConnected()
        if not force_refresh and auth["expires_at"] - 60 > time.time():
            return auth["access_token"]
        async with self._refresh_lock:
            auth = db.get_auth()
            if auth is None:
                raise NotConnected()
            if not force_refresh and auth["expires_at"] - 60 > time.time():
                return auth["access_token"]  # another task refreshed while we waited
            return await self._refresh(auth)

    def is_connected(self) -> bool:
        return db.get_auth() is not None

    # --- API ----------------------------------------------------------------------

    async def _request(self, method: str, path: str, **kwargs):
        token = await self._access_token()
        for attempt in range(2):
            r = await self.http.request(
                method, API_URL + path, headers={"Authorization": f"Bearer {token}"}, **kwargs
            )
            if r.status_code == 401 and attempt == 0:
                token = await self._access_token(force_refresh=True)
                continue
            break
        if r.status_code == 429:
            raise SpotifyError(429, f"Rate limited by Spotify, retry after {r.headers.get('Retry-After', '?')}s")
        if r.status_code >= 400:
            try:
                message = r.json()["error"]["message"]
            except Exception:
                message = r.text
            raise SpotifyError(r.status_code, message)
        if r.status_code == 204 or not r.content:
            return None
        try:
            return r.json()
        except ValueError:
            return None  # some player endpoints return non-JSON bodies

    async def search(self, query: str, limit: int = 10) -> list[dict]:
        # No market=from_token: that requires the user-read-private scope (403 otherwise).
        data = await self._request("GET", "/search", params={"q": query, "type": "track", "limit": limit})
        return [simplify_track(t) for t in data["tracks"]["items"] if t]

    async def track(self, track_id: str) -> dict:
        return simplify_track(await self._request("GET", f"/tracks/{track_id}"))

    async def playback(self) -> dict | None:
        return await self._request("GET", "/me/player")

    async def add_to_queue(self, uri: str) -> None:
        await self._request("POST", "/me/player/queue", params={"uri": uri})

    async def devices(self) -> list[dict]:
        return (await self._request("GET", "/me/player/devices"))["devices"]

    async def queue(self) -> tuple[str | None, list[str]]:
        """(currently playing uri, upcoming uris). Upcoming = manually queued songs, then the
        rest of the playlist/album."""
        data = await self._request("GET", "/me/player/queue") or {}
        current = (data.get("currently_playing") or {}).get("uri")
        return current, [t.get("uri") for t in data.get("queue") or []]

    async def play_now(self, uri: str, device_id: str | None = None, position_ms: int = 0) -> None:
        if not device_id:
            raise SpotifyError(404, "No Spotify device found. Open Spotify on the host device first.")
        # Start the track inside its album rather than with {"uris": [uri]}: the Windows desktop
        # client accepts a bare-uris play (204) but stays silent. If our queue then runs dry,
        # Spotify simply continues the album.
        track = await self._request("GET", f"/tracks/{uri.rsplit(':', 1)[-1]}")
        await self._request("PUT", "/me/player/play", params={"device_id": device_id}, json={
            "context_uri": track["album"]["uri"],
            "offset": {"uri": uri},
            "position_ms": max(0, position_ms),
        })

    async def pause(self) -> None:
        await self._request("PUT", "/me/player/pause")

    async def skip(self) -> None:
        await self._request("POST", "/me/player/next")


spotify = Spotify()
