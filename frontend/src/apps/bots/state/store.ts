// Shared state (OpenBot core.js `state` + chat.js's unread maps), as a tiny external store for useSyncExternalStore.
//
// The snapshot is replaced (never mutated) on every change, so `useBots()` re-renders exactly when something
// changed, and per-bot event arrays keep their identity when that bot got nothing new (memo-friendly).
// Changes made inside batch() publish once, at the end. Module code reads getState(); React reads useBots().
//
// Also here: the poll loop (one in flight), act(), the banner, the status toast, select() + the `/bots/<id>` URL
// (routes.ts: the selection IS the path), the UI request slots (dialog / context menu) the panes open each other
// with, and the Builds chip. The Tasks / Apps / Preferences pages are routes too (App.tsx reads them), not a slot.
import { useSyncExternalStore } from "react";
import { api, apiHooks, markBotSeen, type Bot, type BotEvent, type ImessageState, type SlowCall, type UsageSummary, ApiError } from "../lib/api";
import { SEEN_KEY, newMarkFor, parseSeenMap, unreadOf, unviewedOf } from "../lib/unread";
import { landingBot } from "../lib/landing";
import { botHasUnread } from "@platform/lib/botsPulse";
import { getPrefsShared, putBotsPref } from "@platform/lib/api";
import { sentByYou } from "../lib/derive";
import { chime, notifyEvents, notifyHandover, updateTitle } from "../lib/notify";
import type { NewBotPick } from "../lib/presets";
import { botsMounted } from "../lib/root";
import { publishBots } from "@platform/lib/botsPulse";
import { BOTS_PREFIX, botHref, isBotsPath, parseBotsPath, searchSansTask } from "../routes";
import { navigateUrl, replaceSearch } from "@platform/lib/router";

export { SLOW_MS, STALL_MS } from "../lib/api";

// ------------------------------------------------------------------ shape ----
export interface Toast { id: number; text: string; label: string; dot: string; ts: number; out: boolean }
export interface BuildsChip { n: string; live: boolean; warn: boolean; fresh: boolean; title: string; hidden: boolean }
/** A dialog another module asked for. Known kinds: newBot, settings, routines, skills, usage (dialogs/Dialogs.tsx owns them).
 *  newBot without `pick` is the preset chooser (#pmodal); with `pick` it is the bot dialog filled in from that pick. */
export interface DialogReq { kind: "newBot" | "settings" | "usage" | "browsers" | "failures" | (string & {}); id?: string | null; pick?: NewBotPick; [k: string]: unknown }
/** The bot context menu (components/BotMenu.tsx): openMenu(id, x, y, live, alignRight); one row set wherever it opens. */
export interface MenuReq { id: string; x: number; y: number; live?: boolean; alignRight?: boolean }

export interface BotsState {
  bots: Bot[];
  /** id → merged events, oldest first, capped at EVENT_CAP. */
  events: Record<string, BotEvent[]>;
  /** id → last seq the page holds (sent as `cursors`). */
  cursors: Record<string, number>;
  sel: string | null;
  usage: UsageSummary | null;
  imessage: ImessageState | null;
  /** id → last seq as of opening that bot: the server's Bot.seen_seq, max-merged with marks this page sent that the
   *  server has not echoed yet (pollOnce, markSeen). Absent = never opened. */
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
  /** Calls slower than SLOW_MS (last 30). */
  slow: SlowCall[];
  /** The live view is open: polls every 400 ms and ask for `fast` status. */
  fast: boolean;
  /** Bumped by select(): OpenBot's render(true), "scroll the thread to the end on a new bot". */
  scrollThread: number;
  /** jumpTo(): the message the thread scrolls to once it renders that bot (null once done). */
  scrollSeq: { bot: string; seq: number } | null;
  /** Set by send(): the next render shows the message you just sent. The thread clears it (setScrollToEnd(false)). */
  scrollToEnd: boolean;
  banner: { show: boolean; text: string };
  toasts: Toast[];
  buildsChip: BuildsChip;
  ui: { dialog: DialogReq | null; menu: MenuReq | null };
  /** One-shot after a hand back (the bot's `handback.seq` moved): the composer of bot `id` offers "Anything <name> should know?" until `until`. */
  noteFor: { id: string; until: number } | null;
}

