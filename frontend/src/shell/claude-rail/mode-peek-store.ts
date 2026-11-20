// THE MODE CARD OFF THE RAIL'S MODE DOORS (owner, 2026-11-20: "in the top
// right corner we show the mode in which we render this — above the Git icon
// add all the mode icons except the active one, in the same order; on hover
// we show preview window A … behaviour same as the other items").
//
// A file page's mode menu (Preview / Code / Reader …) says how the file is
// drawn. Each OTHER mode is a door on the rail, above the Git door; its
// window A card (ModePeek, mounted once in the shell beside GitPeek) shows
// the file in that mode. The Git card's twin — door-card-store's store, the
// chassis' rules (DoorCard) — with one difference: SEVERAL DOORS, ONE CARD.
// The intent machine runs on one key per door (`modeKey`), so a hover moving
// from one mode door to the next swaps what the card shows (the rings' own
// rule), and a press on another mode's door swaps a pinned one.
//
// WHAT THIS FILE HOLDS:
//   - the card's store (`modeCardStore`), whose `doorOf` is the door of the
//     mode it shows;
//   - WHICH MODE it shows (`modeShown`), kept through a close (the card fades
//     out on it) and across a reload (a pinned card comes back on it);
//   - the rail that opened it (an explorer split is two rails on two files),
//     git-peek-store's rule;
//   - each rail's facts (`publishRailModes`): the file, its doors, the frame
//     address of a mode and the page's own switch — rail-git-store's shape.
import { useSyncExternalStore, type ReactNode } from "react";
import type { Timers } from "./rail-peek-lib";
import { createDoorCardStore, type DoorCardState, type DoorCardStore } from "./door-card-store";
import { taskHeadUnifiedNow } from "@shell/task-head-flag";

/** The card's DOM id — what a door's `aria-controls` names. */
export const MODE_PEEK_ID = "claude-rail-mode-peek";
/** Window A's mark while the card is pinned (rail-pin-lib `readAPin`). */
export const MODE_PIN_WORD = "mode";
/** `data-rail-door` on every mode door (rail-pin-lib `doorSelector`). */
export const MODE_DOOR = "mode";

