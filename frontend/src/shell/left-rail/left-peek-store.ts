// SIDEBAR PREVIEWS (owner, 2026-11-27: "the same hover-card behaviour on the
// collapsed left rail's icons", behind `left_rail_peek_enabled`, default
// off). Each icon of the collapsed LEFT sidebar — Home, AI Models,
// Community, every project, canvas and favourite — opens the Claude rail's
// window A card (DoorCard, `side: "left"`), to the right of the sidebar:
//
//   - A HOVER PREVIEWS; a CLICK on the icon still NAVIGATES, exactly as
//     before (one click), and puts down a card of that icon that is not held;
//   - a PRESS INSIDE the card makes it temp (an outside press closes it); its
//     PIN holds it (rail-pin-lib), and a held card moves by its head, resizes
//     by its grips and survives a page hop;
//   - the Claude rail's timings (PeekIntent), its cold and hot zones on a
//     zone of the left sidebar's own (left-rail-zone), Escape, the bridge and
//     the safe cone, no room → no card;
//   - ONE NON-HELD LEFT CARD AT A TIME: one opening puts the others down;
//   - NONE OF IT TOUCHES THE CLAUDE RAIL: no window A mark (no `pinWord`), no
//     window position, no pusher, no magnet; its moves and size are kept
//     here, in memory (`leftMove`, `leftSize`).
//
// WHAT THIS FILE HOLDS:
//   - one door-card-store per icon (`leftCardStore`), made when GlobalSidebar
//     first publishes that icon, kept for the document's life;
//   - each icon's facts (`publishLeftItems`): title, icon, what to show, the
//     icon's own click (Open does the same);
//   - the collapsed rail element (`setLeftRail`, SidebarFrame `onRailEl`);
//   - each icon's element (`leftDoorRef`) and the hooks GlobalSidebar hangs
//     on it (`leftDoorFor`, SidebarFrame `SidebarRailItem.door`);
//   - the rules between the cards: one non-held at a time, the rail going,
//     an icon going, the Settings menu open (`setLeftPeekBlocked`).
import { useSyncExternalStore, type KeyboardEvent as ReactKeyboardEvent, type PointerEvent as ReactPointerEvent, type ReactNode } from "react";
import type { SidebarRailDoor } from "@platform/ui/sidebar/sidebar-doors";
import { createDoorCardStore, type DoorCardState, type DoorCardStore } from "@shell/claude-rail/door-card-store";
import type { Timers, StoredSize } from "@shell/claude-rail/rail-peek-lib";
import type { Pos, Size } from "@shell/claude-rail/rail-move-lib";
import type { OwnMoveStore } from "@shell/claude-rail/useOwnCardMove";
import type { PressFlag } from "@shell/claude-rail/RailPin";
import { railExtrasNow } from "@shell/claude-rail/rail-extras-flag";
import { LEFT_DOOR_ATTR, LEFT_OPEN_ATTR, type LeftPeekShow } from "./left-peek-lib";
import { leftPace } from "./left-rail-zone";

/** One collapsed-sidebar icon's card, as GlobalSidebar describes it. */
export interface LeftPeekItem {
  /** The rail item's own key (`home`, `app:<path>`, `pin:<id>` …). */
  key: string;
  title: string;
  icon: ReactNode;
  show: LeftPeekShow;
  /** The icon's own click — Open does the same. A navigation's promise
   *  (false: a leave guard refused it) when it has one. */
  onOpen: () => Promise<boolean> | void;
  /** The entry is the page on screen (lit active): a hover or ArrowRight
   *  opens no card for it (owner, 2026-11-27: "if the page is active it
   *  shouldn't popup"); a card already up stays as it is. */
  active?: boolean;
}

// ---- one bus ----------------------------------------------------------------

const subs = new Set<() => void>();
let snapshot: LeftPeekSnapshot | null = null;
function emit(): void {
  snapshot = null;
  markOpen();
  for (const fn of [...subs]) fn();
}

/** WHILE A CARD THAT IS NOT HELD IS UP, `<html data-left-peek-open>` (claude-
 *  rail.css): the page's own icon or row in the sidebar quietens its `active`
 *  look, so only the entry the card belongs to reads as "this one". */
