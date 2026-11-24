// THE CHAT THE READER LAST PICKED (owner, 2026-11-02: "last selected stuff
// should be in expanded claude sidebar view as well").
//
// The canvas rail's list lights the ring pressed last (ClaudeRail
// `activeRing`); the pane's Recent chats list (apps/claude ui/Lists) marks the same row once the
// pane is open. One answer for both, so collapsing or expanding the pane
// keeps the mark where the reader left it.
//
// TWO MARKS, NOT ONE (owner, 2026-11-04: "last select of fav is diff from
// last selected of the list, both of them should show their last selected"):
// the FAVOURITES keep their own (`setLastFav`, sessionStorage
// `fused-render:last-fav`) — a press on a favourite's ring (the rail's
// favourites section, the pane's Favourites row) moves only that one, and a
// press on a list ring (the rail's chats, the pane's Recent row) only the
// list's. Both show at once, one in each section. Only a PRESS moves either
// (no client follows the server's `starred_open` any more). A
// chat unstarred back into the list keeps the list's mark where it was; the
// favourites' mark on it just matches nothing any more.
//
// SESSION STORAGE, not module memory: the node chat runs in its own
// same-origin frame (CanvasWorkspace `preview-side-frame`), whose modules are
// a second copy — the `storage` event is what carries a press across. Per
// window (a second window has its own), and it survives a reload.
//
// THE OPEN FAVOURITE IS PER WINDOW TOO (owner, 2026-11-04: "if I have
// multiple windows open, the open favourite shouldn't sync; the list items
// (stars) do sync"): which favourite's card (shell FavouritePeek) is up in
// THIS window — `useFavOpen`, sessionStorage `fused-render:fav-open`, the
// session KEY (not `lastChatId`: a favourite is a started chat, whose key
// never rekeys). A favourite ring's press sets it, the card's × clears it,
// and an unstar, an erase or an archive of that chat clears it (FavouritePeek
// reads the pulse). The stars stay the server's and sync everywhere; the
// server's own `starred_open` is no longer what any card follows.
//
// WHEN IT WAS OPENED rides beside the key (`fused-render:fav-open-at`, JSON
// `{key, at}`), in sessionStorage too: a star pressed a moment ago opens its
// card before the pulse says `starred`, and every document of the window —
// the node chat's frame that wrote the key, the shell that draws the card —
// must agree it is "just opened" (FavouritePeek's verdict would otherwise
// close it, and wipe the key, during the pulse's lag). A reload inside the
// settle window (rail-peek-lib FAV_SETTLE_MS, seconds) still counts as just
// opened; past it, the pulse decides alone.
//
// THE ID is the project plus the task number: `task_id` ("TASK-003") is
// numbered per project, and the key alone is rewritten when a queued row
// dispatches (`pending:<entry>` → its session), which the number survives.
import { useSyncExternalStore } from "react";

/** A row's identity for the mark: its project and its task number (or its
 *  key, before it has one). */
export function lastChatId(t: { project?: string | null; task_id?: string | null; key: string }): string {
  return `${t.project || ""}\u0000${t.task_id || t.key}`;
}

/** One mark, kept under one sessionStorage key (header): the list's, or the
 *  favourites'. The two run the same code and never touch each other. */
interface MarkStore {
  get(): string | null;
  /** Null clears the mark (the open favourite's ×). */
  set(id: string | null): void;
  onStorage(e: Pick<StorageEvent, "key" | "storageArea">): void;
  subscribe(fn: () => void): () => void;
}

