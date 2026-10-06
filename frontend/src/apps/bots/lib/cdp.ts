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
import { askConfirm } from "../dialogs/ask";
import { act, cur, getState, poll, select, setFast, showBanner, showToast, subscribe as subscribeStore } from "../state/store";
import { api, type Bot } from "./api";
import { BTN, CAST, CDP_MODS, furlTarget, keyParams, nextDown, showUrl, toPageXY, type FrameMeta, type LastDown } from "./live";

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

// ------------------------------------------------------------------ the socket ----
/** Connect to the driven tab while the view is open; follow tab switches, relaunches and pop-outs by reconnecting when its socket URL changes. */
export function linkSync(): void {
  const b = cur();
  const want = inFull() ? activeWs(b) : null;
  const vis = !!b?.browser?.visible;
  if (link.url === want && link.ws && link.ws.readyState <= 1) {
    if (linked() && !vis) cdp("Page.bringToFront");
    return;
  }
  linkClose();
  if (!want) return;
  link.url = want;
  const ws = new WebSocket(want); link.ws = ws;
  ws.onopen = () => {
    cdp("Page.enable");
    if (!vis) { cdp("Page.bringToFront"); cdp("Emulation.setFocusEmulationEnabled", { enabled: true }); }
    cdp("Page.startScreencast", { format: "jpeg", ...CAST, everyNthFrame: 1 });
    publishLink();
  };
  ws.onmessage = (ev) => {
    const m = JSON.parse(String(ev.data)), p = m.params || {};
    if (m.method === "Page.screencastFrame") {
      link.meta = p.metadata || link.meta;
      const img = $<HTMLImageElement>("fshot"); if (img) img.src = "data:image/jpeg;base64," + p.data;
      cdp("Page.screencastFrameAck", { sessionId: p.sessionId });
    } else if (m.method === "Page.frameNavigated" && !p.frame?.parentId) {
      const furl = $<HTMLInputElement>("furl");
      if (furl && document.activeElement !== furl) furl.value = showUrl(p.frame?.url);
      void poll();  // title and tab strip
    } else if (m.method === "Page.screencastVisibilityChanged" && p.visible === false && !vis) {
      cdp("Page.bringToFront"); cdp("Emulation.setFocusEmulationEnabled", { enabled: true });
    } else if (m.method === "Page.javascriptDialogOpening") {
      // A page dialog would freeze the tab; show it and accept it so the human (or the bot) can carry on.
      showToast({ text: `${p.type}: ${p.message || ""}`, ts: Date.now() / 1000 });
      cdp("Page.handleJavaScriptDialog", { accept: true, promptText: p.defaultPrompt || "" });
    }
  };
  ws.onclose = ws.onerror = () => { if (link.ws === ws) { link.ws = null; publishLink(); } };
}

export function linkClose(): void {
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

/** An empty page has nothing to type into, so focus lands in the URL bar instead of on the stage. */
export function focusCtl(): void {
  if (showUrl(cur()?.browser?.url)) $("stage")?.focus();
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
/** The thumbnail opens the live view to watch. A popped-out bot already has a real window on the desktop; opening the mirror on top of it just fights it for focus. */
export function openFromThumb(): void {
  const b = cur(); if (!b) return;
  if (b.browser?.visible) { showToast({ text: "This bot's browser is open as a real window on your desktop.", ts: Date.now() / 1000 }); return; }
  openFull();
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
const mouse = (type: string, p: { x: number; y: number }, e: MouseEvent, extra: Record<string, unknown> = {}) =>
  cdp("Input.dispatchMouseEvent", { type, x: p.x, y: p.y, modifiers: CDP_MODS(e), ...extra });

/**
 * Wire the stage (pointer, wheel, paste, keys), the document-level Esc, and the per-poll mirrors (follow popups,
 * keep the socket on the driven tab). Mount once (LiveView). Returns the teardown.
 */
export function installLive(stage: HTMLElement): () => void {
  let lastDown: LastDown = { t: 0, x: 0, y: 0, n: 0 };
  // Moves are coalesced to one per animation frame: hover menus stay responsive without flooding the socket.
  let pendingMove: { p: { x: number; y: number }; e: MouseEvent } | null = null;
  const onMove = (e: MouseEvent) => {
    if (!inCtl()) return;
    const p = toPage(e); if (!p) return;
    const first = !pendingMove; pendingMove = { p, e };
    if (first) requestAnimationFrame(() => { const m = pendingMove; pendingMove = null; if (m && inCtl()) mouse("mouseMoved", m.p, m.e); });
  };
  const onDown = (e: MouseEvent) => {
    if (!inCtl()) return; const p = toPage(e); if (!p) return;
    e.preventDefault(); stage.focus();
    lastDown = nextDown(lastDown, performance.now(), p);
    mouse("mousePressed", p, e, { button: BTN[e.button] || "left", clickCount: lastDown.n });
  };
  const onUp = (e: MouseEvent) => {
    if (!inCtl()) return; const p = toPage(e) || { x: lastDown.x, y: lastDown.y };
    mouse("mouseReleased", p, e, { button: BTN[e.button] || "left", clickCount: lastDown.n });
    setTimeout(poll, 700);  // a click may open a tab or change the title
  };
  const onCtx = (e: MouseEvent) => { if (inCtl()) e.preventDefault(); };
  // While the bot drives, a click on the page does nothing to it; offer to take over instead of silently ignoring the click.
  const onClick = async (e: MouseEvent) => {
    const b = cur();
    if (!b || inCtl() || switching || !inFull() || !toPage(e)) return;
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
  const onWheel = (e: WheelEvent) => { if (!inCtl()) return; const p = toPage(e); if (!p) return; e.preventDefault(); mouse("mouseWheel", p, e, { deltaX: e.deltaX, deltaY: e.deltaY }); };
  const keyEv = (e: KeyboardEvent) => {
    if (!inFull() || document.activeElement === $("furl")) return;
    const k = e.key.toLowerCase();
    if (e.metaKey && ["w", "t", "q", "n", "l"].includes(k)) return;
    if (!inCtl()) return;
    if ((e.metaKey || e.ctrlKey) && k === "v") return;  // the paste event carries the text
    if (e.type === "keydown" && ((e.altKey && e.key === "ArrowLeft") || (e.metaKey && e.key === "["))) { e.preventDefault(); nav("back"); return; }
    if (e.type === "keydown" && ((e.altKey && e.key === "ArrowRight") || (e.metaKey && e.key === "]"))) { e.preventDefault(); nav("forward"); return; }
    if (e.type === "keydown" && e.metaKey && k === "r") { e.preventDefault(); nav("reload"); return; }
    e.preventDefault();
    cdp("Input.dispatchKeyEvent", keyParams(e) as unknown as Record<string, unknown>);
    if (e.type === "keydown" && e.key === "Enter") setTimeout(poll, 700);
  };
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
    document.removeEventListener("keydown", onDocKey);
    linkClose();
  };
}