function markOpen(): void {
  if (typeof document === "undefined" || !document.documentElement?.toggleAttribute) return;
  let any = false;
  for (const st of stores.values()) {
    const c = st.state();
    if (c.up && !c.held) any = true;
  }
  document.documentElement.toggleAttribute("data-left-peek-open", any);
}
function subscribe(fn: () => void): () => void {
  subs.add(fn);
  return () => {
    subs.delete(fn);
  };
}

// ---- the stores, by item key -------------------------------------------------

const stores = new Map<string, DoorCardStore>();
/** The `seq` each store last opened with: a new one is a card coming up. */
const seqs = new Map<string, number>();
const wasUp = new Map<string, boolean>();
const wasHeld = new Map<string, boolean>();
let testTimers: Timers | undefined;
let idSeq = 0;

/** Icon `key`'s card store key — its intent machine's, and its move's in
 *  `leftMove` (DoorCard moves a card under its store's key). */
export const leftStoreKey = (key: string): string => "left:" + key;

/** A left card's DOM id — what its icon's `aria-controls` names. Numbered,
 *  so two keys that clean up alike never share one. */
function idFor(key: string): string {
  idSeq += 1;
  return "left-rail-peek-" + idSeq + "-" + key.replace(/[^\w-]/g, "_").slice(0, 40);
}

function onStoreChange(key: string): void {
  const store = stores.get(key);
  if (!store) return;
  const s = store.state();
  // ONE NON-HELD LEFT CARD AT A TIME (header): a card coming up puts the
  // others down — temp ones too; a held card stays.
  if (s.up && seqs.get(key) !== s.seq) {
    seqs.set(key, s.seq);
    closeLeftCards((k, c) => k === key || c.held);
  }
  // …AND A CARD LOSING ITS HOLD WHILE UP (its pin pressed again, free pins
  // off) is a non-held card again: the others that are not held go, so one
  // stays (CodeRabbit on #2495).
  if (s.up && wasHeld.get(key) && !s.held) closeLeftCards((k, c) => k === key || c.held);
  wasHeld.set(key, s.up && s.held);
  // A card going down with FREE PINS (rail-slots-lib): its pin is gone, so is
  // where it floated — the next opening stands by its icon. Without them the
  // spot is kept for the next pin, as window A's is.
  if (!s.up && wasUp.get(key) && railExtrasNow()) {
    movePos.delete(leftStoreKey(key));
    moveSize.delete(leftStoreKey(key));
    tellMove();
  }
  wasUp.set(key, s.up);
  emit();
}

/** The card store of icon `key` (made on first ask, kept). */
export function leftCardStore(key: string): DoorCardStore {
  let store = stores.get(key);
  if (!store) {
    store = createDoorCardStore(leftStoreKey(key), idFor(key), undefined, { pace: leftPace });
    if (testTimers) store.resetForTests(testTimers);
    const k = key;
    store.subscribe(() => onStoreChange(k));
    stores.set(key, store);
    emit();
  }
  return store;
}

/** SOMETHING ELSE TAKES THE SIDEBAR (the Settings menu, a context menu, a
 *  fold): every card that is not held goes, and no hover still waiting on
 *  its delay opens one after (PeekIntent `forget`). */
function quietLeftCards(): void {
  for (const store of stores.values()) store.intent().forget(store.key);
  closeLeftCards((_k, s) => s.held);
}

/** Every left card that is up and that `keep` does not ask to stand. */
export function closeLeftCards(keep?: (key: string, s: DoorCardState) => boolean): void {
  for (const [key, store] of stores) {
    const s = store.state();
    if (s.up && !keep?.(key, s)) store.close();
  }
}

// ---- the icons' facts ----------------------------------------------------------

const items = new Map<string, LeftPeekItem>();
let itemsSig = "";

function showSig(s: LeftPeekShow): string {
  if (s.kind === "compose") return "compose";
  if (s.kind === "app") return `app|${s.still}|${s.picture}|${s.live}`;
  return `${s.kind}|${s.src}|${s.searchHome ?? ""}|${s.spin ? "spin" : ""}`;
}

/** What the collapsed sidebar's icons offer now (GlobalSidebar, each render
 *  while the rail stands). A card stands behind every icon from the moment
 *  it is published; an icon that is gone takes its card with it — held or
 *  not (a project off the desk, a favourite unpinned). */
