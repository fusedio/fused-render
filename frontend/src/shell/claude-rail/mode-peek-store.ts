// THE MODE CARDS OFF THE RAIL'S MODE DOORS (owner, 2026-11-20: "in the top
// right corner we show the mode in which we render this — above the Git icon
// add all the mode icons except the active one, in the same order; on hover
// we show preview window A … behaviour same as the other items").
//
// A file page's mode menu (Preview / Code / Reader …) says how the file is
// drawn. Each OTHER mode is a door on the rail, above the Git door; its
// window A card (ModePeek, mounted in the shell beside GitPeek) shows the
// file in that mode.
//
// EACH MODE DOOR IS AN ITEM OF ITS OWN, EXACTLY AS THE GIT DOOR IS (owner,
// 2026-11-20: "why is pin behaviour different in modes and in git — keep it
// the same, in the rail we treat it like the same item"): one door-card-store
// per mode, with its own card, its own pin and its own mark (`mode:<mode>`,
// rail-pin-lib `readAPin`). A pinned Code card stays where it is while the
// Reader door previews and opens its own card beside it; the rules between
// the cards are ClaudeRail's (one non-pinned window A card at a time). A
// first build made them ONE card with several doors: a pinned card then
// swapped on another mode's click and silenced the other mode doors' hover,
// which no other door on the rail does.
//
// WHAT THIS FILE HOLDS:
//   - the stores, by mode (`modeCardStore`), made on first ask;
//   - all of them read together (`useModeCards`), for the rail's rules;
//   - the rail that opened each (an explorer split is two rails on two
//     files), git-peek-store's rule;
//   - each rail's facts (`publishRailModes`): the file, its doors, the frame
//     address of a mode and the page's own switch — rail-git-store's shape.
import { useSyncExternalStore, type ReactNode } from "react";
import type { Timers } from "./rail-peek-lib";
import { createDoorCardStore, type DoorCardState, type DoorCardStore } from "./door-card-store";
import { taskHeadUnifiedNow } from "@shell/task-head-flag";

/** `data-rail-door` on every mode door (rail-pin-lib `doorSelector`); the
 *  mode itself is `data-rail-mode`. */
export const MODE_DOOR = "mode";

const KEY_PREFIX = "mode:";
/** A mode card's key — its intent machine's one key AND window A's mark while
 *  it is pinned (rail-pin-lib `aPinWord`). */
