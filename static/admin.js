// Admin page: Spotify connection, playback controls, settings, queue moderation, users, history.

const $ = (id) => document.getElementById(id);

// Labels and help text for each setting, in display order.
const SETTINGS = [
  ["requests_open", "Accept requests", "Guests can add songs."],
  ["max_songs_per_user", "Max songs per user", "How many songs one user can have waiting. 0 = unlimited."],
  ["fair_rotation", "Fair rotation", "Everyone's 1st song plays before anyone's 2nd, and so on."],
  ["allow_consecutive", "Allow back-to-back songs from the same user", "When off, another user's song is played in between whenever possible."],
  ["no_duplicates", "Block duplicates", "Reject a song that's already in the queue."],
  ["allow_explicit", "Allow explicit songs", ""],
  ["max_track_minutes", "Max song length (minutes)", "0 = unlimited."],
  ["auto_play", "Auto-play", "Start the next song when playback is stopped (not when paused mid-song)."],
  ["interrupt_auto_mode", "Interrupt auto-mode", "When the queue was empty, play newly added songs immediately instead of waiting for fallback music to finish."],
  ["push_seconds_before_end", "Hand next song to the player (seconds before end)", "Songs handed over can no longer be removed or reordered."],
];

let currentTab = "queue";
let settings = {};

async function checkStatus() {
  const s = await api("GET", "/api/admin/status");
  $("login").classList.toggle("hidden", s.admin);
  $("panel").classList.toggle("hidden", !s.admin);
  if (!s.admin) return false;
  renderStatus(s);
  renderNowPlaying(s.now_playing);
  renderVolume(s.volume);
  return true;
}

// --- now playing ------------------------------------------------------------------------

let nowPlaying = null;
let nowFetchedAt = 0;

function renderNowPlaying(np) {
  nowPlaying = np;
  nowFetchedAt = Date.now();
  const box = $("admin-now");
  if (!np) {
    box.replaceChildren(el("p", { class: "muted" }, "Nothing playing right now."));
    return;
  }
  const source = np.added_by
    ? el("div", {}, el("span", { class: "badge accent" }, "Queue"), " Added by ", el("span", { class: "who" }, np.added_by))
    : el("div", {}, el("span", { class: "badge" }, "Fallback music"), " ",
        el("span", { class: "muted" }, "The queue is empty, so Spotify plays on by itself."));
  box.replaceChildren(
    el("div", { class: "now" },
      art(np.album_art),
      el("div", { class: "info", style: "flex:1;min-width:0" },
        el("div", { class: "title" }, np.title),
        el("div", { class: "muted" }, np.artists),
        source,
        el("div", { class: "muted" }, [np.is_playing ? null : "Paused", np.device ? `On ${np.device}` : null]
          .filter(Boolean).join(" · ")),
        el("div", { class: "progress" }, el("div", { id: "admin-progress-bar" })),
      ),
    ),
  );
  tickNowPlaying();
}

function tickNowPlaying() {
  const np = nowPlaying;
  const bar = $("admin-progress-bar");
  if (!np || !bar || !np.duration_ms) return;
  const elapsed = np.is_playing ? Date.now() - nowFetchedAt : 0;
  bar.style.width = Math.min(100, ((np.progress_ms + elapsed) / np.duration_ms) * 100) + "%";
}
setInterval(tickNowPlaying, 500);

// --- volume -----------------------------------------------------------------------------
// Two sliders: the jukebox volume, and the fallback music volume, which is never louder.
// The device follows whichever one is playing right now.

const VOLUME_SLIDERS = { volume: "volume", pause_volume: "pause-volume" };
let volume = null;
let draggingVolume = null;  // the slider being dragged: don't move it under the pointer

