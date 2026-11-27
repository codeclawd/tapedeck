// tapedeck web console. Vanilla JS, no build step. Every catalog string is inserted with textContent (no innerHTML).
"use strict";

const $ = (sel) => document.querySelector(sel);
const view = $("#view");
let TOKEN = localStorage.getItem("tapedeck.token") || "";

// ---------- tiny DOM helper ----------
function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "text") el.textContent = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  return el;
}
const icon = (name) => h("i", { class: `ph ph-${name}` });
const fmt = (s) => (s ? `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}` : "");
const artistsOf = (t) => (t.artists || []).join(", ");

// ---------- API ----------
async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: { Authorization: `Bearer ${TOKEN}`, ...(opts.body ? { "Content-Type": "application/json" } : {}) },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (res.status === 401) { signOut(); throw new Error("Signed out"); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}
const cover = (t, s = 300) => (t && t.cover ? `/api/cover?s=${s}&t=${encodeURIComponent(TOKEN)}&u=${encodeURIComponent(t.cover)}` : "");
const streamUrl = (vid) => `/api/stream/${vid}?t=${encodeURIComponent(TOKEN)}`;

function toast(msg, isError = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.toggle("error", isError);
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (el.hidden = true), 3200);
}

// ---------- auth ----------
function signOut() {
  if (TOKEN) fetch("/api/logout", { method: "POST", headers: { Authorization: `Bearer ${TOKEN}` } }).catch(() => {});
  closeFs();
  closeQueue();
  localStorage.removeItem("tapedeck.token");
  TOKEN = "";
  $("#app").hidden = true;
  $("#login").hidden = false;
}
$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    const res = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: $("#username").value.trim(), password: $("#password").value }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    TOKEN = data.token;
    $("#password").value = "";
    localStorage.setItem("tapedeck.token", TOKEN);
    start();
  } catch (err) {
    $("#login-error").textContent = err.message;
    $("#login-error").hidden = false;
  }
});

// ---------- player ----------
const audio = $("#audio");
const P = { queue: [], i: -1, sentPlay: false, sentFinish: false, auto: new Set(), fetching: false, playWhenFetched: false };
const current = () => P.queue[P.i];

function event(track, kind) {
  if (!track || !track.video_id) return;
  api("/api/events", { method: "POST", body: { track, event: kind } }).catch(() => {});
}

function leaveCurrent() {
  const t = current();
  if (!t || !P.sentPlay || P.sentFinish) return;
  const pos = audio.currentTime || 0;
  if (pos < 30) event(t, "skip");  // Spotify's rule: a listen past 30 s is a real listen, never a skip
}

