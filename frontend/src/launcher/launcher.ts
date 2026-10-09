// The ⌥Space launcher (launcher.html → here): the old fused_render/static/launcher.html script, ported to a Vite
// entry so a bot row can wear its face from the one drawing (../dock/lib faceSvg, shared with the menu-bar Dock).
// Everything else is the static page's logic, kept as it was: the newest-query-wins search over GET /api/launcher,
// the footer shortcut recorder (PUT /api/prefs + POST /api/launcher/suspend), the keyboard, the size report and the
// bridge to the native panel (fused_render/launcher_panel.py).
//
// What the rows ARE is the server's call, by flavor: under Fused Render apps (app/appfile), then files and folders,
// then the home row; under Fused Bot bots (`kind: "bot"`, `files` always empty), then the home row. The page's copy
// keys off `settings.kind` ("apps" | "bots"), which arrives with the launcher prefs.
import { faceSvg, type DockFace } from "../dock/lib";
import botHomeImg from "@assets/fusedbot-icon-1024.png";
import renderHomeImg from "@assets/fusedrender-icon-1024.png";

interface LauncherRow {
  kind?: "app" | "appfile" | "file" | "folder" | "bot";
  home?: boolean;
  id?: string;
  path: string;
  url?: string;
  name?: string;
  title?: string | null;
  icon?: string | null;
  face?: DockFace | null;
  status?: string;
  running?: boolean;
}
interface LauncherSettings {
  kind?: "apps" | "bots";
  hotkey?: string;
  display?: string;
  bound?: boolean;
  row_modifier?: string;
  row_modifier_display?: string;
}
interface LauncherReply { apps?: LauncherRow[]; files?: LauncherRow[]; files_reason?: string }

declare global {
  interface Window {
    launcherShown?: () => void;
    launcherSettings?: (settings: LauncherSettings | null) => void;
  }
}
type Bridge = { postMessage(m: Record<string, unknown>): void };
const bridge = (): Bridge | undefined =>
  (window as unknown as { webkit?: { messageHandlers?: { launcher?: Bridge } } }).webkit?.messageHandlers?.launcher;

// The launcher panel (launcher_panel.py) tags its web view's UA; a plain
// browser tab (dev) gets a backdrop drawn here and navigates instead of
// asking the app to open a window.
const INAPP = navigator.userAgent.includes("FusedRender/");
document.body.classList.add(INAPP ? "inapp" : "dev");

const q = document.getElementById("q") as HTMLInputElement;
const list = document.getElementById("list") as HTMLElement;
const empty = document.getElementById("empty") as HTMLElement;
const shortcut = document.getElementById("shortcut") as HTMLButtonElement;
const panel = document.getElementById("panel") as HTMLElement;
const HDRS = { "X-Fused": "1", "Content-Type": "application/json" };

const state = {
  apps: [] as LauncherRow[], files: [] as LauncherRow[], filesReason: "", rows: [] as LauncherRow[],
  els: [] as HTMLElement[], sel: 0, seq: 0, recording: false, settings: null as LauncherSettings | null,
  lastH: 0, rowMods: ["alt"], rowSym: "⌥",
};
const SYM: Record<string, string> = { ctrl: "⌃", alt: "⌥", shift: "⇧", cmd: "⌘" };
const bots = () => state.settings?.kind === "bots";