const KEY_PREFIX = "mode:";
/** The intent machine's key for one mode's door. */
export const modeKey = (mode: string): string => KEY_PREFIX + mode;
export function modeOfKey(key: string | null): string | null {
  return key && key.startsWith(KEY_PREFIX) && key.length > KEY_PREFIX.length ? key.slice(KEY_PREFIX.length) : null;
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

// ---- which mode the card shows ----------------------------------------------

const SHOWN_KEY = "fused-render:a-pinned-mode";
function readShown(): string | null {
  try {
    return sessionStorage.getItem(SHOWN_KEY) || null;
  } catch {
    return null;
  }
}
let shown: string | null = readShown();
const subs = new Set<() => void>();
function emit(): void {
  for (const fn of [...subs]) fn();
}
function subscribe(fn: () => void): () => void {
  subs.add(fn);
  return () => {
    subs.delete(fn);
  };
}
function setShown(mode: string): void {
  if (shown === mode) return;
  shown = mode;
  try {
    sessionStorage.setItem(SHOWN_KEY, mode);
  } catch {
    /* no storage: a reload forgets which mode was pinned */
  }
  emit();
}
/** The mode the card shows — or last showed (kept through a close). */
export const modeShown = (): string | null => shown;
export function useModeShown(): string | null {
  return useSyncExternalStore(subscribe, modeShown, modeShown);
}

// ---- the store ---------------------------------------------------------------

const base = createDoorCardStore(MODE_DOOR, MODE_PEEK_ID, MODE_PIN_WORD, (key) => {
  const mode = modeOfKey(key);
  if (mode) setShown(mode);
});

// EACH RAIL'S DOORS, BY MODE. The card stands level with the door of the mode
// it shows; that door gone (the page switched to it), there is none.
const doors = new WeakMap<HTMLElement, Map<string, HTMLElement>>();

/** The store, for the chassis the door cards share (DoorCard). */
export const modeCardStore: DoorCardStore = {
  ...base,
  doorOf: (rail) => (rail && shown !== null && doors.get(rail)?.get(shown)) || null,
};

export const modeCardIntent = () => modeCardStore.intent();
export const closeModeCard = (returningFocus = false): void => modeCardStore.close(returningFocus);
export const modeCardState = (): DoorCardState => modeCardStore.state();
export const useModeCard = (): DoorCardState => modeCardStore.useCard();
export const focusModeCard = (): boolean => modeCardStore.focusCard();

/** `rail`'s door for `mode` drew. The answer takes it off again. */
export function registerModeDoor(rail: HTMLElement, mode: string, door: HTMLElement): () => void {
  let map = doors.get(rail);
  if (!map) {
    map = new Map();
    doors.set(rail, map);
  }
  map.set(mode, door);
  return () => {
    const m = doors.get(rail);
    if (m?.get(mode) === door) m.delete(mode);
  };
}
export const railModeDoorEl = (rail: HTMLElement | null, mode: string): HTMLElement | null =>
  (rail && doors.get(rail)?.get(mode)) || null;

// ---- the rail that opened it (git-peek-store's rule) -------------------------

let opener: HTMLElement | null = null;
function setOpener(next: HTMLElement | null): void {
  if (opener === next) return;
  opener = next;
  emit();
}
/** A mode door of `rail` is opening the card (a hover's arrival, a press). */
export const setModeCardRail = (rail: HTMLElement | null): void => setOpener(rail);
/** `rail` unmounted: if it opened the card, a preview closes and a pinned
 *  card falls back to the top rail (the hop rule). */
export function forgetModeCardRail(rail: HTMLElement): void {
  if (opener !== rail) return;
  const s = modeCardStore.state();
  if (!(taskHeadUnifiedNow() ? s.held : s.pinned)) modeCardStore.close();
  setOpener(null);
}
export const modeCardRail = (): HTMLElement | null => opener;
export function useModeCardRail(): HTMLElement | null {
  return useSyncExternalStore(subscribe, modeCardRail, modeCardRail);
}

// ---- each rail's facts (rail-git-store's shape) ------------------------------

const facts = new Map<HTMLElement, RailModes>();

/** What `rail`'s page offers, or null (no file page, no other mode, gone). */
export function publishRailModes(rail: HTMLElement, value: RailModes | null): void {
  const was = facts.get(rail) ?? null;
  if (was === value) return;
  if (value) facts.set(rail, value);
  else facts.delete(rail);
  emit();
}
export function railModesOf(rail: HTMLElement | null): RailModes | null {
  return (rail && facts.get(rail)) ?? null;
}
export function useRailModes(rail: HTMLElement | null): RailModes | null {
  const get = () => railModesOf(rail);
  return useSyncExternalStore(subscribe, get, get);
}
/** The most recent rail with mode doors, or null. */
export function railModesAnyRail(): HTMLElement | null {
  let last: HTMLElement | null = null;
  for (const k of facts.keys()) last = k;
  return last;
}
export function useRailModesAnyRail(): HTMLElement | null {
  return useSyncExternalStore(subscribe, railModesAnyRail, railModesAnyRail);
}

/** WHAT A PRESS ON A MODE DOOR DOES — a button, not a link, so a modified
 *  press is a plain one. No card to stand (`ready` false): the page's own
 *  switch (`open`). Its own card up by a click: a toggle (`close`). Anything
 *  else — nothing up, a hover's preview, the card on another mode — shows
 *  this mode by a click (`pin`). */
export function modeDoorAction(o: { ready: boolean; own: boolean; pinned: boolean }): "open" | "pin" | "close" {
  if (!o.ready) return "open";
  return o.own && o.pinned ? "close" : "pin";
}

export function resetModeCardForTests(timers?: Timers): void {
  opener = null;
  shown = null;
  facts.clear();
  try {
    sessionStorage.removeItem(SHOWN_KEY);
  } catch {
    /* no storage */
  }
  modeCardStore.resetForTests(timers);
  emit();
}
