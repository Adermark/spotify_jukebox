import json
import sqlite3
import time
from contextlib import contextmanager

from .config import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    token       TEXT NOT NULL UNIQUE,
    blocked     INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL
);

-- status: queued  -> waiting in our queue (can be reordered/removed)
--         sent    -> pushed to Spotify's queue (locked, Spotify can't un-queue)
--         playing -> currently playing on Spotify
--         played  -> finished (or skipped)
--         removed -> removed by the user or admin before being sent
CREATE TABLE IF NOT EXISTS queue_items (
    id           INTEGER PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users(id),
    track_uri    TEXT NOT NULL,
    title        TEXT NOT NULL,
    artists      TEXT NOT NULL,
    album_art    TEXT,
    duration_ms  INTEGER NOT NULL,
    explicit     INTEGER NOT NULL DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'queued',
    priority     INTEGER NOT NULL DEFAULT 0,
    added_at     REAL NOT NULL,
    sent_at      REAL,
    played_at    REAL
);
CREATE INDEX IF NOT EXISTS idx_queue_status ON queue_items(status);

CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS spotify_auth (
    id             INTEGER PRIMARY KEY CHECK (id = 1),
    access_token   TEXT NOT NULL,
    refresh_token  TEXT NOT NULL,
    expires_at     REAL NOT NULL
);
"""

# Admin-editable settings. The type of each default decides how input is coerced.
DEFAULT_SETTINGS = {
    "requests_open": True,          # guests can add songs
    "max_songs_per_user": 3,        # 0 = unlimited (counts queued + sent songs)
    "fair_rotation": True,          # users with fewer songs queued go first
    "allow_consecutive": False,     # same user twice in a row
    "no_duplicates": True,          # reject a track already in the queue
    "allow_explicit": True,
    "max_track_minutes": 10,        # 0 = unlimited
    "push_seconds_before_end": 15,  # when to hand the next song to Spotify
    "auto_play": True,              # start the queue when Spotify is stopped
    "interrupt_auto_mode": True,    # interrupt auto-mode (fallback playlist / radio) when a song is queued
    "pause_volume": 100,            # fallback music volume (0-100), never above the jukebox volume
    "device_id": "",                # Spotify device to play on ("" = Spotify's active device)
    "playback_mode": "spotify",     # "spotify" (Spotify Connect) or "sonos" (direct Sonos control)
    "sonos_rooms": "",              # comma-separated Sonos room ids; the first leads the group
}

ACTIVE_STATUSES = ("queued", "sent", "playing")


@contextmanager
def connect():
    conn = sqlite3.connect(config.database_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


# --- settings ---------------------------------------------------------------

def get_settings() -> dict:
    with connect() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    stored = {r["key"]: json.loads(r["value"]) for r in rows}
    return {k: stored.get(k, v) for k, v in DEFAULT_SETTINGS.items()}


def update_settings(changes: dict) -> dict:
    clean = {}
    for key, value in changes.items():
        if key not in DEFAULT_SETTINGS:
            continue
        default = DEFAULT_SETTINGS[key]
        if isinstance(default, bool):
            clean[key] = bool(value)
        elif isinstance(default, str):
            clean[key] = str(value)[:2000]
            if key == "playback_mode" and clean[key] not in ("spotify", "sonos"):
                raise ValueError("playback_mode must be spotify or sonos")
        else:
            clean[key] = max(0, int(value))
            if key == "pause_volume":
                clean[key] = min(100, clean[key])
    with connect() as conn:
        conn.executemany(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            [(k, json.dumps(v)) for k, v in clean.items()],
        )
    return get_settings()


# Internal state kept between restarts. It shares the settings table under keys starting
# with "_", which get_settings() never returns.

def get_state(key: str, default=None):
    with connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (f"_{key}",)).fetchone()
    return json.loads(row["value"]) if row else default


def set_state(key: str, value) -> None:
    with connect() as conn:
        if value is None:
            conn.execute("DELETE FROM settings WHERE key = ?", (f"_{key}",))
        else:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (f"_{key}", json.dumps(value)),
            )


# --- users ------------------------------------------------------------------

def create_user(name: str, token: str) -> dict:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO users (name, token, created_at) VALUES (?, ?, ?)",
            (name, token, time.time()),
        )
        return dict(conn.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone())


def get_user_by_token(token: str | None) -> dict | None:
    if not token:
        return None
    with connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE token = ?", (token,)).fetchone()
    return dict(row) if row else None


def rename_user(user_id: int, name: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE users SET name = ? WHERE id = ?", (name, user_id))


def set_user_blocked(user_id: int, blocked: bool) -> None:
    with connect() as conn:
        conn.execute("UPDATE users SET blocked = ? WHERE id = ?", (int(blocked), user_id))
        if blocked:
            conn.execute(
                "UPDATE queue_items SET status = 'removed' WHERE user_id = ? AND status = 'queued'",
                (user_id,),
            )


def list_users() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT u.id, u.name, u.blocked, u.created_at,
                   COUNT(q.id) AS total_added,
                   SUM(q.status IN ('queued', 'sent')) AS in_queue
            FROM users u LEFT JOIN queue_items q ON q.user_id = u.id
            GROUP BY u.id ORDER BY u.created_at DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]


# --- queue items ------------------------------------------------------------

_ITEM_SELECT = """
    SELECT q.*, u.name AS added_by
    FROM queue_items q JOIN users u ON u.id = q.user_id
