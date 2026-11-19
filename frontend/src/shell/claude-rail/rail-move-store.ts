// WHERE EACH RAIL WINDOW FLOATS, LIVE (rail-move-lib header; owner,
// 2026-11-17). One position per WINDOW, shared by every card of it — a swap
// inside window A (a ring, another ring, the Tasks door, the + card) keeps it,
// because every one of them reads it here. The stored half is the window's
// mark beside its pin (platform/lib last-chat `windowPosMark`); the live half
// is the drag on now, kept in memory and written to the mark once, on release.
//
// …and which window was pressed last, for the stacking (rail-move-lib
// `raised`).
import { useEffect, useLayoutEffect, useRef, useSyncExternalStore } from "react";
import { setWindowPosMark, subscribeWindowPosMark, windowPosMark } from "@platform/lib/last-chat";
import { clampPos, serialisePos, viewportOf, type Pos, type RailWindow, type Size } from "./rail-move-lib";
import type { Box } from "./rail-peek-lib";
import { railFreePinsNow, subscribeRailFreePins } from "./rail-free-pins-flag";
import { parsePinMark, pushedPins, pushedPos, serialisePinBox, type PinAt, type PinMark, type PushedPos } from "./rail-slots-lib";

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
/** The last answer, so a re-read with nothing changed hands back the same
 *  objects. BOTH pins are solved together (rail-slots-lib `pushedPins`): a pin
 *  the push moved is in the other's way too. */
let solved: { key: string; marks: [PinMark | null, PinMark | null]; out: Record<RailWindow, PushedPos | null> } | null = null;

/** A pin as the solver reads it: its home, and the size it is drawn at — its
 *  frozen one, else its card's live box. None known: it cannot be told what
 *  it lies under, and is not pushed. */
function pinAt(win: RailWindow): PinAt | null {
  const mark = stored(win);
  if (!mark) return null;
  const size: Size | null = mark.size ?? pinBox(win);
  if (!size) return null;
  // WHERE IT IS DRAWN, not where its mark says: a window narrower than the
  // spot the pin was left at draws it squeezed inside (rail-move-lib
  // `floatPlacement`), and it is THAT box a non-pin covers.
  const view = viewportOf(typeof window === "undefined" ? undefined : window);
  const width = Math.min(size.width, view.width);
  const height = Math.min(size.height, view.height);
  const at = clampPos(mark.pos, { width, height }, view);
  const pos = at.left === mark.pos.left && at.top === mark.pos.top ? mark.pos : at;
  return { pos, size: { width, height } };
}

function pushedFor(win: RailWindow, mark: PinMark): PushedPos {
  const a = pinAt("a");
  const b = pinAt("b");
  const sig = (p: PinAt | null) => (p ? `${p.pos.left},${p.pos.top},${p.size.width}x${p.size.height}` : "-");
  const key = `${pushVersion}|${sig(a)}|${sig(b)}`;
  const ma = stored("a");
  const mb = stored("b");
  if (!solved || solved.key !== key || solved.marks[0] !== ma || solved.marks[1] !== mb) {
    solved = {
      key,
      marks: [ma, mb],
      out: pushedPins(
        { a, b },
        [...pushers.values()].map((p) => p.box),
      ),
    };
  }
  return solved.out[win] ?? mark.pos;
}

/** Where window `win` floats now — the drag on now, else the stored mark
 *  (drawn clear of a non-pin standing over it, free pins on) — or null while
 *  it is docked. */
export function windowPos(win: RailWindow): PushedPos | null {
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
  const size = frozenSize(win);
  // A PIN PUSHED UP AND CUT SHORT (rail-slots-lib `pushedPos` "UP FIRST") is
  // drawn at the cut height; its mark keeps its own. Not under its own drag.
  if (!size || live[win] || pushers.size === 0) return size;
  const mark = stored(win);
  const cut = mark ? pushedFor(win, mark).height : undefined;
  if (cut === undefined || cut === size.height) return size;
  if (cutSize[win]?.width !== size.width || cutSize[win]?.height !== cut) cutSize[win] = { width: size.width, height: cut };
  return cutSize[win];
}
/** The size a pin's mark holds — what a move or a freeze writes back. */
function frozenSize(win: RailWindow): Size | null {
  return railFreePinsNow() ? (stored(win)?.size ?? null) : null;
}
const cutSize: Record<RailWindow, Size | null> = { a: null, b: null };

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
  const size = pos ? frozenSize(win) : null;
  setWindowPosMark(win, pos && size ? serialisePinBox({ ...pos, ...size }) : serialisePos(pos));
  notify();
}

/** A PIN'S MARK WITH A POSITION AND NO SIZE gets one (use-pin-box): a window
 *  pinned and dragged before the switch came on kept only where it stood, and
 *  drew at the shared non-pin size — which a non-pin's resize then changed
 *  under it. The position stays; a mark that has a size is left alone. */
export function sizeWindowMark(win: RailWindow, size: Size): void {
  const mark = stored(win);
  if (!mark || mark.size) return;
  setWindowPosMark(win, serialisePinBox({ ...mark.pos, ...size }));
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
  // PINNING FREEZES THE WHOLE PICTURE (rail-slots-lib): the other window's
  // pin, if this card had pushed it aside, stays where it is drawn — its
  // pushed position is its home from here on. Left alone it would slide back
  // the moment this card stopped pushing, under the pin this card becomes.
  const other: RailWindow = win === "a" ? "b" : "a";
  const pushed = pinAt(other);
  if (pushed) {
    const mine = [...pushers.values()].filter((p) => p.win === win).map((p) => p.box);
    const drawn = pushedPos(pushed.pos, pushed.size, mine);
    if (drawn !== pushed.pos) {
      // Its cut height too: the picture stays as it is drawn.
      const kept = stored(other)?.size ?? null;
      const at = { left: drawn.left, top: drawn.top };
      setWindowPosMark(other, kept ? serialisePinBox({ ...at, width: kept.width, height: drawn.height ?? kept.height }) : serialisePos(at));
    }
  }
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
  const was = m.get(owner);
  m.delete(owner);
  if (box) m.set(owner, { box, pin });
  // A PIN WITH NO SIZE IN ITS MARK is pushed by this box's size (`pinAt`): a
  // size that arrives — or changes — after a non-pin already stands over it
  // changes where the pin is drawn, and its readers must hear of it
  // (CodeRabbit on #2349: it stayed covered until something else moved).
  // The same publication again says nothing.
  if (!railFreePinsNow() || pushers.size === 0) return;
  const size = (e: { box: Box; pin: boolean } | undefined) => (e && e.pin ? `${e.box.width}x${e.box.height}` : "");
  if (size(was) === size(box ? { box, pin } : undefined)) return;
  const mark = stored(win);
  if (mark && !mark.size) notify();
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
  // In the layout phase: a pin this card pushes re-renders before the paint
  // that shows this card's new box, not a frame after it.
  useLayoutEffect(() => {
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
  solved = null;
  setWindowPosMark("a", null);
  setWindowPosMark("b", null);
  notify();
}
