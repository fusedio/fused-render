// The live view's link to the bot's Chrome (OpenBot live.js): the page talks CDP to the driven tab directly (the
// same trick DevTools' own screencast uses). One WebSocket per driven tab carries JPEG frames in and mouse/keyboard
// out: no worker hop, no file, no polling. Chrome only accepts a browser-origin handshake when launched with
// --remote-allow-origins (browser.py adds it).
//
// This module is the socket and the link state plus the open / take over / hand back actions. What rides on the
// socket lives beside it: live-page.ts (dialogs, file chooser, HTTP auth, what is intercepted while you drive),
// live-overlays.ts (the menus headless Chrome never paints), live-input.ts (mouse, keys, drag). Those register
// here through `onEvent` / `onOpen` / `onReset`, so nothing imports back into this file.
//
// Frames go straight into #fshot (never through React state); whether the socket is up is a tiny external store
// (useLinked) so #full's .nolink / .ctl classes and the status strip re-render with it. `state.fast` is "#full is
// showing": openFull() turns it on, handBack(true) turns it off.
import { useSyncExternalStore } from "react";
import { flushSync } from "react-dom";
import { askConfirm } from "../dialogs/ask";
import { act, cur, getState, onHandover, poll, select, setFast, showBanner } from "../state/store";
import { api, type Bot } from "./api";
import { CAST, furlTarget, showUrl, type FrameMeta } from "./live";

export const $ = <T extends HTMLElement = HTMLElement>(id: string) => document.getElementById(id) as T | null;
const activeWs = (b: Bot | undefined): string | null => (b?.browser?.tabs || []).find((t) => t.active)?.ws || null;

const link: { ws: WebSocket | null; url: string | null; id: number; tabs: Set<string> | null; meta: FrameMeta | null; retryAt: number } =
  { ws: null, url: null, id: 0, tabs: null, meta: null, retryAt: 0 };
/** The last frame's viewport metadata from Chrome (CSS px the frame covers), for pointer and overlay geometry. */
export const frameMeta = (): FrameMeta | null => link.meta;

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

// ------------------------------------------------------------------ hooks for the modules riding on the socket ----
type Handler = (p: Record<string, unknown>) => void;
const handlers = new Map<string, Handler>();
/** Handle one CDP event method (the last registration wins). */
export function onEvent(method: string, fn: Handler): void { handlers.set(method, fn); }
/** Run after each socket opens (the session is fresh: re-apply per-session state). */
export const onOpen: Array<() => void> = [];
/** Run when the socket goes away, however it went (forget per-session state). */
export const onReset: Array<() => void> = [];

// ------------------------------------------------------------------ calls ----
/** Fire-and-forget: Chrome's replies are not needed for frames or input. */
export function cdp(method: string, params: object = {}): void {
  if (!linked()) return;
  link.ws!.send(JSON.stringify({ id: ++link.id, method, params }));
}
/** The few calls whose reply matters. null when not linked, after 3 s, or if the socket closes meanwhile. */
const pending = new Map<number, (r: Record<string, unknown> | null) => void>();
function cdpCall(method: string, params: object = {}): Promise<Record<string, unknown> | null> {
  if (!linked()) return Promise.resolve(null);
  const id = ++link.id;
  return new Promise((resolve) => {
    const t = window.setTimeout(() => { pending.delete(id); resolve(null); }, 3000);
    pending.set(id, (r) => { window.clearTimeout(t); resolve(r); });
    link.ws!.send(JSON.stringify({ id, method, params }));
  });
}
/** Run an expression in the driven page and return its value (undefined on error or when not linked). */
export async function evalIn<T = unknown>(expression: string): Promise<T | undefined> {
  const r = await cdpCall("Runtime.evaluate", { expression, returnByValue: true });
  return (r as { result?: { value?: T } } | null)?.result?.value;
}

// ------------------------------------------------------------------ the socket ----
/** Connect to the driven tab while the view is open; follow tab switches and relaunches by reconnecting when its socket
 *  URL changes. A failed handshake is retried no sooner than a second later (the store polls every 400 ms). */
export function linkSync(): void {
  const want = inFull() ? activeWs(cur()) : null;
  if (link.url === want && link.ws && link.ws.readyState <= 1) return;
  if (link.url === want && want && !link.ws && Date.now() < link.retryAt) return;
  linkClose();
  if (!want) return;
  link.url = want;
  const ws = new WebSocket(want); link.ws = ws;
  ws.onopen = () => {
    if (link.ws !== ws) return;
    cdp("Page.enable");
    // Headless Chrome composites only a tab it treats as visible and focused; focus emulation is per CDP session, so this
    // socket asks for it too (the bot's own sessions do the same in browser.py _foreground).
    cdp("Page.bringToFront"); cdp("Emulation.setFocusEmulationEnabled", { enabled: true });
    cdp("Page.startScreencast", { format: "jpeg", ...CAST, everyNthFrame: 1 });
    publishLink();
    for (const f of onOpen) f();
  };
  ws.onmessage = (ev) => {
    if (link.ws !== ws) return;  // a late message from a tab we left
    const m = JSON.parse(String(ev.data)), p = (m.params || {}) as Record<string, unknown>;
    if (typeof m.id === "number" && pending.has(m.id)) { pending.get(m.id)!(m.result ?? null); pending.delete(m.id); return; }
    if (m.method === "Page.screencastFrame") {
      link.meta = (p.metadata as FrameMeta) || link.meta;
      const img = $<HTMLImageElement>("fshot"); if (img) img.src = "data:image/jpeg;base64," + p.data;
      cdp("Page.screencastFrameAck", { sessionId: p.sessionId });
    } else if (m.method === "Page.frameNavigated" && !(p.frame as { parentId?: string } | undefined)?.parentId) {
      const furl = $<HTMLInputElement>("furl");
      if (furl && document.activeElement !== furl) furl.value = showUrl((p.frame as { url?: string }).url);
      void poll();  // title and tab strip
    } else if (m.method === "Page.screencastVisibilityChanged" && p.visible === false) {
      cdp("Page.bringToFront"); cdp("Emulation.setFocusEmulationEnabled", { enabled: true });
    } else {
      handlers.get(m.method)?.(p);
    }
  };
  ws.onclose = ws.onerror = () => {
    if (link.ws !== ws) return;
    link.ws = null; link.retryAt = Date.now() + 1000;
    settleAll(); publishLink();
    for (const f of onReset) f();
  };
}
function settleAll() { for (const r of [...pending.values()]) r(null); pending.clear(); }