function playList(tracks, i = 0) {
  const songs = tracks.filter((t) => t.video_id);
  if (!songs.length) return;
  const start = Math.max(0, songs.indexOf(tracks[i]));
  leaveCurrent();
  P.queue = songs;
  P.auto = new Set();
  P.playWhenFetched = false;
  load(start);
}
// Autoplay: with two songs or fewer left, ask tapedeck what follows the last one (Up Next re-ranked by your taste).
function topUp(thenPlay = false) {
  const last = P.queue[P.queue.length - 1];
  // A request already in flight plays its first new track when it lands, so Next at the end never goes nowhere.
  if (P.fetching) { if (thenPlay) P.playWhenFetched = true; return; }
  if (!last || !last.video_id) { if (thenPlay) audio.pause(); return; }
  P.fetching = true;
  P.playWhenFetched = thenPlay;
  const exclude = P.queue.map((x) => x.video_id).slice(-150).join(",");
  const q = P.queue;
  api(`/api/autoplay/${last.video_id}?limit=10&exclude=${encodeURIComponent(exclude)}`).then((more) => {
    if (q !== P.queue || P.i < 0) {
      // A new list started (or the queue was emptied) while this was in flight: these picks belong to the old one.
      P.playWhenFetched = P.fetching = false;
      if (P.i >= 0 && P.queue.length - 1 - P.i <= 2) topUp();
      return;
    }
    const have = new Set(P.queue.map((x) => x.video_id));
    const fresh = (more || []).filter((t) => t.video_id && !have.has(t.video_id));
    fresh.forEach((t) => P.auto.add(t.video_id));
    const atEnd = P.i + 1 >= P.queue.length;
    P.queue.push(...fresh);
    renderQueue();  // an open queue panel must show the tracks Autoplay just added
    const play = P.playWhenFetched;
    P.playWhenFetched = false;
    P.fetching = false;
    if (play && atEnd && fresh.length) load(P.i + 1);
    else if (play && !fresh.length) audio.pause();
  }).catch(() => { if (P.playWhenFetched) audio.pause(); P.playWhenFetched = false; P.fetching = false; });
}
function enqueue(t, next = false) {
  if (!t.video_id) return;
  if (P.i < 0) return playList([t]);
  if (next) {
    if (current().video_id === t.video_id) return toast(`Already playing: ${t.title}`);
    const from = P.queue.findIndex((x, k) => k !== P.i && x.video_id === t.video_id);
    if (from >= 0) { playNext(from); return; }  // already queued: move it, never duplicate it
  }
  if (!next) {
    // Add to queue never makes a second copy: if it is already queued (or playing), leave it where it is.
    if (current().video_id === t.video_id) return toast(`Already playing: ${t.title}`);
    if (P.queue.some((x, k) => k !== P.i && x.video_id === t.video_id)) return toast(`Already in the queue: ${t.title}`);
  }
  P.queue.splice(next ? P.i + 1 : P.queue.length, 0, t);
  renderQueue();
  toast(next ? `Playing next: ${t.title}` : `Added to queue: ${t.title}`);
}
// Queue editing. `to` is the final index after the move; P.i follows the track that is playing.
function moveInQueue(from, to) {
  if (from === to || from < 0 || to < 0 || from >= P.queue.length || to >= P.queue.length) return;
  const [t] = P.queue.splice(from, 1);
  P.queue.splice(to, 0, t);
  if (P.i === from) P.i = to;
  else if (from < P.i && to >= P.i) P.i--;
  else if (from > P.i && to <= P.i) P.i++;
  renderQueue();
}
function playNext(from) {
  if (from === P.i || from < 0 || from >= P.queue.length) return;
  const t = P.queue[from];
  moveInQueue(from, from < P.i ? P.i : P.i + 1);
  toast(`Playing next: ${t.title}`);
}
function removeFromQueue(k) {
  if (k < 0 || k >= P.queue.length) return;
  if (k !== P.i) { P.queue.splice(k, 1); if (k < P.i) P.i--; renderQueue(); return; }
  const playing = !audio.paused;
  leaveCurrent();
  P.queue.splice(k, 1);
  if (k < P.queue.length) return load(k, playing);  // the next track takes this slot; keep playing if it was playing
  if (!P.queue.length) {
    P.i = -1;
    audio.pause();
    audio.removeAttribute("src");
    audio.load();
    renderNow();
    return;
  }
  load(k - 1, false);  // nothing after it: cue the last track, and let Autoplay continue if we were playing
  if (playing) topUp(true);
}
function load(i, autoplay = true) {
  if (i < 0 || i >= P.queue.length) return;
  P.i = i;
  P.sentPlay = P.sentFinish = false;
  const t = current();
  audio.src = streamUrl(t.video_id);
  if (autoplay) {
    const upNext = P.queue.slice(i + 1, i + 3).map((x) => x.video_id).filter(Boolean);
    if (upNext.length) api("/api/prefetch", { method: "POST", body: { video_ids: upNext } }).catch(() => {});
    audio.play().catch((err) => toast(`Can't play: ${err.message}`, true));
  } else setPlayIcon(false);
  renderNow();
  if (autoplay && P.queue.length - 1 - P.i <= 2) topUp();
}
// Footer + full-screen player show whatever is current (or nothing).
function renderNow() {
  const t = current();
  $("#np-title").textContent = t ? t.title : "Nothing playing";
  $("#np-artist").textContent = t ? artistsOf(t) : "";
  const img = $("#np-cover");
  img.hidden = !(t && t.cover);
  if (t && t.cover) img.src = cover(t, 120);
  $("#np-like").disabled = !t;
  $("#np-like").classList.toggle("on", !!(t && t.liked));
  $("#fs-like").disabled = !t;
  $("#fs-like").classList.toggle("on", !!(t && t.liked));
  $("#fs-title").textContent = t ? t.title : "Nothing playing";
  $("#fs-artist").textContent = t ? artistsOf(t) : "";
  const art = $("#fs-art");
  art.hidden = !(t && t.cover);  // no cover: the plain panel behind it shows, never a broken image
  if (t && t.cover) art.src = cover(t, 800);
  document.title = t ? `${t.title} · tapedeck` : "tapedeck";
  if ("mediaSession" in navigator) {
    navigator.mediaSession.metadata = t ? new MediaMetadata({
      title: t.title, artist: artistsOf(t), album: t.album || "",
      artwork: t.cover ? [{ src: cover(t, 512), sizes: "512x512", type: "image/jpeg" }] : [],
    }) : null;
  }
  markPlaying();
  renderQueue();
}
function setPlayIcon(playing) {
  for (const b of [$("#toggle"), $("#fs-toggle")]) { b.replaceChildren(icon(playing ? "pause" : "play")); b.title = playing ? "Pause" : "Play"; }
}
function next() { leaveCurrent(); if (P.i + 1 < P.queue.length) load(P.i + 1); else topUp(true); }
function prev() { if (audio.currentTime > 4) { audio.currentTime = 0; return; } leaveCurrent(); load(Math.max(0, P.i - 1)); }
function markPlaying() {
  const vid = current() && current().video_id;
  document.querySelectorAll("tr[data-vid]").forEach((tr) => tr.classList.toggle("playing", tr.dataset.vid === vid));
}