"""


def add_item(user_id: int, track: dict) -> int:
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO queue_items
                (user_id, track_uri, title, artists, album_art, duration_ms, explicit, added_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id, track["uri"], track["title"], track["artists"], track["album_art"],
                track["duration_ms"], int(track["explicit"]), time.time(),
            ),
        )
        return cur.lastrowid


def get_item(item_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute(_ITEM_SELECT + " WHERE q.id = ?", (item_id,)).fetchone()
    return dict(row) if row else None


def items_with_status(*statuses: str) -> list[dict]:
    marks = ",".join("?" * len(statuses))
    with connect() as conn:
        rows = conn.execute(
            _ITEM_SELECT + f" WHERE q.status IN ({marks}) ORDER BY COALESCE(q.sent_at, q.added_at), q.id",
            statuses,
        ).fetchall()
    return [dict(r) for r in rows]


def count_user_active(user_id: int) -> int:
    with connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM queue_items WHERE user_id = ? AND status IN ('queued', 'sent')",
            (user_id,),
        ).fetchone()[0]


def track_is_active(track_uri: str) -> bool:
    with connect() as conn:
        marks = ",".join("?" * len(ACTIVE_STATUSES))
        return conn.execute(
            f"SELECT 1 FROM queue_items WHERE track_uri = ? AND status IN ({marks}) LIMIT 1",
            (track_uri, *ACTIVE_STATUSES),
        ).fetchone() is not None


def remove_item(item_id: int) -> bool:
    """Remove a queued item. Items already sent to Spotify can't be removed."""
    with connect() as conn:
        cur = conn.execute(
            "UPDATE queue_items SET status = 'removed' WHERE id = ? AND status = 'queued'",
            (item_id,),
        )
        return cur.rowcount > 0


def move_to_top(item_id: int) -> bool:
    with connect() as conn:
        top = conn.execute("SELECT COALESCE(MAX(priority), 0) FROM queue_items WHERE status = 'queued'").fetchone()[0]
        cur = conn.execute(
            "UPDATE queue_items SET priority = ? WHERE id = ? AND status = 'queued'",
            (top + 1, item_id),
        )
        return cur.rowcount > 0


def mark_sent(item_id: int) -> None:
    with connect() as conn:
        conn.execute("UPDATE queue_items SET status = 'sent', sent_at = ? WHERE id = ?", (time.time(), item_id))


def requeue(item_ids: list[int]) -> None:
    """Put sent items back in our queue (Spotify never played them)."""
    with connect() as conn:
        conn.executemany(
            "UPDATE queue_items SET status = 'queued', sent_at = NULL WHERE id = ? AND status = 'sent'",
            [(i,) for i in item_ids],
        )


def mark_now_playing(track_uri: str | None) -> None:
    """Called when Spotify starts a new track: finish the old one, promote ours if it matches."""
    now = time.time()
    with connect() as conn:
        conn.execute("UPDATE queue_items SET status = 'played', played_at = ? WHERE status = 'playing'", (now,))
        if not track_uri:
            return
        row = conn.execute(
            "SELECT id, sent_at FROM queue_items WHERE status = 'sent' AND track_uri = ? ORDER BY sent_at LIMIT 1",
            (track_uri,),
        ).fetchone()
        if row is None:
            return
        conn.execute("UPDATE queue_items SET status = 'playing' WHERE id = ?", (row["id"],))
        # Anything sent before this one was skipped past on Spotify's side.
        conn.execute(
            "UPDATE queue_items SET status = 'played', played_at = ? WHERE status = 'sent' AND sent_at < ?",
            (now, row["sent_at"]),
        )


def history(limit: int = 200) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            _ITEM_SELECT + " WHERE q.status != 'queued' ORDER BY q.added_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


# --- spotify auth -------------------------------------------------------------

def get_auth() -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM spotify_auth WHERE id = 1").fetchone()
    return dict(row) if row else None


def save_auth(access_token: str, refresh_token: str, expires_at: float) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO spotify_auth (id, access_token, refresh_token, expires_at) VALUES (1, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET access_token = excluded.access_token,
                refresh_token = excluded.refresh_token, expires_at = excluded.expires_at
            """,
            (access_token, refresh_token, expires_at),
        )


def clear_auth() -> None:
    with connect() as conn:
        conn.execute("DELETE FROM spotify_auth")
