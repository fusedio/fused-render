// The menu-bar Dock tray (dock.html → here): Render App's dock.html script, as FusedBot ported it, now with
// fused-render's tiles — the user's APPS (Fused Render) or BOTS (Fused Bot); fused_render/dock.py decides, lib.ts
// holds the data model and the face drawing. Everything the data model does not force is the old page's logic, kept
// as it was: the fisheye, the rest layout, the bubble, the keyboard, drag-to-reorder, the separator resize with ⌥
// snaps, report()/dockAnchor/dockShown/dockMenuClosed, the size/tray/resize/menu messages to the native panel
// (fused_render/menubar_dock.py), the in-app vs dev-mode classes and the 1.5 s poll with the newest-poll-wins rule.
import {
  browserHref, bubbleText, displayName, fallbackTile, faceSvg, openBody, tileKey, trayOrder,
  type DockPayload, type Row,
} from "./lib";
import botHomeImg from "@assets/fusedbot-icon-1024.png";
import renderHomeImg from "@assets/fusedrender-icon-1024.png";

declare global {
  interface Window {
    dockAnchor?: (a: Anchor) => void;
    dockShown?: () => void;
    dockMenuClosed?: () => void;
  }
}
interface Anchor { icon: number; left: number; right: number }
type DockHandler = { postMessage(m: Record<string, unknown>): void };
const dockHandler = (): DockHandler | undefined =>
  (window as unknown as { webkit?: { messageHandlers?: { dock?: DockHandler } } }).webkit?.messageHandlers?.dock;
const toNative = (m: Record<string, unknown>) => dockHandler()?.postMessage(m);

// The same marker the app windows carry (mac_window.USER_AGENT_MARKER, both flavors): inside the native panel the
// page draws no glass and no backdrop of its own and places itself by the anchor the panel sends.
const INAPP = navigator.userAgent.includes("FusedRender/");
document.body.classList.add(INAPP ? "inapp" : "dev");

const tray = document.getElementById("tray") as HTMLElement;
const wrap = document.getElementById("wrap") as HTMLElement;
const bubble = document.getElementById("bubble") as HTMLElement;
const menu = document.getElementById("menu") as HTMLElement;
const HDRS = { "X-Fused": "1", "Content-Type": "application/json" };

type Item = HTMLButtonElement;
const state = {
  pinned: [] as Row[], recent: [] as Row[],
  nodes: new Map<string, Item>(), rows: new WeakMap<HTMLElement, Row>(),
  dragging: null as Item | null, menuFor: null as Item | null, hover: null as HTMLElement | null,
  dragEndedAt: 0, loaded: false, saving: 0, gen: 0, pollSeq: 0, resizing: false,
};

// ---------- net ----------
type Reply = { ok?: boolean; native?: boolean; view?: string; tilesize?: number };
async function post(path: string, body?: unknown): Promise<Reply> {
  const r = await fetch(path, { method: "POST", headers: HDRS, body: JSON.stringify(body || {}) });
  if (!r.ok) throw new Error(path + " " + r.status);
  return r.json();
}
// A mutation: polls pause until it lands, and any poll already in flight is discarded, so a 1.5 s refresh can never
// paint the pre-save tray over what the user just did. The old routes replied with the new list; these reply with
// `{ok, …}` only, so the tray is re-read once the mutation is done (a GET started after it, so it carries it).
async function save(fn: () => Promise<unknown>) {
  state.saving++; state.gen++;
  try { await fn(); }
  finally { state.saving--; }
  await refresh();
}
// Newest-started poll wins: a reply is applied only if no save landed and no later poll started while it was in
// flight. Two polls overlap when a dockShown() refresh (after the native menu's Keep in Dock) fires mid-interval;
// the earlier one may still carry the pre-change tray.
async function refresh() {
  if (state.dragging || state.menuFor || state.saving || state.resizing) return;
  const gen = state.gen, seq = ++state.pollSeq;
  try {
    const r = await fetch("/api/dock", { cache: "no-store" });
    if (!r.ok) return;
    const d = (await r.json()) as DockPayload;
    if (gen !== state.gen || seq !== state.pollSeq) return; // superseded while in flight
    apply(d, d.tilesize);
  } catch (_) { if (!state.loaded) render(); /* server down: show empty tray rather than nothing */ }
}
// `tilesize` rides along on GET /api/dock: it is applied before the render so the tray never paints at the default
// size first and then jumps.
function apply(d: DockPayload, tilesize?: number) {
  if (state.dragging || state.menuFor || state.resizing) return;
  if (typeof tilesize === "number" && tilesize !== TILE) setTile(tilesize);
  setHome(d.kind === "bots" ? "bots" : "apps");
  const t = trayOrder(d);
  state.pinned = t.pinned; state.recent = t.recent; state.loaded = true;
  render();
}