audio.addEventListener("playing", () => {
  setPlayIcon(true);
  if (!P.sentPlay) { P.sentPlay = true; event(current(), "play"); }
});
audio.addEventListener("pause", () => setPlayIcon(false));
audio.addEventListener("timeupdate", () => {
  const d = audio.duration || 0, c = audio.currentTime || 0;
  syncPosition();
  for (const id of ["", "fs-"]) {
    $(`#${id}t-cur`).textContent = fmt(c) || "0:00";
    $(`#${id}t-dur`).textContent = fmt(d) || "0:00";
    if (!seeking) $(`#${id}seek`).value = d ? Math.round((c / d) * 1000) : 0;
  }
  if (d && c / d >= 0.9 && !P.sentFinish) { P.sentFinish = true; event(current(), "finish"); }
});
audio.addEventListener("ended", () => {
  if (!P.sentFinish) { P.sentFinish = true; event(current(), "finish"); }
  next();
});
audio.addEventListener("error", () => {
  if (current()) toast(`Stream failed for ${current().title}. The server couldn't fetch it from YouTube.`, true);
});
let seeking = false;
for (const id of ["#seek", "#fs-seek"]) {
  $(id).addEventListener("input", () => (seeking = true));
  $(id).addEventListener("change", (e) => { if (audio.duration) audio.currentTime = (e.target.value / 1000) * audio.duration; seeking = false; });
}
const togglePlay = () => (audio.paused ? (P.i >= 0 && audio.play()) : audio.pause());
$("#toggle").addEventListener("click", togglePlay);
$("#fs-toggle").addEventListener("click", togglePlay);
$("#next").addEventListener("click", next);
$("#fs-next").addEventListener("click", next);
$("#prev").addEventListener("click", prev);
$("#fs-prev").addEventListener("click", prev);
// One volume: both sliders drive audio.volume, and both follow it.
for (const id of ["#volume", "#fs-volume"]) $(id).addEventListener("input", (e) => (audio.volume = e.target.value / 100));
audio.addEventListener("volumechange", () => { for (const id of ["#volume", "#fs-volume"]) $(id).value = Math.round(audio.volume * 100); });
audio.volume = 0.9;
$("#np-like").addEventListener("click", () => toggleLike(current()));
$("#fs-like").addEventListener("click", () => toggleLike(current()));
// iOS ignores audio.volume, so the full-screen volume slider would do nothing there. CSS hides it under html.ios.
if (/iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1)) document.documentElement.classList.add("ios");
// Lock screen position bar. setPositionState throws on a non-finite duration, so guard it.
function syncPosition() {
  if (!("mediaSession" in navigator) || !navigator.mediaSession.setPositionState) return;
  const d = audio.duration;
  if (!Number.isFinite(d) || d <= 0) return;
  try { navigator.mediaSession.setPositionState({ duration: d, position: Math.min(Math.max(audio.currentTime || 0, 0), d), playbackRate: audio.playbackRate || 1 }); } catch (e) { /* ignore */ }
}
audio.addEventListener("durationchange", syncPosition);
audio.addEventListener("ratechange", syncPosition);
audio.addEventListener("seeked", syncPosition);
if ("mediaSession" in navigator) {
  const ms = navigator.mediaSession, set = (n, f) => { try { ms.setActionHandler(n, f); } catch (e) { /* unsupported action */ } };
  set("nexttrack", next);
  set("previoustrack", prev);
  set("play", () => { if (audio.paused) togglePlay(); });
  set("pause", () => { if (!audio.paused) togglePlay(); });
  set("seekto", (d) => { if (d && Number.isFinite(d.seekTime)) { audio.currentTime = d.seekTime; syncPosition(); } });
}
document.addEventListener("keydown", (e) => {
  if (e.code === "Space" && !/INPUT|SELECT|TEXTAREA/.test(document.activeElement.tagName)) { e.preventDefault(); $("#toggle").click(); }
});