function renderVolume(v) {
  volume = v;
  $("volume-box").classList.toggle("hidden", !v);
  if (!v) return;
  const controllable = v.volume !== null;
  $("volume-unavailable").classList.toggle("hidden", controllable);
  for (const [key, id] of Object.entries(VOLUME_SLIDERS)) {
    const slider = $(id);
    slider.disabled = !controllable;
    if (controllable && draggingVolume !== key) slider.value = v[key];
    $(id + "-value").textContent = controllable ? slider.value : "";
  }
  $("pause-volume-help").textContent = v.mode === "pause"
    ? "Fallback music is playing now. It's never louder than the volume above."
    : "Plays when the queue is empty. It's never louder than the volume above.";
}

for (const [key, id] of Object.entries(VOLUME_SLIDERS)) {
  const slider = $(id);
  slider.addEventListener("pointerdown", () => { draggingVolume = key; });
  slider.addEventListener("input", () => {
    let value = Number(slider.value);
    if (key === "pause_volume") value = Math.min(value, Number($("volume").value));
    $(id + "-value").textContent = value;
  });
  slider.addEventListener("change", async () => {
    draggingVolume = null;
    try {
      renderVolume(await api("PUT", "/api/admin/volume", { [key]: Number(slider.value) }));
    } catch (e) {
      toast(e.message, true);
      checkStatus();
    }
  });
}

function renderStatus(s) {
  const box = $("spotify-status");
  if (!s.client_configured) {
    box.replaceChildren(el("p", {}, el("span", { class: "badge danger" }, "Not configured"),
      " Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET in .env and restart."));
  } else if (!s.connected) {
    const onLoopback = location.origin === new URL(s.redirect_uri).origin;
    box.replaceChildren(
      el("p", {}, el("span", { class: "badge danger" }, "Not connected"), " Log in with the host's Spotify Premium account."),
      onLoopback
        ? el("a", { href: "/login" }, el("button", {}, "Connect Spotify"))
        : el("p", { class: "muted" }, "Open this page at ", el("a", { href: new URL("/admin", s.redirect_uri).href }, new URL("/admin", s.redirect_uri).href),
            " on the host computer to connect (Spotify only redirects back there)."),
    );
  } else {
    box.replaceChildren(el("p", {},
      el("span", { class: "badge accent" }, "Spotify connected"), " ",
      s.error ? el("span", { style: "color:var(--danger)" }, s.error) : "",
      " ",
      el("button", { class: "link", onclick: disconnect }, "disconnect"),
    ));
  }
}

async function disconnect() {
  if (!confirm("Disconnect Spotify? Playback control stops until you connect again.")) return;
  await api("POST", "/api/admin/disconnect");
  refresh();
}

$("login-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    await api("POST", "/api/admin/login", { password: $("password").value, remember: $("remember").checked });
    $("password").value = "";
    refresh();
  } catch (e) { toast(e.message, true); }
});

$("logout").addEventListener("click", async () => {
  await api("POST", "/api/admin/logout");
  refresh();
});

$("play-next").addEventListener("click", () => action("POST", "/api/admin/play-next", "Playing next song"));
$("skip").addEventListener("click", () => action("POST", "/api/admin/skip", "Skipped"));

async function action(method, path, okMessage) {
  try {
    await api(method, path);
    if (okMessage) toast(okMessage);
    refreshTab();
  } catch (e) { toast(e.message, true); }
}

// --- tabs ------------------------------------------------------------------------

document.querySelectorAll(".tabs button").forEach((b) =>
  b.addEventListener("click", () => {
    currentTab = b.dataset.tab;
    document.querySelectorAll(".tabs button").forEach((x) => x.classList.toggle("active", x === b));
    for (const t of ["queue", "settings", "users", "history"]) $("tab-" + t).classList.toggle("hidden", t !== currentTab);
    refreshTab();
  }));

async function refreshTab() {
  try {
    if (currentTab === "queue") await loadQueue();
    else if (currentTab === "settings") await loadSettings();
    else if (currentTab === "users") await loadUsers();
    else if (currentTab === "history") await loadHistory();
  } catch (e) {
    if (e.status === 401) checkStatus();
    else toast(e.message, true);
  }
}

// --- queue ----------------------------------------------------------------------------