export function publishLeftItems(list: readonly LeftPeekItem[]): void {
  const sig = list.map((i) => `${i.key}\u0000${i.title}\u0000${showSig(i.show)}\u0000${i.active ? 1 : 0}`).join("\u0001");
  const next = new Map(list.map((i) => [i.key, i] as const));
  // The facts are refreshed every time (the click's closure is the render's);
  // only a change in what is drawn is news.
  for (const [key, item] of next) items.set(key, item);
  const gone = [...items.keys()].filter((k) => !next.has(k));
  for (const k of gone) {
    items.delete(k);
    stores.get(k)?.close();
  }
  for (const k of next.keys()) leftCardStore(k);
  if (sig === itemsSig && gone.length === 0) return;
  itemsSig = sig;
  emit();
}

export function leftItem(key: string): LeftPeekItem | null {
  return items.get(key) ?? null;
}

// ---- the rail ----------------------------------------------------------------

let rail: HTMLElement | null = null;

/** The sidebar's element (`nav#sidebar`, collapsed or expanded — one node),
 *  or null (gone: the switch off, an embed) — SidebarFrame `onRailEl`; a
 *  stable function. The sidebar going puts down every card that is not held,
 *  and a held one that does not float (it has nowhere to stand): a held,
 *  moved card stays where it floats. */
export function setLeftRail(el: HTMLElement | null): void {
  if (rail === el) return;
  rail = el;
  if (!el) closeLeftCards((key, s) => s.held && movePos.has(leftStoreKey(key)));
  emit();
}

export function leftRail(): HTMLElement | null {
  return rail;
}

/** THE SIDEBAR COLLAPSED OR EXPANDED (LeftPeek): every card that is not held
 *  goes; a held one re-places beside the sidebar's new edge, level with its
 *  entry in the new layout if that draws one (the doors registered in the
 *  commit), else in the box it stood in. */
export function leftSidebarFolded(): void {
  quietLeftCards();
  emit();
  // A collapse may hold the expanded rows for its slide (SidebarFrame's tuck)
  // and swap in the rail's icons after it: read the doors again then.
  if (typeof window !== "undefined") window.setTimeout(emit, FOLD_SETTLE_MS);
}
/** Past the sidebar's collapse slide (`--dur-panel`, SidebarFrame's tuck). */
const FOLD_SETTLE_MS = 400;

// ---- the Settings menu --------------------------------------------------------

let blocked = false;

/** The sidebar's Settings menu is open (`on`): no card opens over it, and a
 *  card that is not held goes. */
export function setLeftPeekBlocked(on: boolean): void {
  if (blocked === on) return;
  blocked = on;
  if (on) quietLeftCards();
  emit();
}

// ---- every card, read together ---------------------------------------------------

export interface LeftPeekSnapshot {
  /** The icons that have a store, in the order they were first published. */
  keys: readonly string[];
  /** The facts of the icons published now. */
  items: ReadonlyMap<string, LeftPeekItem>;
  rail: HTMLElement | null;
  blocked: boolean;
}

export function leftPeekSnapshot(): LeftPeekSnapshot {
  if (!snapshot) snapshot = { keys: [...stores.keys()], items: new Map(items), rail, blocked };
  return snapshot;
}

/** Every left card's facts, live. */
export function useLeftPeek(): LeftPeekSnapshot {
  return useSyncExternalStore(subscribe, leftPeekSnapshot, leftPeekSnapshot);
}

// ---- the icons -----------------------------------------------------------------

/** Where an entry stands: an icon of the collapsed rail, or a row of the
 *  expanded sidebar. One card per key serves both. */
export type LeftDoorSlot = "rail" | "row";

const doorRefs = new Map<string, (el: HTMLElement | null) => void>();

/** Entry `key`'s ref in `slot` (stable per key and slot): it registers as its
 *  card's door, on the sidebar it stands in (`nav#sidebar`, one node in both
 *  layouts). The card reads its door at render (LeftPeek re-renders the
 *  cards when the sidebar collapses or expands). */
