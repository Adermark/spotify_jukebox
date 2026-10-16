import asyncio
import logging
import re
import secrets
import threading
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db
from .config import check_password, config, hash_password
from .config import save as save_config
from .player import upcoming
from .playback import playback
from .sonos import SonosError
from .mdns import mdns
from .share import qr_svg, share_info
from .spotify import SpotifyError, spotify

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
USER_COOKIE = "jb_user"
ADMIN_COOKIE = "jb_admin"
STATE_COOKIE = "jb_oauth_state"
NEXT_COOKIE = "jb_oauth_next"
ONE_YEAR = 365 * 24 * 3600
REMEMBER_ADMIN_SECONDS = 30 * 24 * 3600

# Admin sessions live in memory; restarting the server logs the admin out. Logins with
# "Remember me" are kept in the database instead, so they survive restarts.
admin_sessions: set[str] = set()

# Until setup is finished, the setup page only works with this one-time token. It's shown in
# the server window (and opened in the browser), so only whoever started the jukebox can use it.
setup_token = secrets.token_urlsafe(16)
log = logging.getLogger("jukebox")


def local_url(path: str) -> str:
    return f"http://127.0.0.1{'' if config.port == 80 else f':{config.port}'}{path}"


def announce_start() -> None:
    if config.configured:
        url = local_url("/admin")
        print(f"\n  Jukebox is running. Admin page: {url}\n", flush=True)
    else:
        url = local_url(f"/setup?token={setup_token}")
        line = "=" * 60
        print(f"\n  {line}\n  Jukebox needs to be set up. Open this link in your browser:\n\n"
              f"    {url}\n\n  {line}\n", flush=True)
    if config.open_browser:
        threading.Timer(1.5, webbrowser.open, args=(url,)).start()


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    announce_start()
    tasks = [asyncio.create_task(playback.run()), asyncio.create_task(mdns.run())]
    yield
    for task in tasks:
        task.cancel()
    await mdns.close()
    await spotify.close()