// ---------- queue panel ----------
const queuePanel = $("#queue"), queueList = $("#queue-list");
function openQueue() { queuePanel.hidden = false; renderQueue(); $("#queue-close").focus(); }
function closeQueue() { if (queuePanel.hidden) return; queuePanel.hidden = true; }
function toggleQueue() { if (queuePanel.hidden) openQueue(); else closeQueue(); }
function renderQueue() {
  if (queuePanel.hidden) return;
  $("#queue-count").textContent = P.queue.length ? `${P.queue.length - P.i - 1 < 0 ? 0 : P.queue.length - P.i - 1} up next` : "";
  if (!P.queue.length) { queueList.replaceChildren(h("li", { class: "q-empty muted", text: "Nothing queued. Play a song, or use Play next or Add to queue from a song's menu." })); return; }
  queueList.replaceChildren(...P.queue.map((t, k) => queueRow(t, k)));
}
function queueRow(t, k) {
  const grip = h("button", { class: "icon q-grip", title: "Drag to reorder (or Up and Down arrow keys)", "aria-label": `Reorder ${t.title}` }, icon("dots-six-vertical"));
  grip.addEventListener("pointerdown", (e) => dragRow(e, grip, k));
  grip.addEventListener("keydown", (e) => {
    const to = e.key === "ArrowUp" ? k - 1 : e.key === "ArrowDown" ? k + 1 : null;
    if (to == null) return;
    e.preventDefault();
    moveInQueue(k, to);
    const g = queueList.children[Math.max(0, Math.min(to, P.queue.length - 1))];
    if (g) g.querySelector(".q-grip").focus();
  });
  const thumb = t.cover ? h("img", { class: "q-thumb", src: cover(t, 80), alt: "" }) : h("div", { class: "q-thumb" });
  if (t.cover) thumb.addEventListener("error", () => thumb.replaceWith(h("div", { class: "q-thumb" })));
  return h("li", { class: `q-row${k === P.i ? " now" : ""}`, "data-k": k },
    grip, thumb,
    h("button", { class: "q-text", title: "Play this track", onclick: () => { if (k !== P.i) { leaveCurrent(); load(k); } else togglePlay(); } },
      h("div", { class: "ttl", text: t.title }), h("div", { class: "sub", text: artistsOf(t) })),
    k === P.i ? h("span", { class: "pill done", text: "Now" })
      : h("button", { class: "icon", title: "Play next", "aria-label": `Play ${t.title} next`, disabled: k === P.i + 1, onclick: () => playNext(k) }, icon("skip-forward")),
    h("button", { class: "icon", title: "Remove from queue", "aria-label": `Remove ${t.title}`, onclick: () => removeFromQueue(k) }, icon("x")));
}
// Drag with pointer events (works for mouse and touch); the row follows the pointer, the rest slide aside.
function dragRow(e, grip, k) {
  if (e.button) return;
  e.preventDefault();
  const rows = [...queueList.children], rects = rows.map((r) => r.getBoundingClientRect());
  const row = rows[k], startY = e.clientY, ht = rects[k].height;
  let to = k;
  grip.setPointerCapture(e.pointerId);
  row.classList.add("dragging");
  const move = (ev) => {
    const dy = ev.clientY - startY, centre = rects[k].top + ht / 2 + dy;
    row.style.transform = `translateY(${dy}px)`;
    to = rects.filter((r, j) => j !== k && r.top + r.height / 2 < centre).length;
    rows.forEach((r, j) => {
      if (j === k) return;
      r.style.transform = k < to && j > k && j <= to ? `translateY(${-ht}px)` : k > to && j >= to && j < k ? `translateY(${ht}px)` : "";
    });
  };
  const done = (ev) => {
    grip.removeEventListener("pointermove", move);
    grip.removeEventListener("pointerup", done);
    grip.removeEventListener("pointercancel", done);
    rows.forEach((r) => (r.style.transform = ""));
    row.classList.remove("dragging");
    if (ev.type === "pointerup") moveInQueue(k, to);
  };
  grip.addEventListener("pointermove", move);
  grip.addEventListener("pointerup", done);
  grip.addEventListener("pointercancel", done);
}
$("#queue-btn").addEventListener("click", toggleQueue);
$("#fs-queue").addEventListener("click", toggleQueue);
$("#queue-close").addEventListener("click", () => { closeQueue(); (fsOpen() ? $("#fs-queue") : $("#queue-btn")).focus(); });

// ---------- full-screen player ----------
const fs = $("#fs");
let fsReturn = null;
const fsOpen = () => !fs.hidden;
function openFs() {
  if (P.i < 0 || fsOpen()) return;
  closeMenu();
  fsReturn = document.activeElement;
  fs.hidden = false;
  $("#app").inert = true;  // keep focus and clicks inside the player until it closes
  $("#fs-close").focus();
}
function closeFs() {
  if (!fsOpen()) return;
  closeQueue();
  fs.hidden = true;
  $("#app").inert = false;
  if (fsReturn && fsReturn.isConnected) fsReturn.focus();
  fsReturn = null;
}
$("#expand").addEventListener("click", openFs);
$("#np-open").addEventListener("click", openFs);
$("#fs-close").addEventListener("click", closeFs);
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (fsOpen()) closeFs();  // the full-screen player owns Escape while it is open
  else closeQueue();
});

function toggleLike(t) {
  if (!t) return;
  t.liked = !t.liked;
  event(t, t.liked ? "like" : "unlike");
  if (t === current()) { $("#np-like").classList.toggle("on", t.liked); $("#fs-like").classList.toggle("on", t.liked); }
  toast(t.liked ? `Liked ${t.title}` : `Removed like from ${t.title}`);
}

// ---------- actions ----------
async function download(items, label) {
  try {
    const job = await api("/api/downloads", { method: "POST", body: { items, label } });
    toast(`Queued download #${job.id}: ${job.label}`);
    pollBusy();
  } catch (err) { toast(err.message, true); }
}
async function hide(rule) {
  try {
    await api("/api/private", { method: "POST", body: rule });
    toast(`Hidden from agents: ${rule.label}`);
  } catch (err) { toast(err.message, true); }
}

