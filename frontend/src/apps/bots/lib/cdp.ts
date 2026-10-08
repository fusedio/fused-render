// The live view's link to the bot's Chrome (OpenBot live.js): the page talks CDP to the driven tab directly (the
// same trick DevTools' own screencast uses). One WebSocket per driven tab carries JPEG frames in and mouse/keyboard
// out: no worker hop, no file, no polling. Chrome only accepts a browser-origin handshake when launched with
// --remote-allow-origins (browser.py adds it).
//
// Frames go straight into #fshot (never through React state); whether the socket is up is a tiny external store
// (useLinked) so #full's .nolink / .ctl classes and the status strip re-render with it. `state.fast` is "#full is
// showing": openFull() turns it on, handBack(true) turns it off.
import { useSyncExternalStore } from "react";
import { flushSync } from "react-dom";
import { pickFile } from "@platform/lib/api";
import { askConfirm } from "../dialogs/ask";
import { act, cur, getState, poll, select, setFast, showBanner, showToast, subscribe as subscribeStore } from "../state/store";
import { api, type Bot } from "./api";
import { BTN, CAST, CDP_MODS, frameDims, furlTarget, keyAction, nextDown, showUrl, toPageXY, type FrameMeta, type LastDown } from "./live";

const link: { ws: WebSocket | null; url: string | null; id: number; tabs: Set<string> | null; meta: FrameMeta | null } =
  { ws: null, url: null, id: 0, tabs: null, meta: null };  // meta: the last frame's viewport metadata from Chrome

const $ = <T extends HTMLElement = HTMLElement>(id: string) => document.getElementById(id) as T | null;
const activeWs = (b: Bot | undefined): string | null => (b?.browser?.tabs || []).find((t) => t.active)?.ws || null;

// ------------------------------------------------------------------ link state (for React) ----
let linkedSnap = false;
const linkListeners = new Set<() => void>();
function publishLink() {
  const v = linked(); if (v === linkedSnap) return;
  linkedSnap = v; for (const l of [...linkListeners]) l();
}
const subscribeLink = (l: () => void) => { linkListeners.add(l); return () => { linkListeners.delete(l); }; };
/** The socket to the driven tab is open (re-renders on connect / disconnect). */
export const useLinked = (): boolean => useSyncExternalStore(subscribeLink, () => linkedSnap, () => linkedSnap);

// ------------------------------------------------------------------ predicates ----
export const linked = (): boolean => link.ws?.readyState === 1;
/** #full is showing. */
export const inFull = (): boolean => getState().fast;
/** You drive: the view is open, you hold control and the socket is up. */
export const inCtl = (): boolean => inFull() && !!cur()?.control && linked();

/** Fire-and-forget: Chrome's replies are not needed for frames or input. */
export function cdp(method: string, params: Record<string, unknown> = {}): void {
  if (!linked()) return;
  link.ws!.send(JSON.stringify({ id: ++link.id, method, params }));
}
/** The few calls whose reply matters (what is under the pointer, what is focused). null when not linked or after 3 s. */
const pending = new Map<number, (r: Record<string, unknown> | null) => void>();
export function cdpCall(method: string, params: Record<string, unknown> = {}): Promise<Record<string, unknown> | null> {
  if (!linked()) return Promise.resolve(null);
  const id = ++link.id;
  return new Promise((resolve) => {
    const t = window.setTimeout(() => { pending.delete(id); resolve(null); }, 3000);
    pending.set(id, (r) => { window.clearTimeout(t); resolve(r); });
    link.ws!.send(JSON.stringify({ id, method, params }));
  });
}
/** Run an expression in the driven page and return its value (undefined on error or when not linked). */
async function evalIn<T = unknown>(expression: string): Promise<T | undefined> {
  const r = await cdpCall("Runtime.evaluate", { expression, returnByValue: true });
  return (r as { result?: { value?: T } } | null)?.result?.value;
}

