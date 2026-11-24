// WHERE EACH RAIL WINDOW FLOATS, LIVE (rail-move-lib header; owner,
// 2026-11-17). With free pins off: one position per WINDOW ("a" | "b"),
// shared by every card of it — a swap inside window A (a ring, another ring,
// the Tasks door, the + card) keeps it, because every one of them reads it
// here; its stored half is the window's mark beside its pin (platform/lib
// last-chat `windowPosMark`).
//
// FREE PINS (rail-slots-lib; owner, 2026-11-24: "we should allow multiple
// pins"): everything a PIN has is its own, kept under the pin's id (last-chat
// `favPinId`, `botPinId`, a section A word) — its stored box (last-chat
// `pinPosRaw`: position AND frozen size), the drag on it now, where a non-pin
// pushes it to, its card's published box and its place in the stacking.
// What is the SECTION's stays keyed by "a" | "b": its non-pin's box and the
// non-pins that push. A MOVE KEY is either: a window, or a pin's id (none is
// "a" or "b").
//
// The live half is the drag on now, kept in memory and written to the mark
// once, on release.
//
// …and which window was pressed last, for the stacking (rail-move-lib
// `raised`); with free pins also the PINS' order, last pressed on top
// (`pinOrder`, `pinZ`).
import { useEffect, useLayoutEffect, useRef, useSyncExternalStore } from "react";
import {
  pinIdSection,
  pinPosIds,
  pinPosRaw,
  setPinPosRaw,
  setWindowPosMark,
  subscribePinPos,
  subscribeWindowPosMark,
  windowPosMark,
} from "@platform/lib/last-chat";
import { clampPos, serialisePos, viewportOf, type Pos, type RailWindow, type Size } from "./rail-move-lib";
import type { Box } from "./rail-peek-lib";
import { railExtrasNow, subscribeRailExtras } from "./rail-extras-flag";
import { parsePinMark, pushedPins, serialisePinBox, type PinAt, type PinMark, type PushedPos } from "./rail-slots-lib";

/** A window ("a" | "b"), or a free pin's id (header). */
export type MoveKey = string;

const isWin = (key: MoveKey): key is RailWindow => key === "a" || key === "b";

/** The section a move key is of. */
export function sectionOf(key: MoveKey): RailWindow {
  return isWin(key) ? key : pinIdSection(key);
}

const live = new Map<MoveKey, Pos>();
let front: RailWindow | null = null;
const subs = new Set<() => void>();
function notify(): void {
  for (const fn of [...subs]) fn();
}

function rawOf(key: MoveKey): string | null {
  return isWin(key) ? windowPosMark(key) : pinPosRaw(key);
}
function writeRaw(key: MoveKey, raw: string | null): void {
  if (isWin(key)) setWindowPosMark(key, raw);
  else setPinPosRaw(key, raw);
}

/** The parsed mark, kept per raw string so a re-read hands back the same
 *  object (`useSyncExternalStore` compares snapshots by identity). FREE PINS
 *  (rail-slots-lib): the mark carries the pin's frozen SIZE beside its
 *  position; a mark with none is a position alone, as it always was. */
const parsed = new Map<MoveKey, { raw: string | null; mark: PinMark | null }>();
function stored(key: MoveKey): PinMark | null {
  const raw = rawOf(key);
  const c = parsed.get(key);
  if (c && c.raw === raw) return c.mark;
  const mark = parsePinMark(raw);
  parsed.set(key, { raw, mark });
  return mark;
}

/** A PIN PUSHED BY A NON-PIN (rail-slots-lib `pushedPos`; free pins only):
 *  the clicked-open non-pin windows standing now, by owner — published by
 *  each chassis (`usePusher`), read here, where every chassis reads the
 *  position it draws its pinned card at. A pin's own mark is never
 *  rewritten by a push: when the non-pin goes, the pin is drawn at home. */
const pushers = new Map<string, { win: RailWindow; box: Box }>();
let pushVersion = 0;
/** The last answer, so a re-read with nothing changed hands back the same
 *  objects. EVERY pin is solved together (rail-slots-lib `pushedPins`): a pin
 *  the push moved is in the others' way too. */