export function leftDoorRef(key: string, slot: LeftDoorSlot = "rail"): (el: HTMLElement | null) => void {
  const id = slot + "\u0000" + key;
  let fn = doorRefs.get(id);
  if (!fn) {
    let off: (() => void) | null = null;
    fn = (el) => {
      off?.();
      off = null;
      if (!el) return;
      const nav = el.closest<HTMLElement>("#sidebar");
      if (nav) off = leftCardStore(key).registerDoor(nav, el);
    };
    doorRefs.set(id, fn);
  }
  return fn;
}

/** WHETHER A KEY ON AN ENTRY OPENS ITS CARD: a bare ArrowRight, on an entry
 *  whose card can stand, not typed into a field, nothing else holding the
 *  sidebar. (Up / Down stay sidebarArrowNav's; Enter stays the link's.) */
export function leftKeyOpens(o: { key: string; modified: boolean; ready: boolean; editable: boolean; blocked: boolean }): boolean {
  return o.key === "ArrowRight" && !o.modified && o.ready && !o.editable && !o.blocked;
}

function isEditable(t: EventTarget | null): boolean {
  const el = t as (Element & { isContentEditable?: boolean }) | null;
  if (!el || typeof el.closest !== "function") return false;
  return !!el.closest("input, textarea, select") || el.isContentEditable === true;
}

/** The keyboard into a card the key just opened: its first seat, once it is
 *  drawn (ClaudeRail `cardFocusSoon`: a few frames' retries). */
function focusCardSoon(store: DoorCardStore): void {
  if (typeof requestAnimationFrame === "undefined" || typeof document?.getElementById !== "function") return;
  let tries = 0;
  const attempt = () => {
    const card = document.getElementById(store.id);
    if (store.focusCard() && card?.contains(document.activeElement)) return;
    if (++tries < 12) requestAnimationFrame(attempt);
  };
  requestAnimationFrame(attempt);
}

/** SOMETHING ELSE HAS THE SIDEBAR'S ATTENTION: a context menu or a menu is
 *  open (a row's, the Settings menu), a dialog (the New app composer, a
 *  rename), or a field in the sidebar has the keyboard (a bookmark's inline
 *  rename). No card opens then. */
function sidebarBusy(): boolean {
  if (typeof document === "undefined" || typeof document.querySelector !== "function") return false;
  if (document.querySelector(".context-menu, [role='menu'], .modal-overlay:not(.modal-popover)")) return true;
  const a = document.activeElement as (Element & { closest?: (s: string) => Element | null }) | null;
  return !!a && typeof a.closest === "function" && !!a.closest("#sidebar") && !!a.closest("input, textarea, select, [contenteditable='true'], [contenteditable='']");
}

/** "A press started in one of the left sidebar's cards" (rail-pin-lib
 *  `viewPress`, the left view's own): their menus and pickers close no other
 *  left card. */
export const leftViewPress: PressFlag = { inside: false };

/** WHAT ICON `key` WEARS FOR ITS CARD (SidebarFrame `SidebarRailItem.door`),
 *  or undefined — no card for it (not published). */