function markStore(key: string): MarkStore {
  const subs = new Set<() => void>();
  const read = (): string | null => {
    try {
      return typeof sessionStorage === "undefined" ? null : sessionStorage.getItem(key);
    } catch {
      return null;
    }
  };
  let mem: string | null = read();
  /** The last write was refused (private mode, quota): `mem` is the only
   *  copy, and a re-read from storage would throw it away. */
  let unsaved = false;
  const notify = () => {
    for (const fn of subs) fn();
  };
  const onStorage = (e: Pick<StorageEvent, "key" | "storageArea">): void => {
    if (typeof sessionStorage === "undefined" || e.storageArea !== sessionStorage) return;
    if (e.key !== key && e.key !== null) return;
    const next = read();
    unsaved = false;
    if (next === mem) return;
    mem = next;
    notify();
  };
  let listening = false;
  return {
    get: () => mem,
    set: (id) => {
      if (id === mem) return;
      mem = id;
      try {
        if (id === null) sessionStorage.removeItem(key);
        else sessionStorage.setItem(key, id);
        unsaved = false;
      } catch {
        // Storage refused (private mode, quota): this document still knows.
        unsaved = true;
      }
      notify();
    },
    onStorage,
    subscribe: (fn) => {
      subs.add(fn);
      // A second document (the node chat's frame) loaded its copy of this
      // module before a press elsewhere: catch up before the first read —
      // unless this document holds a pick storage refused, which a re-read
      // would drop.
      if (!unsaved) mem = read();
      if (!listening && typeof window !== "undefined") {
        listening = true;
        window.addEventListener("storage", onStorage);
      }
      return () => {
        subs.delete(fn);
      };
    },
  };
}

const list = markStore("fused-render:last-chat");
const fav = markStore("fused-render:last-fav");

/** The last chat picked in this window's LISTS, as `lastChatId` spells it. */
export function lastChat(): string | null {
  return list.get();
}

/** A press on a list chat (a rail ring, a Recent row) makes it the last one. */
export function setLastChat(id: string): void {
  list.set(id);
}

/** Another document of this window wrote the mark (the node chat's frame,
 *  or the page around it): adopt it. The `storage` listener; exported so the
 *  sync can be tested without a browser. */
export function onLastChatStorage(e: Pick<StorageEvent, "key" | "storageArea">): void {
  list.onStorage(e);
}

/** Follow the mark: `fn` runs on every change, this document's or another's.
 *  Returns the unsubscribe. */
export function subscribeLastChat(fn: () => void): () => void {
  return list.subscribe(fn);
}

/** The last chat picked, live: re-renders when any document of this window
 *  picks another. */
export function useLastChat(): string | null {
  return useSyncExternalStore(subscribeLastChat, lastChat, lastChat);
}

/** The FAVOURITES' own last pick (header), as `lastChatId` spells it. */
export function lastFav(): string | null {
  return fav.get();
}

/** A press on a favourite (the rail's favourites ring, the pane's Favourites
 *  row) makes it the favourites' last one; the list's mark stays put. */
export function setLastFav(id: string): void {
  fav.set(id);
}

/** `onLastChatStorage`, for the favourites' mark. */
export function onLastFavStorage(e: Pick<StorageEvent, "key" | "storageArea">): void {
  fav.onStorage(e);
}

/** `subscribeLastChat`, for the favourites' mark. */
export function subscribeLastFav(fn: () => void): () => void {
  return fav.subscribe(fn);
}

const favOpenMark = markStore("fused-render:fav-open");
const FAV_OPEN_AT = "fused-render:fav-open-at";
/** This document's own copy of the open time (header), for a storage that
 *  refuses writes (private mode, quota): then this document is the only one
 *  that knows, which is what it was before. */
let favOpenedAtMem: { key: string; at: number } | null = null;
/** The last write of it was refused: `favOpenedAtMem` is the only copy. */
let favOpenedAtUnsaved = false;

function readFavOpenedAt(): { key: string; at: number } | null {
  if (favOpenedAtUnsaved) return favOpenedAtMem;
  try {
    if (typeof sessionStorage === "undefined") return favOpenedAtMem;
    const raw = sessionStorage.getItem(FAV_OPEN_AT);
    if (raw === null) return null;
    const v = JSON.parse(raw) as { key?: unknown; at?: unknown } | null;
    return v && typeof v.key === "string" && typeof v.at === "number" ? { key: v.key, at: v.at } : null;
  } catch {
    return favOpenedAtMem;
  }
}