function openMenu(anchor, t) {
  const m = $("#menu");
  m.replaceChildren();
  const item = (ic, label, fn) => h("button", { onclick: () => { closeMenu(); fn(); } }, icon(ic), label);
  if (t.kind === "song" || t.video_id) {
    m.append(
      item("play", "Play", () => playList([t])),
      item("skip-forward", "Play next", () => enqueue(t, true)),
      item("plus", "Add to queue", () => enqueue(t)),
      item("radio", "Start radio", () => startRadio(t)),
      item("heart", t.liked ? "Remove like" : "Like", () => toggleLike(t)),
      t.owned ? null : item("download-simple", "Download", () => download([{ video_id: t.video_id, title: t.title, artist: artistsOf(t) }])),
      h("hr"),
      item("eye-slash", "Hide song from agents", () => hide({ kind: "song", key: t.video_id, label: `${t.title} by ${artistsOf(t)}` })),
    );
  }
  if (t.album_id) {
    m.append(item("download-simple", "Download album", () => download([{ album_id: t.album_id }], t.album || t.title)),
      item("eye-slash", "Hide album from agents", () => hide({ kind: "album", key: t.album_id, ref: t.album || t.title, label: t.album || t.title })));
  }
  const artist = t.kind === "artist" ? t.title : (t.artists || [])[0];
  if (artist) m.append(item("eye-slash", `Hide ${artist} from agents`, () => hide({ kind: "artist", key: artist, ref: t.artist_id, label: artist })));
  const r = anchor.getBoundingClientRect();
  m.hidden = false;
  m.style.left = `${Math.min(r.left, innerWidth - m.offsetWidth - 8)}px`;
  m.style.top = `${Math.min(r.bottom + 4, innerHeight - m.offsetHeight - 8)}px`;
}
function closeMenu() { $("#menu").hidden = true; }
document.addEventListener("click", (e) => { if (!e.target.closest("#menu") && !e.target.closest("[data-menu]")) closeMenu(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeMenu(); });

async function startRadio(t) {
  try {
    const tracks = await api(`/api/radio/${t.video_id}`);
    playList(tracks, 0);
    toast(`Radio from ${t.title}`);
  } catch (err) { toast(err.message, true); }
}

// ---------- building blocks ----------
function open(t, list) {
  if (t.kind === "album") location.hash = `#/album/${t.album_id}`;
  else if (t.kind === "artist") location.hash = `#/artist/${t.artist_id}`;
  else if (t.kind === "playlist") location.hash = `#/playlist/${t.playlist_id}`;
  else playList(list, list.indexOf(t));
}
function card(t, list) {
  const play = t.video_id ? h("button", { class: "play", title: "Play", onclick: (e) => { e.stopPropagation(); open(t, list); } }, icon("play")) : null;
  return h("div", { class: `card ${t.kind}`, tabindex: 0, onclick: () => open(t, list), onkeydown: (e) => e.key === "Enter" && open(t, list),
    oncontextmenu: (e) => { e.preventDefault(); openMenu(e.currentTarget, t); } },
    h("div", { class: "art" }, t.cover ? h("img", { src: cover(t), alt: "", loading: "lazy" }) : null, play),
    h("div", { class: "t", text: t.title }),
    h("div", { class: "a", text: t.kind === "artist" ? "Artist" : t.kind === "playlist" ? (artistsOf(t) || "Playlist") : artistsOf(t) }));
}
function shelf(s) {
  return h("section", { class: "shelf" },
    h("h2", { text: s.title }), h("div", { class: "sub", text: s.subtitle || "" }),
    h("div", { class: "row" }, s.items.map((t) => card(t, s.items))));
}
// alone: search results aren't a playlist, so a song plays by itself and Autoplay picks what matches it.
function trackTable(tracks, { numbers = false, alone = false } = {}) {
  const playAt = (i) => alone ? playList([tracks[i]]) : playList(tracks, i);
  if (!tracks.length) return empty("music-notes", "No songs here.");
  return h("table", { class: "tracks" }, h("tbody", {}, tracks.map((t, i) => {
    const menuBtn = h("button", { class: "icon", title: "More", "data-menu": true, onclick: (e) => { e.stopPropagation(); openMenu(e.currentTarget, t); } }, icon("dots-three"));
    const dl = t.owned ? h("span", { class: "icon owned", title: "In your library" }, icon("check-circle"))
      : t.video_id ? h("button", { class: "icon", title: "Download", onclick: (e) => { e.stopPropagation(); download([{ video_id: t.video_id, title: t.title, artist: artistsOf(t) }]); } }, icon("download-simple")) : null;
    return h("tr", { "data-vid": t.video_id || "", ondblclick: () => playAt(i) },
      h("td", { class: "mono", text: numbers ? i + 1 : "" }, numbers ? null : h("button", { class: "icon", title: "Play", onclick: () => playAt(i) }, icon("play"))),
      h("td", {}, t.cover ? h("img", { class: "thumb", src: cover(t, 80), alt: "", loading: "lazy" }) : null),
      h("td", {}, h("div", { class: "ttl", text: t.title }), h("div", { class: "sub", text: artistsOf(t) })),
      h("td", { class: "sub hide-sm", text: t.album || "" }),
      h("td", { class: "dur mono", text: fmt(t.duration) }),
      h("td", {}, dl, menuBtn));
  })));
}
function empty(ic, msg, ...extra) { return h("div", { class: "empty" }, icon(ic), h("div", { text: msg }), ...extra); }
function loadingShelves() {
  return [0, 1].map(() => h("section", { class: "shelf skeleton" }, h("h2", { text: " " }),
    h("div", { class: "row" }, Array.from({ length: 6 }, () => h("div", { class: "card" }, h("div", { class: "art" }), h("div", { class: "t" }), h("div", { class: "a" }))))));
}
function failed(err, retry) {
  return empty("warning", err.message, h("div", { style: "margin-top:12px" }, h("button", { class: "btn", onclick: retry }, icon("arrow-clockwise"), "Try again")));
}
function page(title, ...tools) { return h("div", { class: "toolbar" }, h("h1", { text: title }), ...tools); }

// ---------- views ----------
async function vDiscover(refresh = false) {
  const btn = h("button", { class: "btn", onclick: () => vDiscover(true) }, icon("arrow-clockwise"), "Refresh");
  view.replaceChildren(page("Discover", btn), ...loadingShelves());
  try {
    const feed = await api(`/api/feed${refresh ? "?refresh=1" : ""}`);
    const intro = feed.cold_start ? h("p", { class: "muted", text: "Genius learns from what you play here. Until then, these are YouTube Music's shelves, fetched with no account." }) : null;
    view.replaceChildren(...[page("Discover", btn), intro, ...feed.shelves.map(shelf)].filter(Boolean));
  } catch (err) { view.replaceChildren(page("Discover", btn), failed(err, () => vDiscover(true))); }
}

const SPOTIFY = /open\.spotify\.com\/|spotify:(playlist|album|track):/;
let searchState = { q: "", kind: "songs" };
function vSearch() {
  const input = h("input", { type: "search", placeholder: "Songs, albums, artists, or paste a Spotify link", value: searchState.q, style: "flex:1;min-width:240px", "aria-label": "Search" });
  const tabs = h("div", { class: "tabs" }, ["songs", "albums", "artists"].map((k) =>
    h("button", { class: searchState.kind === k ? "on" : "", onclick: () => { searchState.kind = k; run(); } }, k[0].toUpperCase() + k.slice(1))));
  const results = h("div", {});
  const form = h("form", { class: "toolbar", onsubmit: (e) => { e.preventDefault(); searchState.q = input.value.trim(); run(); } }, input, tabs, h("button", { class: "btn primary", type: "submit" }, icon("magnifying-glass"), "Search"));
  view.replaceChildren(page("Search"), form, results);
  input.focus();
  async function run() {
    tabs.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.textContent.toLowerCase() === searchState.kind));
    const q = searchState.q;
    if (!q) { results.replaceChildren(empty("magnifying-glass", "Search YouTube Music with no account, or paste a public Spotify playlist, album or track link.")); return; }
    results.replaceChildren(h("p", { class: "muted", text: "Searching…" }));
    try {
      if (SPOTIFY.test(q)) {
        const sp = await api(`/api/spotify?link=${encodeURIComponent(q)}`);
        results.replaceChildren(
          h("div", { class: "panel" }, h("h2", {}, icon("spotify-logo"), ` ${sp.name}`),
            h("p", { text: `${sp.tracks.length} songs${sp.truncated ? " (Spotify's public page lists only the first 100)" : ""}. Each one is matched to YouTube Music on the server; songs without a confident match are skipped, never guessed.` }),
            h("div", { class: "form-row" }, h("button", { class: "btn primary", onclick: () => download([{ spotify: q }], sp.name) }, icon("download-simple"), "Download all"))),
          trackTable(sp.tracks, { numbers: true }));
        return;
      }
      const items = await api(`/api/search?q=${encodeURIComponent(q)}&kind=${searchState.kind}&limit=30`);
      if (!items.length) return results.replaceChildren(empty("magnifying-glass", `Nothing found for "${q}".`));
      results.replaceChildren(searchState.kind === "songs" ? trackTable(items, { alone: true }) : h("div", { class: "row", style: "grid-auto-flow:row;grid-template-columns:repeat(auto-fill,minmax(150px,1fr))" }, items.map((t) => card(t, items))));
    } catch (err) { results.replaceChildren(failed(err, run)); }
  }
  run();
}