// ---------- net ----------
async function put(body: Record<string, unknown>): Promise<LauncherSettings> {
  const r = await fetch("/api/prefs", { method: "PUT", headers: HDRS, body: JSON.stringify(body || {}) });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || ("PUT /api/prefs " + r.status));
  return data.launcher || {};
}
// While the footer recorder is armed the app unbinds its live shortcuts
// (the Carbon hotkey and the row digits), else pressing the current
// combination toggles this panel closed instead of being recorded.
function suspend(on: boolean) {
  fetch("/api/launcher/suspend", { method: "POST", headers: HDRS, body: JSON.stringify({ on }) })
    .catch(() => undefined);
}
// Newest query wins: a slow reply for an older query is dropped.
async function query(text: string) {
  const seq = ++state.seq;
  try {
    const r = await fetch("/api/launcher?q=" + encodeURIComponent(text), { cache: "no-store" });
    if (!r.ok) return;
    const data = (await r.json()) as LauncherReply;
    if (seq !== state.seq) return;
    state.apps = Array.isArray(data.apps) ? data.apps : [];
    state.files = Array.isArray(data.files) ? data.files : [];
    state.filesReason = String(data.files_reason || "");
    // One list to select from: apps (or bots), then files, then the home row —
    // the home row stays last so <modifier>+0 and the footer read the same.
    const home = state.apps.filter((a) => a.home);
    // Under Fused Bot the server sends no files; the section never draws even if one slips through.
    state.rows = state.apps.filter((a) => !a.home).concat(bots() ? [] : state.files, home);
    state.sel = 0;
    render();
  } catch (e) { console.warn("launcher query failed", e); }
}
async function loadSettings() {
  try {
    const r = await fetch("/api/prefs", { cache: "no-store" });
    if (r.ok) setSettings((await r.json()).launcher || {});
  } catch { /* footer keeps its last label */ }
}
function setSettings(h: LauncherSettings) {
  const flavorChanged = (state.settings?.kind === "bots") !== (h.kind === "bots");
  state.settings = h;
  q.placeholder = bots() ? "Search bots…" : "Search apps and files…";
  if (flavorChanged) render();
  if (h.row_modifier) {
    state.rowMods = h.row_modifier.split("+");
    state.rowSym = h.row_modifier_display || state.rowMods.map((m) => SYM[m] || m).join("");
    state.els.forEach((li, i) => { if (i !== state.sel) hintOf(li).textContent = rowHint(i); });
  }
  if (state.recording) return;
  shortcut.textContent = h.display || h.hotkey || "—";
  shortcut.classList.toggle("bad", h.bound === false);
  shortcut.title = h.bound === false
    ? "Could not bind this shortcut (another app may use it). Click, then press a new one."
    : "Click, then press the new shortcut";
}

// ---------- render ----------
function hash(s: string) { let h = 2166136261; for (const c of s) { h ^= c.codePointAt(0)!; h = Math.imul(h, 16777619); } return h >>> 0; }
function monogram(name: string) {
  const d = document.createElement("div");
  d.className = "mono";
  d.style.background = `hsl(${hash(name) % 360} 45% 48%)`;
  d.textContent = (name.trim()[0] || "?").toUpperCase();
  return d;
}
function tilde(p: string) { return (p || "").replace(/^\/Users\/[^/]+/, "~"); }
function subtitle(app: LauncherRow) {
  if (app.home) return "Open the shell";
  // A bot has no path to show: its status when it is not idle (the Dock bubble's rule, dock/lib bubbleText).
  if (app.kind === "bot") return app.status && app.status !== "idle" ? app.status : "";
  // A file row names its folder: the name is already the title.
  if (app.kind === "file" || app.kind === "folder") return tilde(app.path.replace(/\/[^/]*$/, "") || "/");
  return tilde(app.path);
}
function rowHint(i: number) {
  if (state.rows[i]?.home) return state.rowSym + "0";
  return i < 9 ? state.rowSym + (i + 1) : "";
}
const hintOf = (li: HTMLElement) => li.querySelector(".hint") as HTMLElement;
const FILE_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">'
  + '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/></svg>';
const FOLDER_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">'
  + '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>';