/** The favourite whose card is open in THIS window (header), by task key. */
export function favOpen(): string | null {
  return favOpenMark.get();
}

/** Open `key`'s favourite card in this window (a favourite ring's press, a
 *  star pressed on), or close it (`null`: the ×, an unstar, an archive). The
 *  open time first, so a document hearing the key's `storage` event reads
 *  it beside the key (header). */
export function setFavOpen(key: string | null): void {
  favOpenedAtMem = key === null ? null : { key, at: Date.now() };
  try {
    if (key === null) sessionStorage.removeItem(FAV_OPEN_AT);
    else sessionStorage.setItem(FAV_OPEN_AT, JSON.stringify(favOpenedAtMem));
    favOpenedAtUnsaved = false;
  } catch {
    // Storage refused: `favOpenedAtMem` is this document's answer.
    favOpenedAtUnsaved = true;
  }
  favOpenMark.set(key);
}

/** Close the card only if it is still on `key` (a verdict about one chat
 *  must not close a card that has moved on since). */
export function clearFavOpen(key: string): void {
  if (favOpenMark.get() === key) setFavOpen(null);
}

/** When `key` was opened in this window (ms) — by this document or another
 *  of the window (header) — or null if it was not. */
export function favOpenedAtMs(key: string): number | null {
  const v = readFavOpenedAt();
  return v && v.key === key ? v.at : null;
}

/** `onLastChatStorage`, for the open favourite. */
export function onFavOpenStorage(e: Pick<StorageEvent, "key" | "storageArea">): void {
  favOpenMark.onStorage(e);
}

/** `subscribeLastChat`, for the open favourite. */
export function subscribeFavOpen(fn: () => void): () => void {
  return favOpenMark.subscribe(fn);
}

/** The open favourite, live. */
export function useFavOpen(): string | null {
  return useSyncExternalStore(subscribeFavOpen, favOpen, favOpen);
}

// MANY PINS (claude-rail rail-slots-lib "FREE PINS"; owner, 2026-11-24: "we
// should allow multiple pins", cap "No cap"): with Rail extras on, each of
// the three pin marks below is a LIST — every list task, door and + card
// pinned in section A, every favourite and every bot pinned in section B —
// kept under the same sessionStorage key as the one word it was. One item is
// stored as that bare word, exactly as before (a value the earlier build
// wrote reads as a one-item list, and with the switch off nothing else is
// ever written); two or more as a JSON array of strings.
//
// Each list keeps two faces: the SCALAR one the rules of rail-pin-lib have
// always read with the switch off (`aPinned`, `setAPinned` — the last item;
// a set replaces the whole list), and the LIST one free pins read (`aPins`,
// `addAPin`, `dropAPin`, `hasAPin`, `useAPins`). With at most one item the
// two say the same thing.
interface ListMark {
  /** The items, oldest first — the same array while the stored value has not
   *  changed (`useSyncExternalStore` compares snapshots by identity). */
  list(): readonly string[];
  /** The newest item, or null (the scalar face). */
  last(): string | null;
  has(item: string): boolean;
  /** Replace the whole list with `item` (or empty it: `null`). */
  set(item: string | null): void;
  add(item: string): void;
  drop(item: string): void;
  subscribe(fn: () => void): () => void;
  onStorage(e: Pick<StorageEvent, "key" | "storageArea">): void;
}

const NO_ITEMS: readonly string[] = Object.freeze([]);

/** A stored list as it reads: a bare word is a one-item list (header). */
export function parsePinList(raw: string | null): readonly string[] {
  if (raw === null || raw === "") return NO_ITEMS;
  if (raw.startsWith("[")) {
    try {
      const v = JSON.parse(raw) as unknown;
      if (Array.isArray(v)) {
        const out: string[] = [];
        for (const x of v) if (typeof x === "string" && x !== "" && !out.includes(x)) out.push(x);
        return out;
      }
    } catch {
      // Not JSON after all: a word that starts with a bracket.
    }
  }
  return [raw];
}

