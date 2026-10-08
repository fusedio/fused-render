// Shared state (OpenBot core.js `state` + chat.js's unread maps), as a tiny external store for useSyncExternalStore.
//
// The snapshot is replaced (never mutated) on every change, so `useBots()` re-renders exactly when something
// changed, and per-bot event arrays keep their identity when that bot got nothing new (memo-friendly).
// Changes made inside batch() publish once, at the end. Module code reads getState(); React reads useBots().
//
// Also here: the poll loop (one in flight), act(), the banner, the status toast, select() + the `?bot=` URL,
// the UI request slots (dialog / panel / context menu) the panes open each other with, and the Builds chip.
import { useSyncExternalStore } from "react";
import { api, apiHooks, type Bot, type BotEvent, type ImessageState, type SlowCall, type UsageSummary } from "../lib/api";
import { loadSeen, newMarkFor, saveSeen, unreadOf, unviewedOf } from "../lib/unread";
import { notifyEvents, updateTitle } from "../lib/notify";
import type { NewBotPick } from "../lib/presets";

export { SLOW_MS, STALL_MS } from "../lib/api";

// ------------------------------------------------------------------ shape ----
export interface Toast { id: number; text: string; label: string; dot: string; ts: number; out: boolean }
export interface BuildsChip { n: string; live: boolean; warn: boolean; fresh: boolean; title: string; hidden: boolean }
/** A dialog another module asked for. Known kinds: newBot, settings, routines, skills, usage (dialogs/Dialogs.tsx owns them).
 *  newBot without `pick` is the preset chooser (#pmodal); with `pick` it is the bot dialog filled in from that pick. */
export interface DialogReq { kind: "newBot" | "settings" | "routines" | "skills" | "usage" | (string & {}); id?: string | null; pick?: NewBotPick; [k: string]: unknown }
/** The bot context menu (components/BotMenu.tsx): openMenu(id, x, y, full, live, alignRight) in OpenBot. */
export interface MenuReq { id: string; x: number; y: number; full?: boolean; live?: boolean; alignRight?: boolean }
export type PanelName = "builds" | "apps";

export interface BotsState {
  bots: Bot[];
  /** id → merged events, oldest first, capped at EVENT_CAP. */
  events: Record<string, BotEvent[]>;
  /** id → last seq the page holds (sent as `cursors`). */
  cursors: Record<string, number>;
  sel: string | null;
  usage: UsageSummary | null;
  imessage: ImessageState | null;
  /** id → last seq as of opening that bot (localStorage browser-bot.seen). */
  seen: Record<string, number>;
  /** id → the "N new messages" baseline. */
  base: Record<string, number>;
  /** id → seqs scrolled into view at least once. Mutated in place by the thread's observer (see viewedSet). */
  viewed: Record<string, Set<number>>;
  /** Where the "New" rule sits; set when a bot is opened, cleared on switching. */
  newMark: { id: string; seq: number } | null;
  /** The pill's count (unviewed messages below you). */
  newCount: number;
  /** The thread sits at its end (following along). */
  pinned: boolean;
  showHidden: boolean;
  /** A poll landed while the live view was open; the thread pins to the end when it closes. */
  renderDirty: boolean;
  /** Calls slower than SLOW_MS (last 30). */
  slow: SlowCall[];
  /** The live view is open: polls every 400 ms and ask for `fast` status. */
  fast: boolean;
  /** Bumped by select() (and on leaving the live view after renderDirty): OpenBot's render(true), "scroll the thread to the end on a new bot". */
  scrollThread: number;
  /** jumpTo(): the message the thread scrolls to once it renders that bot (null once done). */
  scrollSeq: { bot: string; seq: number } | null;
  /** Set by send(): the next render shows the message you just sent. The thread clears it (setScrollToEnd(false)). */
  scrollToEnd: boolean;
  banner: { show: boolean; text: string };
  toasts: Toast[];
  buildsChip: BuildsChip;
  ui: { dialog: DialogReq | null; panel: PanelName | null; menu: MenuReq | null };
}

export const EVENT_CAP = 600;
const EMPTY: BotEvent[] = [];

const initialSel = (): string | null => {
  try { return new URLSearchParams(location.search).get("bot") || null; } catch { return null; }
};