// The home row wears the flavor's app icon, as the Dock's Home tile does (src/dock/dock.ts setHome): Fused Bot's
// 1024 px artwork (scale 1024/824) or Fused Render's sparkle card (scale 1/0.88), scaled so the card fills the box
// and the box's radius clips the transparent margin. Render's until the settings say otherwise.
function homeIcon(icon: HTMLElement) {
  const b = bots();
  icon.classList.add("home");
  icon.style.setProperty("--home-scale", b ? "124.3%" : "113.7%");
  const img = document.createElement("img");
  img.alt = "";
  img.draggable = false;
  img.src = b ? botHomeImg : renderHomeImg;
  icon.appendChild(img);
}
function rowModsHeld(e: KeyboardEvent) {
  const held: Record<string, boolean> = { ctrl: e.ctrlKey, alt: e.altKey, shift: e.shiftKey, cmd: e.metaKey };
  return Object.keys(held).every((m) => held[m] === state.rowMods.includes(m));
}
function emptyCopy(text: string): { line: string; tip: string } {
  if (bots()) {
    return text
      ? { line: `No bot matches “${text}”`, tip: "" }
      : { line: "No bots yet", tip: "Type to search your bots." };
  }
  const line = text ? `No app or file matches “${text}”` : "Nothing opened yet";
  let tip = "";
  if (!text) tip = "Type to search every app in your workspace, and the files and folders in your home.";
  else if (state.filesReason === "scanning") tip = "The file index is still scanning — files appear once it has covered your home folder.";
  else if (state.filesReason) tip = "Files are searched from the index; your home folder is not indexed yet (Preferences → Index).";
  return { line, tip };
}
function render() {
  list.textContent = "";
  empty.textContent = "";
  state.els = [];
  const rows = state.rows;
  if (!rows.some((a) => !a.home)) {
    const { line, tip } = emptyCopy(q.value.trim());
    empty.textContent = line;
    if (tip) {
      const span = document.createElement("span");
      span.className = "tip";
      span.textContent = tip;
      empty.appendChild(span);
    }
  }
  let sect = "";
  rows.forEach((app, i) => {
    const kind = app.home ? "" : (app.kind === "file" || app.kind === "folder") ? "files" : "apps";
    if (kind && kind !== sect && kind === "files") {
      const h = document.createElement("li");
      h.className = "sect";
      h.setAttribute("aria-hidden", "true");
      h.textContent = "Files";
      list.appendChild(h);
    }
    if (kind) sect = kind;
    const li = document.createElement("li");
    li.className = "row" + (i === state.sel ? " sel" : "") + (kind === "files" ? " file" : "")
      + (app.kind === "bot" ? " bot" : "");
    li.setAttribute("role", "option");
    li.dataset.i = String(i);
    const icon = document.createElement("div");
    icon.className = "icon";
    if (app.home) {
      homeIcon(icon);
    } else if (app.kind === "bot") {
      icon.innerHTML = faceSvg({ id: app.id || app.path, name: app.name, face: app.face });
    } else if (kind === "files") {
      const g = document.createElement("div");
      g.className = "glyph";
      g.innerHTML = app.kind === "folder" ? FOLDER_SVG : FILE_SVG;
      icon.appendChild(g);
    } else if (app.icon) {
      const img = document.createElement("img");
      img.alt = "";
      img.src = app.icon;
      img.onerror = () => { icon.textContent = ""; icon.appendChild(monogram(app.name || "?")); };
      icon.appendChild(img);
    } else icon.appendChild(monogram(app.name || "?"));
    if (app.running) { const dot = document.createElement("span"); dot.className = "dot"; dot.title = "open"; icon.appendChild(dot); }
    const text = document.createElement("div");
    text.className = "text";
    const name = document.createElement("div");
    name.className = "name";
    name.textContent = app.title || app.name || "";
    const sub = document.createElement("div");
    sub.className = "sub";
    sub.textContent = subtitle(app);
    text.append(name, sub);
    const hint = document.createElement("div");
    hint.className = "hint";
    hint.textContent = i === state.sel ? "↩" : rowHint(i);
    li.append(icon, text, hint);
    li.addEventListener("pointermove", () => select(i));
    li.addEventListener("click", () => open(i));
    list.appendChild(li);
    state.els.push(li);
  });
  report();
}
function select(i: number) {
  if (i === state.sel || i < 0 || i >= state.rows.length) return;
  const rows = state.els;
  rows[state.sel]?.classList.remove("sel");
  if (rows[state.sel]) hintOf(rows[state.sel]).textContent = rowHint(state.sel);
  state.sel = i;
  rows[i].classList.add("sel");
  hintOf(rows[i]).textContent = "↩";
}
function report() {
  const h = Math.ceil(panel.getBoundingClientRect().height);
  if (h === state.lastH) return;
  state.lastH = h;
  bridge()?.postMessage({ type: "size", width: panel.offsetWidth, height: h });
}