export const modeKey = (mode: string): string => KEY_PREFIX + mode;
export function modeOfKey(key: string | null): string | null {
  return key && key.startsWith(KEY_PREFIX) && key.length > KEY_PREFIX.length ? key.slice(KEY_PREFIX.length) : null;
}
/** A mode card's DOM id — what its door's `aria-controls` names. */
export const modePeekId = (mode: string): string => "claude-rail-mode-peek-" + mode.replace(/[^\w-]/g, "_");
/** One mode door of one rail, for the outside-press classifier. */
export const modeDoorSelector = (mode: string): string => `[data-rail-door="${MODE_DOOR}"][data-rail-mode="${CSS_ESCAPE(mode)}"]`;
function CSS_ESCAPE(v: string): string {
  return v.replace(/["\\]/g, "\\$&");
}

/** One mode door, as the page's mode menu draws that mode. */
export interface RailModeDoor {
  mode: string;
  /** The mode's name ("Code", "Reader"; platform/lib/mode-name). */
  title: string;
  icon: ReactNode;
}

/** What a rail knows about its page's file. */
export interface RailModes {
  /** The file. */
  path: string;
  /** The modes on offer, the active one left out, in the menu's order. */
  doors: RailModeDoor[];
  /** The frame that draws the file in `mode`. */
  src: (mode: string) => string;
  /** The page's own switch to `mode` (the menu's). */
  onOpen: (mode: string) => void;
}

// ---- one bus for everything here ----------------------------------------------

const subs = new Set<() => void>();
function emit(): void {
  snapshot = null;
  for (const fn of [...subs]) fn();
}
function subscribe(fn: () => void): () => void {
  subs.add(fn);
  return () => {
    subs.delete(fn);
  };
}

// ---- the stores, by mode --------------------------------------------------------

const stores = new Map<string, DoorCardStore>();
let testTimers: Timers | undefined;

/** `mode`'s card store (made on first ask, kept for the document's life). */
export function modeCardStore(mode: string): DoorCardStore {
  let store = stores.get(mode);
  if (!store) {
    store = createDoorCardStore(modeKey(mode), modePeekId(mode), modeKey(mode));
    if (testTimers) store.resetForTests(testTimers);
    store.subscribe(emit);
    stores.set(mode, store);
    emit();
  }
  return store;
}

const CLOSED: DoorCardState = { up: false, pinned: false, held: false, seq: 0, ready: false };

/** Every mode card as it stands. */
export interface ModeCards {
  /** The modes that have a store, in the order they were first asked for. */
  modes: readonly string[];
  /** A mode's card; closed for a mode with no store yet. */
  of(mode: string): DoorCardState;
}
let snapshot: ModeCards | null = null;
export function modeCards(): ModeCards {
  if (!snapshot) {
    const states = new Map([...stores].map(([mode, s]) => [mode, s.state()] as const));
    snapshot = { modes: [...states.keys()], of: (mode) => states.get(mode) ?? CLOSED };
  }
  return snapshot;
}
/** `modeCards`, live: re-renders on any mode card's change. */
export function useModeCards(): ModeCards {
  return useSyncExternalStore(subscribe, modeCards, modeCards);
}

export const closeModeCard = (mode: string, returningFocus = false): void => {
  stores.get(mode)?.close(returningFocus);
};
/** Every mode card that is up and that `keep` does not ask to stand. */
export function closeModeCards(keep?: (mode: string, s: DoorCardState) => boolean): void {
  for (const [mode, store] of stores) {
    const s = store.state();
    if (s.up && !keep?.(mode, s)) store.close();
  }
}

// ---- the rail that opened each (git-peek-store's rule) --------------------------

const openers = new Map<string, HTMLElement>();
/** `mode`'s door of `rail` is opening its card (a hover's arrival, a press). */
export function setModeCardRail(mode: string, rail: HTMLElement | null): void {
  if ((openers.get(mode) ?? null) === rail) return;
  if (rail) openers.set(mode, rail);
  else openers.delete(mode);
  emit();
}
/** `rail` unmounted: a preview it opened closes, a pinned card falls back to
 *  the top rail (the hop rule). */
export function forgetModeCardRail(rail: HTMLElement): void {
  let changed = false;
  for (const [mode, opener] of [...openers]) {
    if (opener !== rail) continue;
    const store = stores.get(mode);
    const s = store?.state();
    if (store && s && !(taskHeadUnifiedNow() ? s.held : s.pinned)) store.close();
    openers.delete(mode);
    changed = true;
  }
  if (changed) emit();
}
export const modeCardRail = (mode: string): HTMLElement | null => openers.get(mode) ?? null;
export function useModeCardRail(mode: string): HTMLElement | null {
  const get = () => modeCardRail(mode);
  return useSyncExternalStore(subscribe, get, get);
}

// ---- each rail's facts (rail-git-store's shape) ---------------------------------

const facts = new Map<HTMLElement, RailModes>();

/** What `rail`'s page offers, or null (no file page, no other mode, gone). */
export function publishRailModes(rail: HTMLElement, value: RailModes | null): void {
  const was = facts.get(rail) ?? null;
  if (was === value) return;
  if (value) {
    facts.set(rail, value);
    // A card stands behind every door from the moment it is drawn.
    for (const d of value.doors) modeCardStore(d.mode);
  } else facts.delete(rail);
  emit();
}
export function railModesOf(rail: HTMLElement | null): RailModes | null {
  return (rail && facts.get(rail)) ?? null;
}
export function useRailModes(rail: HTMLElement | null): RailModes | null {
  const get = () => railModesOf(rail);
  return useSyncExternalStore(subscribe, get, get);
}
/** The most recent rail with a door for `mode`, or null. */
export function railWithMode(mode: string): HTMLElement | null {
  let last: HTMLElement | null = null;
  for (const [rail, f] of facts) if (f.doors.some((d) => d.mode === mode)) last = rail;
  return last;
}
export function useRailWithMode(mode: string): HTMLElement | null {
  const get = () => railWithMode(mode);
  return useSyncExternalStore(subscribe, get, get);
}

/** WHAT A PRESS ON A MODE DOOR DOES — the Git door's (git-peek-store
 *  `gitDoorAction`): a button, not a link, so a modified press is a plain
 *  one. No card to stand: the page's own switch (`open`); else a toggle. */
export function modeDoorAction(o: { ready: boolean; pinned: boolean }): "open" | "pin" | "close" {
  if (!o.ready) return "open";
  return o.pinned ? "close" : "pin";
}

export function resetModeCardsForTests(timers?: Timers): void {
  testTimers = timers;
  openers.clear();
  facts.clear();
  for (const store of stores.values()) store.resetForTests(timers);
  // `resetForTests` drops a store's subscribers, this file's among them.
  for (const store of stores.values()) store.subscribe(emit);
  emit();
}