// ------------------------------------------------------------------ the socket ----
/** Connect to the driven tab while the view is open; follow tab switches and relaunches by reconnecting when its socket URL changes. */
export function linkSync(): void {
  const b = cur();
  const want = inFull() ? activeWs(b) : null;
  if (link.url === want && link.ws && link.ws.readyState <= 1) {
    if (linked()) cdp("Page.bringToFront");
    return;
  }
  linkClose();
  if (!want) return;
  link.url = want;
  const ws = new WebSocket(want); link.ws = ws;
  ws.onopen = () => {
    cdp("Page.enable");
    // Headless Chrome composites only a tab it treats as visible and focused; focus emulation is per CDP session, so this
    // socket asks for it too (the bot's own sessions do the same in browser.py _foreground).
    cdp("Page.bringToFront"); cdp("Emulation.setFocusEmulationEnabled", { enabled: true });
    // Headless Chrome has no OS file dialog: a click on <input type=file> raises Page.fileChooserOpened here instead.
    cdp("Page.setInterceptFileChooserDialog", { enabled: true });
    // HTML5 drag-and-drop never starts from synthetic mouse events (the OS drag loop is not there in headless): Chrome
    // instead reports Input.dragIntercepted, and the live view replays the drag with Input.dispatchDragEvent (Puppeteer's mouse.drag).
    cdp("Input.setInterceptDrags", { enabled: true });
    cdp("Page.startScreencast", { format: "jpeg", ...CAST, everyNthFrame: 1 });
    publishLink();
  };
  ws.onmessage = (ev) => {
    const m = JSON.parse(String(ev.data)), p = m.params || {};
    if (typeof m.id === "number" && pending.has(m.id)) { pending.get(m.id)!(m.result ?? null); pending.delete(m.id); return; }
    if (m.method === "Page.screencastFrame") {
      link.meta = p.metadata || link.meta;
      const img = $<HTMLImageElement>("fshot"); if (img) img.src = "data:image/jpeg;base64," + p.data;
      cdp("Page.screencastFrameAck", { sessionId: p.sessionId });
    } else if (m.method === "Page.frameNavigated" && !p.frame?.parentId) {
      const furl = $<HTMLInputElement>("furl");
      if (furl && document.activeElement !== furl) furl.value = showUrl(p.frame?.url);
      void poll();  // title and tab strip
    } else if (m.method === "Page.screencastVisibilityChanged" && p.visible === false) {
      cdp("Page.bringToFront"); cdp("Emulation.setFocusEmulationEnabled", { enabled: true });
    } else if (m.method === "Page.javascriptDialogOpening") {
      void pageDialog(p as { type: string; message?: string; defaultPrompt?: string });
    } else if (m.method === "Page.fileChooserOpened") {
      void fileChooser(p as { backendNodeId?: number; mode?: string });
    } else if (m.method === "Input.dragIntercepted") {
      drag = { data: p.data as Record<string, unknown>, entered: false };
    }
  };
  ws.onclose = ws.onerror = () => { if (link.ws === ws) { link.ws = null; publishLink(); } };
}

/** alert / confirm / prompt / beforeunload from the page. While you drive, a real dialog: confirm and beforeunload can be
 *  refused, prompt accepts its default. While the bot drives, accept at once (the bot's own run does the same) and say so. */