/** …and its stored form: none is no value, one the bare word, more a JSON
 *  array. */
export function serialisePinList(items: readonly string[]): string | null {
  if (items.length === 0) return null;
  if (items.length === 1 && !items[0]!.startsWith("[")) return items[0]!;
  return JSON.stringify(items);
}

function listMark(store: MarkStore): ListMark {
  let raw: string | null = null;
  let items: readonly string[] = NO_ITEMS;
  const list = (): readonly string[] => {
    const now = store.get();
    if (now !== raw) {
      raw = now;
      items = parsePinList(now);
    }
    return items;
  };
  const write = (next: readonly string[]) => store.set(serialisePinList(next));
  return {
    list,
    last: () => {
      const l = list();
      return l.length > 0 ? l[l.length - 1]! : null;
    },
    has: (item) => list().includes(item),
    set: (item) => write(item === null ? NO_ITEMS : [item]),
    add: (item) => {
      const l = list();
      if (!l.includes(item)) write([...l, item]);
    },
    drop: (item) => {
      const l = list();
      if (l.includes(item)) write(l.filter((x) => x !== item));
    },
    subscribe: store.subscribe,
    onStorage: store.onStorage,
  };
}

// THE OPEN FAVOURITE'S PIN (claude-rail rail-pin-lib; owner, 2026-11-16):
// which favourite's card the reader PINNED
// in this window (the pin icon, a double-click on its ring) — the open card is
// pinned while this names the open key, TEMP otherwise. sessionStorage
// `fused-render:fav-pinned`, so a reload brings back only a pinned card
// (FavouritePeek). FREE PINS: every pinned favourite (header "MANY PINS").
const favPinnedMark = listMark(markStore("fused-render:fav-pinned"));

/** The favourite whose card is pinned in THIS window, by task key (the
 *  newest, with free pins). */
export function favPinned(): string | null {
  return favPinnedMark.last();
}

/** Pin `key`'s favourite card in this window, or let the pin go (`null`).
 *  Replaces every pin (the switch-off rule: one pinned favourite). */
export function setFavPinned(key: string | null): void {
  const was = favPinnedMark.list();
  favPinnedMark.set(key);
  for (const k of was) if (k !== key) settlePinPos(favPinId(k));
  if (key === null) settlePos("b");
}

/** FREE PINS: every favourite pinned in this window, oldest first. */
export function favPins(): readonly string[] {
  return favPinnedMark.list();
}
export function hasFavPin(key: string): boolean {
  return favPinnedMark.has(key);
}
/** Pin one more favourite; the others stay. */
export function addFavPin(key: string): void {
  favPinnedMark.add(key);
}
/** Let one favourite's pin go; the others stay. */
export function dropFavPin(key: string): void {
  if (!favPinnedMark.has(key)) return;
  favPinnedMark.drop(key);
  settlePinPos(favPinId(key));
  settlePos("b");
}
export function useFavPins(): readonly string[] {
  return useSyncExternalStore(favPinnedMark.subscribe, favPinnedMark.list, favPinnedMark.list);
}

// WINDOW A'S PIN (claude-rail rail-pin-lib; owner, 2026-11-16: "when we pin we
// are pinning the window rather than the task"): what the pinned window A
// shows in this window — `task:<key>`, `plus`, `tasks`, `bots`, `git`
// (`aPinWord`) — or nothing while it is not pinned. sessionStorage
// `fused-render:a-pinned`, so the pin outlives the rail that held it: a page
// hop, a page with no rail, a reload (ClaudeRail's restore).
// FREE PINS: every pinned section A card, each by its word (header "MANY
// PINS").
const aPinnedMark = listMark(markStore("fused-render:a-pinned"));

/** What the pinned window A shows in THIS window, or null (the newest pin,
 *  with free pins). */
