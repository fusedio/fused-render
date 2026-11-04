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
import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { FAV_MARK_MS, favOpenMark, favOpenStep, type Box, type FavOpenMark } from "./rail-peek-lib";

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
 *  ring for `key`, looked for first in the section `section` names — the
 *  rail's favourites (`"foot"`: the favourite's card, and the rail's own card
 *  letting go of a chat that was just starred or opened as a favourite,
 *  whose ring now lives there) or its list (`"list"`: an unstarred
 *  favourite, whose foot ring is about to go) — then in the other one (the
 *  pulse may not have moved the ring yet); else the rail's first ring that
 *  is not `key`'s foot ring when that is going (`unstarred`); else the rail
 *  itself (made focusable for the purpose). Rings carry their key
 *  (ClaudeRail `data-task-key`). */
export function focusRail(
  rail: HTMLElement | null,
  key: string | null,
  section: "foot" | "list",
  unstarred = false,
): void {
  if (!rail || rail.isConnected === false) return;
  const scopes = section === "foot" ? [".claude-rail-favs", ".claude-rail-chats"] : [".claude-rail-chats", ".claude-rail-favs"];
  const goingFoot = (el: HTMLElement) => unstarred && el.dataset.taskKey === key && !!el.closest(".claude-rail-favs");
  let target: HTMLElement | null = null;
  if (key) {
    search: for (const scope of scopes) {
      for (const el of rail.querySelectorAll<HTMLElement>(`${scope} .claude-rail-chat[data-task-key]`)) {
        if (el.dataset.taskKey === key && !goingFoot(el)) {
          target = el;
          break search;
        }
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

/** THE FAVOURITE OPENED AHEAD OF THE PULSE (rail-peek-lib `FavOpenMark`):
 *  the key a press (ClaudeRail's ring) or an "open" answer (FavouritePeek's
 *  card) put up, over the pulse's open favourite `pulseOpen` until the pulse
 *  agrees or moves elsewhere (`favOpenStep`). `live(key)`: the key is still
 *  a favourite the pulse lists. `key` is the mark's key, or null with none
 *  standing; `mark` sets one, `drop` takes it down (only if it is still on
 *  `key`, when given — a refused write must not drop a later press's). */
export function useFavOpenMark(
  pulseOpen: string | null,
  live: (key: string) => boolean,
): { key: string | null; mark: (key: string) => void; drop: (key?: string) => void } {
  const [mark, setMark] = useState<FavOpenMark | null>(null);
  const pulseRef = useRef(pulseOpen);
  pulseRef.current = pulseOpen;
  const alive = mark ? live(mark.key) : false;
  useEffect(() => {
    if (!mark) return;
    const next = favOpenStep(mark, pulseOpen, alive, Date.now());
    if (next !== mark) {
      setMark(next);
      return;
    }
    // Waiting out a stale pulse: past FAV_MARK_MS the pulse wins anyway.
    const id = window.setTimeout(
      () => setMark((m) => (m === mark ? null : m)),
      Math.max(0, mark.at + FAV_MARK_MS - Date.now()),
    );
    return () => window.clearTimeout(id);
  }, [mark, pulseOpen, alive]);
  const set = useCallback((key: string) => {
    setMark((prev) => favOpenMark(key, pulseRef.current, prev, Date.now()));
  }, []);
  const drop = useCallback((key?: string) => {
    setMark((m) => (m && (key === undefined || m.key === key) ? null : m));
  }, []);
  return { key: mark && alive ? mark.key : null, mark: set, drop };
}