// ---------- fallback tile ----------
function paintFallback(tile: HTMLElement, name: string) {
  // Solid palette tile (colour hashed from the name) with the name's first character.
  tile.classList.add("fallback");
  const f = fallbackTile(name);
  tile.style.background = f.color;
  const span = document.createElement("span"); span.className = "mono"; span.textContent = f.letter;
  tile.replaceChildren(span);
}

// ---------- item nodes ----------
// One button per bot or app (`.entry`, plus `.bot` / `.app`), keyed by tileKey; Home is `.util.home`.
function makeItem(row: Row): Item {
  const b = document.createElement("button");
  b.className = "item entry " + row.kind; b.type = "button";
  b.dataset.key = tileKey(row);
  const tile = document.createElement("div"); tile.className = "tile";
  const dot = document.createElement("div"); dot.className = "dot";
  b.append(tile, dot);
  b.addEventListener("click", () => openRow(b));
  b.addEventListener("contextmenu", (e) => { e.preventDefault(); showMenu(b); });
  b.addEventListener("pointerdown", onPointerDown);
  return b;
}
const faces = new WeakMap<HTMLElement, string>();
function updateItem(b: Item, row: Row) {
  state.rows.set(b, row);
  const tile = b.firstElementChild as HTMLElement;
  const name = displayName(row);
  b.dataset.name = name;
  b.dataset.label = bubbleText(row);
  b.dataset.pinned = row.pinned ? "1" : "";
  b.setAttribute("aria-label", b.dataset.label);
  b.classList.toggle("running", row.kind === "bot" && !!row.running);
  if (row.kind === "bot") {
    // The face is rewritten only when it changes (a poll re-render leaves the node alone).
    const svg = faceSvg(row);
    if (faces.get(tile) !== svg) { tile.innerHTML = svg; faces.set(tile, svg); } // built by faceSvg from validated data only
    return;
  }
  const img = tile.querySelector("img");
  // An icon that failed to load stays on the letter tile until the icon changes (the server stamps its mtime into
  // the URL): without this mark every 1.5 s poll would see no <img>, build a new one and refetch the failing URL.
  const failKey = row.icon || "";
  if (row.icon && tile.dataset.failed !== failKey) {
    const src = row.icon;
    if (!img || img.dataset.src !== src) {
      tile.classList.remove("fallback"); tile.style.background = "";
      const im = document.createElement("img"); im.alt = ""; im.draggable = false; im.dataset.src = src; im.src = src;
      im.addEventListener("error", () => { tile.dataset.failed = failKey; paintFallback(tile, name); }, { once: true });
      tile.replaceChildren(im);
    }
  } else if (img || tile.dataset.name !== name) {
    paintFallback(tile, name);
  }
  tile.dataset.name = name;
}

function utilItem(label: string, html: string, onClick: () => void): Item {
  const b = document.createElement("button");
  b.className = "item util"; b.type = "button"; b.dataset.name = label;
  b.setAttribute("aria-label", label);
  const tile = document.createElement("div"); tile.className = "tile";
  tile.innerHTML = html; // static, author-controlled markup only
  b.append(tile);
  b.addEventListener("click", onClick);
  return b;
}
// Home wears the app's own icon, the one in /Applications: Fused Bot's 1024px artwork (the dark squircle on the
// macOS grid, 100 px margin in 1024 → scale 1024/824) or Fused Render's sparkle card (assets/fusedrender-icon-1024.png,
// the build_dmg.sh render: 6 % margin → scale 1/0.88). The CSS scales the image by --home-scale so the card fills the
// tile and the tile's radius clips the transparent margin. Which one is the payload's `kind` (the server knows the
// flavor; the page does not).
const homeBtn = utilItem("Home", "", async () => {
  bump(homeBtn);
  try { const r = await post("/api/dock/home"); if (!r.ok || r.native === false) location.href = r.view || "/"; }
  catch (_) { location.href = "/"; }
});
const hint = document.createElement("span"); hint.className = "hint"; hint.textContent = "Apps you use appear here";
homeBtn.classList.add("home");
let homeKind: "apps" | "bots" | null = null;
function setHome(kind: "apps" | "bots") {
  if (homeKind === kind) return;
  homeKind = kind;
  const tile = homeBtn.firstElementChild as HTMLElement;
  const im = document.createElement("img"); im.alt = ""; im.draggable = false;
  im.src = kind === "bots" ? botHomeImg : renderHomeImg;
  tile.replaceChildren(im);
  homeBtn.style.setProperty("--home-scale", kind === "bots" ? "124.3%" : "113.7%");
  hint.textContent = kind === "bots" ? "Bots you use appear here" : "Apps you use appear here";
}
setHome("apps");
const sepA = document.createElement("div"); sepA.className = "sep"; sepA.dataset.role = "pin-sep";
sepA.setAttribute("role", "separator"); sepA.setAttribute("aria-label", "Resize Dock");
for (const el of [homeBtn, hint]) el.addEventListener("pointerdown", onPointerDown);
sepA.addEventListener("pointerdown", onSepDown);

