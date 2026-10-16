// Setup wizard: admin password, Spotify developer app, connecting the host's Spotify account.

const content = document.getElementById("content");

// The one-time setup token comes in the link printed by the server; keep it for this tab.
(function keepToken() {
  const params = new URLSearchParams(location.search);
  const fromUrl = params.get("token");
  let token = null;
  try {
    if (fromUrl) sessionStorage.setItem("setupToken", fromUrl);
    token = fromUrl || sessionStorage.getItem("setupToken");
  } catch { token = fromUrl; }
  if (token) api.headers["X-Setup-Token"] = token;
  if (fromUrl) history.replaceState(null, "", "/setup"); // keep it out of the address bar
})();

let status = null;
const editing = new Set(); // steps the user reopened with "Change"

async function load() {
  try {
    status = await api("GET", "/api/setup/status");
    render();
  } catch (e) {
    content.replaceChildren(el("p", { style: "color:var(--danger)" }, e.message));
  }
}

function render() {
  if (!status.allowed) {
    content.replaceChildren(el("section", { class: "card" }, status.configured
      ? [el("p", {}, "The jukebox is already set up."),
         el("p", {}, "To change these settings, ", el("a", { href: "/admin" }, "log in on the admin page"), " first, then come back here.")]
      : [el("p", {}, "This jukebox isn't set up yet."),
         el("p", { class: "muted" }, "Open the setup link shown in the window where the jukebox is running, on the computer that runs it. "
           + "(If you started it with run.bat, it should have opened in your browser by itself.)")]));
    return;
  }
  const steps = [passwordStep(), spotifyAppStep(), connectStep(), finishedStep()];
  content.replaceChildren(...steps);
}

function step(n, title, done, locked, body) {
  return el("section", { class: `card step${done ? " done" : ""}${locked ? " locked" : ""}` },
    el("h2", {}, el("span", { class: "num" }, done ? "✓" : n), title),
    ...(locked ? [el("p", { class: "muted" }, "Finish the step above first.")] : body));
}

function changeLink(key) {
  return el("button", { class: "link", onclick: () => { editing.add(key); render(); } }, "Change");
}

// --- 1. admin password ------------------------------------------------------------------

function passwordStep() {
  const done = status.has_password;
  if (done && !editing.has("password")) {
    return step(1, "Admin password", true, false,
      [el("p", {}, el("span", { class: "done-line" }, "Set. "), "You use it to log in on the admin page. ", changeLink("password"))]);
  }
  const pw = el("input", { type: "password", id: "pw", autocomplete: "new-password", minlength: 6, required: true });
  const pw2 = el("input", { type: "password", id: "pw2", autocomplete: "new-password", required: true });
  const form = el("form", {
    onsubmit: async (ev) => {
      ev.preventDefault();
      if (pw.value !== pw2.value) return toast("The passwords don't match", true);
      try {
        await api("POST", "/api/setup/password", { password: pw.value });
        editing.delete("password");
        toast("Password saved");
        load();
      } catch (e) { toast(e.message, true); }
    },
  },
    el("div", { class: "field" }, el("label", { for: "pw" }, "Password"), pw),
    el("div", { class: "field" }, el("label", { for: "pw2" }, "Repeat password"), pw2),
    el("button", { type: "submit" }, "Save password"));
  return step(1, "Choose an admin password", false, false, [
    el("p", { class: "muted" }, "The admin page controls the music and settings. Guests never need this password. At least 6 characters."),
    form,
  ]);
}

// --- 2. Spotify developer app -------------------------------------------------------------

function copyBox(text) {
  return el("div", { class: "copy" }, el("code", {}, text),
    el("button", { class: "secondary", type: "button", onclick: async () => {
      try { await navigator.clipboard.writeText(text); toast("Copied"); }
      catch { toast("Couldn't copy, select the text instead", true); }
    } }, "Copy"));
}