export const EVENT_CAP = 600;
const EMPTY: BotEvent[] = [];

/** The bot the URL names (`/bots/<id>`), else null. */
const urlSel = (): string | null => {
  try { const r = parseBotsPath(location.pathname); return r.kind === "bot" ? r.id : null; } catch { return null; }
};

let S: BotsState = {
  bots: [], events: {}, cursors: {}, sel: typeof location === "undefined" ? null : urlSel(),
  usage: null, imessage: null,
  seen: {}, base: {}, viewed: {}, newMark: null, newCount: 0, pinned: true,
  showHidden: false, slow: [], fast: false, scrollThread: 0, scrollToEnd: false, scrollSeq: null,
  banner: { show: false, text: "" }, toasts: [],
  buildsChip: { n: "", live: false, warn: false, fresh: false, title: "Tasks · Claude tasks that create fused apps", hidden: false },
  ui: { dialog: null, menu: null },
  noteFor: null,
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

// ------------------------------------------------------------------ prefs ----
// The three page-wide switches (Settings > General) are server prefs (shell/prefs.py `bots_autostage`, `bots_sounds`,
// `bots_clear_enabled`; GET /api/prefs → `bots`), D1386. They used to be localStorage, but the desktop app walks ports
// 1777..1787 then an ephemeral one and each port is a new origin with empty storage, so a relaunch on another port
// quietly turned the chime back on and the Clear context button back off. Reads stay synchronous: `flagMem` is the
// in-memory copy, seeded once per document by hydrateBotsPrefs (App mount) and advanced by the setters at once, so a
// switch governs the page the moment it flips. Until the GET lands a flag reads as its default (the server's default
// too); the first poll can open Stage on a hand-over in that window even if autostage was turned off (accepted: the
// GET is one local request racing the first /api/bots poll).
type BotsPrefKey = "bots_autostage" | "bots_sounds" | "bots_clear_enabled";
const flagMem = new Map<BotsPrefKey, boolean>();
/** A flag flipped in this document: the hydrate GET, which may have started before the write, must not undo it, and
 *  an old localStorage value must not be imported over it. */
const flagLocal = new Set<BotsPrefKey>();
const readFlag = (k: BotsPrefKey, dflt = true): boolean => flagMem.get(k) ?? dflt;
const writeFlag = (k: BotsPrefKey, on: boolean): void => {
  flagMem.set(k, on); flagLocal.add(k);
  void putBotsPref(k, on).catch(() => { /* the switch still governs this document; the next write retries */ });
};
/** import-only (D1386): the old localStorage key ("1" / "0"), its prefs key, the `Prefs.bots` field and the default. */
const FLAG_IMPORT: [string, BotsPrefKey, "autostage" | "sounds" | "clear_enabled", boolean][] = [
  ["browser-bot.autostage", "bots_autostage", "autostage", true],
  ["browser-bot.sounds", "bots_sounds", "sounds", true],
  ["browser-bot.clear", "bots_clear_enabled", "clear_enabled", false],
];
const dropKey = (k: string): void => { try { localStorage.removeItem(k); } catch { /* no storage */ } };
let hydrated: Promise<void> | null = null;
/** Seed the page-wide switches from the server, once per document (the Bots route remounts; this does not re-run),
 *  and run the one-shot D1386 import of the old localStorage keys.
 *
 *  The server is read FIRST and an old switch key is imported only while the server still holds that pref's default.
 *  The port walk leaves one stale copy per origin: the user may have flipped the switch on 1778 (which is now the
 *  server value) while 1777's old key still says otherwise, so a server value off its default was set by the user and
 *  wins. The old key is removed once that decision is made; a pushed value's key only after its PUT succeeded, so a
 *  failed push re-decides next load under the same gate. An unreachable server (or one without `bots`) decides
 *  nothing and keeps the keys. A switch flipped in this document before the GET landed beats both.
 *
 *  The seen map needs no gate: `/seen` only ever RAISES a bot's seen_seq (bots/routes.py `_seen`, under the bot lock),
 *  so a stale origin's lower mark is a no-op server-side, and the local max-merge never lowers either. Its key goes
 *  once every POST landed or 404'd (a bot deleted since: final); any other failure keeps it for the next load. */
export function hydrateBotsPrefs(): Promise<void> {
  return hydrated || (hydrated = hydrateOnce());
}
async function hydrateOnce(): Promise<void> {
  try {
    const raw = localStorage.getItem(SEEN_KEY);
    if (raw !== null) {
      const imported = parseSeenMap(raw);
      // Shown at once: the first render must not paint every bot as never opened while the POSTs travel; pollOnce's
      // max-merge keeps these until the server echoes them (or a higher mark of its own wins).
      commit({ seen: maxMerge(S.seen, imported) });
      void Promise.allSettled(Object.entries(imported).map(([id, seq]) => markBotSeen(id, seq))).then((rs) => {
        // The key goes only when the server HAS the marks: every POST landed, or 404'd (a bot deleted since —
        // nothing to mark, final). A network error or 5xx keeps the key so the next load imports again.
        const landed = rs.every((r) => r.status === "fulfilled" || (r.reason instanceof ApiError && r.reason.status === 404));
        if (landed) dropKey(SEEN_KEY);
      });
    }
  } catch { /* storage blocked: nothing to import */ }
  let server: { autostage?: unknown; sounds?: unknown; clear_enabled?: unknown } | undefined;
  try { server = (await getPrefsShared()).bots; } catch { /* server unreachable: defaults stand; the poll's banner says why */ }
  if (server) {
    for (const [old, key, field, dflt] of FLAG_IMPORT) {
      const sv = typeof server[field] === "boolean" ? (server[field] as boolean) : dflt;
      let v: string | null = null;
      try { v = localStorage.getItem(old); } catch { /* no storage */ }
      if (flagLocal.has(key)) { if (v !== null) dropKey(old); continue; }  // flipped here meanwhile: that wins
      if (v !== null && sv === dflt && (v === "1") !== dflt) {
        flagMem.set(key, v === "1");
        void putBotsPref(key, v === "1").then(() => dropKey(old), () => {});
        continue;
      }
      flagMem.set(key, sv);
      if (v !== null) dropKey(old);
    }
  }
  touch();  // an open chat header follows clear_enabled
}
/** "Open the browser when a bot needs me": a bot-initiated hand-over on the selected bot opens Stage by itself. */
export const autoStage = (): boolean => readFlag("bots_autostage");
export const setAutoStage = (on: boolean): void => writeFlag("bots_autostage", on);
/** The hand-over chime (lib/notify.ts chime). */
export const soundsOn = (): boolean => readFlag("bots_sounds");
export const setSounds = (on: boolean): void => writeFlag("bots_sounds", on);
/** The chat header's Clear context button (ChatPane.tsx, Bot.clear_context): a feature flag, OFF by default. Off, the
 *  button is not rendered at all; markers already in a thread still fold it (Thread.tsx). The setter republishes so
 *  an open header follows the switch at once. */
export const clearContextOn = (): boolean => readFlag("bots_clear_enabled", false);
export const setClearContext = (on: boolean): void => { writeFlag("bots_clear_enabled", on); touch(); };

// ------------------------------------------------------------------ hand-over ----
// The bot asked for you in its browser (control_by flipped to "bot"): open Stage. lib/cdp.ts registers openFull here
// (it imports this module, so importing it back would be a cycle).
type HandoverHook = (opts: { focus: boolean }) => void;
let handoverHook: HandoverHook | null = null, closeHook: (() => void) | null = null;
/** cdp.ts: `open` = openFull, `close` = leave Stage and drop the live socket (handBack(true) minus the giveback). */
export function onHandover(open: HandoverHook, close: () => void): void { handoverHook = open; closeHook = close; }
const closeStage = (): void => { if (closeHook) closeHook(); else setFast(false); };
/** Waiting for an open dialog / menu to close before Stage opens for this bot. */
let pendingStage: string | null = null;
/** The bot whose hand-over opened the current Stage by itself; the poll closes it again when that hand-over ends.
 *  null for a Stage you opened by hand (watch mode), which only you close. Cleared whenever Stage closes (setFast). */
let autoOpened: string | null = null;
/** The bot asked for you in its browser (the list dot, the title prefix, the composer hint, the one-time alert). Stays
 *  true through a pop-out to a real window: that is the same hand-over, not a new one. */
export const needsYou = (b: Bot | undefined): boolean => !!b && !!b.control && b.control_by === "bot";
/** ...and Stage can show it: the task has not ended and the browser is not popped out (nothing to stream). The same
 *  checks that close an auto-opened Stage, so a remount never flashes one open for a poll. */
const stageable = (b: Bot | undefined): boolean => needsYou(b) && b!.status !== "idle" && b!.status !== "error" && !b!.browser?.headed;
/** Open Stage for the selected bot `id` unless it is already open or the pref is off; with a dialog / menu up, later
 *  (closeDialog / closeMenu). Unsent composer text keeps its focus: the page does not take the keyboard then. */
function requestStage(id: string): void {
  if (!autoStage() || id !== S.sel) return;
  // Stage is already up (opened for another bot's hand-over, or by hand): it now serves this bot, so this bot's hand
  // back closes it, not the first one's.
  if (S.fast) { if (autoOpened) autoOpened = id; return; }
  if (S.ui.dialog || S.ui.menu) { pendingStage = id; return; }
  pendingStage = null;
  // Deferred: this runs inside a poll's or select()'s batch, and openFull's flushSync needs the store published first.
  // botsMounted: a notification click or a late timer after the route unmounted must not open Stage on nothing.
  setTimeout(() => {
    if (!botsMounted() || S.fast || S.sel !== id || !stageable(cur()) || !handoverHook) return;
    // A dialog opened meanwhile (the ?new=1 chooser lands in the same flush as a remount): wait for it, as above.
    if (S.ui.dialog || S.ui.menu) { pendingStage = id; return; }
    const input = document.getElementById("input") as HTMLTextAreaElement | null;
    handoverHook({ focus: !input?.value.trim() });
    if (S.fast) autoOpened = id;
  }, 0);
}
function flushPendingStage(): void {
  const id = pendingStage; pendingStage = null;
  if (id && stageable(botById(id))) requestStage(id);
}

// ------------------------------------------------------------------ banner ----
export function showBanner(msg: string): void { commit({ banner: { show: true, text: msg } }); }
export function hideBanner(): void { if (S.banner.show) commit({ banner: { ...S.banner, show: false } }); }

// ------------------------------------------------------------------ unread ----
// "Seen" is the server's Bot.seen_seq (D1386; it was localStorage, which the port walk empties on relaunch). markSeen
// moves the local mark at once (the pill and the list dot clear on the click) and POSTs it; pollOnce max-merges the
// server's value back in, so a poll that lands before the POST cannot pull the mark back and re-light the pill — which
// would make Thread.tsx call markSeen again, a write loop. The server never lowers it, so max is the truth.
/** a ∪ b, keeping the larger seq per id. */
function maxMerge(a: Record<string, number>, b: Record<string, number>): Record<string, number> {
  const out = { ...a };
  for (const [id, seq] of Object.entries(b)) if (!(out[id] >= seq)) out[id] = seq;
  return out;
}
// One POST in flight per bot; marks made meanwhile (a running bot you are watching moves seq every poll) collapse into
// one follow-up carrying the latest.
const seenInFlight = new Set<string>(), seenNext = new Map<string, number>();
function pushSeen(id: string, seq: number): void {
  if (seenInFlight.has(id)) { seenNext.set(id, Math.max(seq, seenNext.get(id) ?? seq)); return; }
  seenInFlight.add(id);
  void markBotSeen(id, seq).catch(() => { /* the mark holds in this document; the bot's next seq sends again */ }).finally(() => {
    seenInFlight.delete(id);
    const next = seenNext.get(id); seenNext.delete(id);
    if (next !== undefined && next > seq) pushSeen(id, next);
  });
}
export function markSeen(id: string | null | undefined, seq: number): void {
  if (!id || S.seen[id] >= seq) return;
  pushSeen(id, seq);
  commit({ seen: { ...S.seen, [id]: seq } });
  // The sidebar's chip counts this bot until it hears the mark moved; the next poll is up to 3 s away (CodeRabbit, #1796).
  publishPulse();
}

/** The sidebar's Bots entry reads this page's rows (platform/lib/botsPulse) with the store's MERGED read mark, not
 *  the server's raw `seen_seq`: a bot you just opened is read here at once, and the chip must not count it for the
 *  poll or two until the server echoes the POST. Called on every poll and on every local mark. */
function publishPulse(): void {
  publishBots(S.bots.map((b) => ({ id: b.id, status: b.status, seq: b.seq, news_seq: b.news_seq, seen_seq: S.seen[b.id], hidden: b.hidden })));
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

/** Put the selection on the URL (routes.ts: `/bots/<id>` IS the selection). The store outlives the route (module
 *  state), so a poll that lands after the reader navigated away must not write onto whatever page they are on now.
 *  `push` is a user gesture (a row click, the Dock): a history entry, so Back steps between bots, and it always lands
 *  on the chat — from the Tasks / Apps page too. `replace` is the store's own fallback (the first poll's default
 *  pick, a deleted bot): no entry, and it never yanks the reader off a page they deep-linked to; it only fills in
 *  the bare `/bots`. The query rides along: a side app's params and the `?new=1` / `?phone=1` one-shots live there. */
function writeUrl(id: string | null, mode: "push" | "replace") {
  try {
    if (!isBotsPath(location.pathname)) return;
    const r = parseBotsPath(location.pathname);
    if (r.kind === "bot" && r.id === id) return;
    if (mode === "replace" && r.kind !== "list" && r.kind !== "bot") return;
    const href = (id ? botHref(id) : BOTS_PREFIX) + location.search + location.hash;
    if (mode === "push") navigateUrl(href); else replaceSearch(href);
  } catch { /* no history (tests) */ }
}

/** Leave the Tasks / Apps / Preferences page for the selected bot's chat (their Back button, Esc, Stage opening).
 *  Already on a chat: nothing. */
export function goChat(): void {
  try {
    if (!isBotsPath(location.pathname) || parseBotsPath(location.pathname).kind === "bot") return;
    // The query rides along minus the Tasks page's own `task` key, which names nothing on a chat.
    navigateUrl((S.sel ? botHref(S.sel) : BOTS_PREFIX) + searchSansTask(location.search));
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

/** Everything select() does to the store for a new selection, minus the URL: the resets a bot switch needs whoever
 *  moved it (a click, or the URL under back/forward — syncFromUrl). */
function applySel(id: string | null): void {
  if (id !== S.sel) { for (const cb of [...selectListeners]) cb(id); clearToast(); }  // a reply and a status toast belong to one bot
  commit({ sel: id, scrollThread: S.scrollThread + 1 });
  const b = cur(); if (b) { setNewMark(b); markSeen(b.id, b.seq); }
  // A bot waiting for you in its browser opens Stage the moment you pick it.
  if (b && stageable(b)) requestStage(b.id);
}

/** `replace`: the store's own fallback pick (see writeUrl); a user gesture pushes. `sel` is committed BEFORE the
 *  navigation, so the fused:navigate it fires finds syncFromUrl with nothing to do. (navigateUrl asks the shell's leave
 *  guard first; a cancelled confirm would leave `sel` ahead of the path. Only the shell's own chat composer registers
 *  one and it is not on this page, so this is noted, not defended.) */
export function select(id: string | null, opts: { replace?: boolean } = {}): void {
  batch(() => {
    applySel(id);
    writeUrl(id, opts.replace ? "replace" : "push");
  });
}

/** Open `id`'s chat at message `seq` ("Read more" on a hand-off line). The thread clears scrollSeq once it has scrolled. */
export function jumpTo(id: string, seq: number): void {
  if (!S.bots.some((b) => b.id === id)) return;
  batch(() => { select(id); commit({ scrollSeq: { bot: id, seq } }); });
}
export const clearScrollSeq = (): void => { if (S.scrollSeq) commit({ scrollSeq: null }); };

/** The sidebar's dot rule, over this store's merged read mark (publishPulse): the bare door lands on what the dot
 *  pointed at. */
const hasNews = (b: Bot): boolean => botHasUnread({ seq: b.seq, news_seq: b.news_seq, seen_seq: S.seen[b.id] });

/** Fill in the bare `/bots` (the sidebar row, the Dock, a remount) with a bot: the one waiting on you, else one
 *  with unread news, else the one that was open (lib/landing.ts). A replace: the click already made the history
 *  entry. A different bot than the one on screen gets the select resets (applySel), and landing marks it read.
 *  With no rows yet the pick waits for the first poll (pollOnce), which calls this again. */
function landOnBare(): void {
  const pick = S.bots.length ? landingBot(S.bots, hasNews, S.sel) : S.sel;
  if (!pick) return;
  batch(() => {
    if (pick !== S.sel) applySel(pick);
    writeUrl(pick, "replace");
  });
}

// The URL moved under us (back/forward, the Dock's fallback rewrite, the sidebar's Bots row): follow it. A bot the
// path names gets the full select resets — Back between two chats is a bot switch like any click, and B's reply
// quote must not land in A's composer. The bare `/bots` reached while the page is mounted (the sidebar row from a
// chat) is filled in by landOnBare — a bot waiting on you or carrying unread news first (owner, 2026-10-22: "when
// click on bot from sidebar, it should go to the bot with unread messages if any … blocker > unread"), else the
// bot on screen — a replace, so the URL never stays behind the view (I4). A gesture of ours already committed
// `sel` before it navigated, so for it this is a no-op.
function syncFromUrl() {
  const id = urlSel();
  if (id) {
    if (id !== S.sel && (!S.bots.length || S.bots.some((b) => b.id === id))) batch(() => applySel(id));
    return;
  }
  if (isBotsPath(location.pathname) && parseBotsPath(location.pathname).kind === "list" && (S.bots.length || S.sel)) landOnBare();
}

/** The menu-bar Dock's bot tile (fused_render/mac_window.py `show_bot`) lands here: a user gesture from outside
 *  the page, so it is a full `openBot` — the select() resets (reply quote, toast) and the open call — not the quiet
 *  URL follow above. */
export const OPEN_EVENT = "fused:bots-open";
function onOpenEvent(e: Event) {
  const id = (e as CustomEvent<{ id?: unknown }>).detail?.id;
  if (typeof id !== "string" || !id) return;
  e.preventDefault();  // "handled": the sender falls back to rewriting ?bot= when nobody is mounted to hear this
  openBot(id);
}

// ------------------------------------------------------------------ ui slots ----
export const openDialog = (req: DialogReq): void => commit({ ui: { ...S.ui, dialog: req, menu: null } });
export const closeDialog = (): void => { if (S.ui.dialog) { commit({ ui: { ...S.ui, dialog: null } }); flushPendingStage(); } };
export const openMenu = (req: MenuReq): void => commit({ ui: { ...S.ui, menu: req } });
export const closeMenu = (): void => { if (S.ui.menu) { commit({ ui: { ...S.ui, menu: null } }); flushPendingStage(); } };
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
      const prev = new Map(S.bots.map((b) => [b.id, b]));
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
      // Seen comes from the server (Bot.seen_seq, another window's opens included), max-merged with marks of ours it has
      // not echoed yet (see markSeen). In the same commit as `bots`, so the first-load setNewMark below reads it.
      let seen: Record<string, number> = {}, seenSame = true;
      for (const b of r.bots) {
        const mine = S.seen[b.id] as number | undefined, theirs = b.seen_seq ?? undefined;
        const v = mine === undefined ? theirs : theirs === undefined ? mine : Math.max(mine, theirs);
        if (v !== undefined) seen[b.id] = v;
        if (v !== mine) seenSame = false;
      }
      // Nothing moved (and no bot left): keep the reference.
      if (seenSame && Object.keys(seen).length === Object.keys(S.seen).length) seen = S.seen;
      commit({
        bots: r.bots, events, cursors, seen,
        ...(r.usage ? { usage: r.usage } : {}), ...(r.imessage ? { imessage: r.imessage } : {}),
      });
      // The sidebar's Bots entry: while this page polls, it is the one source, and the rail moves on the same tick
      // the list does (publishPulse).
      publishPulse();
      if (toast) showToast(toast);
      // Hand-overs: the bot asked for you in its browser (control_by flipped to "bot"; a take-over of yours is "user" and
      // stays quiet). The selected bot opens Stage, any other one gets a toast; chime + OS notification either way. On
      // first load only the selected bot reacts, silently. A hand back the server settled (handback.seq moved) arms the
      // composer's one-shot "anything to add?" placeholder.
      // One chime per poll however many bots flipped together. No note when you handed back BY replying (a message of
      // yours after the question): you already said what you had to say.
      let ring = false;
      for (const b of r.bots) {
        const was = prev.get(b.id);
        if (needsYou(b) && (first ? b.id === S.sel : !needsYou(was))) {
          if (b.id === S.sel) requestStage(b.id);
          else showToast({ text: `${b.name} needs you in the browser`, ts: Date.now() / 1000 });
          if (!first) { ring = true; notifyHandover(b); }
        }
        if (!first && b.handback && b.handback.seq !== was?.handback?.seq) {
          const hb = b.handback.seq, replied = (events[b.id] || []).some((e) => sentByYou(e) && e.seq > hb);
          if (!replied) commit({ noteFor: { id: b.id, until: Date.now() + 15_000 } });
        }
      }
      if (ring) chime();
      // Spec §4: a Stage the hand-over opened by itself closes when that hand-over ends: control dropped (a chat reply
      // handed back), the bot finished or failed, or it popped out to a real window (nothing left to stream). A Stage
      // you opened by hand stays.
      if (S.fast && autoOpened && autoOpened === S.sel) {
        const b = cur();
        if (!b || !b.control || b.status === "idle" || b.status === "error" || b.browser?.headed) closeStage();
      }
      // A bot picked from the URL skips select(): place its "New" rule and clear its dot once, on first load. Later polls leave "seen" alone so the dot lights while you watch.
      if (first && S.sel && !document.hidden) { const c = cur(); if (c) { setNewMark(c); markSeen(c.id, c.seq); } }
      // Nothing selected yet: a bot waiting on you, else one with news, else Super Bot (seeded on first run,
      // registry.seed_super) — the chat a new user should land in (lib/landing.ts).
      if (!S.sel && r.bots.length) landOnBare();
      if (S.sel && !r.bots.find((b) => b.id === S.sel)) select(r.bots[0]?.id || null, { replace: true });
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

/** The live view (Stage) opened/closed: poll every 400 ms while open. The thread stays beside the page, so nothing re-pins. */
export function setFast(on: boolean): void {
  if (S.fast === on) return;
  if (!on) autoOpened = null;
  commit({ fast: on });
  // The pending tick may be an idle 3 s one; the live view wants its 400 ms cadence now.
  if (on) kickLoop();
}

// Poll faster while the live view is open (tab strip, URL bar, popups), else slower (loopDelay); frames come over the live view socket.
// `loopGen` ties a loop to one startStore(): the route can unmount and remount while a poll is in flight, and the
// old loop must not re-arm beside the new one (two loops = twice the polls).
let loopGen = 0, loopTimer: ReturnType<typeof setTimeout> | null = null;
export async function loop(gen = loopGen): Promise<void> {
  await poll();
  if (gen > 0 && gen === loopGen) loopTimer = setTimeout(() => { loopTimer = null; void loop(gen); }, loopDelay());
}
// How long until the next /api/bots poll. This poll was ~40% of every request in a live log (2026-10-08), and every
// native window shares WebKit's 6-connection HTTP/1.1 pool per host:port — so the idle cadence is a real tax on
// every other window's calls. Live view open: 400 ms (tab strip, URL bar, popups). A bot RUNNING: 1.5 s, the old
// rate, so steps and replies stream. Nothing running (idle / waiting on you / paused / error): 3 s — nothing moves
// server-side without a run, and your own actions poll at once through act(). Hidden window: 5 s, NOT paused —
// this poll is what raises the OS notification / chime for a question, approval or hand-over (notify.ts fires them
// precisely when the tab is hidden), so stopping it would silence the alarm for the window you are away from; the
// visibilitychange hook in startStore catches up the moment the window shows again.
function loopDelay(): number {
  if (typeof document !== "undefined" && document.hidden) return 5000;
  if (S.fast) return 400;
  return S.bots.some((b) => b.status === "running") ? 1500 : 3000;
}
// Poll now instead of waiting out the pending delay. Only when a timer is PENDING — mid-poll the chain re-arms on
// its own (with the new delay), and starting another loop here would run two side by side.
function kickLoop(): void {
  if (loopGen <= 0 || !loopTimer) return;
  clearTimeout(loopTimer);
  loopTimer = null;
  void loop(loopGen);
}

/** Boot: wire the api hooks and URL listeners, start the loop. Returns the teardown. Call once per mount (App).
 *  The state outlives a remount (leaving /bots and coming back): the URL's `/bots/<id>` wins when it names one, else
 *  the bot that was open goes back onto the URL (a replace: the bare `/bots` becomes its chat). */
export function startStore(): () => void {
  apiHooks.onStall = showBanner;
  apiHooks.onSettle = hideBanner;
  apiHooks.onSlow = (rec) => { const slow = [...S.slow, rec]; if (slow.length > 30) slow.shift(); commit({ slow }); };
  window.addEventListener("popstate", syncFromUrl);
  window.addEventListener("fused:navigate", syncFromUrl);
  window.addEventListener("fused:urlchange", syncFromUrl);
  window.addEventListener(OPEN_EVENT, onOpenEvent);
  const urlBot = urlSel();
  if (urlBot && urlBot !== S.sel) commit({ sel: urlBot, scrollThread: S.scrollThread + 1 });
  else if (!urlBot && S.sel) landOnBare();  // the bare door from another page: waiting > unread > the bot that was open
  const gen = Math.abs(loopGen) + 1;
  loopGen = gen;
  void loop(gen);
  // Shown again: poll now rather than waiting out the (up to 5 s) hidden-window delay.
  const onVisible = () => { if (!document.hidden && loopGen === gen) kickLoop(); };
  document.addEventListener("visibilitychange", onVisible);
  // The store outlives the route: coming back to /bots with the selected bot still mid-hand-over reopens Stage (the
  // poll diff only fires on the flip, which happened while you were away).
  if (stageable(cur())) requestStage(S.sel!);
  return () => {
    if (loopGen === gen) loopGen = -gen;  // stops this loop; a newer mount has already moved loopGen on
    if (loopTimer) { clearTimeout(loopTimer); loopTimer = null; }  // null: kickLoop reads a non-null timer as "pending"
    document.removeEventListener("visibilitychange", onVisible);
    if (S.ui.dialog || S.ui.menu) commit({ ui: { dialog: null, menu: null } });  // a route change closes them
    pendingStage = null; if (S.fast) closeStage();  // Stage does not outlive the route (and a queued auto-open must not fire into it)
    window.removeEventListener("popstate", syncFromUrl);
    window.removeEventListener("fused:navigate", syncFromUrl);
    window.removeEventListener("fused:urlchange", syncFromUrl);
    window.removeEventListener(OPEN_EVENT, onOpenEvent);
  };
}