// ---------- render (keyed, in place) ----------
function render() {
  const seen = new Set<string>();
  const order: HTMLElement[] = [];
  const place = (r: Row) => {
    const k = tileKey(r);
    let n = state.nodes.get(k);
    if (!n) { n = makeItem(r); state.nodes.set(k, n); }
    updateItem(n, r); seen.add(k); order.push(n);
  };
  // Home leads the pinned zone (then pinned bots, then pinned apps); the separator divides pinned from recent
  // (up to 3 bots, then up to 3 apps: lib.trayOrder). It is always there (the Dock's is too): it is the resize handle.
  order.push(homeBtn);
  state.pinned.forEach(place);
  order.push(sepA);
  state.recent.forEach(place);
  if (!state.pinned.length && !state.recent.length) order.push(hint);
  for (const [k, n] of state.nodes) if (!seen.has(k)) { n.remove(); state.nodes.delete(k); }
  // reconcile children order with minimal moves
  let i = 0;
  for (const n of order) {
    const cur = tray.children[i];
    if (cur !== n) tray.insertBefore(n, cur || null);
    i++;
  }
  while (tray.children.length > order.length) tray.lastElementChild!.remove();
  mag.rest = null;
  if (!mag.active) report();
}

// The status item's centre and the leftmost/rightmost x the tray may take (screen edges), all in canvas px — sent by
// the native side (dockAnchor) on show and after every size report. Without it (browser dev mode) #wrap sits at 0.
let anchor: Anchor | null = null;
window.dockAnchor = (a: Anchor) => {
  if (anchor && anchor.icon === a.icon && anchor.left === a.left && anchor.right === a.right) return;
  anchor = a;
  if (!state.resizing) report();  // re-place for the new geometry
};
function placedX(w: number) { return anchor ? Math.max(anchor.left, Math.min(anchor.icon - w / 2, anchor.right - w)) : 0; }
function trayRect() {
  const t = tray.getBoundingClientRect();
  return { x: Math.round(t.left), y: Math.round(t.top), w: Math.round(t.width), h: Math.round(t.height) };
}
// Glass only (cheap: one view frame, the panel itself is untouched).
function reportTray() {
  toNative({ type: "tray", tray: trayRect() });
}
function report() {
  // Pin #wrap to the rest width (+ slack both sides) so a live wave widens the centred tray symmetrically inside a
  // panel that does not move. (While the separator is being dragged #wrap is pinned to the LARGEST size the drag can
  // reach, once, so the panel stays put: see onSepDown.)
  if (!state.resizing) {
    wrap.style.left = "";  // measure at 0: near the canvas's right edge an absolute box shrinks to fit
    wrap.style.width = "";
    wrap.style.width = Math.ceil(wrap.getBoundingClientRect().width) + "px";
  }
  let r = wrap.getBoundingClientRect();
  let w = Math.ceil(r.width), h = Math.ceil(r.height);
  // Place #wrap in the canvas where the tray rests: centred under the icon, clamped to the screen (the native side
  // sent both, in canvas px).
  if (INAPP) { wrap.style.left = Math.round(placedX(w)) + "px"; r = wrap.getBoundingClientRect(); }
  const x = Math.round(r.left);
  if (menu.classList.contains("on")) {
    const m = menu.getBoundingClientRect();
    w = Math.max(w, Math.ceil(m.right - x + 6)); h = Math.max(h, Math.ceil(m.bottom + 6));
  }
  // The region of the canvas the panel shows, and the tray rect (canvas coordinates, top-left origin) where the
  // native glass goes; everything outside the tray stays fully transparent.
  toNative({ type: "size", x, width: w, height: h, tray: trayRect() });
}