async function vDetail(kind, id) {
  view.replaceChildren(...loadingShelves());
  try {
    const d = await api(`/api/${kind}/${encodeURIComponent(id)}`);
    const tracks = d.tracks || d.songs || [];
    const title = d.title || d.name;
    const actions = h("div", { class: "actions" },
      tracks.length ? h("button", { class: "btn primary", onclick: () => playList(tracks) }, icon("play"), "Play") : null,
      kind === "album" ? h("button", { class: "btn", onclick: () => download([{ album_id: id }], title) }, icon("download-simple"), "Download album") : null,
      kind === "playlist" ? h("button", { class: "btn", onclick: () => download([{ playlist_id: id }], title) }, icon("download-simple"), "Download playlist") : null,
      kind === "artist" ? h("button", { class: "btn", onclick: () => hide({ kind: "artist", key: title, ref: id, label: title }) }, icon("eye-slash"), "Hide from agents") : null);
    const sub = kind === "album" ? [ (d.artists || []).join(", "), d.year ].filter(Boolean).join(" · ") : kind === "artist" ? "Artist" : "Playlist";
    const parts = [
      h("button", { class: "btn", style: "margin-bottom:14px", onclick: () => history.back() }, icon("caret-left"), "Back"),
      h("div", { class: `hero ${kind}` }, d.cover ? h("img", { src: cover(d, 400), alt: "" }) : h("div"), h("div", {}, h("div", { class: "muted", text: sub }), h("h1", { text: title }), actions)),
      kind === "artist" ? h("h2", { class: "shelf", text: "Top songs" }) : null,
      trackTable(tracks, { numbers: kind === "album" }),
    ];
    if (kind === "artist") {
      for (const [label, items] of [["Albums", d.albums], ["Singles", d.singles], ["Fans also like", d.related]]) {
        if (items && items.length) parts.push(h("div", { style: "height:22px" }), shelf({ title: label, items }));
      }
    }
    view.replaceChildren(...parts.filter(Boolean));
    markPlaying();
  } catch (err) { view.replaceChildren(failed(err, () => vDetail(kind, id))); }
}