export function aPinned(): string | null {
  return aPinnedMark.last();
}

/** Pin window A on `word`, or let the pin go (`null`). Replaces every pin
 *  (the switch-off rule: one pinned window A). */
export function setAPinned(word: string | null): void {
  const was = aPinnedMark.list();
  aPinnedMark.set(word);
  for (const w of was) if (w !== word) settlePinPos(w);
  if (word === null) settlePos("a");
}

/** What the pinned window A shows, live. */
export function useAPinned(): string | null {
  return useSyncExternalStore(aPinnedMark.subscribe, aPinnedMark.last, aPinnedMark.last);
}

/** FREE PINS: every section A pin's word, oldest first. */
export function aPins(): readonly string[] {
  return aPinnedMark.list();
}
export function hasAPin(word: string): boolean {
  return aPinnedMark.has(word);
}
/** Pin one more section A card; the others stay. */
export function addAPin(word: string): void {
  aPinnedMark.add(word);
}
/** Let one section A pin go; the others stay. */
export function dropAPin(word: string): void {
  if (!aPinnedMark.has(word)) return;
  aPinnedMark.drop(word);
  settlePinPos(word);
  settlePos("a");
}
export function useAPins(): readonly string[] {
  return useSyncExternalStore(aPinnedMark.subscribe, aPinnedMark.list, aPinnedMark.list);
}

/** `onLastChatStorage`, for the favourite's pin. */
export function onFavPinnedStorage(e: Pick<StorageEvent, "key" | "storageArea">): void {
  favPinnedMark.onStorage(e);
}

/** The pinned favourite, live. */
export function useFavPinned(): string | null {
  return useSyncExternalStore(favPinnedMark.subscribe, favPinnedMark.last, favPinnedMark.last);
}

// THE OPEN BOT CARD IS PER WINDOW TOO (owner, 2026-11-08: "bots open in
// window B close when I switch view, which is wrong — tasks open in window B
// don't; they should only close on a switch to a task or another bot, or the
// ×"): which pinned bot's card (ClaudeRail's BotPeek) is up in THIS window,
// by bot id — sessionStorage `fused-render:bot-open`, the favourite's rule.
// The rail is a page's (AppPage, FallbackRail) and remounts with the page,
// where the favourite's card is the shell's; the mark is what lets the next
// rail pick the card back up. A bot ring's press (pin) sets it, the ×, a
// favourite opening or another bot's press clears or replaces it.
// FREE PINS: every pinned bot (header "MANY PINS").
const botOpenMark = listMark(markStore("fused-render:bot-open"));

/** The bot whose card is pinned in THIS window, by id (the newest, with
 *  free pins). */
export function botOpen(): string | null {
  return botOpenMark.last();
}

/** Pin `id`'s bot card in this window, or let it go (`null`). Replaces every
 *  pin (the switch-off rule). */
export function setBotOpen(id: string | null): void {
  const was = botOpenMark.list();
  botOpenMark.set(id);
  for (const b of was) if (b !== id) settlePinPos(botPinId(b));
  if (id === null) settlePos("b");
}

/** `onLastChatStorage`, for the open bot. */
export function onBotOpenStorage(e: Pick<StorageEvent, "key" | "storageArea">): void {
  botOpenMark.onStorage(e);
}

export function useBotOpen(): string | null {
  return useSyncExternalStore(botOpenMark.subscribe, botOpenMark.last, () => null);
}

/** FREE PINS: every bot pinned in this window, oldest first. */
export function botPins(): readonly string[] {
  return botOpenMark.list();
}
export function hasBotPin(id: string): boolean {
  return botOpenMark.has(id);
}
export function addBotPin(id: string): void {
  botOpenMark.add(id);
}
export function dropBotPin(id: string): void {
  if (!botOpenMark.has(id)) return;
  botOpenMark.drop(id);
  settlePinPos(botPinId(id));
  settlePos("b");
}
export function useBotPins(): readonly string[] {
  return useSyncExternalStore(botOpenMark.subscribe, botOpenMark.list, () => NO_ITEMS);
}