// ---------- open ----------
function bump(b: HTMLElement) { b.classList.remove("bounce"); void b.offsetWidth; b.classList.add("bounce"); }
function shake(b: HTMLElement) { b.classList.remove("shake"); void b.offsetWidth; b.classList.add("shake"); }
async function openRow(b: Item) {
  const row = state.rows.get(b);
  if (!row || Date.now() - state.dragEndedAt < 300) return; // swallow the click that ends a drag
  bump(b);
  try {
    const r = await post("/api/dock/open", openBody(row));
    if (r && r.ok === false) throw new Error("open refused");
    if (r && r.native === false && r.view) location.href = r.view;
  } catch (_) { shake(b); }
}
// Keep in Dock / Remove from Dock. Bots pin through the bots page's own pin (bot.json), apps through dock.json; one
// route tells them apart by `id` vs `path`.
function setPinned(row: Row, pinned: boolean) {
  return row.kind === "bot"
    ? post("/api/dock/pin", { id: row.id, pinned })
    : post("/api/dock/pin", { path: row.path, pinned });
}

// ---------- hover magnification (downward; names live under the tiles) ----------
function items(): Item[] { return [...tray.querySelectorAll<Item>(".item")]; }
// ---- The Dock's fisheye, done the way the Dock does it -------------------
// 1. Each tile's TARGET size is a cosine bell of the pointer's distance to the tile's REST centre (the layout with
//    every tile at 1×). Measuring against rest positions — not the live, already-magnified ones — is what keeps the
//    wave stable; measuring live feeds the output back into the input and jitters.
// 2. The pointer is tracked over the WHOLE tray, gaps and separators included, so the wave never collapses until the
//    pointer leaves.
// 3. Sizes ease toward their targets every animation frame (exponential smoothing), so pointer-event cadence never
//    shows.
// 4. Tiles are sized in flow (the tray widens, neighbours slide) with no transforms — a transformed, shadowed layer
//    in a transparent web view leaves ghost slabs behind. The panel is NOT resized while the wave is live: #wrap
//    reserves horizontal slack for the widest wave up front.
const MAG_MAX = 1.5, EASE = 0.28;
// TILE is the Dock's tilesize (CSS --tile). The wave's reach scales with it (2.5 tiles either side: 130px at the
// 52px default).
let TILE = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--tile")) || 52;
const magRange = () => TILE * 2.5;
function setTile(px: number) {
  TILE = px;
  document.documentElement.style.setProperty("--tile", px + "px");
  mag.rest = null;  // rest geometry is a function of TILE
}
const REDUCED = matchMedia("(prefers-reduced-motion: reduce)").matches;
interface RestCenter { n: Item; l: number; w: number; cx: number }
const mag = {
  rest: null as { left: number; right: number; centers: RestCenter[] } | null,
  cur: new Map<HTMLElement, number>(), target: new Map<HTMLElement, number>(),
  raf: 0, px: null as number | null, active: false, frozen: false,
};