let S: BotsState = {
  bots: [], events: {}, cursors: {}, sel: typeof location === "undefined" ? null : initialSel(),
  usage: null, imessage: null,
  seen: typeof localStorage === "undefined" ? {} : loadSeen(), base: {}, viewed: {}, newMark: null, newCount: 0, pinned: true,
  showHidden: false, renderDirty: false, slow: [], fast: false, scrollThread: 0, scrollToEnd: false, scrollSeq: null,
  banner: { show: false, text: "" }, toasts: [],
  buildsChip: { n: "", live: false, warn: false, fresh: false, title: "Tasks · Claude tasks that create fused apps", hidden: false },
  ui: { dialog: null, panel: null, menu: null },
};

// ------------------------------------------------------------------ plumbing ----
const listeners = new Set<() => void>();
let depth = 0, pending = false;
function emit() { for (const l of [...listeners]) l(); }
/** Merge a patch into a fresh snapshot and publish (deferred inside batch()). */
function commit(patch: Partial<BotsState>) {
  S = { ...S, ...patch };
  if (depth) pending = true; else emit();
}
/** Run fn with every change it makes published once, at the end. */
export function batch<T>(fn: () => T): T {
  depth++;
  try { return fn(); }
  finally { if (--depth === 0 && pending) { pending = false; emit(); } }
}
export const getState = (): BotsState => S;
export const subscribe = (l: () => void): (() => void) => { listeners.add(l); return () => { listeners.delete(l); }; };
/** Escape hatch for fields without a setter of their own. */
export const setState = (patch: Partial<BotsState>): void => commit(patch);
/** Republish unchanged state (OpenBot's bare render()), e.g. after mutating viewed in place. */
export const touch = (): void => commit({});
/** The whole state; re-renders on every change. */
export const useBots = (): BotsState => useSyncExternalStore(subscribe, getState, getState);
/** One derived value; re-renders only when it changes (must return a primitive or a stable reference). */
export function useBotsSelector<T>(sel: (s: BotsState) => T): T {
  return useSyncExternalStore(subscribe, () => sel(S), () => sel(S));
}

// ------------------------------------------------------------------ helpers ----
/** The selected bot. */
export const cur = (): Bot | undefined => S.bots.find((b) => b.id === S.sel);
export const botById = (id: string | null | undefined): Bot | undefined => (id ? S.bots.find((b) => b.id === id) : undefined);
/** A bot's merged events (a stable empty array when none). */
export const eventsOf = (id: string | null | undefined): BotEvent[] => (id && S.events[id]) || EMPTY;
export const errMsg = (e: unknown): string => String((e as { message?: unknown })?.message || e);

// ------------------------------------------------------------------ banner ----
export function showBanner(msg: string): void { commit({ banner: { show: true, text: msg } }); }
export function hideBanner(): void { if (S.banner.show) commit({ banner: { ...S.banner, show: false } }); }

// ------------------------------------------------------------------ unread ----
export function markSeen(id: string | null | undefined, seq: number): void {
  if (!id || S.seen[id] === seq) return;
  const seen = { ...S.seen, [id]: seq };
  saveSeen(seen);
  commit({ seen });
}
export const unreadCount = (b: Bot): number => unreadOf(S.seen[b.id], eventsOf(b.id), b);
/** The "new" rule sits at the seen mark as of opening this bot and stays put until you switch bots; set only when something is past it, else cleared. */
export function setNewMark(b: Bot): boolean {
  const { base, newMark } = newMarkFor(S.seen[b.id], b);
  commit({ base: { ...S.base, [b.id]: base }, newMark });
  return !!newMark;
}
/** Messages past the pill baseline never scrolled into view. */
export const unviewed = (b: Bot, evs: BotEvent[] = eventsOf(b.id)): BotEvent[] => unviewedOf(S.base[b.id], S.viewed[b.id], evs);
/** The bot's viewed-seq set, created on first use; mutate it, then setNewCount(). */
export function viewedSet(id: string): Set<number> { return S.viewed[id] || (S.viewed[id] = new Set()); }
export function setBase(id: string, seq: number): void { commit({ base: { ...S.base, [id]: seq } }); }
export function setNewCount(n: number): void { if (S.newCount !== n) commit({ newCount: n }); }
export function setPinned(on: boolean): void { if (S.pinned !== on) commit({ pinned: on }); }
export function setScrollToEnd(on: boolean): void { if (S.scrollToEnd !== on) commit({ scrollToEnd: on }); }
export function setShowHidden(on: boolean): void { commit({ showHidden: on }); }

