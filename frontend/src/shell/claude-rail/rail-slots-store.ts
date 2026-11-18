// FREE PINS, LIVE (rail-slots-lib.ts header; owner, 2026-11-18): the few
// things the four windows say to each other that are not geometry
// (rail-move-store has that) and not a window's mark (platform/lib last-chat
// has those):
//
//   - WHICH PINNED WINDOW A PIN ICON'S HOVER MARKS (`doomed`): the pin icon of
//     a non-pin card, hovered or focused, wears its section's pinned window a
//     red border — the one its press would close;
//   - WHAT EACH PINNED WINDOW IS CALLED (`pinTitle`), for that icon's caption;
//   - WHICH OF A CHAT WINDOW'S TWO SLOTS IS THE NON-PIN (`freeSlot`): the pin
//     and the unpin swap the slots' roles, so the card on screen never
//     remounts;
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

/** THE TWO SLOTS OF A CHAT WINDOW (owner, 2026-11-18: "when i click pin the
 *  whole window moves … it should be smooth, no flicker, jumping"). A task's
 *  or a bot's window is drawn by one of TWO mounted cards of the same kind —
 *  slots `x` and `y` — and what tells them apart is their ROLE: one is the
 *  section's non-pin (the FREE slot: the rail's hovers and clicks drive it),
 *  the other its pin. The pin's press does not hand the chat to another
 *  card: the two slots SWAP ROLES. The card the reader is looking at stays
 *  mounted, where it is, with its chat, its scroll and its words; only its
 *  role changes, and the other slot — which showed the pin this one
 *  replaces, or nothing — becomes the free one. An unpin swaps them back.
 *  One pair per kind of card: the list's chat card (`a`), the favourite's
 *  (`fav`) and the bot's (`bot`). */
export type Slot = "x" | "y";
export type SlotGroup = "a" | "fav" | "bot";
export const SLOTS: readonly Slot[] = ["x", "y"];
export function otherSlot(slot: Slot): Slot {
  return slot === "x" ? "y" : "x";
}

const free: Record<SlotGroup, Slot> = { a: "x", fav: "x", bot: "x" };

/** The slot that is the group's non-pin now. */
export function freeSlot(group: SlotGroup): Slot {
  return free[group];
}
export function setFreeSlot(group: SlotGroup, slot: Slot): void {
  if (free[group] === slot) return;
  free[group] = slot;
  notify();
}
export function useFreeSlot(group: SlotGroup): Slot {
  return useSyncExternalStore(
    subscribe,
    () => free[group],
    () => "x" as Slot,
  );
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
  free.a = "x";
  free.fav = "x";
  free.bot = "x";
  focusers.a = null;
  focusers.b = null;
  notify();
}