// ---------- actions ----------
function openHome() {
  const b = bridge();
  if (b) b.postMessage({ type: "home" });
  else location.href = "/";
}
function open(i: number) {
  const app = state.rows[i];
  if (!app) return;
  if (app.home) { openHome(); return; }
  // The native side opens by (kind, key): a bot's key is its id (the row's `path`), anything else its real path.
  const b = bridge();
  if (b) b.postMessage({ type: "open", kind: app.kind, key: app.path });
  else location.href = app.url || "/";
}
function close() {
  bridge()?.postMessage({ type: "close" });
}
function reset() {
  q.value = "";
  // The panel was closed (click-away) mid-recording: give the shortcuts back.
  if (state.recording) suspend(false);
  state.recording = false;
  shortcut.classList.remove("rec");
  if (state.settings) setSettings(state.settings);
  // Drop the last search now: until the recents list arrives, Enter or a
  // click must not launch a stale row from the previous query.
  state.apps = [];
  state.files = [];
  state.rows = [];
  state.sel = 0;
  render();
  query("");
  q.focus();
}

// ---------- keyboard ----------
const MODS = new Set(["Shift", "Control", "Alt", "Meta", "CapsLock", "Fn"]);
function recordKey(e: KeyboardEvent) {
  e.preventDefault();
  if (e.key === "Escape") { state.recording = false; suspend(false); shortcut.classList.remove("rec"); setSettings(state.settings || {}); q.focus(); return; }
  if (MODS.has(e.key)) return; // wait for the key itself
  const mods: string[] = [];
  if (e.ctrlKey) mods.push("ctrl");
  if (e.altKey) mods.push("alt");
  if (e.shiftKey) mods.push("shift");
  if (e.metaKey) mods.push("cmd");
  if (!mods.length) { shortcut.textContent = "Add a modifier (⌥ ⌘ ⌃ ⇧)…"; return; }
  const spec = mods.concat([e.code]).join("+");
  state.recording = false;
  shortcut.classList.remove("rec");
  put({ launcher_hotkey: spec })
    // One re-read after a write that landed (the rebind's `bound` verdict follows the PUT): a single look, never a
    // chain. A failed PUT changed nothing on the server, so its error stands for 1.5 s and the footer then goes back
    // to the settings already held — no second request, so a server that keeps refusing is not asked on a timer.
    .then((h) => { setSettings(h); setTimeout(loadSettings, 300); })
    .catch((err) => { shortcut.textContent = String(err?.message || err); shortcut.classList.add("bad"); setTimeout(() => setSettings(state.settings || {}), 1500); })
    // Resume AFTER the PUT: the rebind it triggers and this resume both
    // bind the stored spec, in that order, on the app's main thread.
    .finally(() => { suspend(false); q.focus(); });
}
document.addEventListener("keydown", (e) => {
  if (state.recording) { recordKey(e); return; }
  const n = state.rows.length;
  if (e.key === "ArrowDown") { e.preventDefault(); if (n) select(Math.min(state.sel + 1, n - 1)); }
  else if (e.key === "ArrowUp") { e.preventDefault(); if (n) select(Math.max(state.sel - 1, 0)); }
  else if (e.key === "Enter") { e.preventDefault(); open(state.sel); }
  else if (e.key === "Escape") { e.preventDefault(); if (q.value) { q.value = ""; query(""); } else close(); }
  else if (rowModsHeld(e) && e.code === "Digit0") { e.preventDefault(); openHome(); }
  else if (rowModsHeld(e) && /^Digit[1-9]$/.test(e.code)) {
    // ⌥1 arrives as e.key "¡": match the physical key, not the character.
    e.preventDefault();
    open(Number(e.code.slice(5)) - 1);
  }
  else if (e.key === "Tab") e.preventDefault();
});
q.addEventListener("input", () => query(q.value));
// Anything typed while a row has focus still lands in the field.
document.addEventListener("pointerdown", (e) => {
  if (!(e.target as Element | null)?.closest?.("#shortcut")) setTimeout(() => q.focus(), 0);
});

shortcut.addEventListener("click", () => {
  state.recording = true;
  suspend(true);
  shortcut.classList.add("rec");
  shortcut.classList.remove("bad");
  shortcut.textContent = "Press the new shortcut…";
  shortcut.focus();
});

// ---------- native hooks ----------
window.launcherShown = () => { reset(); loadSettings(); };
window.launcherSettings = (h) => setSettings(h || {});

new ResizeObserver(report).observe(panel);
// Boot: the first prefs read lands before the first rows, so the placeholder and copy are the flavor's from the
// start (loadSettings swallows its own failures, so the rows come either way).
void loadSettings().then(reset);