async function loadQueue() {
  const { queue } = await api("GET", "/api/admin/queue");
  $("queue-empty").classList.toggle("hidden", queue.length > 0);
  $("queue").replaceChildren(...queue.map((item, idx) =>
    el("li", {},
      el("span", { class: "pos" }, idx + 1),
      art(item.album_art),
      el("div", { class: "info" },
        el("div", {}, item.title, statusBadge(item), item.priority > 0 ? el("span", { class: "badge" }, "pinned") : null),
        el("div", { class: "sub" }, item.artists, " · ", fmtDuration(item.duration_ms)),
        el("div", { class: "sub" }, "Added by ", el("span", { class: "who" }, item.added_by)),
      ),
      item.status === "queued"
        ? el("div", { class: "actions" },
            el("button", { class: "secondary", title: "Move to top", onclick: () => action("POST", `/api/admin/queue/${item.id}/top`) }, "⤒"),
            el("button", { class: "danger", title: "Remove", onclick: () => action("DELETE", `/api/admin/queue/${item.id}`) }, "✕"))
        : null,
    ),
  ));
}

// --- settings -------------------------------------------------------------------------

async function loadSettings(force = false) {
  const s = await api("GET", "/api/admin/settings");
  settings = s;
  const form = $("settings-form");
  if (!force && form.contains(document.activeElement)) return; // don't clobber while editing
  const playbackRows = [modeSetting()];
  if (settings.playback_mode === "sonos") {
    playbackRows.push(el("div", { id: "sonos-rooms", class: "setting" }, el("span", { class: "muted" }, "Looking for Sonos speakers…")));
  } else {
    const { devices } = await api("GET", "/api/admin/devices");
    playbackRows.push(deviceSetting(devices));
  }
  form.replaceChildren(...playbackRows, ...SETTINGS.map(([key, label, help]) => {
    const isBool = typeof settings[key] === "boolean";
    const input = el("input", {
      id: "set-" + key,
      type: isBool ? "checkbox" : "number",
      min: isBool ? null : 0,
      checked: isBool && settings[key],
      value: isBool ? null : settings[key],
      onchange: () => saveSetting(key, isBool ? input.checked : Number(input.value)),
    });
    return el("div", { class: "setting" },
      el("label", { for: "set-" + key }, label, help ? el("span", { class: "help" }, help) : null),
      input);
  }));
  if (settings.playback_mode === "sonos") loadSonosRooms();
}

function modeSetting() {
  const select = el("select", {
    id: "set-playback_mode",
    onchange: async () => { await saveSetting("playback_mode", select.value); loadSettings(true); },
  },
    el("option", { value: "spotify", selected: settings.playback_mode === "spotify" }, "Spotify (computer, phone, Connect speakers)"),
    el("option", { value: "sonos", selected: settings.playback_mode === "sonos" }, "Sonos speakers"));
  return el("div", { class: "setting" },
    el("label", { for: "set-playback_mode" }, "Play through",
      el("span", { class: "help" }, "Sonos is controlled directly, since Spotify doesn't let apps control Sonos.")),
    select);
}

async function loadSonosRooms() {
  const box = $("sonos-rooms");
  try {
    const { rooms } = await api("GET", "/api/admin/sonos/rooms");
    renderSonosRooms(rooms);
  } catch (e) {
    box.replaceChildren(el("span", { style: "color:var(--danger)" }, e.message));
  }
}

function renderSonosRooms(rooms) {
  const box = $("sonos-rooms");
  if (!box) return;
  if (!rooms.length) {
    box.replaceChildren(el("span", { class: "muted" }, "No Sonos speakers found on this network."));
    return;
  }
  // Keep the saved order (the first room leads the group); newly ticked rooms go last.
  const order = settings.sonos_rooms.split(",").filter(Boolean);
  const list = el("div", { style: "display:grid;gap:6px" }, ...rooms.map((r) => {
    const box = el("input", {
      type: "checkbox", checked: r.selected,
      onchange: () => saveSonosRooms(box.checked ? [...order.filter((u) => u !== r.uid), r.uid] : order.filter((u) => u !== r.uid)),
    });
    const lead = order[0] === r.uid && r.selected;
    return el("label", { class: "row" }, box, `${r.name}`,
      el("span", { class: "muted" }, ` ${r.model || ""}`),
      lead ? el("span", { class: "badge accent" }, "leads group") : null,
      r.responding ? null : el("span", { class: "badge danger", title: "Unplug it for 10 seconds and plug it back in" }, "not responding"));
  }));
  box.replaceChildren(
    el("label", {}, "Sonos rooms", el("span", { class: "help" }, "Ticked rooms are grouped and play together.")),
    list);
}