async function pageDialog(p: { type: string; message?: string; defaultPrompt?: string }): Promise<void> {
  const msg = p.message || "";
  if (!inCtl() || p.type === "alert") {
    showToast({ text: `${p.type}: ${msg}`, ts: Date.now() / 1000 });
    cdp("Page.handleJavaScriptDialog", { accept: true, promptText: p.defaultPrompt || "" });
    return;
  }
  const title = p.type === "beforeunload" ? "Leave this page?" : p.type === "prompt" ? "The page asks for a value" : "The page asks";
  const text = p.type === "prompt" ? `${msg}\n\nOK answers with "${p.defaultPrompt || ""}" (the page's default); Cancel answers nothing.` : msg;
  const ok = await askConfirm(title, text, p.type === "beforeunload" ? "Leave" : "OK", false);
  cdp("Page.handleJavaScriptDialog", { accept: ok, promptText: p.defaultPrompt || "" });
}
/** <input type=file> clicked in the page: the OS picker runs in the server process, the chosen path lands on the input. */
async function fileChooser(p: { backendNodeId?: number; mode?: string }): Promise<void> {
  if (!p.backendNodeId) return;
  let path: string | null = null;
  try { path = await pickFile({ title: "Choose a file for the page" }); } catch { path = null; }
  cdp("DOM.setFileInputFiles", { files: path ? [path] : [], backendNodeId: p.backendNodeId });
}

/** An intercepted HTML5 drag in flight: its DragData, replayed as dragEnter/dragOver on moves and drop on release. */
let drag: { data: Record<string, unknown>; entered: boolean } | null = null;

// ------------------------------------------------------------------ <select> menus ----
// Headless Chrome paints no native popups: a <select>'s menu, a datalist's suggestions and the date picker open as
// widgets outside the page and never reach the screencast, while an invisibly open menu swallows the next clicks.
// A click (or Space/Enter/arrows) on a closed single <select> therefore opens a menu of ours over the stage instead,
// and picking sets the value in the page with input + change events. Datalist and date inputs keep working by typing.
export interface SelectOpt { t: string; v: string; s: boolean; d: boolean }
export interface SelectMenu { opts: SelectOpt[]; left: number; top: number; width: number }
let selMenu: SelectMenu | null = null;
const selListeners = new Set<() => void>();
const subscribeSel = (l: () => void) => { selListeners.add(l); return () => { selListeners.delete(l); }; };
function setSelMenu(m: SelectMenu | null): void { selMenu = m; for (const l of [...selListeners]) l(); }
export const useSelectMenu = (): SelectMenu | null => useSyncExternalStore(subscribeSel, () => selMenu, () => selMenu);
const SELECT_PROBE = (where: string) => `(() => { const el = ${where}; if (!el || el.tagName !== "SELECT" || el.multiple || el.size > 1 || el.disabled) return null;
  window.__fusedSel = el; const r = el.getBoundingClientRect();
  return { opts: [...el.options].map((o) => ({ t: o.text, v: o.value, s: o.selected, d: o.disabled })), r: [r.left, r.top, r.width, r.height] }; })()`;
/** Open our menu for the <select> at page point p (or the focused one when p is null). True when one opened. */
async function openSelectAt(p: { x: number; y: number } | null): Promise<boolean> {
  const hit = await evalIn<{ opts: SelectOpt[]; r: [number, number, number, number] } | null>(
    SELECT_PROBE(p ? `document.elementFromPoint(${p.x}, ${p.y})` : "document.activeElement"));
  if (!hit) return false;
  const img = $<HTMLImageElement>("fshot"), stage = $("stage");
  if (!img || !stage) return false;
  const ir = img.getBoundingClientRect(), sr = stage.getBoundingClientRect();
  const [vw, vh] = frameDims(link.meta, cur()?.viewport, [img.naturalWidth, img.naturalHeight]);
  const sx = ir.width / (vw || 1), sy = ir.height / (vh || 1);
  setSelMenu({ opts: hit.opts, left: ir.left - sr.left + hit.r[0] * sx, top: ir.top - sr.top + (hit.r[1] + hit.r[3]) * sy, width: hit.r[2] * sx });
  return true;
}
/** A row of our menu was picked (null: closed without picking). */
export function pickSelect(v: string | null): void {
  setSelMenu(null);
  if (v !== null) cdp("Runtime.evaluate", { expression: `(() => { const el = window.__fusedSel; if (!el) return; el.value = ${JSON.stringify(v)};
    el.dispatchEvent(new Event("input", { bubbles: true })); el.dispatchEvent(new Event("change", { bubbles: true })); el.focus(); })()` });
  focusCtl();
}