app = FastAPI(title="Jukebox", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.exception_handler(SpotifyError)
async def spotify_error_handler(request: Request, exc: SpotifyError):
    status = exc.status if 400 <= exc.status < 600 else 502
    return JSONResponse({"detail": str(exc)}, status_code=status)


@app.exception_handler(SonosError)
async def sonos_error_handler(request: Request, exc: SonosError):
    return JSONResponse({"detail": str(exc)}, status_code=exc.status)


# --- auth helpers ---------------------------------------------------------------

def current_user(request: Request) -> dict | None:
    return db.get_user_by_token(request.cookies.get(USER_COOKIE))


def require_user(request: Request) -> dict:
    user = current_user(request)
    if user is None:
        raise HTTPException(401, "Pick a name first")
    if user["blocked"]:
        raise HTTPException(403, "You have been blocked by the host")
    return user


def is_admin(request: Request) -> bool:
    token = request.cookies.get(ADMIN_COOKIE)
    return bool(token) and (token in admin_sessions or db.is_remembered_admin(token))


def require_admin(request: Request) -> None:
    if not is_admin(request):
        raise HTTPException(401, "Admin login required")


def public_item(item: dict, user: dict | None) -> dict:
    return {
        "id": item["id"],
        "title": item["title"],
        "artists": item["artists"],
        "album_art": item["album_art"],
        "duration_ms": item["duration_ms"],
        "status": item["status"],
        "added_by": item["added_by"],
        "added_at": item["added_at"],
        "mine": user is not None and item["user_id"] == user["id"],
    }


# --- pages ------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
async def index():
    if not config.configured:
        return RedirectResponse("/setup")
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/admin", include_in_schema=False)
async def admin_page():
    if not config.configured:
        return RedirectResponse("/setup")
    return FileResponse(STATIC_DIR / "admin.html")


@app.get("/setup", include_in_schema=False)
async def setup_page():
    return FileResponse(STATIC_DIR / "setup.html")


@app.get("/share", include_in_schema=False)
async def share_page():
    return FileResponse(STATIC_DIR / "share.html")


def _port(request: Request) -> int:
    return request.url.port or (443 if request.url.scheme == "https" else 80)


@app.get("/api/share")
async def get_share_info(request: Request):
    return share_info(_port(request))


@app.get("/share/qr.svg", include_in_schema=False)
async def share_qr(request: Request):
    return Response(qr_svg(share_info(_port(request))["qr"] or ""), media_type="image/svg+xml",
                    headers={"Cache-Control": "no-store"})


# --- guest API --------------------------------------------------------------------

class JoinBody(BaseModel):
    name: str = Field(min_length=1, max_length=30)


@app.post("/api/join")
async def join(body: JoinBody, request: Request, response: Response):
    name = " ".join(body.name.split())
    if not name:
        raise HTTPException(400, "Name can't be empty")
    user = current_user(request)
    if user:
        db.rename_user(user["id"], name)
    else:
        user = db.create_user(name, secrets.token_urlsafe(24))
        response.set_cookie(USER_COOKIE, user["token"], max_age=ONE_YEAR, httponly=True, samesite="lax")
    return {"id": user["id"], "name": name}


@app.get("/api/me")
async def me(request: Request):
    user = current_user(request)
    if user is None:
        return {"user": None}
    return {"user": {"id": user["id"], "name": user["name"], "blocked": bool(user["blocked"])}}


@app.get("/api/state")
async def state(request: Request):
    user = current_user(request)
    s = db.get_settings()
    queue = [public_item(i, user) for i in upcoming()]
    return {
        "connected": spotify.is_connected(),
        "now_playing": playback.now_playing,
        "queue": queue,
        "rules": {
            "requests_open": s["requests_open"],
            "max_songs_per_user": s["max_songs_per_user"],
        },
        "my_count": sum(1 for i in queue if i["mine"]),
    }


@app.get("/api/search")
async def search(q: str, user: dict = Depends(require_user)):
    q = q.strip()
    if not q:
        return {"results": []}
    return {"results": await spotify.search(q[:100])}


class AddBody(BaseModel):
    uri: str


TRACK_URI = re.compile(r"^spotify:track:([A-Za-z0-9]{22})$")


@app.post("/api/queue")
async def add_to_queue(body: AddBody, background: BackgroundTasks, user: dict = Depends(require_user)):
    m = TRACK_URI.match(body.uri)
    if not m:
        raise HTTPException(400, "Not a Spotify track")
    s = db.get_settings()
    if not s["requests_open"]:
        raise HTTPException(403, "Requests are closed right now")
    if s["max_songs_per_user"] and db.count_user_active(user["id"]) >= s["max_songs_per_user"]:
        raise HTTPException(403, f"You already have {s['max_songs_per_user']} songs in the queue")

    # Look the track up ourselves rather than trusting details from the browser.
    track = await spotify.track(m.group(1))
    if s["no_duplicates"] and db.track_is_active(track["uri"]):
        raise HTTPException(409, "That song is already in the queue")
    if not s["allow_explicit"] and track["explicit"]:
        raise HTTPException(403, "Explicit songs aren't allowed")
    if s["max_track_minutes"] and track["duration_ms"] > s["max_track_minutes"] * 60_000:
        raise HTTPException(403, f"Songs longer than {s['max_track_minutes']} minutes aren't allowed")

    was_empty = not upcoming()
    item_id = db.add_item(user["id"], track)

    # Interrupt auto-mode (fallback playlist / radio) if the queue was empty. It fades the
    # music out first, so it runs after the response: the guest doesn't wait for the fade.
    if (was_empty and playback.is_in_auto_mode()
            and s["auto_play"] and s.get("interrupt_auto_mode", True) and playback.can_play()):
        background.add_task(interrupt_auto_mode)

    return {"id": item_id}


async def interrupt_auto_mode() -> None:
    try:
        await playback.interrupt_auto_mode()
    except Exception as e:
        log.warning("Could not interrupt auto-mode: %s", e)


@app.delete("/api/queue/{item_id}")
async def remove_own(item_id: int, user: dict = Depends(require_user)):
    item = db.get_item(item_id)
    if item is None or item["user_id"] != user["id"]:
        raise HTTPException(404, "Not found")
    if not db.remove_item(item_id):
        raise HTTPException(409, "That song has already been handed to the speakers and can't be removed")
    return {"ok": True}


# --- admin API --------------------------------------------------------------------

class LoginBody(BaseModel):
    password: str
    remember: bool = False


@app.post("/api/admin/login")
async def admin_login(body: LoginBody, response: Response):
    if not check_password(body.password):
        raise HTTPException(401, "Wrong password")
    start_admin_session(response, remember=body.remember)
    return {"ok": True}


def start_admin_session(response: Response, remember: bool = False) -> None:
    token = secrets.token_urlsafe(32)
    if remember:
        # A cookie that outlives the browser session, backed by the database.
        db.remember_admin(token, REMEMBER_ADMIN_SECONDS)
        response.set_cookie(ADMIN_COOKIE, token, max_age=REMEMBER_ADMIN_SECONDS, httponly=True, samesite="lax")
    else:
        admin_sessions.add(token)
        response.set_cookie(ADMIN_COOKIE, token, httponly=True, samesite="lax")


@app.post("/api/admin/logout")
async def admin_logout(request: Request, response: Response):
    token = request.cookies.get(ADMIN_COOKIE, "")
    admin_sessions.discard(token)
    db.forget_admin(token)
    response.delete_cookie(ADMIN_COOKIE)
    return {"ok": True}


@app.get("/api/admin/status")
async def admin_status(request: Request):
    if not is_admin(request):
        return {"admin": False}
    return {
        "admin": True,
        "connected": spotify.is_connected(),
        "error": playback.error,
        "playback_mode": db.get_settings()["playback_mode"],
        "volume": playback.volume_status(),
        "now_playing": playback.now_playing,
        "redirect_uri": config.redirect_uri,
        "client_configured": bool(config.client_id and config.client_secret),
    }


class VolumeBody(BaseModel):
    volume: int | None = None        # jukebox volume
    pause_volume: int | None = None  # fallback music volume


@app.put("/api/admin/volume", dependencies=[Depends(require_admin)])
async def put_volume(body: VolumeBody):
    try:
        return await playback.set_volume(body.volume, body.pause_volume)
    except (SpotifyError, SonosError) as e:
        raise HTTPException(409, f"Couldn't change the volume: {e}")


@app.get("/api/admin/settings", dependencies=[Depends(require_admin)])
async def get_settings():
    return db.get_settings()


@app.put("/api/admin/settings", dependencies=[Depends(require_admin)])
async def put_settings(changes: dict):
    try:
        mode = changes.pop("playback_mode", None)
        if mode is not None:
            if mode not in ("spotify", "sonos"):
                raise ValueError(mode)
            await playback.switch_mode(mode)  # also moves the current song to the new player
        return db.update_settings(changes)
    except (TypeError, ValueError):
        raise HTTPException(400, "Invalid setting value")


@app.get("/api/admin/devices", dependencies=[Depends(require_admin)])
async def admin_devices():
    if not spotify.is_connected():
        return {"devices": []}
    return {"devices": [
        {"id": d["id"], "name": d["name"], "type": d["type"], "is_active": d["is_active"]}
        for d in await spotify.devices() if d.get("id") and not d.get("is_restricted")
    ]}


@app.get("/api/admin/sonos/rooms", dependencies=[Depends(require_admin)])
async def admin_sonos_rooms():
    return {"rooms": await playback.sonos_controller.rooms()}


class SonosRoomsBody(BaseModel):
    uids: list[str]


@app.put("/api/admin/sonos/rooms", dependencies=[Depends(require_admin)])
async def admin_set_sonos_rooms(body: SonosRoomsBody):
    uids = [u for u in body.uids if u]
    if db.get_settings()["playback_mode"] == "sonos":
        await playback.sonos.apply_rooms(uids)  # groups them; starts the music if it wasn't playing
    else:
        db.update_settings({"sonos_rooms": ",".join(uids)})
    return {"rooms": await playback.sonos_controller.rooms()}


@app.get("/api/admin/queue", dependencies=[Depends(require_admin)])
async def admin_queue():
    return {"queue": [{**public_item(i, None), "user_id": i["user_id"], "priority": i["priority"]} for i in upcoming()]}


@app.delete("/api/admin/queue/{item_id}", dependencies=[Depends(require_admin)])
async def admin_remove(item_id: int):
    if not db.remove_item(item_id):
        raise HTTPException(409, "Only songs not yet handed to the speakers can be removed")
    return {"ok": True}


@app.post("/api/admin/queue/{item_id}/top", dependencies=[Depends(require_admin)])
async def admin_move_top(item_id: int):
    if not db.move_to_top(item_id):
        raise HTTPException(409, "Only songs not yet handed to the speakers can be moved")
    return {"ok": True}


@app.post("/api/admin/play-next", dependencies=[Depends(require_admin)])
async def admin_play_next():
    item = await playback.play_next_now()
    if item is None:
        raise HTTPException(409, "The queue is empty")
    return {"playing": item["title"]}


@app.post("/api/admin/skip", dependencies=[Depends(require_admin)])
async def admin_skip():
    await playback.skip()
    return {"ok": True}


@app.get("/api/admin/users", dependencies=[Depends(require_admin)])
async def admin_users():
    return {"users": db.list_users()}


class BlockBody(BaseModel):
    blocked: bool


@app.post("/api/admin/users/{user_id}/block", dependencies=[Depends(require_admin)])
async def admin_block(user_id: int, body: BlockBody):
    db.set_user_blocked(user_id, body.blocked)
    return {"ok": True}


@app.get("/api/admin/history", dependencies=[Depends(require_admin)])
async def admin_history():
    return {"history": [{**public_item(i, None), "user_id": i["user_id"]} for i in db.history()]}


@app.post("/api/admin/disconnect", dependencies=[Depends(require_admin)])
async def admin_disconnect():
    db.clear_auth()
    return {"ok": True}


# --- setup ------------------------------------------------------------------------

def setup_allowed(request: Request) -> bool:
    """The admin may always change setup; before setup is done, the one-time token is needed."""
    if is_admin(request):
        return True
    token = request.headers.get("X-Setup-Token", "")
    return not config.configured and bool(token) and secrets.compare_digest(token, setup_token)


def require_setup(request: Request) -> None:
    if not setup_allowed(request):
        raise HTTPException(403, "Open the setup link shown in the jukebox window, or log in on the admin page.")


@app.get("/api/setup/status")
async def setup_status(request: Request):
    return {
        "configured": config.configured,
        "allowed": setup_allowed(request),
        "has_password": config.has_admin_password,
        "has_spotify_app": config.has_spotify_app,
        "client_id": config.client_id if setup_allowed(request) else "",
        "spotify_connected": spotify.is_connected(),
        "redirect_uri": config.redirect_uri,
        "on_loopback": request.url.hostname in ("127.0.0.1", "localhost", "::1"),
        "admin_url": local_url("/admin"),
    }


class SetupPasswordBody(BaseModel):
    password: str = Field(min_length=6, max_length=200)


@app.post("/api/setup/password", dependencies=[Depends(require_setup)])
async def setup_password(body: SetupPasswordBody, response: Response):
    save_config({"ADMIN_PASSWORD_HASH": hash_password(body.password), "ADMIN_PASSWORD": ""})
    # A new password logs everyone out, including browsers that were remembered.
    admin_sessions.clear()
    db.forget_admin()
    start_admin_session(response)  # logged in for the rest of setup
    return {"ok": True}


class SetupSpotifyAppBody(BaseModel):
    client_id: str
    client_secret: str


SPOTIFY_APP_KEY = re.compile(r"^[0-9a-f]{32}$")


@app.post("/api/setup/spotify-app", dependencies=[Depends(require_setup)])
async def setup_spotify_app(body: SetupSpotifyAppBody):
    client_id, client_secret = body.client_id.strip(), body.client_secret.strip()
    if not SPOTIFY_APP_KEY.match(client_id):
        raise HTTPException(400, "The Client ID should be 32 characters of 0-9 and a-f. Copy it from your app's Basic Information page.")
    if not SPOTIFY_APP_KEY.match(client_secret):
        raise HTTPException(400, "The Client secret should be 32 characters of 0-9 and a-f. Click \"View client secret\" to see it.")
    problem = await spotify.check_app_credentials(client_id, client_secret)
    if problem:
        raise HTTPException(400, problem)
    if client_id != config.client_id:
        db.clear_auth()  # a login made for another app doesn't work with this one
    save_config({"SPOTIFY_CLIENT_ID": client_id, "SPOTIFY_CLIENT_SECRET": client_secret})
    return {"ok": True}


# --- Spotify OAuth (host only) ----------------------------------------------------

@app.get("/login", include_in_schema=False)
async def spotify_login(request: Request):
    if not is_admin(request):
        return RedirectResponse("/admin")
    state = secrets.token_urlsafe(16)
    resp = RedirectResponse(spotify.auth_url(state))
    resp.set_cookie(STATE_COOKIE, state, max_age=600, httponly=True, samesite="lax")
    # Where to land after Spotify: back to the setup page when connecting from there.
    resp.set_cookie(NEXT_COOKIE, "/setup" if request.query_params.get("next") == "setup" else "/admin",
                    max_age=600, httponly=True, samesite="lax")
    return resp


@app.get("/callback", include_in_schema=False)
async def spotify_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    if not is_admin(request):
        raise HTTPException(401, "Admin login required")
    if error:
        raise HTTPException(400, f"Spotify login failed: {error}")
    expected = request.cookies.get(STATE_COOKIE)
    if not code or not state or not expected or not secrets.compare_digest(state, expected):
        raise HTTPException(400, "Invalid OAuth state, try connecting again")
    await spotify.exchange_code(code)
    resp = RedirectResponse("/setup" if request.cookies.get(NEXT_COOKIE) == "/setup" else "/admin")
    resp.delete_cookie(STATE_COOKIE)
    resp.delete_cookie(NEXT_COOKIE)
    return resp
