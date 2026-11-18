// FREE PINS, LIVE (rail-slots-lib.ts header; owner, 2026-11-18): the few
// things the four windows say to each other that are not geometry
// (rail-move-store has that) and not a window's mark (platform/lib last-chat
// has those):
//
//   - WHICH PINNED WINDOW A PIN ICON'S HOVER MARKS (`doomed`): the pin icon of
//     a non-pin card, hovered or focused, wears its section's pinned window a
//     red border — the one its press would close;
//   - WHAT EACH PINNED WINDOW IS CALLED (`pinTitle`), for that icon's caption;
//   - A PINNED WINDOW ASKING ITS SECTION'S NON-PIN TO OPEN (`askOpen`): the
//     unpin puts the window back beside the rail, and the card beside the rail
//     is another component's (the rail's own list card, the favourite's card,
//     the bot's) — it hears the ask and opens on the key;
//   - THE KEYBOARD INTO A PINNED WINDOW (`focusPin`): a ring's click on a
//     pinned task raises its window and lands in its composer.
import { useSyncExternalStore } from "react";
import type { RailWindow } from "./rail-move-lib";

let doomed: RailWindow | null = null;
const subs = new Set<() => void>();
function notify(): void {
  for (const fn of [...subs]) fn();
}
function subscribe(fn: () => void): () => void {
  subs.add(fn);
  return () => {
    subs.delete(fn);
  };
}

/** The pinned window a pin icon's hover marks (rail-slots-lib `doomedBy`), or
 *  none. */
export function setDoomedWindow(win: RailWindow | null): void {
  if (doomed === win) return;
  doomed = win;
  notify();
}
export function doomedWindow(): RailWindow | null {
  return doomed;
}
export function useDoomedWindow(): RailWindow | null {
  return useSyncExternalStore(subscribe, doomedWindow, () => null);
}

const titles: Record<RailWindow, { owner: string; title: string } | null> = { a: null, b: null };

/** A pinned window says what it shows (its host, while it is up), or lets go
 *  (`null`) — only its own word: a host going never wipes the next one's. */
export function setPinTitle(win: RailWindow, owner: string, title: string | null): void {
  const was = titles[win];
  if (title === null) {
    if (!was || was.owner !== owner) return;
    titles[win] = null;
  } else {
    if (was && was.owner === owner && was.title === title) return;
    titles[win] = { owner, title };
  }
  notify();
}
export function pinTitle(win: RailWindow): string | null {
  return titles[win]?.title ?? null;
}
export function usePinTitle(win: RailWindow | null): string | null {
  return useSyncExternalStore(
    subscribe,
    () => (win ? pinTitle(win) : null),
    () => null,
  );
}

/** What a section's non-pin is asked to open: a task's chat (by key), or a
 *  bot's (by id). */
export type OpenAsk = { win: "a"; kind: "task"; key: string } | { win: "b"; kind: "task"; key: string } | { win: "b"; kind: "bot"; key: string };

const askers = new Set<(ask: OpenAsk) => boolean>();

/** The section's non-pin card listens (the rail for window A's list card and
 *  the bot's, the favourite's card for its own). True: it took the ask. */
export function onOpenAsk(fn: (ask: OpenAsk) => boolean): () => void {
  askers.add(fn);
  return () => {
    askers.delete(fn);
  };
}

/** Open `ask` in its section's non-pin window. False when nobody could (no
 *  rail on this page): the window is then simply closed. */
export function askOpen(ask: OpenAsk): boolean {
  for (const fn of [...askers]) if (fn(ask)) return true;
  return false;
}

const focusers: Record<RailWindow, (() => boolean) | null> = { a: null, b: null };

/** A pinned window's host registers how the keyboard lands in it. */
export function registerPinFocus(win: RailWindow, fn: () => boolean): () => void {
  focusers[win] = fn;
  return () => {
    if (focusers[win] === fn) focusers[win] = null;
  };
}

/** Put the keyboard in section `win`'s pinned window; true once it is there. */
export function focusPin(win: RailWindow): boolean {
  return focusers[win]?.() ?? false;
}

export function resetRailSlotsForTests(): void {
  doomed = null;
  titles.a = null;
  titles.b = null;
  askers.clear();
  focusers.a = null;
  focusers.b = null;
  notify();
}