export function linkClose(): void {
  const ws = link.ws; link.ws = null; link.url = null; link.meta = null;
  if (ws) {
    ws.onclose = ws.onerror = null;  // this close is ours: reset once, below, not again from the handler
    try { if (ws.readyState === 1) ws.send(JSON.stringify({ id: ++link.id, method: "Page.stopScreencast" })); ws.close(); } catch { /* already gone */ }
    settleAll();
    for (const f of onReset) f();
  }
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

/** Keyboard focus while you drive: #fkeys, the hidden textarea in the stage (live-input.ts forwards from it). An empty
 *  page has nothing to type into, so focus lands in the URL bar instead. preventScroll: #fkeys sits at the stage's top. */
export function focusCtl(): void {
  if (showUrl(cur()?.browser?.url)) $("fkeys")?.focus({ preventScroll: true });
  else { const f = $<HTMLInputElement>("furl"); if (f) { f.value = ""; f.focus(); } }
}

/** Open the live view on the selected bot (the thumbnail, the bot menu, the Browsers dialog's Sign in…). */
/** `focus: false` (a hand-over landing while the composer holds unsent text) opens Stage without taking the keyboard. */
export function openFull({ focus = true }: { focus?: boolean } = {}): void {
  const b = cur(); if (!b) return;
  flushSync(() => setFast(true));  // #full must be showing before anything in it can take focus
  const shot = $<HTMLImageElement>("shot"), fshot = $<HTMLImageElement>("fshot");
  if (fshot) { const s = shot?.getAttribute("src"); if (s) fshot.src = s; else fshot.removeAttribute("src"); }
  if (b.control && focus) focusCtl();
  link.tabs = null;
  askedTakeover = false;
  // An asleep browser has nothing to stream: wake it whenever it is not running, not only when the thumbnail wore the "asleep"
  // badge (a bot with no screenshot yet, or after the cache was cleared, showed "Connecting to the browser…" forever instead).
  if (!b.browser?.running) void act(() => api.wake(b.id), true);
  void poll().then(linkSync);  // status carries the driven tab's socket URL
  linkSync();
}
// The store's poll opens Stage through this on a bot-initiated hand-over (state/store.ts requestStage).
onHandover((o) => openFull(o), () => { setFast(false); linkClose(); });
/** The bot menu's "Open live view" (selects the bot first when the menu belongs to another one). */
export function openLive(id?: string): void {
  if (id && id !== getState().sel) select(id);
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
/** A take-over is in flight (clicks meanwhile are ignored). */
export const isSwitching = (): boolean => switching;

/** One navigation in flight per bot: the worker serialises these on the browser lock, so a second one would only queue
 *  behind the first (a held ⌘[ once stacked fifty of them, each waiting its turn). */
const navBusy = new Set<string>();
async function navigate(id: string, call: () => Promise<unknown>, silent: boolean): Promise<void> {
  if (navBusy.has(id)) return;
  navBusy.add(id);
  try { await act(call, silent); } finally { navBusy.delete(id); }
}
export function nav(op: "back" | "forward" | "reload"): void {
  const id = getState().sel;
  if (inCtl() && id) void navigate(id, () => api.nav(id, op), true);
}
/** #furl Enter: only while you drive. */
export async function gotoTyped(value: string): Promise<void> {
  if (!inCtl()) return;
  const u = value.trim(), id = getState().sel; if (!u || !id) return;
  await navigate(id, () => api.goto(id, furlTarget(u)), false);
}

/** The first click on the page or the tab strip while the bot drives asks once per opening; later ones take over at once. */
export async function askTakeOver(text: string): Promise<boolean> {
  if (askedTakeover) return true;
  askedTakeover = true;
  return askConfirm("Take over?", text, "Take over", false);
}

/** Tab strip clicks: close (×), new (+) or switch; while the bot drives, the first one asks to take over. */
export async function tabstripClick(target: Element): Promise<void> {
  const b = cur();
  if (!b || !inFull() || switching) return;
  const x = target.closest<HTMLElement>("[data-close]"), n = target.closest("[data-new]"), t = target.closest<HTMLElement>(".tab");
  const sw = !!t && !t.classList.contains("active");
  if (!x && !n && !sw) return;
  if (!b.control) {
    if (!(await askTakeOver("Switching tabs pauses the bot and you drive this page yourself. Hand back whenever you are done."))) return;
    await takeOver();
    if (!cur()?.control) return;
  }
  const id = getState().sel; if (!id) return;
  if (x) { await act(() => api.tab(id, { tab: "close", index: Number(x.dataset.close) }), true); return; }
  // A fresh or blank tab drops focus into the URL bar so typing starts at once; a loaded one focuses the page.
  if (n) { await act(() => api.tab(id, { tab: "new" }), true); focusCtl(); return; }
  if (sw && t) { await act(() => api.tab(id, { tab: "switch", index: Number(t.dataset.i) }), true); focusCtl(); }
}
