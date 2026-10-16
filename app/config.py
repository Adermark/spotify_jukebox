"""Configuration from environment variables and the config file (.env).

The setup page writes to the config file, so a fresh install needs no hand-editing.
Real environment variables win over the file.
"""
import hashlib
import hmac
import os
import re
import secrets
from dataclasses import asdict, dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = Path(os.getenv("CONFIG_FILE") or PROJECT_DIR / ".env")

load_dotenv(CONFIG_FILE)


@dataclass
class Config:
    client_id: str
    client_secret: str
    redirect_uri: str
    admin_password: str       # plain text (older installs / hand-written .env)
    admin_password_hash: str  # written by the setup page
    database_path: str
    poll_interval: float
    public_url: str
    port: int
    mdns_name: str
    host_ip: str
    open_browser: bool

    @property
    def has_admin_password(self) -> bool:
        return bool(self.admin_password or self.admin_password_hash)

    @property
    def has_spotify_app(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @property
    def configured(self) -> bool:
        return self.has_admin_password and self.has_spotify_app


def load_config() -> Config:
    port = int(os.getenv("PORT", "8000"))
    return Config(
        client_id=os.getenv("SPOTIFY_CLIENT_ID", "").strip(),
        client_secret=os.getenv("SPOTIFY_CLIENT_SECRET", "").strip(),
        redirect_uri=os.getenv("SPOTIFY_REDIRECT_URI") or f"http://127.0.0.1{'' if port == 80 else f':{port}'}/callback",
        admin_password=os.getenv("ADMIN_PASSWORD", ""),
        admin_password_hash=os.getenv("ADMIN_PASSWORD_HASH", ""),
        database_path=os.getenv("DATABASE_PATH", "jukebox.db"),
        poll_interval=float(os.getenv("POLL_INTERVAL", "2")),
        public_url=os.getenv("PUBLIC_URL", ""),
        port=port,
        mdns_name=os.getenv("MDNS_NAME", "jukebox").strip().lower(),
        host_ip=os.getenv("HOST_IP", "").strip(),
        open_browser=os.getenv("OPEN_BROWSER", "1") not in ("0", "false", "no", ""),
    )


config = load_config()


def save(updates: dict[str, str]) -> None:
    """Write settings to the config file (keeping its comments) and apply them right away."""
    lines = CONFIG_FILE.read_text(encoding="utf-8").splitlines() if CONFIG_FILE.exists() else []
    remaining = dict(updates)
    for i, line in enumerate(lines):
        m = re.match(r"\s*([A-Z_][A-Z0-9_]*)\s*=", line)
        if m and m.group(1) in remaining:
            lines[i] = f"{m.group(1)}={remaining.pop(m.group(1))}"
    lines += [f"{k}={v}" for k, v in remaining.items()]
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ.update(updates)
    for key, value in asdict(load_config()).items():
        setattr(config, key, value)


# --- port -------------------------------------------------------------------------------

PREFERRED_PORTS = (80, 8000, 8080, 8888)


def port_is_free(port: int) -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("0.0.0.0", port))  # fails if taken, or (macOS/Linux) if < 1024 without root
            return True
        except OSError:
            return False


def ensure_port(is_free=port_is_free) -> int:
    """Pick the port on the very first start and save it, so it never changes afterwards.

    Port 80 gives the shortest guest link (http://jukebox.local/). The port is part of the
    redirect URI registered in the Spotify app, so once chosen it must stay the same.
    """
    if os.getenv("PORT"):
        return config.port
    if config.configured:
        port = 8000  # set up before ports were picked automatically: keep the old default
    else:
        port = next((p for p in PREFERRED_PORTS if is_free(p)), 8000)
    save({"PORT": str(port)})
    return port


# --- admin password -------------------------------------------------------------------

_ITERATIONS = 600_000


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), _ITERATIONS).hex()
    return f"pbkdf2_sha256:{_ITERATIONS}:{salt}:{digest}"  # no "$": .env files expand those


def check_password(password: str) -> bool:
    if config.admin_password_hash:
        try:
            _, iterations, salt, digest = config.admin_password_hash.split(":")
            candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations)).hex()
        except ValueError:
            return False
        return hmac.compare_digest(candidate, digest)
    if config.admin_password:
        return hmac.compare_digest(password.encode(), config.admin_password.encode())
    return False