export function linkClose(): void {
  setSelMenu(null); drag = null;
  const ws = link.ws; link.ws = null; link.url = null; link.meta = null;
  if (ws) { try { if (ws.readyState === 1) ws.send(JSON.stringify({ id: ++link.id, method: "Page.stopScreencast" })); ws.close(); } catch { /* already gone */ } }
  publishLink();
}

/** A link you click may open a new tab; while you drive, follow it there (the worker switches, the socket URL changes, linkSync reconnects). */
export function followPopups(b: Bot): void {
  const tabs = b.browser?.tabs || [], ids = new Set(tabs.map((t) => t.id));
  if (inCtl() && link.tabs && tabs.length > link.tabs.size) {
    const seen = link.tabs, fresh = tabs.filter((t) => !seen.has(t.id) && !t.active).pop();
    if (fresh) { showBanner("That link opened a new tab; showing it here instead."); void act(() => api.tab(b.id, { tab: "switch", index: fresh.i }), true); }
  }
  link.tabs = ids;
}

/** The preview thumbnail changed: mirror it into the live view until frames arrive (OpenBot render()). */
export function mirrorThumb(u: string): void {
  if (!u || linked()) return;
  const img = $<HTMLImageElement>("fshot"); if (img) img.src = u;
}

// ------------------------------------------------------------------ open / take over / hand back ----
let switching = false, askedTakeover = false;

/** Keyboard focus while you drive: #fkeys, the hidden textarea in the stage (installLive forwards from it). An empty page has
 *  nothing to type into, so focus lands in the URL bar instead. */
export function focusCtl(): void {
  if (showUrl(cur()?.browser?.url)) ($("fkeys") || $("stage"))?.focus();
  else { const f = $<HTMLInputElement>("furl"); if (f) { f.value = ""; f.focus(); } }
}

export function openFull(): void {
  const b = cur(); if (!b) return;
  flushSync(() => setFast(true));  // #full must be showing before anything in it can take focus
  const shot = $<HTMLImageElement>("shot"), fshot = $<HTMLImageElement>("fshot");
  if (fshot) { const s = shot?.getAttribute("src"); if (s) fshot.src = s; else fshot.removeAttribute("src"); }
  if (b.control) focusCtl();
  link.tabs = null;
  askedTakeover = false;
  // An asleep browser has nothing to stream: wake it whenever it is not running, not only when the thumbnail wore the "asleep"
  // badge (a bot with no screenshot yet, or after the cache was cleared, showed "Connecting to the browser…" forever instead).
  if (!b.browser?.running) void act(() => api.wake(b.id), true);
  void poll().then(linkSync);  // status carries the driven tab's socket URL
  linkSync();
}
/** The bot menu's "Open live view" (selects the bot first when the menu belongs to another one). */
export function openLive(id?: string): void {
  if (id && id !== getState().sel) select(id);
  openFull();
}
/** The thumbnail opens the live view to watch. */
export function openFromThumb(): void {
  if (cur()) openFull();
}

export async function takeOver(): Promise<void> {
  const b = cur(); if (!b || b.control || switching) return;
  switching = true;
  try { await act(() => api.takeover(b.id)); } finally { switching = false; }
  focusCtl();
}
/** One exit: hand control back to the bot (if you had it); with close, leave the live view for the thread. */
export async function handBack(close: boolean): Promise<void> {
  if (switching) return;
  switching = true;
  const b = cur();
  try { if (b?.control) await act(() => api.giveback(b.id), true); } finally { switching = false; }
  if (close) { setFast(false); linkClose(); }
}
export const toggleCtl = (): Promise<void> => (cur()?.control ? handBack(false) : takeOver());

