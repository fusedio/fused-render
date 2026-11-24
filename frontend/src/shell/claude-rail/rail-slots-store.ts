// FREE PINS, LIVE (rail-slots-lib.ts header; owner, 2026-11-18, many pins
// 2026-11-24): the few things the windows say to each other that are not
// geometry (rail-move-store has that) and not a pin's mark (platform/lib
// last-chat has those):
//
//   - WHICH CARD OF A CHAT WINDOW IS WHICH (`useSlots`): the free slot — the
//     section's non-pin — and one slot per pin. A pin's press hands the free
//     slot's card the pin's role and makes a fresh free slot, so the card on
//     screen never remounts;
//   - THE KEYBOARD INTO A PINNED WINDOW (`focusPin`): a ring's click on a
//     pinned task raises its window and lands in its composer.
//
// (Until 2026-11-24 a section had one pin, and this store also said which
// pinned window a non-pin's pin icon would replace — the red border — and
// what that window was called, for the icon's caption. With no cap a pin
// replaces nothing, and both went.)
import { useSyncExternalStore } from "react";

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

/** THE SLOTS OF A CHAT WINDOW (owner, 2026-11-18: "when i click pin the whole
 *  window moves … it should be smooth, no flicker, jumping"; many pins,
 *  2026-11-24). A task's or a bot's window is drawn by a LIST of mounted cards
 *  of the same kind, each a slot, and what tells them apart is their ROLE:
 *  exactly one is the section's non-pin (the FREE slot, `pin` null: the
 *  rail's hovers and clicks drive it); every other one shows one pinned thing
 *  (`pin`: a task's key, a bot's id). The pin's press does not hand the chat
 *  to another card: the free slot TAKES THE PIN (`pinSlot`) — the card the
 *  reader is looking at stays mounted, where it is, with its chat, its scroll
 *  and its words; only its role changes — and a fresh free slot is made. A
 *  pin let go is CLOSING for as long as its card takes to fade out where it
 *  stands, and is then dropped (`syncSlots`).
 *  React keys are the slots' ids, so nothing remounts on a role change. One
 *  list per kind of card: the list's chat card (`a`), the favourite's (`fav`)
 *  and the bot's (`bot`). The first free slot is "x". */
export type SlotGroup = "a" | "fav" | "bot";
export interface Slot {
  /** The card's identity: its React key, and its pinned DOM id's suffix. */
  id: string;
  /** What it shows pinned, or null: the free slot. */
  pin: string | null;
  /** Its pin was let go: it fades out where it stands, and goes. */
  closing: boolean;
}

/** How long a slot whose pin went stays mounted to fade (var(--dur-med) and
 *  a frame or two). */
export const SLOT_FADE_MS = 320;

const FIRST: readonly Slot[] = Object.freeze([{ id: "x", pin: null, closing: false }]);
const groups: Record<SlotGroup, readonly Slot[]> = { a: FIRST, fav: FIRST, bot: FIRST };
let seq = 0;
const nextId = () => `s${++seq}`;
const timers = new Map<string, ReturnType<typeof setTimeout>>();

function write(group: SlotGroup, next: readonly Slot[]): void {
  groups[group] = next;
  notify();
}

/** The group's slots now, in their order. */
export function slotsNow(group: SlotGroup): readonly Slot[] {
  return groups[group];
}
export function useSlots(group: SlotGroup): readonly Slot[] {
  return useSyncExternalStore(
    subscribe,
    () => groups[group],
    () => FIRST,
  );
}

/** The id of the slot that is the group's non-pin now. */
export function freeSlot(group: SlotGroup): string {
  return groups[group].find((s) => s.pin === null)?.id ?? "x";
}

/** THE PIN'S PRESS: slot `id` — the free one — shows `pin` from now on, and
 *  a fresh free slot is made after it. A slot that is not free, or a pin some
 *  other open slot shows already, changes nothing. */
export function pinSlot(group: SlotGroup, id: string, pin: string): void {
  const list = groups[group];
  const at = list.findIndex((s) => s.id === id);
  if (at < 0 || list[at]!.pin !== null) return;
  if (list.some((s) => s.pin === pin && !s.closing)) return;
  const next = list.map((s, i) => (i === at ? { id: s.id, pin, closing: false } : s));
  next.push({ id: nextId(), pin: null, closing: false });
  write(group, next);
}

/** THE SLOTS FOLLOW THE MARKS (`pins`: what the group's marks pin now, in
 *  their order): a pin with no open slot gets one (a reload, a pin made in
 *  another document of the window, the switch coming on) — a slot fading out
 *  on that very pin comes back rather than a second one; a slot whose pin is
 *  gone fades out (`closing`) and is dropped SLOT_FADE_MS later. The free
 *  slot is never touched. */
export function syncSlots(group: SlotGroup, pins: readonly string[]): void {
  const list = groups[group];
  let changed = false;
  const next: Slot[] = [];
  for (const s of list) {
    if (s.pin === null) {
      next.push(s);
      continue;
    }
    const wanted = pins.includes(s.pin);
    if (wanted && s.closing && !list.some((o) => o !== s && o.pin === s.pin && !o.closing) && !next.some((o) => o.pin === s.pin && !o.closing)) {
      // The pin came back while its card faded: it stays.
      next.push({ ...s, closing: false });
      clearTimer(s.id);
      changed = true;
    } else if (!wanted && !s.closing) {
      next.push({ ...s, closing: true });
      dropLater(group, s.id);
      changed = true;
    } else next.push(s);
  }
  for (const pin of pins) {
    if (next.some((s) => s.pin === pin && !s.closing)) continue;
    // New slots go before the free one: its card is the last in the list.
    const free = next.findIndex((s) => s.pin === null);
    const slot: Slot = { id: nextId(), pin, closing: false };
    if (free < 0) next.push(slot);
    else next.splice(free, 0, slot);
    changed = true;
  }
  if (!next.some((s) => s.pin === null)) {
    next.push({ id: nextId(), pin: null, closing: false });
    changed = true;
  }
  if (changed) write(group, next);
}

function clearTimer(id: string): void {
  const t = timers.get(id);
  if (t !== undefined) clearTimeout(t);
  timers.delete(id);
}

function dropLater(group: SlotGroup, id: string): void {
  clearTimer(id);
  const drop = () => {
    timers.delete(id);
    const list = groups[group];
    const s = list.find((x) => x.id === id);
    if (!s || !s.closing) return;
    write(
      group,
      list.filter((x) => x.id !== id),
    );
  };
  if (typeof setTimeout === "undefined") {
    drop();
    return;
  }
  timers.set(id, setTimeout(drop, SLOT_FADE_MS));
}

const focusers = new Map<string, () => boolean>();

/** A pinned window's host registers how the keyboard lands in it, by the
 *  pin's id (platform/lib last-chat: `task:<key>`, `fav:<key>`, `bot:<id>`). */
export function registerPinFocus(pinId: string, fn: () => boolean): () => void {
  focusers.set(pinId, fn);
  return () => {
    if (focusers.get(pinId) === fn) focusers.delete(pinId);
  };
}

/** Put the keyboard in pin `pinId`'s window; true once it is there. */
export function focusPin(pinId: string): boolean {
  return focusers.get(pinId)?.() ?? false;
}

export function resetRailSlotsForTests(): void {
  for (const t of timers.values()) clearTimeout(t);
  timers.clear();
  groups.a = FIRST;
  groups.fav = FIRST;
  groups.bot = FIRST;
  seq = 0;
  focusers.clear();
  notify();
}
