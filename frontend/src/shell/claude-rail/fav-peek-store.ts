// THE RAIL AND ITS FAVOURITE RINGS, FOR THE SHELL'S FAVOURITE CARD (owner,
// 2026-11-03: "the favourite window will stay open regardless … when I
// change views why doesn't it stay as is, it flickers and reloads every
// time"). The favourite's card is mounted ONCE, in the shell (FavouritePeek),
// above every page; the rail is a page's own and comes and goes with it.
// This store is how the two meet: each rail registers itself as it mounts,
// the card reads it to stand on its bottom edge — and hides, chat still
// mounted, while no rail is on the page.
// The card's box goes the other way, so the rail's own card can keep clear
// of it (RailPeek `avoid`).
//
// A STACK OF RAILS, NOT ONE SLOT: two rails can overlap for a moment (a page
// hop mounts the next page's rail before the last one's cleanup runs, and
// React may run them in either order). The card stands beside the LAST rail
// registered that is still on the page; a rail leaving that is not the top
// one takes only itself off the stack.
//
// A KEYBOARD PRESS ON A FAVOURITE'S RING HANDS THE KEYBOARD TO ITS CARD
// (`pendingFocus`): the press writes "open" and the card mounts on the
// pulse, a round trip later. The rail leaves the key here; the card takes it
// (`takeFavFocus`) once it stands on that key — however long that took, up to
// PENDING_FOCUS_MS, past which a stale wish must not steal the keyboard from
// whatever the reader did since.
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
  /** The favourite whose card should take the keyboard when it stands. */
  pendingFocus: string | null;
}

/** How long a keyboard press's wish to focus the card outlives the press. */
export const PENDING_FOCUS_MS = 10_000;

let live: FavPeekState = { rail: null, favBox: null, pendingFocus: null };
/** Every rail registered and not yet gone, oldest first (header). */
let rails: HTMLElement[] = [];
let pendingAt = 0;
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
  if (live.rail === seen.rail && sameBox(live.favBox, seen.favBox) && live.pendingFocus === seen.pendingFocus) {
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

/** The top of the stack: the last rail registered that is still mounted. */
function topRail(): HTMLElement | null {
  for (let i = rails.length - 1; i >= 0; i--) {
    const el = rails[i]!;
    if (el.isConnected !== false) return el;
  }
  return null;
}

function settleRail(): void {
  const top = topRail();
  if (live.rail !== top) mark({ ...live, rail: top });
}

/** A rail mounted: it goes on top of the stack (header). The answer takes it
 *  off again — itself only, wherever it stands in the stack. */
export function registerRail(el: HTMLElement): () => void {
  rails = [...rails.filter((r) => r !== el), el];
  settleRail();
  return () => {
    rails = rails.filter((r) => r !== el);
    settleRail();
  };
}

/** Where the favourite's card stands (null while closed). */
export function publishFavBox(box: Box | null): void {
  if (sameBox(live.favBox, box)) return;
  mark({ ...live, favBox: box });
}

/** A keyboard press on a favourite's ring (header): its card takes the
 *  keyboard once it stands on `key`. Null withdraws the wish. */
export function requestFavFocus(key: string | null): void {
  pendingAt = key ? Date.now() : 0;
  if (live.pendingFocus === key) return;
  mark({ ...live, pendingFocus: key });
}

/** AN "OPEN" WRITE ANSWERED OPEN (ClaudeRail `openFavourite`): the card
 *  shut on that key by its × ahead of the pulse (FavouritePeek `closing`)
 *  shows again, though the pulse may never have seen it closed. Heard at
 *  once, outside the coalesced state: a signal, not a value. */
const openSubs = new Set<(key: string) => void>();

export function noteFavOpened(key: string): void {
  for (const fn of [...openSubs]) fn(key);
}

export function onFavOpened(fn: (key: string) => void): () => void {
  openSubs.add(fn);
  return () => {
    openSubs.delete(fn);
  };
}

/** The card stands on `key`: true (and the wish spent) when a press asked
 *  for the keyboard there, recently enough to still mean it. */
export function takeFavFocus(key: string): boolean {
  if (live.pendingFocus !== key) return false;
  const fresh = Date.now() - pendingAt <= PENDING_FOCUS_MS;
  requestFavFocus(null);
  return fresh;
}

/** WHERE THE KEYBOARD GOES WHEN A CARD SHUTS UNDER IT — never the body: the
 *  ring for `key` in the rail's favourites (`"foot"`, the favourite's card)
 *  or its list (`"list"`, the rail's own card, and an unstarred favourite,
 *  whose foot ring is about to go), else the rail's first ring that is not
 *  `key`'s foot ring when that is going (`unstarred`), else the rail itself
 *  (made focusable for the purpose). Rings carry their key (ClaudeRail
 *  `data-task-key`). */
export function focusRail(
  rail: HTMLElement | null,
  key: string | null,
  section: "foot" | "list",
  unstarred = false,
): void {
  if (!rail || rail.isConnected === false) return;
  const scope = section === "foot" ? ".claude-rail-favs" : ".claude-rail-chats";
  const goingFoot = (el: HTMLElement) => unstarred && el.dataset.taskKey === key && !!el.closest(".claude-rail-favs");
  let target: HTMLElement | null = null;
  if (key) {
    for (const el of rail.querySelectorAll<HTMLElement>(`${scope} .claude-rail-chat[data-task-key]`)) {
      if (el.dataset.taskKey === key && !goingFoot(el)) {
        target = el;
        break;
      }
    }
  }
  if (!target) {
    for (const el of rail.querySelectorAll<HTMLElement>(".claude-rail-chat")) {
      if (!goingFoot(el)) {
        target = el;
        break;
      }
    }
  }
  if (!target) {
    if (!rail.hasAttribute("tabindex")) rail.setAttribute("tabindex", "-1");
    target = rail;
  }
  target.focus({ preventScroll: true });
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
  live = { rail: null, favBox: null, pendingFocus: null };
  seen = live;
  dirty = false;
  rails = [];
  pendingAt = 0;
}