// Rest geometry: item centres and tray extent with every tile at 1×. Computed from the item order and the fixed
// metrics (tile, gap, separator), never measured — so it is valid at any moment of the wave. A DOM measurement was
// only right at rest, and a poll re-render mid-hover could leave the wave with no geometry at all (it froze).
const GAP = 8, PAD_X = 10, SEP_W = 1 + 4 + 4;
function restLayout() {
  if (mag.rest) return mag.rest;
  const centers: RestCenter[] = [];
  let x = PAD_X, first = true;
  for (const el of tray.children) {
    if (!first) x += GAP;
    first = false;
    if (el.classList.contains("item")) { centers.push({ n: el as Item, l: x, w: TILE, cx: x + TILE / 2 }); x += TILE; }
    else if (el.classList.contains("sep")) x += SEP_W;
    else x += el.getBoundingClientRect().width;  // the hint text: constant width
  }
  mag.rest = { left: 0, right: x + PAD_X, centers };
  return mag.rest;
}
// Map a live pointer x (the tray is wider while magnified) back to rest coordinates: same item / same gap, same
// fraction across it.
function toRestX(px: number) {
  const rest = mag.rest!, live = items().map(n => n.getBoundingClientRect());
  const t = tray.getBoundingClientRect();
  const x = px - t.left;
  for (let i = 0; i < live.length; i++) {
    const l = live[i].left - t.left, r = live[i].right - t.left;
    if (x <= r) {
      if (x >= l) return rest.centers[i].l + (x - l) / (r - l) * rest.centers[i].w;
      const prevR = i ? live[i - 1].right - t.left : 0;
      const prevRestR = i ? rest.centers[i - 1].l + rest.centers[i - 1].w : 0;
      return prevRestR + (x - prevR) / Math.max(1, l - prevR) * (rest.centers[i].l - prevRestR);
    }
  }
  return rest.right - rest.left;
}
function bell(d: number) { const R = magRange(); return d >= R ? 0 : 0.5 * (1 + Math.cos(Math.PI * d / R)); }
function retarget() {
  const list = items();
  if (mag.px === null || REDUCED || !mag.rest) { for (const n of list) mag.target.set(n, 1); return; }
  const rx = toRestX(mag.px);
  let best: Item | null = null, bestD = Infinity;
  for (const c of mag.rest.centers) {
    const d = Math.abs(rx - c.cx);
    mag.target.set(c.n, 1 + (MAG_MAX - 1) * bell(d));
    if (d < bestD) { bestD = d; best = c.n; }
  }
  // The bubble names the tile the pointer is nearest to, gaps included.
  setHover(best);
}
function tick() {
  mag.raf = 0;
  const list = items();
  let moving = false;
  for (const n of list) {
    const cur = mag.cur.get(n) ?? 1, tgt = mag.target.get(n) ?? 1;
    let next = cur + (tgt - cur) * EASE;
    if (Math.abs(next - tgt) < 0.002) next = tgt; else moving = true;
    if (next !== cur) { mag.cur.set(n, next); n.style.setProperty("--s", next.toFixed(4)); }
  }
  placeBubble();
  reportTray();  // the native glass follows the widening tray every frame
  if (moving) mag.raf = requestAnimationFrame(tick);
  // Back at rest: settle the panel size — unless the separator is being dragged (the panel was sized once for the
  // whole drag; onSepDown's release settles it).
  else if (!mag.active && !state.resizing) { mag.rest = null; report(); }
}
function kick() { if (!mag.raf) mag.raf = requestAnimationFrame(tick); }
function pointerAt(px: number) {
  if (mag.frozen) return;  // a tile's menu is up: the wave holds on it
  if (!mag.rest && !restLayout()) return;  // still animating out; ignore until settled
  mag.px = px; mag.active = true;
  retarget(); kick();
}
function pointerGone() {
  if (mag.frozen) return;
  mag.px = null; mag.active = false;
  for (const n of items()) mag.target.set(n, 1);
  state.hover = null; bubble.classList.remove("on");
  kick();
}
function placeBubble() {
  const b = state.hover;
  if (!b || !mag.active || mag.frozen) { bubble.classList.remove("on"); return; }
  const r = b.querySelector(".tile")!.getBoundingClientRect();
  // Mirrored Dock: the bubble hangs BELOW the tile, arrow up.
  bubble.style.top = (r.bottom + 9) + "px";
  const bw = bubble.offsetWidth, cx = r.left + r.width / 2;
  // Clamp inside #wrap, the region of the canvas the panel shows (in the app the window is the whole 1400px canvas;
  // anything past #wrap is cut).
  const w = wrap.getBoundingClientRect();
  bubble.style.left = Math.min(Math.max(cx, w.left + bw / 2 + 4), w.right - bw / 2 - 4) + "px";
  bubble.classList.add("on");
}
function setHover(b: HTMLElement | null) {
  if (state.hover === b) return;
  state.hover = b;
  if (!b) return;
  const name = document.createElement("span");
  name.textContent = b.dataset.label || b.dataset.name || "";
  bubble.replaceChildren(name);
}
tray.addEventListener("pointermove", (e) => {
  if (state.dragging || state.menuFor || state.resizing) return;
  pointerAt(e.clientX);
});
tray.addEventListener("pointerleave", () => { if (!state.dragging) pointerGone(); });
wrap.addEventListener("pointerleave", () => { if (!state.dragging) pointerGone(); });
tray.addEventListener("focusin", (e) => {
  const b = (e.target as HTMLElement).closest(".item");
  if (b) { const r = b.getBoundingClientRect(); pointerAt(r.left + r.width / 2); }
});
tray.addEventListener("focusout", () => { if (!tray.matches(":hover")) pointerGone(); });

// ---------- keyboard ----------
document.addEventListener("keydown", (e) => {
  if (menu.classList.contains("on")) { menuKey(e); return; }
  const list = items();
  const cur = (document.activeElement as HTMLElement | null)?.closest?.<Item>(".item") ?? null;
  const i = cur ? list.indexOf(cur) : -1;
  if (e.key === "ArrowRight") { e.preventDefault(); list[i < 0 ? 0 : Math.min(i + 1, list.length - 1)]?.focus(); }
  else if (e.key === "ArrowLeft") { e.preventDefault(); list[i < 0 ? list.length - 1 : Math.max(i - 1, 0)]?.focus(); }
  else if (e.key === "Escape") pointerGone();
});