async function vLibrary() {
  view.replaceChildren(page("Library"), h("p", { class: "muted", text: "Loading…" }));
  try {
    const rows = await api("/api/library?limit=500");
    const tracks = rows.map((r) => ({ ...r, owned: true, duration: null }));
    view.replaceChildren(page("Library", h("span", { class: "muted", text: `${rows.length} downloaded by tapedeck` })),
      rows.length ? trackTable(tracks) : empty("music-notes", "Nothing downloaded yet. Use the download button on any song, album or Spotify link."));
    markPlaying();
  } catch (err) { view.replaceChildren(page("Library"), failed(err, vLibrary)); }
}

let activityTimer = null;
async function vActivity() {
  clearTimeout(activityTimer);
  try {
    const jobs = await api("/api/jobs?limit=50");
    const table = jobs.length ? h("table", { class: "table" },
      h("thead", {}, h("tr", {}, ["#", "Download", "Started by", "Progress", "Status", "When", ""].map((c) => h("th", { text: c })))),
      h("tbody", {}, jobs.map((j) => h("tr", {},
        h("td", { class: "mono", text: j.id }), h("td", { text: j.label || "download" }),
        h("td", {}, j.actor === "agent" ? h("span", {}, icon("robot"), " agent") : h("span", {}, icon("user-circle"), " you")),
        h("td", { class: "mono", text: `${j.done || 0}${j.total ? `/${j.total}` : ""}${j.failed ? `, ${j.failed} failed` : ""}` }),
        h("td", {}, h("span", { class: `pill ${j.status}`, text: j.status || "done", title: j.error || "" })),
        h("td", { class: "muted", text: (j.created || "").slice(0, 16).replace("T", " ") }),
        h("td", {}, j.done && j.status !== "undone" ? h("button", { class: "btn danger", onclick: () => undo(j) }, icon("arrow-counter-clockwise"), "Undo") : null)))))
      : empty("list-checks", "No downloads yet.");
    if (location.hash.startsWith("#/activity")) view.replaceChildren(page("Activity", h("button", { class: "btn", onclick: vActivity }, icon("arrow-clockwise"), "Refresh")), table);
    if (jobs.some((j) => j.status === "queued" || j.status === "running")) activityTimer = setTimeout(() => location.hash.startsWith("#/activity") && vActivity(), 3000);
  } catch (err) { view.replaceChildren(page("Activity"), failed(err, vActivity)); }
}
async function undo(j) {
  if (!confirm(`Delete the ${j.done} file(s) download #${j.id} added to your library?`)) return;
  try { const r = await api(`/api/jobs/${j.id}/undo`, { method: "POST" }); toast(`Removed ${r.removed} file(s)`); vActivity(); }
  catch (err) { toast(err.message, true); }
}
let busyTimer = null;
async function pollBusy() {
  clearTimeout(busyTimer);
  try {
    const jobs = await api("/api/jobs?limit=20");
    const n = jobs.filter((j) => j.status === "queued" || j.status === "running").length;
    $("#busy").hidden = !n;
    $("#busy").textContent = n;
    if (n) busyTimer = setTimeout(pollBusy, 4000);
  } catch { /* the badge is best-effort */ }
}

