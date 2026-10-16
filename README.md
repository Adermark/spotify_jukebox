# Jukebox

A shared Spotify queue. Guests open the website, pick a name, search for songs and add them.
Everyone sees the full queue and who added each song. The host's Spotify account does the playback.

- **Backend:** Python, FastAPI, SQLite
- **Frontend:** plain HTML, CSS and JavaScript (no frameworks, no build step)

## How it works

The real queue lives in SQLite, not in Spotify. That's because Spotify's API can add to its queue but
can't remove or reorder songs. A background loop polls Spotify every couple of seconds. When the
current song has `push_seconds_before_end` seconds left, it hands the next song from our queue to
Spotify. Until then, songs can be reordered, removed or pinned freely. Songs already handed over show
as **up next** and are locked.

When our queue is empty, Spotify keeps playing whatever it was playing, like a playlist or album. You
can use that as a fallback playlist.

## Requirements

- Python 3.11+. On Windows, `run.bat` offers to install it. On macOS, install it from
  [python.org](https://www.python.org/downloads/macos/): the `python3` that comes with macOS is usually too old.
- The host needs **Spotify Premium**, because the API's playback control doesn't work on free accounts.
- Spotify must be open and playing on some device: the host's phone, computer or a speaker.

## Setup

1. Start the jukebox:

   **Windows:** double-click `run.bat`. The first time, it creates its own Python environment in
   `.venv` and installs the packages. Running `python run.py` directly uses your system Python,
   which doesn't have them.

   **macOS:** double-click `run.command`. It opens in Terminal and works like `run.bat`.
   - The first time, macOS may say it "can't be opened because it is from an unidentified
     developer". Right-click the file, choose **Open**, then **Open** again.
   - If the macOS firewall asks whether Python may accept incoming connections, click **Allow**.

   **Linux:** run `./run.sh` in a terminal.

   **Docker:** see [Docker](#docker) below.

2. **The setup page opens in your browser by itself.** It walks you through:
   - choosing the admin password, which is stored hashed;
   - creating the Spotify app. It gives step-by-step instructions and the exact redirect URI to
     paste, then checks the Client ID and secret with Spotify before saving them;
   - connecting the host's Spotify account.

   Everything is saved to `.env`, so there's nothing to edit by hand.
   - The setup page only works with the one-time link printed in the jukebox window, so other
     people on the network can't take over an unfinished installation.
   - If the browser didn't open, copy the link from the window.
   - Later, the admin can reopen the page from **setup** at the top of the admin page, for example
     to switch to another Spotify app.

3. On the admin page, choose where the music plays, then share the guest link (see below).

Advanced settings, like port, network name and polling interval, are listed in `.env.example`.

## Playing on Sonos

Spotify doesn't let apps control Sonos speakers: they show up as a "restricted device". So the
jukebox can control Sonos directly over the local network instead.

1. In the Sonos app, make sure **Spotify is added as a music service**.
2. In the admin page under **Settings**, set **Play through** to **Sonos speakers**.
3. Tick the room(s) to play in. Ticked rooms are grouped automatically, and the first one ticked
   leads the group.
4. Press **Play next now**. This switches the speakers to the jukebox, even if they were playing
   TV sound or a Spotify Connect session.

How it works:
- The jukebox keeps its own queue, as in Spotify mode.
- It hands the next song to the Sonos queue shortly before the current one ends.
- **Skip** jumps straight to the next jukebox song.
- **Auto-play** only restarts the speakers when they've stopped. A pause, TV sound or a Spotify
  Connect session is left alone.

Search and song details still come from Spotify, so the Spotify login on the admin page is still
needed. The computer running the jukebox must be on the same network as the speakers. This uses
Sonos' local control interface (via the SoCo library). It isn't officially documented, but it's
what Home Assistant and many other tools use.

## Sharing with guests

The admin page shows the **guest link**, e.g. `http://jukebox.local/`, with a Copy button.
**QR code / TV screen** opens `/share`: a large QR code plus the link and the song that's playing.
Put it on a TV or print it.

- **Friendly name:** the server advertises `jukebox.local` on the network using mDNS, the same
  system printers and AirPlay use. iPhones, Macs, Windows 10+ and most recent Android phones
  resolve it. Change it with `MDNS_NAME` in `.env`, or leave that empty to turn it off.
- **Port:** on first start the jukebox uses port 80, which gives a link without a port number. If
  port 80 is taken by another program, or not allowed (Linux without root), it uses
  8000 and the link becomes `http://jukebox.local:8000/`. The choice is saved as `PORT` in `.env`
  and kept from then on, because it's part of the redirect URI in your Spotify app. To change it
  later, edit `PORT` in `.env`, then open **setup** and add the new redirect URI it shows to your
  Spotify app.
- **The QR code uses the IP address,** e.g. `http://192.168.1.81/`, because that works on
  every phone. The IP is also shown under the name as a fallback.
- The address is detected again every 30 seconds, so it follows the computer to any network
  (home, office, party). It only works for devices on the **same network** as the host computer.
- If you have a DNS name or reverse proxy, set `PUBLIC_URL` in `.env` and that's shown instead.
- Windows asks to allow Python through the firewall the first time it's run. If guests can't
  connect, check that inbound connections for `python.exe` are allowed, including on **Public**
  networks: Windows often marks new office or guest Wi-Fi networks as Public.
- Some office and guest Wi-Fi networks block devices from talking to each other ("client
  isolation"). Then the jukebox can't be reached from phones at all, and you'd need to host it
  somewhere else.

## Admin page (`/admin`)

- **Queue:** pin a song to the top (⤒) or remove it (✕).
- **Settings:** changes take effect immediately.
  | Setting | Meaning |
  |---|---|
  | Playback device | Which Spotify device plays the music. Pick one if your account lists several, because Spotify sometimes reports a stale device as "active" |
  | Accept requests | Turn guest requests on or off |
  | Max songs per user | Waiting songs per user (0 = unlimited) |
  | Fair rotation | Everyone's 1st song plays before anyone's 2nd, and so on |
  | Allow back-to-back | When off, another user's song is put in between whenever possible |
  | Block duplicates | Reject a song already in the queue |
  | Allow explicit | Reject songs Spotify marks as explicit |
  | Max song length | In minutes (0 = unlimited) |
  | Auto-play | Start the next song when Spotify is stopped (not when paused mid-song) |
  | Hand next song to Spotify | Seconds before the end of the current song |
- **Users:** see how many songs each user has added, and block or unblock them. Blocking removes their
  waiting songs.
- **History:** every song that was requested, with who added it, when, and what happened to it.
- **Play next now:** starts the next song immediately. Use it to start the party, or when nothing is
  playing.
- **Skip:** skips to the next jukebox song. If that song isn't lined up in Spotify's queue yet, it's
  added first, so the skip lands on it instead of the next song in Spotify's playlist or album.

Songs are started as a track within their album, because the Windows desktop client accepts a
single-track play command but stays silent. So if the jukebox queue runs dry after a song started
this way, Spotify continues that album.

## Guests

Guests are identified by a random cookie, which is what lets the app track who added what. Clearing
cookies or switching browsers makes them a new user. Guests can remove their own songs until the
songs are handed to Spotify.

## Docker

Docker suits an always-on machine such as a Raspberry Pi, home server or NAS. The database is kept
in the `jukebox-data` volume.

**Linux:**

```bash
docker compose up -d
```

`compose.yaml` uses host networking, so the LAN address is detected and `jukebox.local` is
announced, just like running it directly.

**Docker Desktop (Windows/macOS):**

```bash
docker compose -f compose.desktop.yaml up -d
```

Containers run inside a hidden VM here, so the app can't see your computer's network. As a result:
- Set `HOST_IP` in `.env` to the computer's LAN IP, so the guest link and QR code are right.
  Update it when you change network.
- `jukebox.local` isn't available.

For a laptop that moves between networks, `run.bat` is the better choice.

**First start in Docker:** run `docker compose logs` to find the one-time setup link. The setup is
saved to `/data/config.env` in the volume.

**Connecting Spotify on a headless server:** Spotify only redirects back to `127.0.0.1`, so open
an SSH tunnel from your own computer and do the setup through it:

```bash
ssh -L 8000:127.0.0.1:8000 user@server
```

Then open the setup link from the logs (or <http://127.0.0.1:8000/admin> once set up) in your local
browser.

## Making a zip to share

Run `make_dist.py` with the project's Python (see [Development](#development)):

```bash
python make_dist.py
```

This creates `dist/jukebox-<date>.zip`. Only an explicit list of files goes in, so your `.env`
(Spotify secret, admin password), `jukebox.db` (Spotify login, history) and `.venv` are never
included. Docker files and tests are left out too. Whoever unzips it double-clicks `run.bat`:
- If Python is missing, `run.bat` offers to install it.
- The setup page then opens in their browser.

`START HERE.txt` in the zip explains this to them.

## Development

Run the start script once (`run.bat` on Windows, `run.command` on macOS) to create the project's
Python environment in `.venv`. Then activate it in the terminal you work in, so `python` and
`pytest` use it:

| Platform | Activate |
|---|---|
| Windows (PowerShell) | `.venv\Scripts\Activate.ps1` |
| Windows (cmd) | `.venv\Scripts\activate.bat` |
| macOS / Linux | `source .venv/bin/activate` |

## Tests

With the environment activated:

```bash
pip install -r requirements-dev.txt
pytest
```

## Contributing

`main` is the release branch and `develop` is the working branch. Work happens in `feature/` or
`fix/` branches off `develop`, merged back through pull requests. See [AGENTS.md](AGENTS.md) for
the full workflow, including how to release.

## Code map

| File | What |
|---|---|
| `app/main.py` | FastAPI routes: guest API, admin API, Spotify OAuth |
| `app/playback.py` | Runs the playback loop for the chosen mode (Spotify or Sonos) |
| `app/player.py` | Spotify Connect mode: watches playback and pushes the next song |
| `app/sonos.py`, `app/sonos_player.py` | Sonos mode: direct speaker control and its playback loop |
| `app/queue_logic.py` | Queue ordering rules (pure functions, unit tested) |
| `app/spotify.py` | Spotify Web API client with token refresh |
| `app/db.py` | SQLite schema and queries, settings defaults |
| `static/` | Guest page (`index.html`, `app.js`), admin page (`admin.html`, `admin.js`) |