// ---------- context menu ----------
function mi(label: string, fn: (() => unknown) | null, opts?: { href: string }) {
  const li = document.createElement("li"); li.setAttribute("role", "menuitem"); li.tabIndex = -1;
  if (opts && opts.href) {
    const a = document.createElement("a"); a.href = opts.href; a.target = "_blank"; a.rel = "noopener"; a.textContent = label;
    li.append(a); li.addEventListener("click", () => { hideMenu(); });
  } else {
    li.textContent = label;
    li.addEventListener("click", async () => { hideMenu(); try { await fn?.(); } catch (_) {} refresh(); });
  }
  return li;
}
function showMenu(b: Item) {
  const row = state.rows.get(b);
  if (!row) return;
  const pinned = !!row.pinned;
  if (INAPP && dockHandler()) {
    // In the app the menu is a real NSMenu popped from the tile (native look, arrow keys, ⎋); the HTML menu below is
    // the browser fallback. Freeze the wave on this tile while the menu is up (dockMenuClosed thaws it); the bubble
    // goes, the menu names the bot or app itself.
    mag.frozen = true;
    bubble.classList.remove("on");
    const r = b.querySelector(".tile")!.getBoundingClientRect();
    toNative({
      type: "menu", kind: row.kind, ...(row.kind === "bot" ? { id: row.id } : { path: row.path }),
      name: displayName(row), pinned,
      running: b.classList.contains("running"),
      x: Math.round(r.left), y: Math.round(r.bottom + 6),
    });
    return;
  }
  state.menuFor = b;
  menu.replaceChildren(
    mi("Open", () => openRow(b)),
    mi(pinned ? "Remove from Dock" : "Keep in Dock", () => save(() => setPinned(row, !pinned))),
    ...(row.kind !== "bot" ? [mi("Show in Finder", () => post("/api/dock/reveal", { path: row.path }))] : []),
    mi("Open in Browser", null, { href: browserHref(row, location.origin) }),
  );
  pointerGone();
  menu.classList.add("on");
  const m = menu.getBoundingClientRect();
  // The Dock hangs from the menu bar, so the menu drops BELOW the tray (the real Dock's menus rise above it). Make
  // room underneath; the native panel grows to the reported size.
  const need = m.height + 14;
  wrap.style.paddingBottom = need + "px";
  const r = b.getBoundingClientRect(), t = tray.getBoundingClientRect();
  let left = r.left + r.width / 2 - m.width / 2;
  left = Math.min(Math.max(4, left), Math.max(4, window.innerWidth - m.width - 4));
  const top = t.bottom + 8;
  menu.style.left = left + "px"; menu.style.top = top + "px";
  report();
}
function hideMenu() {
  if (!menu.classList.contains("on")) return;
  menu.classList.remove("on"); state.menuFor = null; wrap.style.paddingBottom = "";
  report(); refresh();
}
function menuKey(e: KeyboardEvent) {
  const lis = [...menu.querySelectorAll<HTMLElement>("li[role=menuitem]")];
  const i = lis.findIndex(l => l.classList.contains("focus"));
  if (e.key === "Escape") { e.preventDefault(); hideMenu(); }
  else if (e.key === "ArrowDown") { e.preventDefault(); lis[i]?.classList.remove("focus"); lis[(i + 1) % lis.length].classList.add("focus"); }
  else if (e.key === "ArrowUp") { e.preventDefault(); lis[i]?.classList.remove("focus"); lis[i < 0 ? lis.length - 1 : (i - 1 + lis.length) % lis.length].classList.add("focus"); }
  else if (e.key === "Enter") { e.preventDefault(); (lis[i]?.querySelector("a") || lis[i])?.click(); }
}
document.addEventListener("pointerdown", (e) => { if (!menu.contains(e.target as Node)) hideMenu(); }, true);
window.addEventListener("blur", hideMenu);
document.addEventListener("contextmenu", (e) => { if (!(e.target as HTMLElement).closest?.(".entry")) e.preventDefault(); });