async function vPrivacy() {
  const kind = h("select", { "aria-label": "Kind" }, ["artist", "song", "album"].map((k) => h("option", { value: k, text: k[0].toUpperCase() + k.slice(1) })));
  const key = h("input", { type: "text", placeholder: "Artist name, or a song/album id", style: "min-width:260px" });
  const add = h("form", { class: "form-row", onsubmit: async (e) => {
    e.preventDefault();
    if (!key.value.trim()) return;
    await hide({ kind: kind.value, key: key.value.trim(), label: key.value.trim() });
    vPrivacy();
  } }, h("div", { class: "field" }, h("label", { text: "Hide" }), kind), h("div", { class: "field" }, h("label", { text: "Which" }), key), h("button", { class: "btn primary", type: "submit" }, icon("eye-slash"), "Hide from agents"));
  const explain = h("div", { class: "panel" }, h("h2", { text: "What agents see" }),
    h("p", { text: "Agents (Claude over MCP, or anything using the agent token) get your feed, library, likes, top artists and download history with everything below removed. They never see file paths, play times or this list, and they can't change it." }),
    h("p", { text: "Public search results are not filtered, because an empty search would itself reveal what is hidden. Hidden songs simply show as not in your library." }),
    h("p", { text: "Hide things from any song's ⋯ menu, or add a rule here." }), add);
  const perms = h("div", { class: "panel" }, h("h2", { text: "What agents may do" }), h("p", { class: "muted", text: "Loading…" }));
  api("/api/permissions").then((p) => {
    const labels = { library: "See your library", feed: "See your feed and top artists", likes: "See your likes",
      download: "Start downloads", jobs: "See download activity" };
    const toggles = Object.entries(labels).map(([k, label]) => h("label", { class: "toggle" },
      h("input", { type: "checkbox", checked: p[k] ? true : null, onchange: (e) => savePerm({ [k]: e.target.checked }) }), h("span", { text: label })));
    const undo = h("select", { "aria-label": "Undo downloads", onchange: (e) => savePerm({ undo: e.target.value }) },
      [["off", "Never"], ["own", "Only downloads an agent started"], ["all", "Any download"]].map(([v, t]) =>
        h("option", { value: v, text: t, selected: p.undo === v ? true : null })));
    perms.replaceChildren(h("h2", { text: "What agents may do" }),
      h("p", { text: "Agents can always search the public catalog. Everything else is up to you, and agents can read these settings so they know what they're allowed." }),
      h("div", { class: "toggles" }, toggles), h("div", { class: "form-row" }, h("div", { class: "field" }, h("label", { text: "Undo downloads" }), undo)));
  }).catch((err) => perms.replaceChildren(failed(err, vPrivacy)));
  async function savePerm(change) {
    try { await api("/api/permissions", { method: "PUT", body: change }); toast("Saved"); }
    catch (err) { toast(err.message, true); vPrivacy(); }
  }
  try {
    const rules = await api("/api/private");
    const list = rules.length ? h("table", { class: "table" }, h("thead", {}, h("tr", {}, ["Kind", "Hidden", "Since", ""].map((c) => h("th", { text: c })))),
      h("tbody", {}, rules.map((r) => h("tr", {}, h("td", { text: r.kind }), h("td", { text: r.label || r.key }),
        h("td", { class: "muted", text: new Date(r.created * 1000).toLocaleDateString() }),
        h("td", {}, h("button", { class: "btn", onclick: async () => { await api(`/api/private/${r.id}`, { method: "DELETE" }); toast(`Agents can see ${r.label || r.key} again`); vPrivacy(); } }, icon("trash"), "Unhide"))))))
      : empty("shield-check", "Nothing hidden. Agents see your listening, minus file paths and play times.");
    view.replaceChildren(page("Privacy"), perms, explain, list);
  } catch (err) { view.replaceChildren(page("Privacy"), failed(err, vPrivacy)); }
}

async function vSystem() {
  try {
    const s = await api("/api/status");
    const rows = [["tapedeck", s.version], ["yt-dlp", s.yt_dlp], ["Downloads", s.downloads], ["Queue", s.queue], ["Navidrome rescans", s.navidrome ? "on" : "off"],
      ["YouTube audio", s.youtube.youtube == null ? "not checked yet" : `${s.youtube.youtube ? "reachable" : "BLOCKED"} (${s.youtube.detail})`],
      ["Listening events", s.events], ["Privacy rules", s.private_rules], ["Feed age", s.feed_age == null ? "not built yet" : `${Math.round(s.feed_age / 60)} min`], ["Library folder", s.library]];
    const mcp = `${location.origin}/mcp`;
    const check = h("button", { class: "btn", onclick: async () => {
      check.disabled = true; check.textContent = "Checking…";
      try { const r = await api("/api/check-stream", { method: "POST" }); toast(r.youtube ? "YouTube audio is reachable" : `Blocked: ${r.detail}`, !r.youtube); }
      catch (err) { toast(err.message, true); }
      vSystem();
    } }, icon("arrow-clockwise"), "Check YouTube");
    view.replaceChildren(page("System", check, h("button", { class: "btn", onclick: signOut }, "Sign out")),
      h("div", { class: "panel" }, h("h2", { text: "Status" }), h("dl", { class: "kv" }, rows.flatMap(([k, v]) => [h("dt", { text: k }), h("dd", { class: "mono", text: v ?? "" })]))),
      h("div", { class: "panel" }, h("h2", {}, icon("robot"), " Connect an agent"),
        h("p", { text: "Agents connect over MCP with the agent token, so they always get the privacy-filtered view. Print the token on the server with the command below; never give an agent the owner token." }),
        h("pre", { class: "mono", text: `tapedeck token --show agent\n\nclaude mcp add --transport http tapedeck ${mcp} \\\n  --header "Authorization: Bearer <agent token>"` })));
  } catch (err) { view.replaceChildren(page("System"), failed(err, vSystem)); }
}

// ---------- router ----------
function route() {
  closeMenu();
  const [, name = "discover", id] = location.hash.split("/");
  document.querySelectorAll("[data-nav]").forEach((a) => a.classList.toggle("active", a.dataset.nav === name));
  view.scrollTop = 0;
  ({
    discover: () => vDiscover(), search: vSearch, library: vLibrary, activity: vActivity, privacy: vPrivacy, system: vSystem,
    album: () => vDetail("album", id), artist: () => vDetail("artist", id), playlist: () => vDetail("playlist", id),
  }[name] || vDiscover)();
}
window.addEventListener("hashchange", route);

function start() {
  $("#login").hidden = true;
  $("#app").hidden = false;
  route();
  pollBusy();
}
if (TOKEN) api("/api/whoami").then((w) => (w.scope === "owner" ? start() : signOut())).catch(signOut);
else signOut();
