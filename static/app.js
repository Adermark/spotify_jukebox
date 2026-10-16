// Guest page: join, search, add songs, and watch the shared queue.

const $ = (id) => document.getElementById(id);
const POLL_MS = 3000;

let me = null;
let lastState = null;
let nowFetchedAt = 0; // local time when now_playing was last received

async function loadMe() {
  me = (await api("GET", "/api/me")).user;
  $("join").classList.toggle("hidden", !!me);
  $("whoami").replaceChildren();
  if (me) {
    $("whoami").append(
      `You are ${me.name} `,
      el("button", { class: "link", onclick: rename }, "change"),
    );
    if (me.blocked) toast("You have been blocked by the host", true);
  }
}

async function rename() {
  const name = prompt("New name", me ? me.name : "");
  if (!name) return;
  try {
    await api("POST", "/api/join", { name });
    await loadMe();
    refresh();
  } catch (e) { toast(e.message, true); }
}

$("join-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    await api("POST", "/api/join", { name: $("join-name").value });
    await loadMe();
    refresh();
    $("search").focus();
  } catch (e) { toast(e.message, true); }
});

// --- state polling -------------------------------------------------------------

async function refresh() {
  try {
    lastState = await api("GET", "/api/state");
    nowFetchedAt = Date.now();
    render();
  } catch (e) {
    console.warn("state refresh failed", e);
  }
}

function render() {
  const s = lastState;
  $("not-connected").classList.toggle("hidden", s.connected);
  $("search-section").classList.toggle("hidden", !me || !s.connected || me.blocked);

  renderNow(s.now_playing);
  renderQueue(s.queue);

  const max = s.rules.max_songs_per_user;
  let info = !s.rules.requests_open
    ? "Requests are closed right now."
    : max ? `You have ${s.my_count} of ${max} songs in the queue.` : "";
  $("limit-info").textContent = info;
}

function renderNow(np) {
  const box = $("now");
  if (!np) {
    box.replaceChildren(el("p", { class: "muted" }, "Nothing playing right now."));
    return;
  }
  box.replaceChildren(
    el("div", { class: "now" },
      art(np.album_art),
      el("div", { class: "info", style: "flex:1;min-width:0" },
        el("div", { class: "title" }, np.title),
        el("div", { class: "muted" }, np.artists),
        np.added_by ? el("div", {}, "Added by ", el("span", { class: "who" }, np.added_by)) : null,
        np.is_playing ? null : el("div", { class: "muted" }, "Paused"),
        el("div", { class: "progress" }, el("div", { id: "progress-bar" })),
      ),
    ),
  );
  tickProgress();
}

function tickProgress() {
  const np = lastState && lastState.now_playing;
  const bar = $("progress-bar");
  if (!np || !bar) return;
  const elapsed = np.is_playing ? Date.now() - nowFetchedAt : 0;
  const pct = Math.min(100, ((np.progress_ms + elapsed) / np.duration_ms) * 100);
  bar.style.width = pct + "%";
}
setInterval(tickProgress, 500);

function renderQueue(queue) {
  $("queue-count").textContent = queue.length || "";
  $("queue-empty").classList.toggle("hidden", queue.length > 0);
  $("queue").replaceChildren(...queue.map((item, idx) =>
    el("li", { class: item.mine ? "mine" : "" },
      el("span", { class: "pos" }, idx + 1),
      art(item.album_art),
      el("div", { class: "info" },
        el("div", {}, item.title, statusBadge(item)),
        el("div", { class: "sub" }, item.artists, " · ", fmtDuration(item.duration_ms)),
        el("div", { class: "sub" }, "Added by ", el("span", { class: "who" }, item.added_by), item.mine ? " (you)" : ""),
      ),
      item.mine && item.status === "queued"
        ? el("div", { class: "actions" },
            el("button", { class: "danger", title: "Remove", onclick: () => removeSong(item.id) }, "✕"))
        : null,
    ),
  ));
}

async function removeSong(id) {
  try {
    await api("DELETE", `/api/queue/${id}`);
    refresh();
  } catch (e) { toast(e.message, true); }
}

// --- search ----------------------------------------------------------------------

let searchTimer;
let searchSeq = 0;
$("search").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(runSearch, 350);
});

async function runSearch() {
  const q = $("search").value.trim();
  const seq = ++searchSeq;
  if (!q) { $("results").replaceChildren(); return; }
  try {
    const { results } = await api("GET", `/api/search?q=${encodeURIComponent(q)}`);
    if (seq !== searchSeq) return; // a newer search has started
    $("results").replaceChildren(...results.map((t) =>
      el("li", {},
        art(t.album_art),
        el("div", { class: "info" },
          el("div", {}, t.title, t.explicit ? el("span", { class: "badge" }, "E") : null),
          el("div", { class: "sub" }, t.artists, " · ", fmtDuration(t.duration_ms)),
        ),
        el("div", { class: "actions" },
          el("button", { onclick: (ev) => addSong(t, ev.currentTarget) }, "Add")),
      ),
    ));
  } catch (e) { toast(e.message, true); }
}

async function addSong(track, button) {
  button.disabled = true;
  try {
    await api("POST", "/api/queue", { uri: track.uri });
    toast(`Added “${track.title}”`);
    $("search").value = "";
    $("results").replaceChildren();
    refresh();
  } catch (e) {
    toast(e.message, true);
    button.disabled = false;
  }
}

// --- start -------------------------------------------------------------------------

loadMe().then(refresh);
setInterval(refresh, POLL_MS);