export function nav(op: "back" | "forward" | "reload"): void {
  const id = getState().sel;
  if (inCtl() && id) void act(() => api.nav(id, op), true);
}
/** #furl Enter: only while you drive. */
export async function gotoTyped(value: string): Promise<void> {
  if (!inCtl()) return;
  const u = value.trim(), id = getState().sel; if (!u || !id) return;
  await act(() => api.goto(id, furlTarget(u)));
}

/** Tab strip clicks: close (×), new (+) or switch; while the bot drives, the first one asks to take over. */
export async function tabstripClick(target: Element): Promise<void> {
  const b = cur();
  if (!b || !inFull() || switching) return;
  const x = target.closest<HTMLElement>("[data-close]"), n = target.closest("[data-new]"), t = target.closest<HTMLElement>(".tab");
  const sw = !!t && !t.classList.contains("active");
  if (!x && !n && !sw) return;
  if (!b.control) {
    const asked = askedTakeover;
    askedTakeover = true;
    if (!asked && !(await askConfirm("Take over?", "Switching tabs pauses the bot and you drive this page yourself. Hand back whenever you are done.", "Take over", false))) return;
    await takeOver();
    if (!cur()?.control) return;
  }
  const id = getState().sel; if (!id) return;
  if (x) { await act(() => api.tab(id, { tab: "close", index: Number(x.dataset.close) }), true); return; }
  // A fresh or blank tab drops focus into the URL bar so typing starts at once; a loaded one focuses the page.
  if (n) { await act(() => api.tab(id, { tab: "new" }), true); focusCtl(); return; }
  if (sw && t) { await act(() => api.tab(id, { tab: "switch", index: Number(t.dataset.i) }), true); focusCtl(); }
}

// ------------------------------------------------------------------ input forwarding ----
// Map to CSS viewport pixels (what CDP expects): the frame's own metadata, else the bot's viewport.
function toPage(e: MouseEvent): { x: number; y: number } | null {
  const img = $<HTMLImageElement>("fshot"); if (!img) return null;
  return toPageXY(e.clientX, e.clientY, img.getBoundingClientRect(), [img.naturalWidth, img.naturalHeight], link.meta, cur()?.viewport);
}
// `buttons` (the held-button bitmask, same encoding in DOM and CDP) is what lets Chrome see a drag and a right-button press.
const mouse = (type: string, p: { x: number; y: number }, e: MouseEvent, extra: Record<string, unknown> = {}) =>
  cdp("Input.dispatchMouseEvent", { type, x: p.x, y: p.y, modifiers: CDP_MODS(e), buttons: e.buttons, ...extra });

/**
 * Wire the stage (pointer, wheel, paste, keys), the document-level Esc, and the per-poll mirrors (follow popups,
 * keep the socket on the driven tab). Mount once (LiveView). Returns the teardown.
 */