// ---------- drag to reorder (pointer based) ----------
function onPointerDown(e: PointerEvent) {
  const b = (e.target as HTMLElement).closest?.<Item>(".entry");
  if (!b || e.button !== 0) return;
  const start = { x: e.clientX, y: e.clientY, b, id: e.pointerId, moved: false };
  const move = (ev: PointerEvent) => {
    if (ev.pointerId !== start.id) return;
    if (!start.moved) {
      if (Math.hypot(ev.clientX - start.x, ev.clientY - start.y) < 6) return;
      start.moved = true; beginDrag(b);
    }
    dragMove(ev.clientX);
  };
  const up = (ev: PointerEvent) => {
    window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up); window.removeEventListener("pointercancel", up);
    if (start.moved) { ev.preventDefault(); endDrag(); }
  };
  window.addEventListener("pointermove", move); window.addEventListener("pointerup", up); window.addEventListener("pointercancel", up);
}
function beginDrag(b: Item) {
  state.dragging = b; pointerGone(); hideMenu();
  b.classList.add("dragging");
}
function pinnedNodes() { return items().filter(n => n.classList.contains("entry") && n.dataset.pinned && n !== state.dragging); }
function pinBoundary() {
  // Right edge of the pinned zone: the separator (always rendered).
  return sepA.getBoundingClientRect().right;
}
function dragMove(x: number) {
  const b = state.dragging!, dragIsPinned = !!b.dataset.pinned;
  // Unpinned item still right of the pin boundary: stays in the recent zone (no pin).
  if (!dragIsPinned && x > pinBoundary()) {
    b.dataset.pinDrop = ""; return;
  }
  b.dataset.pinDrop = "1";
  let ref: Element | null = null;
  for (const n of pinnedNodes()) { const r = n.getBoundingClientRect(); if (x < r.left + r.width / 2) { ref = n; break; } }
  if (!ref) ref = sepA.isConnected ? sepA : null;  // end of the pinned zone (never left of Home)
  if (b.nextSibling !== ref) tray.insertBefore(b, ref);
}
async function endDrag() {
  const b = state.dragging; if (!b) return;
  const row = state.rows.get(b);
  const wasPinned = !!b.dataset.pinned, pinDrop = b.dataset.pinDrop === "1";
  delete b.dataset.pinDrop; b.classList.remove("dragging");
  state.dragging = null; state.dragEndedAt = Date.now();
  if (pinDrop && row) {
    // The pinned apps' new order, left to right, as the drop left the DOM.
    const paths: string[] = [];
    for (const n of items()) {
      const r = state.rows.get(n);
      if (r && r.kind !== "bot" && (n.dataset.pinned || n === b)) paths.push(r.path);
    }
    try {
      await save(async () => {
        if (row.kind === "bot") {
          // Bots have no order route: pinned bots keep the bots page's own order (by name), so a drop left of the
          // separator only pins, and a pinned bot dragged among the pins snaps back when the re-read renders.
          if (!wasPinned) await post("/api/dock/pin", { id: row.id, pinned: true });
          return;
        }
        if (!wasPinned) await post("/api/dock/pin", { path: row.path, pinned: true });
        await post("/api/dock/order", { paths });
      });
    } catch (_) { refresh(); }
  } else render();
}