// A PIN'S ID (free pins): what its position, its stacking and its keyboard
// are kept under. Section A's is its word as `aPinned` spells it (`task:<key>`,
// `plus`, `tasks`, `bots`, `git`, `mode:<m>`); section B's says which list it
// is in — `fav:<key>`, `bot:<id>`. No word of section A starts so.
export function favPinId(key: string): string {
  return "fav:" + key;
}
export function botPinId(id: string): string {
  return "bot:" + id;
}
/** The section a pin id is in. */
export function pinIdSection(id: string): "a" | "b" {
  return id.startsWith("fav:") || id.startsWith("bot:") ? "b" : "a";
}
/** Is `id` one of this window's pins now? */
export function isPinnedId(id: string): boolean {
  if (id.startsWith("fav:")) return favPinnedMark.has(id.slice(4));
  if (id.startsWith("bot:")) return botOpenMark.has(id.slice(4));
  return aPinnedMark.has(id);
}
/** Every pin id of this window now. */
export function pinnedIds(): string[] {
  return [...aPinnedMark.list(), ...favPinnedMark.list().map(favPinId), ...botOpenMark.list().map(botPinId)];
}

// WHERE A PINNED WINDOW FLOATS (claude-rail rail-move-lib; owner, 2026-11-17:
// a pinned window A or B dragged by its head stays where it was put): one
// position per window, JSON `{left, top}` — sessionStorage
// `fused-render:a-pos` and `fused-render:fav-pos` (window B: the favourite's
// card and the bot's card alike), beside the pins they belong to. It lives
// EXACTLY AS LONG AS THE PIN: a page hop and a reload keep it with the pin, and
// the pin going clears it.
//
// THE PIN GOING, NOT ITS MARK BLINKING: a swap inside a pinned window (a door
// to a ring, a favourite to a bot) can clear the old card's mark a moment
// before the new card writes its own — the pin is the window's and never went.
// So the position is cleared only once the window's marks are STILL all empty
// after that moment (`settlePos`, the next task).
const aPosMark = markStore("fused-render:a-pos");
const favPosMark = markStore("fused-render:fav-pos");
const posMark = (win: "a" | "b"): MarkStore => (win === "a" ? aPosMark : favPosMark);
/** Is the window pinned on any card right now (its marks)? */
function windowPinnedNow(win: "a" | "b"): boolean {
  return win === "a" ? aPinnedMark.list().length > 0 : favPinnedMark.list().length > 0 || botOpenMark.list().length > 0;
}
const settleTimers: Record<"a" | "b", ReturnType<typeof setTimeout> | null> = { a: null, b: null };
function settlePos(win: "a" | "b"): void {
  if (settleTimers[win] !== null || typeof setTimeout === "undefined") return;
  settleTimers[win] = setTimeout(() => {
    settleTimers[win] = null;
    if (!windowPinnedNow(win)) posMark(win).set(null);
  }, 0);
}

/** Window `win`'s stored position, as stored (rail-move-lib `parsePos` reads
 *  it), or null while it is docked. */
export function windowPosMark(win: "a" | "b"): string | null {
  return posMark(win).get();
}

/** Float window `win` at a stored position (rail-move-lib `serialisePos`), or
 *  dock it (`null`). */
export function setWindowPosMark(win: "a" | "b", raw: string | null): void {
  posMark(win).set(raw);
}

/** `subscribeLastChat`, for a window's position. */
export function subscribeWindowPosMark(win: "a" | "b", fn: () => void): () => void {
  return posMark(win).subscribe(fn);
}

/** `onLastChatStorage`, for the windows' positions. */
export function onWindowPosStorage(e: Pick<StorageEvent, "key" | "storageArea">): void {
  aPosMark.onStorage(e);
  favPosMark.onStorage(e);
  pinPosMark.onStorage(e);
}

