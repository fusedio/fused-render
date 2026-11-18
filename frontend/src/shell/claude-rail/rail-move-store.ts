// WHERE EACH RAIL WINDOW FLOATS, LIVE (rail-move-lib header; owner,
// 2026-11-17). One position per WINDOW, shared by every card of it — a swap
// inside window A (a ring, another ring, the Tasks door, the + card) keeps it,
// because every one of them reads it here. The stored half is the window's
// mark beside its pin (platform/lib last-chat `windowPosMark`); the live half
// is the drag on now, kept in memory and written to the mark once, on release.
//
// …and which window was pressed last, for the stacking (rail-move-lib
// `raised`).
import { useEffect, useRef, useSyncExternalStore } from "react";
import { setWindowPosMark, subscribeWindowPosMark, windowPosMark } from "@platform/lib/last-chat";
import { serialisePos, type Pos, type RailWindow, type Size } from "./rail-move-lib";
import type { Box } from "./rail-peek-lib";
import { railFreePinsNow, subscribeRailFreePins } from "./rail-free-pins-flag";
import { parsePinMark, pushedPos, serialisePinBox, type PinMark } from "./rail-slots-lib";

const live: Record<RailWindow, Pos | null> = { a: null, b: null };
let front: RailWindow | null = null;
const subs = new Set<() => void>();
function notify(): void {
  for (const fn of [...subs]) fn();
}

/** The parsed mark, kept per raw string so a re-read hands back the same
 *  object (`useSyncExternalStore` compares snapshots by identity). FREE PINS
 *  (rail-slots-lib): the mark carries the pin's frozen SIZE beside its
 *  position; a mark with none is a position alone, as it always was. */
const parsed: Record<RailWindow, { raw: string | null; mark: PinMark | null }> = {
  a: { raw: null, mark: null },
  b: { raw: null, mark: null },
};
function stored(win: RailWindow): PinMark | null {
  const raw = windowPosMark(win);
  const c = parsed[win];
  if (c.raw !== raw) parsed[win] = { raw, mark: parsePinMark(raw) };
  return parsed[win].mark;
}

/** A PIN PUSHED BY A NON-PIN (rail-slots-lib `pushedPos`; free pins only):
 *  the clicked-open non-pin windows standing now, by owner — published by
 *  each chassis (`usePusher`), read here, where every chassis reads the
 *  position it draws its pinned card at. The pin's own mark is never
 *  rewritten by a push: when the non-pin goes, the pin is drawn at home. */
const pushers = new Map<string, { win: RailWindow; box: Box }>();
let pushVersion = 0;
/** The last answer per window, so a re-read with nothing changed hands back
 *  the same object. */
const pushedCache: Record<RailWindow, { mark: PinMark; version: number; w: number; h: number; pos: Pos } | null> = { a: null, b: null };

function pushedFor(win: RailWindow, mark: PinMark): Pos {
  // The size the pin is drawn at: its frozen one, else its card's live box.
  const size: Size | null = mark.size ?? pinBox(win);
  if (!size) return mark.pos;
  const c = pushedCache[win];
  if (c && c.mark === mark && c.version === pushVersion && c.w === size.width && c.h === size.height) return c.pos;
  const pos = pushedPos(
    mark.pos,
    size,
    [...pushers.values()].map((p) => p.box),
  );
  pushedCache[win] = { mark, version: pushVersion, w: size.width, h: size.height, pos };
  return pos;
}

/** Where window `win` floats now — the drag on now, else the stored mark
 *  (drawn clear of a non-pin standing over it, free pins on) — or null while
 *  it is docked. */
export function windowPos(win: RailWindow): Pos | null {
  if (live[win]) return live[win];
  const mark = stored(win);
  if (!mark) return null;
  if (!railFreePinsNow() || pushers.size === 0) return mark.pos;
  return pushedFor(win, mark);
}

/** A FREE PIN'S FROZEN SIZE (rail-slots-lib "A PIN IS FROZEN WHERE IT
 *  STOOD"), or null: free pins off, the window docked, or a mark from before
 *  the switch — the chassis then draws the size it always drew. */
export function windowSize(win: RailWindow): Size | null {
  return railFreePinsNow() ? (stored(win)?.size ?? null) : null;
}

