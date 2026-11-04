// THE RAIL AND ITS FAVOURITE RINGS, FOR THE SHELL'S FAVOURITE CARD (owner,
// 2026-11-03: "the favourite window will stay open regardless … when I
// change views why doesn't it stay as is, it flickers and reloads every
// time"). The favourite's card is mounted ONCE, in the shell (FavouritePeek),
// above every page; the rail is a page's own and comes and goes with it.
// This store is how the two meet: each rail registers itself as it mounts,
// the card reads it to stand on its bottom edge — and hides, chat still
// mounted, while no rail is on the page.
// The card's box goes the other way, so the rail's own card can keep above
// it (RailPeek `bottomLimit`).
//
// WRITES COALESCE, READERS HEAR ONLY A REAL CHANGE: the card's box write
// re-renders the rail, whose own writes would re-render the card (React
// #185 the first time round, with ring refs in the loop). So a write only
// marks the store dirty; a microtask later the live state is compared with
// the last one the readers saw, and they hear of it only if something is
// actually different.
import { useSyncExternalStore } from "react";
import type { Box } from "./rail-peek-lib";

export interface FavPeekState {
  rail: HTMLElement | null;
  favBox: Box | null;
}

let live: FavPeekState = { rail: null, favBox: null };
/** The state as the readers last heard it (`useSyncExternalStore`'s
 *  snapshot: one reference until a real change). */
let seen: FavPeekState = live;
let dirty = false;
const subs = new Set<() => void>();

function sameBox(a: Box | null, b: Box | null): boolean {
  if (a === b) return true;
  if (!a || !b) return false;
  return a.left === b.left && a.top === b.top && a.width === b.width && a.height === b.height;
}

function flush(): void {
  dirty = false;
  if (live.rail === seen.rail && sameBox(live.favBox, seen.favBox)) {
    // Back where it started: nothing to say.
    live = seen;
    return;
  }
  seen = live;
  for (const fn of subs) fn();
}

function mark(next: FavPeekState): void {
  live = next;
  if (dirty) return;
  dirty = true;
  queueMicrotask(flush);
}

/** The rail on the page now (null when none, or when it unmounted). A rail
 *  that is not the registered one unregistering is a no-op: two rails never
 *  stand on one page, but a late unmount must not clear a fresh mount. */
export function registerRail(el: HTMLElement | null, was?: HTMLElement | null): void {
  if (el === null && was && live.rail !== was) return;
  if (live.rail === el) return;
  mark({ ...live, rail: el });
}

/** Where the favourite's card stands (null while closed). */
export function publishFavBox(box: Box | null): void {
  if (sameBox(live.favBox, box)) return;
  mark({ ...live, favBox: box });
}

export function favPeekState(): FavPeekState {
  return seen;
}

function subscribe(fn: () => void): () => void {
  subs.add(fn);
  return () => {
    subs.delete(fn);
  };
}

/** The rail and the favourite card's box as they stand, re-rendering on
 *  every real change. */
export function useFavPeekState(): FavPeekState {
  return useSyncExternalStore(subscribe, favPeekState, favPeekState);
}

export function resetFavPeekStoreForTests(): void {
  live = { rail: null, favBox: null };
  seen = live;
  dirty = false;
}