// ------------------------------------------------------------------ toast ----
// System notes are ephemeral status; text maps to a short label + status dot, unmapped text shows as is.
export const TOAST_LABELS: [RegExp, string, string][] = [
  [/^Paused$/, "Paused", "paused"], [/^Resumed$/, "Resumed", "running"], [/^Stop requested$/, "Stopping…", "paused"], [/^Stopped$/, "Stopped", ""],
  [/^Opened this bot's browser/, "Opened on desktop", "running"], [/^(Desktop window closed|Browser is headless again)/, "Docked", ""],
  [/^Browser closed after/, "Browser asleep", ""], [/^Routine .* (fired|run now)/, "Routine started", "running"], [/^Routine .* skipped/, "Routine skipped", "paused"],
  [/^Task (received|started)/, "Task started", "running"], [/^Saved playbook/, "Playbook saved", ""], [/^Learning a playbook/, "Learning a playbook", "running"],
  [/^(Build|Update) started/, "Build started", "running"],
];
export const toastLabel = (t: string): [string, string] => { for (const [re, l, d] of TOAST_LABELS) if (re.test(t)) return [l, d]; return [t, ""]; };
export const TOAST_HOLD_MS = 4000, TOAST_FRESH_S = 15, TOAST_OUT_MS = 320;
let toastTimer: ReturnType<typeof setTimeout> | null = null, toastSeq = 0;
/** Fade the given pills out (class .out), then drop them. */
function dropToasts(ids: number[]) {
  if (!ids.length) return;
  commit({ toasts: S.toasts.map((t) => (ids.includes(t.id) ? { ...t, out: true } : t)) });
  setTimeout(() => commit({ toasts: S.toasts.filter((t) => !ids.includes(t.id)) }), TOAST_OUT_MS);
}
export function showToast(ev: Pick<BotEvent, "text" | "ts">): void {
  const [label, dot] = toastLabel(ev.text || "");
  batch(() => {
    dropToasts(S.toasts.filter((t) => !t.out).map((t) => t.id));
    commit({ toasts: [...S.toasts, { id: ++toastSeq, text: ev.text || "", label, dot, ts: ev.ts, out: false }] });
  });
  armToast();
}
/** Hovering a pill keeps it readable (mouseenter). */
export function holdToast(): void { if (toastTimer) clearTimeout(toastTimer); }
/** (Re)start the hold timer (mouseleave). */
export function armToast(): void { if (toastTimer) clearTimeout(toastTimer); toastTimer = setTimeout(hideToast, TOAST_HOLD_MS); }
export function hideToast(): void { dropToasts(S.toasts.filter((t) => !t.out).map((t) => t.id)); }
export function clearToast(): void { if (toastTimer) clearTimeout(toastTimer); if (S.toasts.length) commit({ toasts: [] }); }

// ------------------------------------------------------------------ select ----
const selectListeners = new Set<(id: string | null) => void>();
/** Called when the selection moves to another bot (before it moves): the composer drops its reply quote here. */
export function onSelectChange(cb: (id: string | null) => void): () => void { selectListeners.add(cb); return () => { selectListeners.delete(cb); }; }

/** The route this page lives on in the shell. The store outlives the route (module state), so a poll that lands
 *  after the reader navigated away must not write `?bot=` onto whatever page they are on now. */
export const BOTS_PATH = "/bots";

function writeUrlBot(id: string | null) {
  try {
    if (location.pathname !== BOTS_PATH) return;
    const u = new URL(location.href);
    if (id) u.searchParams.set("bot", id); else u.searchParams.delete("bot");
    if (u.href !== location.href) history.replaceState(history.state, "", u.href);
  } catch { /* no history (tests) */ }
}

const openedOnce = new Set<string>();
const openRetryAt: Record<string, number> = {};
const OPEN_RETRY_MS = 3_000;
/** Tell the server the bot is open (the Bots page shows it). Once per bot per page load, except while Super Bot's first
 *  task is `pending` (Claude not linked or not measured yet) or the call failed: then it is asked again every
 *  OPEN_RETRY_MS so linking Claude later, or a measure landing, still starts it. The server rate-limits the measure
 *  itself (one a minute) and finishes a recent open on its own once it lands, so asking often is cheap and a fallback. */
function sendOpen(id: string): void {
  if (openedOnce.has(id) || (openRetryAt[id] || 0) > Date.now()) return;
  openedOnce.add(id);
  void act(() => api.open(id), true).then((r) => {
    if (r === undefined || r.setup === "pending") { openedOnce.delete(id); openRetryAt[id] = Date.now() + OPEN_RETRY_MS; }
  });
}

/** A user gesture opened the bot (a row click): select it and send the open. */
export function openBot(id: string): void {
  select(id);
  sendOpen(id);
}

export function select(id: string | null): void {
  batch(() => {
    if (id !== S.sel) { for (const cb of [...selectListeners]) cb(id); clearToast(); }  // a reply and a status toast belong to one bot
    commit({ sel: id, scrollThread: S.scrollThread + 1 });
    writeUrlBot(id);
    const b = cur(); if (b) { setNewMark(b); markSeen(b.id, b.seq); }
  });
}

/** Open `id`'s chat at message `seq` ("Read more" on a hand-off line). The thread clears scrollSeq once it has scrolled. */
export function jumpTo(id: string, seq: number): void {
  if (!S.bots.some((b) => b.id === id)) return;
  batch(() => { select(id); commit({ scrollSeq: { bot: id, seq } }); });
}
export const clearScrollSeq = (): void => { if (S.scrollSeq) commit({ scrollSeq: null }); };

// Another writer (an embedded app's params, back/forward) moved `?bot=`: follow it without the select() resets.
function onUrlChange() {
  const id = new URLSearchParams(location.search).get("bot");
  if (id && id !== S.sel && S.bots.find((b) => b.id === id)) commit({ sel: id, scrollThread: S.scrollThread + 1 });
}

/** The menu-bar Dock's bot tile (fused_render/mac_window.py `show_bot`) lands here: a user gesture from outside
 *  the page, so it is a full `openBot` — the select() resets (reply quote, toast) and the open call — not the quiet
 *  URL follow above. */
export const OPEN_EVENT = "fused:bots-open";
function onOpenEvent(e: Event) {
  const id = (e as CustomEvent<{ id?: unknown }>).detail?.id;
  if (typeof id === "string" && id) openBot(id);
}

// ------------------------------------------------------------------ ui slots ----
export const openDialog = (req: DialogReq): void => commit({ ui: { ...S.ui, dialog: req, menu: null } });
export const closeDialog = (): void => { if (S.ui.dialog) commit({ ui: { ...S.ui, dialog: null } }); };
export const openPanel = (panel: PanelName): void => commit({ ui: { ...S.ui, panel, menu: null } });
export const closePanel = (): void => { if (S.ui.panel) commit({ ui: { ...S.ui, panel: null } }); };
export const openMenu = (req: MenuReq): void => commit({ ui: { ...S.ui, menu: req } });
export const closeMenu = (): void => { if (S.ui.menu) commit({ ui: { ...S.ui, menu: null } }); };
/** builds/ owns the numbers; the list footer renders them. */
export const setBuildsChip = (patch: Partial<BuildsChip>): void => commit({ buildsChip: { ...S.buildsChip, ...patch } });

// ------------------------------------------------------------------ poll ----
// One poll in flight at a time; one requested meanwhile runs after it settles, so callers never see pre-action state.
let pollBusy: Promise<void> | null = null;
export function poll(): Promise<void> {
  if (pollBusy) return pollBusy.then(poll);
  return (pollBusy = pollOnce().finally(() => { pollBusy = null; }));
}

export async function pollOnce(): Promise<void> {
  try {
    const r = await api.status({ cursors: S.cursors, shot_for: S.sel || "", fast: S.fast });
    batch(() => {
      hideBanner();
      const first = !S.bots.length;
      const events = { ...S.events }, cursors = { ...S.cursors };
      let toast: BotEvent | undefined;
      for (const b of r.bots) {
        let list = events[b.id] || [];
        if (b.events.length) {
          list = list.concat(b.events);
          cursors[b.id] = b.seq;
          if (!first) notifyEvents(b, b.events);
          // Fresh system notes for the bot on screen surface as a toast; never on first load, never stale ones. Hand-off
          // lines (docs §11) render in the thread instead, so they do not toast as well.
          if (!first && b.id === S.sel) toast = b.events.filter((e) => e.role === "system" && e.source !== "handoff" && Date.now() / 1000 - e.ts < TOAST_FRESH_S).pop() || toast;
        }
        if (!(b.id in cursors)) cursors[b.id] = b.seq;
        if (list.length > EVENT_CAP) list = list.slice(list.length - EVENT_CAP);
        events[b.id] = list;
      }
      commit({
        bots: r.bots, events, cursors,
        ...(r.usage ? { usage: r.usage } : {}), ...(r.imessage ? { imessage: r.imessage } : {}),
        ...(S.fast ? { renderDirty: true } : {}),
      });
      if (toast) showToast(toast);
      // A bot picked from the URL skips select(): place its "New" rule and clear its dot once, on first load. Later polls leave "seen" alone so the dot lights while you watch.
      if (first && S.sel && !document.hidden) { const c = cur(); if (c) { setNewMark(c); markSeen(c.id, c.seq); } }
      // Nothing selected yet: Super Bot (seeded on first run, registry.seed_super) is the chat a new user should land in.
      if (!S.sel && r.bots.length) select(r.bots.find((b) => b.kind === "super" && !b.hidden)?.id || r.bots.filter((b) => !b.hidden)[0]?.id || r.bots[0].id);
      if (S.sel && !r.bots.find((b) => b.id === S.sel)) select(r.bots[0]?.id || null);
      // The bot on screen is open: the Bots page shows it (by a click, a deep link or landing here). Only this page
      // polls, so onboarding or another page never counts. Once per bot per page load; the server ignores repeats.
      if (S.sel && !document.hidden) sendOpen(S.sel);
      updateTitle();
    });
  } catch (e) {
    showBanner("Worker unreachable: " + errMsg(e));
  }
}

/** Run a mutation, then poll so the page shows its effect; errors land in the banner unless silent. Resolves undefined on failure. */
export async function act<T>(call: () => Promise<T>, silent = false): Promise<T | undefined> {
  try { const r = await call(); await poll(); return r; }
  catch (e) { if (!silent) showBanner(errMsg(e)); return undefined; }
}

/** The live view opened/closed: poll every 400 ms while open; on close the thread re-pins if polls landed meanwhile. */
export function setFast(on: boolean): void {
  if (S.fast === on) return;
  if (!on && S.renderDirty) commit({ fast: false, renderDirty: false, scrollThread: S.scrollThread + 1 });
  else commit({ fast: on });
}

// Poll faster while the live view is open (tab strip, URL bar, popups), else every 1.5 s; frames come over the live view socket.
// `loopGen` ties a loop to one startStore(): the route can unmount and remount while a poll is in flight, and the
// old loop must not re-arm beside the new one (two loops = twice the polls).
let loopGen = 0, loopTimer: ReturnType<typeof setTimeout> | null = null;
export async function loop(gen = loopGen): Promise<void> {
  await poll();
  if (gen > 0 && gen === loopGen) loopTimer = setTimeout(() => void loop(gen), S.fast ? 400 : 1500);
}

/** Boot: wire the api hooks and URL listeners, start the loop. Returns the teardown. Call once per mount (App).
 *  The state outlives a remount (leaving /bots and coming back): the URL's `?bot=` wins when it names one, else
 *  the bot that was open goes back onto the URL. */
export function startStore(): () => void {
  apiHooks.onStall = showBanner;
  apiHooks.onSettle = hideBanner;
  apiHooks.onSlow = (rec) => { const slow = [...S.slow, rec]; if (slow.length > 30) slow.shift(); commit({ slow }); };
  window.addEventListener("popstate", onUrlChange);
  window.addEventListener("fused:urlchange", onUrlChange);
  window.addEventListener(OPEN_EVENT, onOpenEvent);
  const urlBot = initialSel();
  if (urlBot && urlBot !== S.sel) commit({ sel: urlBot, scrollThread: S.scrollThread + 1 });
  else if (!urlBot && S.sel) writeUrlBot(S.sel);
  const gen = Math.abs(loopGen) + 1;
  loopGen = gen;
  void loop(gen);
  return () => {
    if (loopGen === gen) loopGen = -gen;  // stops this loop; a newer mount has already moved loopGen on
    if (loopTimer) clearTimeout(loopTimer);
    if (S.ui.dialog || S.ui.panel || S.ui.menu) commit({ ui: { dialog: null, panel: null, menu: null } });  // a route change closes them
    window.removeEventListener("popstate", onUrlChange);
    window.removeEventListener("fused:urlchange", onUrlChange);
    window.removeEventListener(OPEN_EVENT, onOpenEvent);
  };
}