// ---------- drag the separator to resize (the Dock's own gesture) ----------
// Spec, from the Dock: hover the separator → up/down resize cursor; drag away from the screen edge to grow, toward
// it to shrink, the far edge tracking the pointer 1:1 (hanging from the menu bar, down = bigger); tilesize range
// 16–128 (the Size slider's), live while dragging, held ⌥ snaps to the sizes icons are crispest at (16/32/64/128);
// the size persists (the Dock's `defaults write com.apple.dock tilesize`); the tray shrinks to fit the screen rather
// than run off it.
const TILE_MIN = 16, TILE_MAX = 128, TILE_SNAPS = [16, 32, 64, 128];
const PANEL_MAX_W = 1400;  // the native canvas width: nothing renders beyond it
function maxTileForScreen() {
  // Rest width at tile t: 2·PAD_X + n·t + (n−1)·GAP + SEP_W + hint, plus the wave slack (1.1·t each side) and the
  // 4px margins the native placement keeps.
  const n = items().length;
  let fixed = 2 * PAD_X + Math.max(0, n - 1) * GAP + SEP_W + 8;
  if (hint.isConnected) fixed += hint.getBoundingClientRect().width + GAP;
  const avail = anchor ? anchor.right - anchor.left : Math.min(screen.availWidth || Infinity, PANEL_MAX_W);
  return Math.max(TILE_MIN, Math.min(TILE_MAX, Math.floor((avail - fixed) / (n + 2.2))));
}
// Smoothness: the native panel is resized and re-centred ONCE, at drag start, to the largest size the drag can
// reach; after that only the tray changes (the glass follows per frame, as in the fisheye). Resizing the panel on
// every pointer event moved the whole page under the tiles. Pointer events are coalesced to one layout per animation
// frame, and the tray is snapped to a whole pixel (its width changes parity with every 1px step, which otherwise
// puts the icons on half pixels: shimmer).
//
// Where the tray sits inside that big #wrap: exactly where it will REST for the current tile size — #wrap is placed
// by the same centre-and-clamp as at rest (placedX), just for its pinned width, so on a narrow screen it is shoved
// left and spans most of the canvas while the tray must not move. Without an anchor (browser dev mode) the tray is
// centred.
function snapTray() {
  const slack = parseFloat(getComputedStyle(wrap).paddingLeft);
  const inner = wrap.clientWidth - 2 * slack;
  const trayW = restLayout().right;
  const natural = (inner - trayW) / 2;  // centred: where the tray sits with left = 0
  let want = natural;
  if (anchor) {
    const restW = Math.ceil(trayW + 2 * slack);  // #wrap at rest = tray + slack both sides
    const trayRestX = placedX(restW) + (restW - trayW) / 2;  // canvas x of the tray at rest
    const wrapX = parseFloat(wrap.style.left) || 0;
    want = Math.max(0, Math.min(trayRestX - wrapX - slack, inner - trayW));
  }
  tray.style.left = (Math.floor(want) - natural) + "px";
}
function onSepDown(e: PointerEvent) {
  if (e.button !== 0 || state.dragging) return;
  e.preventDefault();
  const start = { y: e.clientY, tile: TILE, id: e.pointerId, max: maxTileForScreen(), want: TILE, raf: 0 };
  try { sepA.setPointerCapture(e.pointerId); } catch (_) {}
  state.resizing = true; pointerGone(); hideMenu();
  document.body.classList.add("resizing");
  // In the app the cursor is held natively for the drag (CSS cursors do not survive the panel/glass frame changes).
  toNative({ type: "resize", active: true });
  // Pin #wrap to the size of the largest reachable tray, then size the panel to it once. The tray stays centred
  // inside as it grows.
  setTile(start.max);
  wrap.style.left = ""; wrap.style.width = ""; wrap.style.height = "";  // measure at 0 (see report)
  const big = wrap.getBoundingClientRect();
  wrap.style.width = Math.ceil(big.width) + "px"; wrap.style.height = Math.ceil(big.height) + "px";
  setTile(start.tile);
  // Place the big #wrap where report() will, snap the tray to its rest spot inside it, then report once: region and
  // glass in one message.
  wrap.style.left = Math.round(placedX(Math.ceil(big.width))) + "px";
  snapTray(); report();
  const frame = () => {
    start.raf = 0;
    if (start.want !== TILE) { setTile(start.want); snapTray(); reportTray(); }
  };
  const move = (ev: PointerEvent) => {
    if (ev.pointerId !== start.id) return;
    // Anchored to the menu bar: the tray's bottom edge moves 1:1 with the tile.
    let t = Math.round(start.tile + (ev.clientY - start.y));
    if (ev.altKey) t = TILE_SNAPS.reduce((a, b) => Math.abs(b - t) < Math.abs(a - t) ? b : a);
    // Clamp AFTER the snap: the nearest snap can sit above the screen-fit cap the panel was reserved for at drag
    // start.
    t = Math.min(Math.max(t, TILE_MIN), start.max);
    start.want = t;
    if (!start.raf) start.raf = requestAnimationFrame(frame);
  };
  const up = async (ev: PointerEvent) => {
    if (ev.pointerId !== start.id) return;
    sepA.removeEventListener("pointermove", move); sepA.removeEventListener("pointerup", up); sepA.removeEventListener("pointercancel", up);
    try { sepA.releasePointerCapture(start.id); } catch (_) {}
    if (start.raf) { cancelAnimationFrame(start.raf); start.raf = 0; }
    if (start.want !== TILE) setTile(start.want);
    state.resizing = false; document.body.classList.remove("resizing");
    toNative({ type: "resize", active: false });
    wrap.style.height = ""; tray.style.left = "";
    report();
    if (TILE !== start.tile) {
      try {
        const r = await post("/api/dock/size", { tilesize: TILE });
        if (typeof r.tilesize === "number" && r.tilesize !== TILE) { setTile(r.tilesize); report(); }
      } catch (_) {}
    }
    refresh();
  };
  sepA.addEventListener("pointermove", move); sepA.addEventListener("pointerup", up); sepA.addEventListener("pointercancel", up);
}

// ---------- lifecycle ----------
let timer: ReturnType<typeof setInterval> | null = null;
// The interval tick skips while a previous poll is still in flight: a slow /api/dock (the server busy, or the request
// queued behind the 6-connection-per-host:port pool every WebKit window shares — measured 2026-10-08) would
// otherwise stack a new request every 1.5 s on top of the stalled one. Explicit refresh() calls (after an action,
// dockShown) are not gated — they must see the change, and newest-poll-wins already settles the overlap.
let polling = false;
function pollTick() {
  if (polling) return;
  polling = true;
  void refresh().finally(() => { polling = false; });
}
function schedule() {
  if (timer) clearInterval(timer);
  timer = null;
  if (document.visibilityState === "visible") timer = setInterval(pollTick, 1500);
}
document.addEventListener("visibilitychange", () => { schedule(); if (document.visibilityState === "visible") refresh(); });
window.dockShown = () => { hideMenu(); pointerGone(); refresh(); };
window.dockMenuClosed = () => {
  mag.frozen = false;
  if (!tray.matches(":hover")) pointerGone();
};
window.addEventListener("resize", report);
refresh(); schedule();