export function leftDoorFor(key: string, slot: LeftDoorSlot = "rail"): SidebarRailDoor | undefined {
  const store = stores.get(key);
  if (!store || !items.has(key)) return undefined;
  const s = store.state();
  /** The entry's page is on screen: nothing opens (`LeftPeekItem.active`). */
  const here = () => items.get(key)?.active === true;
  const putDownOwn = () => {
    const c = store.state();
    if (c.up && !c.held) store.close();
  };
  return {
    ref: leftDoorRef(key, slot),
    onPointerEnter: (e: ReactPointerEvent<HTMLElement>) => {
      // A touch is no hover; a pointer dragging something (a row's reorder,
      // the width seam) opens nothing; nor does one while a menu, a dialog
      // or a rename has the sidebar.
      if (e.pointerType === "touch" || e.buttons !== 0 || blocked || sidebarBusy() || here()) return;
      if (!store.state().ready) return;
      store.intent().enterRing(store.key, "pointer");
    },
    onPointerLeave: () => store.intent().leaveRing("pointer"),
    // A PRESS ON THE ENTRY — its link (the click navigates), a kebab, a
    // glyph's icon picker, a drag's start — puts its card down unless held.
    onPointerDown: putDownOwn,
    // Its context menu opening: every card that is not held goes.
    onContextMenu: quietLeftCards,
    // The keyboard: Escape lets a preview go (a held card closes by its pin
    // or Open only) — the Claude rail's Git door's rule; a focus opens
    // nothing, as there. ARROWRIGHT OPENS THE CARD (CodeRabbit on #2495; Enter
    // still navigates): the door's press, from the keyboard — the card opens
    // temp and the keyboard lands in it; its Escape hands it back here
    // (DoorCard `close`). Not from a field (a row's inline rename).
    onKeyDown: (e: ReactKeyboardEvent<HTMLElement>) => {
      const c = store.state();
      if (e.key === "Escape" && c.up && !c.held) {
        e.preventDefault();
        store.close(true);
        return;
      }
      if (leftKeyOpens({ key: e.key, modified: e.altKey || e.ctrlKey || e.metaKey || e.shiftKey, ready: c.ready && !here(), editable: isEditable(e.target), blocked: blocked || sidebarBusy() })) {
        e.preventDefault();
        store.intent().press(store.key);
        focusCardSoon(store);
      }
    },
    // THE CLICK NAVIGATES (header): a card of this icon that is not held goes
    // with it; a held one stays.
    onActivate: putDownOwn,
    attrs: {
      [LEFT_DOOR_ATTR]: key,
      // THE ENTRY WHOSE CARD IS UP LOOKS OPEN (claude-rail.css; the Claude
      // rail's `doorOpen`): a preview or temp card, or a held one.
      ...(s.up ? { [LEFT_OPEN_ATTR]: s.held ? "pinned" : "open" } : {}),
      ...(s.ready && (s.up || !here())
        ? { "aria-haspopup": "dialog", "aria-expanded": s.up ? "true" : "false", "aria-controls": store.id, "aria-keyshortcuts": "ArrowRight" }
        : {}),
    },
    // The card speaks for the icon: no tooltip beside it.
    quietTip: s.ready && (s.up || !here()),
  };
}

// ---- where a held card floats, and its size (memory) -------------------------

const movePos = new Map<string, Pos>();
const moveSize = new Map<string, Size>();
let moveFront: string | null = null;
const moveSubs = new Set<() => void>();
function tellMove(): void {
  for (const fn of [...moveSubs]) fn();
}

/** The left cards' own move store (useOwnCardMove), keyed by the card
 *  store's key. Never the Claude rail's windows. */
export const leftMove: OwnMoveStore = {
  pos: (key) => movePos.get(key) ?? null,
  size: (key) => moveSize.get(key) ?? null,
  setPos(key, pos) {
    const was = movePos.get(key) ?? null;
    if (was === pos || (was && pos && was.left === pos.left && was.top === pos.top)) return;
    if (pos) movePos.set(key, pos);
    else movePos.delete(key);
    tellMove();
  },
  setSize(key, size) {
    if (size) moveSize.set(key, size);
    else moveSize.delete(key);
    tellMove();
  },
  front: () => moveFront,
  raise(key) {
    if (moveFront === key) return;
    moveFront = key;
    tellMove();
  },
  subscribe(fn) {
    moveSubs.add(fn);
    return () => {
      moveSubs.delete(fn);
    };
  },
};

/** The left cards' docked size, once the reader drags one (until then window
 *  A's remembered size). Memory: shared by every left card. */
let leftSizeNow: StoredSize | null = null;
export const leftSize = {
  get: (): StoredSize | null => leftSizeNow,
  set: (size: StoredSize): void => {
    leftSizeNow = size;
  },
};

// ---- tests -----------------------------------------------------------------------

export function resetLeftPeekForTests(timers?: Timers): void {
  testTimers = timers;
  items.clear();
  itemsSig = "";
  rail = null;
  blocked = false;
  movePos.clear();
  moveSize.clear();
  moveFront = null;
  leftSizeNow = null;
  seqs.clear();
  wasUp.clear();
  wasHeld.clear();
  leftViewPress.inside = false;
  for (const [key, store] of stores) {
    store.resetForTests(timers);
    // `resetForTests` drops a store's subscribers, this file's among them.
    store.subscribe(() => onStoreChange(key));
  }
  emit();
}