/** The drag's frame (useCardMove): live, not stored. */
export function setLiveWindowPos(win: RailWindow, pos: Pos | null): void {
  const was = live[win];
  if (was === pos || (was && pos && was.left === pos.left && was.top === pos.top)) return;
  live[win] = pos;
  notify();
}

/** Where the window now stands for good (a drag's release, a resize from a
 *  left or top grip), or docked (`null`: a double-click on the head). */
export function commitWindowPos(win: RailWindow, pos: Pos | null): void {
  live[win] = null;
  // A free pin's frozen size rides on in the mark through a move.
  const size = pos ? windowSize(win) : null;
  setWindowPosMark(win, pos && size ? serialisePinBox({ ...pos, ...size }) : serialisePos(pos));
  notify();
}

/** A FREE PIN'S BOX, WHOLE (rail-slots-lib): frozen at the pin's press
 *  (`freezeFromDock`), and rewritten by the reader's own resize. */
export function commitWindowBox(win: RailWindow, box: Box): void {
  live[win] = null;
  setWindowPosMark(win, serialisePinBox(box));
  notify();
}

/** THE PIN'S PRESS FREEZES THE WINDOW WHERE IT STANDS (rail-slots-lib): the
 *  section's non-pin card as it is drawn now (`dockBox`) becomes window
 *  `win`'s frozen box. Before the pin is written: the card that then floats
 *  reads it on its first placement. False with no card up to freeze — the
 *  caller's card then stands wherever the mark already said, or docked. */
/** HOW MANY TIMES WINDOW `win` HAS BEEN PINNED ANEW (`freezeFromDock`). A
 *  window's position is one mark, and the pin that is REPLACED reads the same
 *  mark as the one replacing it: for the frames it takes to go, it would
 *  follow the new pin's box — jump to the rail and fade out over the window
 *  that took its place (owner, 2026-11-18: "no flicker, jumping"). A pinned
 *  card remembers the count it was pinned at (useCardMove); once the count
 *  has moved on it is the replaced one, and stands where it stood. */
const pinEpochs: Record<RailWindow, number> = { a: 0, b: 0 };
export function pinEpoch(win: RailWindow): number {
  return pinEpochs[win];
}

export function freezeFromDock(win: RailWindow): boolean {
  const box = dockBox(win);
  if (!box) return false;
  pinEpochs[win] += 1;
  // THE CARD BEING PINNED STOPS PUSHING, NOW (owner, 2026-11-18: "when i click
  // pin the whole window moves"): it is this window's non-pin until its next
  // render says otherwise, and a pin frozen under its own old box would be
  // drawn pushed clear of it for those frames — a slide out and back. Its
  // window's pushers go with the freeze; a non-pin that still stands says so
  // again on its next placement.
  for (const [owner, p] of [...pushers]) if (p.win === win) pushers.delete(owner);
  pushVersion += 1;
  commitWindowBox(win, box);
  return true;
}

export function subscribeWindowPos(fn: () => void): () => void {
  subs.add(fn);
  const offA = subscribeWindowPosMark("a", fn);
  const offB = subscribeWindowPosMark("b", fn);
  return () => {
    subs.delete(fn);
    offA();
    offB();
  };
}

/** A press inside one of `win`'s cards: it is the one on top. */
export function noteFrontWindow(win: RailWindow): void {
  if (front === win) return;
  front = win;
  notify();
}

export function frontWindow(): RailWindow | null {
  return front;
}

export function useFrontWindow(): RailWindow | null {
  return useSyncExternalStore(subscribeWindowPos, frontWindow, frontWindow);
}

/** EACH WINDOW'S CARD AS IT STANDS NOW, docked or floating — what the other
 *  window's head drag snaps its edges to (rail-move-lib `snapEdges`; owner,
 *  2026-11-17: "within 12px its edges snap flush to the other window's
 *  edges"). Published by each chassis while its card is up, per OWNER (a
 *  unique id per mounted card: a card going never wipes the one coming).
 *  Read at the drag, never subscribed: nothing re-renders on it. Apart from
 *  the docked placement's stores (fav-peek-store `favBox`, card-snap), which
 *  a floating window must not feed. */
const boxes: Record<RailWindow, Map<string, { box: Box; pin: boolean }>> = { a: new Map(), b: new Map() };

/** `pin`: the card is the section's FREE PIN (rail-slots-lib) — with free
 *  pins on a window has two cards at once, its pin and its non-pin, and the
 *  two are told apart here. Off, no card says it. */