async function saveSonosRooms(uids) {
  try {
    const { rooms } = await api("PUT", "/api/admin/sonos/rooms", { uids });
    settings.sonos_rooms = uids.join(",");
    renderSonosRooms(rooms);
    toast(uids.length ? "Rooms grouped" : "Saved");
  } catch (e) { toast(e.message, true); loadSonosRooms(); }
}

function deviceSetting(devices) {
  const options = [el("option", { value: "" }, "Spotify's active device")];
  for (const d of devices) {
    options.push(el("option", { value: d.id, selected: d.id === settings.device_id },
      `${d.name} (${d.type})${d.is_active ? " · active" : ""}`));
  }
  if (settings.device_id && !devices.some((d) => d.id === settings.device_id)) {
    options.push(el("option", { value: settings.device_id, selected: true }, "Saved device (offline)"));
  }
  const select = el("select", { id: "set-device_id", onchange: () => saveSetting("device_id", select.value) }, options);
  return el("div", { class: "setting" },
    el("label", { for: "set-device_id" }, "Playback device",
      el("span", { class: "help" }, "Where songs play. Pick one if Spotify lists several devices.")),
    select);
}

async function saveSetting(key, value) {
  try {
    settings = await api("PUT", "/api/admin/settings", { [key]: value });
    toast("Saved");
  } catch (e) { toast(e.message, true); }
}

// --- users ----------------------------------------------------------------------------

async function loadUsers() {
  const { users } = await api("GET", "/api/admin/users");
  $("users").replaceChildren(...users.map((u) =>
    el("tr", {},
      el("td", {}, u.name, u.blocked ? el("span", { class: "badge danger" }, "blocked") : null),
      el("td", {}, u.in_queue || 0),
      el("td", {}, u.total_added),
      el("td", {}, el("button", {
        class: u.blocked ? "secondary" : "danger",
        onclick: () => blockUser(u, !u.blocked),
      }, u.blocked ? "Unblock" : "Block")),
    )));
}

async function blockUser(user, blocked) {
  if (blocked && !confirm(`Block ${user.name}? Their waiting songs will be removed.`)) return;
  try {
    await api("POST", `/api/admin/users/${user.id}/block`, { blocked });
    loadUsers();
  } catch (e) { toast(e.message, true); }
}

// --- history --------------------------------------------------------------------------

async function loadHistory() {
  const { history } = await api("GET", "/api/admin/history");
  $("history").replaceChildren(...history.map((h) =>
    el("tr", {},
      el("td", {}, h.title, el("div", { class: "sub muted" }, h.artists)),
      el("td", {}, h.added_by),
      el("td", {}, new Date(h.added_at * 1000).toLocaleString()),
      el("td", {}, h.status),
    )));
}

// --- share link -------------------------------------------------------------------------

async function loadShare() {
  const s = await api("GET", "/api/share");
  $("share-url").textContent = s.friendly || "no network found";
  $("share-alt").textContent = s.ip && s.ip !== s.friendly
    ? `If that name doesn't open on someone's phone, use ${s.ip} (the QR code uses this one).`
    : "";
}

$("copy-url").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText($("share-url").textContent);
    toast("Copied");
  } catch { toast("Couldn't copy, select the link instead", true); }
});

// --- start ------------------------------------------------------------------------------

async function refresh() {
  if (await checkStatus()) {
    loadShare();
    refreshTab();
  }
}

refresh();
setInterval(async () => {
  if (await checkStatus() && currentTab !== "settings") refreshTab();
}, 4000);