// WHERE EACH FREE PIN FLOATS (claude-rail rail-slots-lib; owner, 2026-11-24:
// many pins, each "its own frozen position AND size"): one box per PIN, not
// per window — sessionStorage `fused-render:pin-pos`, a JSON object from pin
// id (above) to the box's stored form (claude-rail rail-slots-lib
// `serialisePinBox`, read back by `parsePinMark`). It lives exactly as long as
// its pin: a pin let go has its box cleared a moment later (`settlePinPos`,
// the window position's rule above), so the card fading out still reads it.
const pinPosMark = markStore("fused-render:pin-pos");
let pinPosRawSeen: string | null = null;
let pinPosMap: Readonly<Record<string, string>> = Object.freeze({});
function pinPosAll(): Readonly<Record<string, string>> {
  const raw = pinPosMark.get();
  if (raw === pinPosRawSeen) return pinPosMap;
  pinPosRawSeen = raw;
  let next: Record<string, string> = {};
  if (raw) {
    try {
      const v = JSON.parse(raw) as unknown;
      if (v && typeof v === "object" && !Array.isArray(v)) {
        for (const [k, s] of Object.entries(v as Record<string, unknown>)) if (typeof s === "string") next[k] = s;
      }
    } catch {
      next = {};
    }
  }
  pinPosMap = Object.freeze(next);
  return pinPosMap;
}

/** Pin `id`'s stored box, as stored, or null. */
export function pinPosRaw(id: string): string | null {
  return pinPosAll()[id] ?? null;
}

/** Every pin id that has a stored box. */
export function pinPosIds(): string[] {
  return Object.keys(pinPosAll());
}

/** Store pin `id`'s box (`null`: clear it). */
export function setPinPosRaw(id: string, raw: string | null): void {
  const all = pinPosAll();
  if ((all[id] ?? null) === raw) return;
  const next: Record<string, string> = { ...all };
  if (raw === null) delete next[id];
  else next[id] = raw;
  pinPosMark.set(Object.keys(next).length === 0 ? null : JSON.stringify(next));
}

/** `subscribeLastChat`, for the pins' boxes. */
export function subscribePinPos(fn: () => void): () => void {
  return pinPosMark.subscribe(fn);
}

const pinSettles = new Set<string>();
function settlePinPos(id: string): void {
  if (typeof setTimeout === "undefined") {
    if (!isPinnedId(id)) setPinPosRaw(id, null);
    return;
  }
  if (pinSettles.has(id)) return;
  pinSettles.add(id);
  setTimeout(() => {
    pinSettles.delete(id);
    if (!isPinnedId(id)) setPinPosRaw(id, null);
  }, 0);
}

// A position the window's storage kept with no pin beside it (the pin went in
// a document that closed before its clear ran): docked.
if (aPosMark.get() !== null && !windowPinnedNow("a")) aPosMark.set(null);
if (favPosMark.get() !== null && !windowPinnedNow("b")) favPosMark.set(null);
// …and a pin's box the same way.
for (const id of pinPosIds()) if (!isPinnedId(id)) setPinPosRaw(id, null);
// A WINDOW'S BOX FROM THE EARLIER BUILD (one pin per section, its box kept
// under the window's mark): the section's one pin adopts it as its own, so a
// reload into this build finds the pin where it stood. The window's mark is
// left as it is — the switch-off rules still read it.
adoptLegacyWindowBoxes();
/** The adoption above (exported: the test seam that proves it). */
export function adoptLegacyWindowBoxes(): void {
  adoptWindowPos("a", aPinnedMark.list());
  adoptWindowPos("b", [...favPinnedMark.list().map(favPinId), ...botOpenMark.list().map(botPinId)]);
}
function adoptWindowPos(win: "a" | "b", ids: readonly string[]): void {
  const raw = posMark(win).get();
  if (raw === null || ids.length !== 1 || pinPosRaw(ids[0]!) !== null) return;
  setPinPosRaw(ids[0]!, raw);
}