export function publishWindowBox(win: RailWindow, owner: string, box: Box | null, pin = false): void {
  const m = boxes[win];
  m.delete(owner);
  if (box) m.set(owner, { box, pin });
}

/** Window `win`'s card up now (the newest publisher, should two overlap for
 *  a fade), or null. Its PIN when it has one (free pins): the guides and the
 *  magnet are between the two pinned windows. */
export function windowBox(win: RailWindow): Box | null {
  return pinBox(win) ?? dockBox(win);
}

/** THE OTHER WINDOW'S CARD A MAGNET REACHES FOR (useCardMove's head drag,
 *  every chassis' grips): free pins off, the one card it has. On, a window
 *  has two — a FLOATING card (a pin) lines up with the other section's pin
 *  ("guides between the two pinned windows"), a docked one (a non-pin) with
 *  the other section's non-pin, the card it shares the rail's column with. */
export function magnetBox(other: RailWindow, floating: boolean): Box | null {
  if (!railFreePinsNow()) return windowBox(other);
  return floating ? pinBox(other) : dockBox(other);
}

/** Window `win`'s pinned card as it is drawn now (free pins), or null. */
function pinBox(win: RailWindow): Box | null {
  let last: Box | null = null;
  for (const b of boxes[win].values()) if (b.pin) last = b.box;
  return last;
}

/** Window `win`'s NON-PIN card as it is drawn now, or null. */
export function dockBox(win: RailWindow): Box | null {
  let last: Box | null = null;
  for (const b of boxes[win].values()) if (!b.pin) last = b.box;
  return last;
}

let boxSeq = 0;

/** A card's live box for the other window's drag (`publishWindowBox`):
 *  `box` while it is up, null otherwise; withdrawn on unmount. */
export function useWindowBox(win: RailWindow, box: Box | null, pin = false): void {
  const owner = useRef<string | null>(null);
  if (owner.current === null) owner.current = `box-${++boxSeq}`;
  const id = owner.current;
  const left = box?.left;
  const top = box?.top;
  const width = box?.width;
  const height = box?.height;
  useEffect(() => {
    publishWindowBox(
      win,
      id,
      left === undefined || top === undefined || width === undefined || height === undefined ? null : { left, top, width, height },
      pin,
    );
  }, [win, id, left, top, width, height, pin]);
  useEffect(() => () => publishWindowBox(win, id, null), [win, id]);
}

/** A clicked-open non-pin window says where it stands (`pushers`), or that it
 *  is gone (`null`). Readers of a pinned window's position hear a real change
 *  only. */
export function publishPusher(win: RailWindow, owner: string, box: Box | null): void {
  const was = pushers.get(owner)?.box ?? null;
  if (!box) {
    if (!was) return;
    pushers.delete(owner);
  } else {
    if (was && was.left === box.left && was.top === box.top && was.width === box.width && was.height === box.height) return;
    pushers.set(owner, { win, box });
  }
  pushVersion += 1;
  notify();
}

/** A NON-PIN CARD THAT PUSHES (rail-slots-lib): `box` while it is a
 *  clicked-open non-pin beside the rail, null otherwise — a hover's preview,
 *  a pinned card, a closed one, or free pins off; withdrawn on unmount. */
export function usePusher(win: RailWindow, box: Box | null): void {
  const owner = useRef<string | null>(null);
  if (owner.current === null) owner.current = `push-${++boxSeq}`;
  const id = owner.current;
  const left = box?.left;
  const top = box?.top;
  const width = box?.width;
  const height = box?.height;
  useEffect(() => {
    publishPusher(win, id, left === undefined || top === undefined || width === undefined || height === undefined ? null : { left, top, width, height });
  }, [win, id, left, top, width, height]);
  useEffect(() => () => publishPusher(win, id, null), [win, id]);
}

// The switch going on or off re-reads every position: a pin is pushed, and
// its frozen size read, only with free pins on.
subscribeRailFreePins(notify);

/** Test seam. */
export function resetRailMoveForTests(): void {
  live.a = null;
  live.b = null;
  front = null;
  boxes.a.clear();
  boxes.b.clear();
  pushers.clear();
  pushVersion += 1;
  pushedCache.a = null;
  pushedCache.b = null;
  setWindowPosMark("a", null);
  setWindowPosMark("b", null);
  notify();
}