export function installLive(stage: HTMLElement): () => void {
  let lastDown: LastDown = { t: 0, x: 0, y: 0, n: 0 };
  // Our <select> menu lives inside the stage: its clicks are its own, never page input (the stage's mousedown would otherwise
  // close the menu before the option's click could land).
  const inMenu = (e: Event): boolean => !!(e.target as Element | null)?.closest?.(".selmenu");
  // Moves are coalesced to one per animation frame: hover menus stay responsive without flooding the socket.
  let pendingMove: { p: { x: number; y: number }; e: MouseEvent } | null = null;
  const onMove = (e: MouseEvent) => {
    if (!inCtl() || inMenu(e)) return;
    const p = toPage(e); if (!p) return;
    const first = !pendingMove; pendingMove = { p, e };
    if (first) requestAnimationFrame(() => {
      const m = pendingMove; pendingMove = null; if (!m || !inCtl()) return;
      mouse("mouseMoved", m.p, m.e);
      if (drag) {
        cdp("Input.dispatchDragEvent", { type: drag.entered ? "dragOver" : "dragEnter", x: m.p.x, y: m.p.y, data: drag.data, modifiers: CDP_MODS(m.e) });
        drag.entered = true;
      }
    });
  };
  // A left press first asks the page what is under it: a closed <select> opens our menu instead of Chrome's invisible one.
  // The press is sent once that answer is in, and the matching release waits for it, so the two never cross.
  let pressed: Promise<void> = Promise.resolve();
  const onDown = (e: MouseEvent) => {
    if (!inCtl() || inMenu(e)) return; const p = toPage(e); if (!p) return;
    e.preventDefault(); ($("fkeys") || stage).focus();
    if (selMenu) setSelMenu(null);
    lastDown = nextDown(lastDown, performance.now(), p);
    const n = lastDown.n;
    pressed = (async () => {
      if (e.button === 0 && n === 1 && await openSelectAt(p)) { skipRelease = true; return; }
      mouse("mousePressed", p, e, { button: BTN[e.button] || "left", clickCount: n });
    })();
  };
  let skipRelease = false;
  const onUp = (e: MouseEvent) => {
    if (!inCtl() || inMenu(e)) return; const p = toPage(e) || { x: lastDown.x, y: lastDown.y };
    const n = lastDown.n;
    void pressed.then(() => {
      if (skipRelease) { skipRelease = false; return; }
      if (drag) {
        if (!drag.entered) cdp("Input.dispatchDragEvent", { type: "dragEnter", x: p.x, y: p.y, data: drag.data, modifiers: CDP_MODS(e) });
        cdp("Input.dispatchDragEvent", { type: "drop", x: p.x, y: p.y, data: drag.data, modifiers: CDP_MODS(e) });
        drag = null;
      }
      mouse("mouseReleased", p, e, { button: BTN[e.button] || "left", clickCount: n });
      setTimeout(poll, 700);  // a click may open a tab or change the title
    });
  };
  const onCtx = (e: MouseEvent) => { if (inCtl() && !inMenu(e)) e.preventDefault(); };
  // While the bot drives, a click on the page does nothing to it; offer to take over instead of silently ignoring the click.
  const onClick = async (e: MouseEvent) => {
    const b = cur();
    if (!b || inCtl() || switching || !inFull() || inMenu(e) || !toPage(e)) return;
    if (b.control || askedTakeover) { void takeOver(); return; }
    askedTakeover = true;
    if (await askConfirm("Take over?", "The bot pauses and you drive this page yourself. Hand back whenever you are done.", "Take over", false)) void takeOver();
  };
  const onPaste = (e: ClipboardEvent) => {
    if (!inCtl()) return;
    const text = e.clipboardData && e.clipboardData.getData("text/plain");
    if (!text) return;
    e.preventDefault();
    cdp("Input.insertText", { text });
  };
  const onWheel = (e: WheelEvent) => { if (!inCtl() || inMenu(e)) return; const p = toPage(e); if (!p) return; e.preventDefault(); mouse("mouseWheel", p, e, { deltaX: e.deltaX, deltaY: e.deltaY }); };
  // Keys arrive through #fkeys, a hidden textarea inside the stage: only an editable element gets composition events, so
  // dead keys (⌥e e → é) and IMEs (日本) compose there and land in the page as one Input.insertText on compositionend. Plain
  // keys are forwarded as CDP key events (keyAction) and prevented from typing into the textarea; it is emptied after each.
  const keys = $<HTMLTextAreaElement>("fkeys");
  const keyEv = (e: KeyboardEvent) => {
    if (!inFull() || document.activeElement === $("furl")) return;
    const k = e.key.toLowerCase();
    if (e.metaKey && ["w", "t", "q", "n", "l"].includes(k)) return;
    if (!inCtl()) return;
    if ((e.metaKey || e.ctrlKey) && k === "v") return;  // the paste event carries the text
    if (e.isComposing || e.keyCode === 229 || e.key === "Dead" || e.key === "Process") return;  // compositionend forwards it
    // History only on ⌘[ / ⌘] (never ⌥←/⌥→: those are word moves on macOS and belong to the page).
    if (e.type === "keydown" && e.metaKey && e.key === "[") { e.preventDefault(); nav("back"); return; }
    if (e.type === "keydown" && e.metaKey && e.key === "]") { e.preventDefault(); nav("forward"); return; }
    if (e.type === "keydown" && e.metaKey && k === "r") { e.preventDefault(); nav("reload"); return; }
    e.preventDefault();
    if (selMenu) { if (e.type === "keydown" && e.key === "Escape") setSelMenu(null); return; }  // the menu owns the keyboard
    if (e.type === "keydown" && !e.metaKey && !e.ctrlKey && !e.altKey && (e.key === " " || e.key === "Enter" || e.key === "ArrowDown" || e.key === "ArrowUp")) {
      // On a focused closed <select> these open Chrome's (invisible) menu: open ours instead, else forward as usual.
      void openSelectAt(null).then((opened) => { if (!opened) { const a = keyAction(e); if (a?.kind === "key") cdp("Input.dispatchKeyEvent", a.params as unknown as Record<string, unknown>); } });
      return;
    }
    const a = keyAction(e);
    if (a?.kind === "key") cdp("Input.dispatchKeyEvent", a.params as unknown as Record<string, unknown>);
    else if (a?.kind === "insert") cdp("Input.insertText", { text: a.text });
    if (keys) keys.value = "";
    if (e.type === "keydown" && e.key === "Enter") setTimeout(poll, 700);
  };
  const onCompose = (e: CompositionEvent) => {
    if (inCtl() && e.data) cdp("Input.insertText", { text: e.data });
    if (keys) keys.value = "";
  };
  // Whatever still reaches the textarea outside a composition (a key while the bot drives, autocorrect) is dropped at once.
  const onKeysInput = (e: Event) => { if (keys && !(e as InputEvent).isComposing) keys.value = ""; };
  const onDocKey = (e: KeyboardEvent) => { if (e.key === "Escape" && !inCtl() && inFull()) void handBack(true); };

  stage.addEventListener("mousemove", onMove);
  stage.addEventListener("mousedown", onDown);
  stage.addEventListener("mouseup", onUp);
  stage.addEventListener("contextmenu", onCtx);
  stage.addEventListener("click", onClick);
  stage.addEventListener("paste", onPaste);
  stage.addEventListener("wheel", onWheel, { passive: false });
  stage.addEventListener("keydown", keyEv);
  stage.addEventListener("keyup", keyEv);
  keys?.addEventListener("compositionend", onCompose);
  keys?.addEventListener("input", onKeysInput);
  document.addEventListener("keydown", onDocKey);

  // OpenBot ran renderFullMirrors() from every render(): follow popups and keep the socket on the driven tab after each poll.
  let lastBots = getState().bots, lastFast = getState().fast, lastSel = getState().sel;
  const unsub = subscribeStore(() => {
    const s = getState();
    if (s.bots === lastBots && s.fast === lastFast && s.sel === lastSel) return;  // a ?bot= change while open must follow too
    lastBots = s.bots; lastFast = s.fast; lastSel = s.sel;
    const b = cur(); if (b) followPopups(b);
    linkSync();
  });

  return () => {
    unsub();
    stage.removeEventListener("mousemove", onMove);
    stage.removeEventListener("mousedown", onDown);
    stage.removeEventListener("mouseup", onUp);
    stage.removeEventListener("contextmenu", onCtx);
    stage.removeEventListener("click", onClick);
    stage.removeEventListener("paste", onPaste);
    stage.removeEventListener("wheel", onWheel);
    stage.removeEventListener("keydown", keyEv);
    stage.removeEventListener("keyup", keyEv);
    keys?.removeEventListener("compositionend", onCompose);
    keys?.removeEventListener("input", onKeysInput);
    document.removeEventListener("keydown", onDocKey);
    linkClose();
  };
}