function spotifyAppStep() {
  const done = status.has_spotify_app;
  const locked = !status.has_password;
  if (done && !editing.has("app")) {
    return step(2, "Spotify app", true, locked,
      [el("p", {}, el("span", { class: "done-line" }, "Saved and checked with Spotify. "), changeLink("app"))]);
  }
  const id = el("input", { type: "text", id: "cid", autocomplete: "off", spellcheck: "false", value: status.client_id || "", placeholder: "32 characters, e.g. 1a2b3c…" });
  const secret = el("input", { type: "password", id: "csecret", autocomplete: "off", spellcheck: "false", placeholder: "32 characters" });
  const save = el("button", { type: "submit" }, "Check and save");
  const form = el("form", {
    onsubmit: async (ev) => {
      ev.preventDefault();
      save.disabled = true;
      save.textContent = "Checking with Spotify…";
      try {
        await api("POST", "/api/setup/spotify-app", { client_id: id.value, client_secret: secret.value });
        editing.delete("app");
        toast("Spotify app saved");
        load();
      } catch (e) {
        toast(e.message, true);
        save.disabled = false;
        save.textContent = "Check and save";
      }
    },
  },
    el("div", { class: "field" }, el("label", { for: "cid" }, "Client ID"), id),
    el("div", { class: "field" }, el("label", { for: "csecret" }, "Client secret"), secret),
    save);

  return step(2, "Create a Spotify app", false, locked, [
    el("p", {}, "The jukebox needs its own app registration at Spotify to search and control playback. It's free and takes a few minutes."),
    el("div", { class: "note" }, "You need ", el("strong", {}, "Spotify Premium"),
      " on the account that will play the music. Spotify doesn't allow apps to control playback on free accounts."),
    el("ol", {},
      el("li", {}, "Open the ", el("a", { href: "https://developer.spotify.com/dashboard", target: "_blank", rel: "noopener" }, "Spotify Developer Dashboard"),
        " and log in with your normal Spotify account. The first time, accept the developer terms."),
      el("li", {}, "Click ", el("strong", {}, "Create app"), "."),
      el("li", {}, el("strong", {}, "App name"), " and ", el("strong", {}, "App description"), ": anything, for example “Jukebox”."),
      el("li", {}, el("strong", {}, "Redirect URIs"), ": paste this exactly and click ", el("strong", {}, "Add"), ":",
        copyBox(status.redirect_uri)),
      el("li", {}, "Under “Which API/SDKs are you planning to use?” tick only ", el("strong", {}, "Web API"), "."),
      el("li", {}, "Accept the terms and click ", el("strong", {}, "Save"), "."),
      el("li", {}, "On your new app's page, open ", el("strong", {}, "Settings"), ". Copy the ", el("strong", {}, "Client ID"),
        ", then click ", el("strong", {}, "View client secret"), " and copy that too. Paste both below."),
    ),
    el("div", { class: "note" }, "New Spotify apps start in development mode. That's all the jukebox needs: only your account logs in to Spotify, "
      + "and guests never do. If the account that plays the music isn't the one that created the app, add it under ",
      el("strong", {}, "User Management"), " on the app's page."),
    form,
  ]);
}

// --- 3. connect the host's Spotify account ---------------------------------------------

function connectStep() {
  const locked = !status.has_spotify_app || !status.has_password;
  if (status.spotify_connected) {
    return step(3, "Connect your Spotify account", true, locked,
      [el("p", {}, el("span", { class: "done-line" }, "Connected. "),
        el("a", { href: "/login?next=setup" }, "Connect a different account"))]);
  }
  const body = [el("p", {}, "Log in with the Spotify account that will play the music. You'll come back here afterwards.")];
  if (status.on_loopback) {
    body.push(el("a", { href: "/login?next=setup" }, el("button", {}, "Connect Spotify")));
  } else {
    body.push(el("div", { class: "note" }, "Spotify only sends you back to this computer's own address, so do this step on the computer running the jukebox: open ",
      el("a", { href: status.admin_url }, status.admin_url), " there, log in and click Connect Spotify."));
  }
  return step(3, "Connect your Spotify account", false, locked, body);
}

// --- done -----------------------------------------------------------------------------

function finishedStep() {
  const ready = status.has_password && status.has_spotify_app && status.spotify_connected;
  return step(4, "Start the music", false, !ready, [
    el("p", {}, "All set! On the admin page you can choose where the music plays (this computer, your phone or Sonos speakers), "
      + "set the rules, and find the link and QR code to share with guests."),
    el("div", { class: "row", style: "flex-wrap:wrap" },
      el("a", { href: "/admin" }, el("button", {}, "Open the admin page")),
      el("a", { href: "/" }, el("button", { class: "secondary" }, "Open the guest page"))),
  ]);
}

load();
