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
import { parsePos, serialisePos, type Pos, type RailWindow } from "./rail-move-lib";
import type { Box } from "./rail-peek-lib";

const live: Record<RailWindow, Pos | null> = { a: null, b: null };
let front: RailWindow | null = null;
const subs = new Set<() => void>();
function notify(): void {
  for (const fn of [...subs]) fn();
}

/** The parsed mark, kept per raw string so a re-read hands back the same
 *  object (`useSyncExternalStore` compares snapshots by identity). */
const parsed: Record<RailWindow, { raw: string | null; pos: Pos | null }> = {
  a: { raw: null, pos: null },
  b: { raw: null, pos: null },
};
function stored(win: RailWindow): Pos | null {
  const raw = windowPosMark(win);
  const c = parsed[win];
  if (c.raw !== raw) parsed[win] = { raw, pos: parsePos(raw) };
  return parsed[win].pos;
}

/** Where window `win` floats now — the drag on now, else the stored mark —
 *  or null while it is docked. */
export function windowPos(win: RailWindow): Pos | null {
  return live[win] ?? stored(win);
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
  setWindowPosMark(win, serialisePos(pos));
  notify();
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
const boxes: Record<RailWindow, Map<string, Box>> = { a: new Map(), b: new Map() };

export function publishWindowBox(win: RailWindow, owner: string, box: Box | null): void {
  const m = boxes[win];
  m.delete(owner);
  if (box) m.set(owner, box);
}

/** Window `win`'s card up now (the newest publisher, should two overlap for
 *  a fade), or null. */
export function windowBox(win: RailWindow): Box | null {
  let last: Box | null = null;
  for (const b of boxes[win].values()) last = b;
  return last;
}

let boxSeq = 0;

/** A card's live box for the other window's drag (`publishWindowBox`):
 *  `box` while it is up, null otherwise; withdrawn on unmount. */
export function useWindowBox(win: RailWindow, box: Box | null): void {
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
    );
  }, [win, id, left, top, width, height]);
  useEffect(() => () => publishWindowBox(win, id, null), [win, id]);
}

/** Test seam. */
export function resetRailMoveForTests(): void {
  live.a = null;
  live.b = null;
  front = null;
  boxes.a.clear();
  boxes.b.clear();
  setWindowPosMark("a", null);
  setWindowPosMark("b", null);
  notify();
}