let solved: { key: string; marks: (PinMark | null)[]; out: Record<string, PushedPos | null> } | null = null;

/** A pin as the solver reads it: its home, and the size it is drawn at — its
 *  frozen one, else its card's live box. None known: it cannot be told what
 *  it lies under, and is not pushed. */
function pinAt(key: MoveKey): PinAt | null {
  const mark = stored(key);
  if (!mark) return null;
  const size: Size | null = mark.size ?? pinBox(key);
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

/** The pins the solver weighs: every free pin with a stored box. */
function pinKeys(): MoveKey[] {
  return pinPosIds();
}

function pushedFor(key: MoveKey, mark: PinMark): PushedPos {
  const keys = pinKeys();
  const ats = keys.map((k) => pinAt(k));
  const sig = (p: PinAt | null) => (p ? `${p.pos.left},${p.pos.top},${p.size.width}x${p.size.height}` : "-");
  const sigKey = `${pushVersion}|${keys.map((k, i) => `${k}=${sig(ats[i]!)}`).join("|")}`;
  const marks = keys.map((k) => stored(k));
  if (!solved || solved.key !== sigKey || solved.marks.length !== marks.length || solved.marks.some((m, i) => m !== marks[i])) {
    const pins: Record<string, PinAt | null> = {};
    keys.forEach((k, i) => {
      pins[k] = ats[i]!;
    });
    solved = {
      key: sigKey,
      marks,
      out: pushedPins(
        pins,
        [...pushers.values()].map((p) => p.box),
      ),
    };
  }
  return solved.out[key] ?? mark.pos;
}

/** Where `key` floats now — the drag on now, else the stored mark (a pin
 *  drawn clear of a non-pin standing over it, free pins on) — or null while
 *  it is docked (a window) or has no box (a pin). */
export function windowPos(key: MoveKey): PushedPos | null {
  const drag = live.get(key);
  if (drag) return drag;
  const mark = stored(key);
  if (!mark) return null;
  if (!railExtrasNow() || pushers.size === 0 || isWin(key)) return mark.pos;
  return pushedFor(key, mark);
}

/** A FREE PIN'S FROZEN SIZE (rail-slots-lib "A PIN IS FROZEN WHERE IT
 *  STOOD"), or null: free pins off, no box, or a mark from before the switch
 *  — the chassis then draws the size it always drew. */
export function windowSize(key: MoveKey): Size | null {
  const size = frozenSize(key);
  // A PIN PUSHED UP AND CUT SHORT (rail-slots-lib `pushedPos` "UP FIRST") is
  // drawn at the cut height; its mark keeps its own. Not under its own drag.
  if (!size || live.has(key) || pushers.size === 0 || isWin(key)) return size;
  const mark = stored(key);
  const cut = mark ? pushedFor(key, mark).height : undefined;
  if (cut === undefined || cut === size.height) return size;
  const was = cutSize.get(key);
  if (was?.width === size.width && was.height === cut) return was;
  const next = { width: size.width, height: cut };
  cutSize.set(key, next);
  return next;
}
/** The size a pin's mark holds — what a move or a freeze writes back. */
function frozenSize(key: MoveKey): Size | null {
  return railExtrasNow() ? (stored(key)?.size ?? null) : null;
}
const cutSize = new Map<MoveKey, Size>();

/** The drag's frame (useCardMove): live, not stored. */
export function setLiveWindowPos(key: MoveKey, pos: Pos | null): void {
  const was = live.get(key) ?? null;
  if (was === pos || (was && pos && was.left === pos.left && was.top === pos.top)) return;
  if (pos) live.set(key, pos);
  else live.delete(key);
  notify();
}

/** Where `key` now stands for good (a drag's release, a resize from a left
 *  or top grip), or docked (`null`: a double-click on the head). */
export function commitWindowPos(key: MoveKey, pos: Pos | null): void {
  live.delete(key);
  // A free pin's frozen size rides on in the mark through a move.
  const size = pos ? frozenSize(key) : null;
  writeRaw(key, pos && size ? serialisePinBox({ ...pos, ...size }) : serialisePos(pos));
  notify();
}

/** A PIN'S MARK WITH A POSITION AND NO SIZE gets one (use-pin-box): a window
 *  pinned and dragged before the switch came on kept only where it stood, and
 *  drew at the shared non-pin size — which a non-pin's resize then changed
 *  under it. The position stays; a mark that has a size is left alone. */
export function sizeWindowMark(key: MoveKey, size: Size): void {
  const mark = stored(key);
  if (!mark || mark.size) return;
  writeRaw(key, serialisePinBox({ ...mark.pos, ...size }));
  notify();
}

/** A FREE PIN'S BOX, WHOLE (rail-slots-lib): frozen at the pin's press
 *  (`freezeFromDock`), and rewritten by the reader's own resize. */
export function commitWindowBox(key: MoveKey, box: Box): void {
  live.delete(key);
  writeRaw(key, serialisePinBox(box));
  notify();
}

/** THE PIN'S PRESS FREEZES THE CARD WHERE IT STANDS (rail-slots-lib): section
 *  `win`'s non-pin card as it is drawn now (`dockBox`) becomes pin `pinId`'s
 *  frozen box, and the pin goes on top of the others. Before the pin is
 *  written: the card that then floats reads it on its first placement. False
 *  with no card up to freeze — the caller's card then stands wherever the
 *  mark already said, or where use-pin-box puts a pin with no box.
 *  (Until 2026-11-24 a window had ONE position, shared by a pin and the one
 *  replacing it, and a count told the replaced card it was on its way out.
 *  Each pin's box is its own now, so nothing needs telling.) */
export function freezeFromDock(win: RailWindow, pinId: MoveKey): boolean {
  const box = dockBox(win);
  if (!box) return false;
  // PINNING FREEZES THE WHOLE PICTURE (rail-slots-lib): every pin this card
  // had pushed aside stays where it is drawn — its pushed position is its
  // home from here on. Left alone it would slide back the moment this card
  // stopped pushing, under the pin this card becomes.
  const mine = [...pushers.values()].filter((p) => p.win === win).map((p) => p.box);
  if (mine.length > 0) {
    const writes: [MoveKey, string | null][] = [];
    // WHICH PINS ARE THIS CARD'S DOING: the ones its own docks move — itself,
    // or through a pin it moved onto them (owner, 2026-11-24, many pins) —
    // with nothing else pushing. A pin pushed by the other section's non-pin
    // alone is not this freeze's, and goes home when that one closes.
    const keys = pinKeys();
    const ats: Record<string, PinAt | null> = {};
    for (const k of keys) ats[k] = pinAt(k);
    const alone = pushedPins(ats, mine);
    for (const other of keys) {
      if (other === pinId) continue;
      const mark = stored(other);
      const home = ats[other];
      if (!mark || !home || (alone[other] ?? home.pos) === home.pos) continue;
      // What is frozen is what is DRAWN — the joint answer, every non-pin and
      // every pin counted (`pushedFor`): this card's docks alone could give an
      // up/cut box the reader never saw (a step up the other non-pin blocks).
      const drawn = pushedFor(other, mark);
      if (drawn === mark.pos) continue;
      // Its cut height too: the picture stays as it is drawn.
      const kept = mark.size ?? null;
      const at = { left: drawn.left, top: drawn.top };
      writes.push([other, kept ? serialisePinBox({ ...at, width: kept.width, height: drawn.height ?? kept.height }) : serialisePos(at)]);
    }
    // Read every answer first, then write: one pin's new home must not move
    // the answer for the next.
    for (const [k, raw] of writes) writeRaw(k, raw);
  }
  // THE CARD BEING PINNED STOPS PUSHING, NOW (owner, 2026-11-18: "when i click
  // pin the whole window moves"): it is this section's non-pin until its next
  // render says otherwise, and a pin frozen under its own old box would be
  // drawn pushed clear of it for those frames — a slide out and back. Its
  // section's pushers go with the freeze; a non-pin that still stands says so
  // again on its next placement.
  for (const [owner, p] of [...pushers]) if (p.win === win) pushers.delete(owner);
  pushVersion += 1;
  raisePin(pinId);
  commitWindowBox(pinId, box);
  return true;
}

/** FREEZE FROM THE WINDOW'S OWN MARK (the switch turning on over a card held
 *  the old way): the box the window floated at becomes the pin's. */
export function adoptWindowBox(win: RailWindow, pinId: MoveKey): boolean {
  const raw = windowPosMark(win);
  if (raw === null || rawOf(pinId) !== null) return false;
  writeRaw(pinId, raw);
  notify();
  return true;
}

export function subscribeWindowPos(fn: () => void): () => void {
  subs.add(fn);
  const offA = subscribeWindowPosMark("a", fn);
  const offB = subscribeWindowPosMark("b", fn);
  const offPins = subscribePinPos(fn);
  return () => {
    subs.delete(fn);
    offA();
    offB();
    offPins();
  };
}

/** THE PINS' STACKING (rail-slots-lib "LAST PRESSED ON TOP"; owner,
 *  2026-11-24): every pin pressed, oldest first — the last is on top, the
 *  others keep their order among themselves. */
let pinOrder: MoveKey[] = [];
function raisePin(pinId: MoveKey): void {
  if (pinOrder[pinOrder.length - 1] === pinId) return;
  pinOrder = [...pinOrder.filter((k) => k !== pinId), pinId];
  notify();
}

/** A press inside one of `win`'s cards: it is the one on top — and, when the
 *  card is a free pin (`pinId`), that pin over every other pin. */
export function noteFrontWindow(win: RailWindow, pinId: MoveKey | null = null): void {
  if (pinId !== null && railExtrasNow() && !isWin(pinId)) raisePin(pinId);
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

/** THE PINS' z-index band (claude-rail.css "FREE PINS"): the top pin at
 *  PIN_Z_TOP, the next one a step under it, and so on down to PIN_Z_FLOOR —
 *  under the hover's bridge (44) and the non-pins (45–48), over the page. */
export const PIN_Z_TOP = 43;
export const PIN_Z_FLOOR = 31;

/** Where pin `pinId` stands in the stacking: its z-index. Only the pins that
 *  have a box count; one never pressed is under every pressed one. */
export function pinZ(pinId: MoveKey): number {
  const pins = new Set(pinKeys());
  const order = pinOrder.filter((k) => pins.has(k));
  for (const k of pins) if (!order.includes(k)) order.unshift(k);
  const at = order.indexOf(pinId);
  const rank = at < 0 ? order.length : order.length - 1 - at;
  return Math.max(PIN_Z_FLOOR, PIN_Z_TOP - rank);
}

/** Is any of section `win`'s pins floating (free pins), or — off — window
 *  `win` itself? */
export function sectionFloats(win: RailWindow): boolean {
  if (!railExtrasNow()) return windowPos(win) !== null;
  return pinKeys().some((k) => pinIdSection(k) === win);
}

/** EACH CARD AS IT STANDS NOW, docked or floating — what another card's drag
 *  snaps its edges to (rail-move-lib `snapEdges`; owner, 2026-11-17: "within
 *  12px its edges snap flush to the other window's edges"). Published by
 *  each chassis while its card is up, per OWNER (a unique id per mounted
 *  card: a card going never wipes the one coming). Read at the drag, never
 *  subscribed: nothing re-renders on it. Apart from the docked placement's
 *  stores (fav-peek-store `favBox`, card-snap), which a floating window must
 *  not feed. */
const boxes = new Map<string, { win: RailWindow; box: Box; pin: MoveKey | null }>();

/** `pin`: the card is a FREE PIN (rail-slots-lib), by its id — with free pins
 *  on a section has its non-pin and any number of pins at once, and they are
 *  told apart here. Off, no card says it. */
export function publishWindowBox(win: RailWindow, owner: string, box: Box | null, pin: MoveKey | null = null): void {
  const was = boxes.get(owner);
  boxes.delete(owner);
  if (box) boxes.set(owner, { win, box, pin });
  // A PIN WITH NO SIZE IN ITS MARK is pushed by this box's size (`pinAt`): a
  // size that arrives — or changes — after a non-pin already stands over it
  // changes where the pin is drawn, and its readers must hear of it
  // (CodeRabbit on #2349: it stayed covered until something else moved).
  // The same publication again says nothing.
  if (!railExtrasNow() || pushers.size === 0) return;
  const size = (e: { box: Box; pin: MoveKey | null } | undefined) => (e && e.pin ? `${e.pin}:${e.box.width}x${e.box.height}` : "");
  if (size(was) === size(box ? { box, pin } : undefined)) return;
  const key = pin ?? was?.pin ?? null;
  const mark = key ? stored(key) : null;
  if (mark && !mark.size) notify();
}

/** Window `win`'s card up now (the newest publisher, should two overlap for
 *  a fade), or null — free pins off, where a window has the one card. A pin
 *  before a non-pin, as ever. */
export function windowBox(win: RailWindow): Box | null {
  let pin: Box | null = null;
  let dock: Box | null = null;
  for (const b of boxes.values()) {
    if (b.win !== win) continue;
    if (b.pin !== null) pin = b.box;
    else dock = b.box;
  }
  return pin ?? dock;
}

/** The distance between two boxes: 0 when they touch or overlap. */
function gapBetween(a: Box, b: Box): number {
  const dx = Math.max(0, b.left - (a.left + a.width), a.left - (b.left + b.width));
  const dy = Math.max(0, b.top - (a.top + a.height), a.top - (b.top + b.height));
  return Math.hypot(dx, dy);
}

/** THE CARD A MAGNET REACHES FOR (useCardMove's head drag, every chassis'
 *  grips): free pins off, the other window's one card. On, a FLOATING card (a
 *  pin) lines up with the NEAREST other pin to it, any section ("guides
 *  between the pinned windows"; owner, 2026-11-24, many pins) — `near`: the
 *  card's own key and its box now; a docked one (a non-pin) with the other
 *  section's non-pin, the card it shares the rail's column with. */
export function magnetBox(other: RailWindow, floating: boolean, near?: { self: MoveKey | null; box: Box | null }): Box | null {
  if (!railExtrasNow()) return windowBox(other);
  if (!floating) return dockBox(other);
  const self = near?.self ?? null;
  const mine = near?.box ?? null;
  let best: Box | null = null;
  let bestGap = Infinity;
  for (const b of boxes.values()) {
    if (b.pin === null || b.pin === self) continue;
    if (!mine) {
      // No box to measure from: the other section's newest pin, as before.
      if (b.win === other) best = b.box;
      continue;
    }
    const g = gapBetween(mine, b.box);
    if (g <= bestGap) {
      bestGap = g;
      best = b.box;
    }
  }
  return best;
}

/** Pin `pinId`'s card as it is drawn now (free pins), or null. */
function pinBox(pinId: MoveKey): Box | null {
  let last: Box | null = null;
  for (const b of boxes.values()) if (b.pin === pinId) last = b.box;
  return last;
}

/** Section `win`'s NON-PIN card as it is drawn now, or null. */
export function dockBox(win: RailWindow): Box | null {
  let last: Box | null = null;
  for (const b of boxes.values()) if (b.win === win && b.pin === null) last = b.box;
  return last;
}

let boxSeq = 0;

/** A card's live box for another card's drag (`publishWindowBox`): `box`
 *  while it is up, null otherwise; withdrawn on unmount. */
export function useWindowBox(win: RailWindow, box: Box | null, pin: MoveKey | null = null): void {
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
subscribeRailExtras(notify);

/** Test seam. */
export function resetRailMoveForTests(): void {
  live.clear();
  front = null;
  pinOrder = [];
  boxes.clear();
  pushers.clear();
  pushVersion += 1;
  solved = null;
  setWindowPosMark("a", null);
  setWindowPosMark("b", null);
  for (const k of pinPosIds()) setPinPosRaw(k, null);
  notify();
}
