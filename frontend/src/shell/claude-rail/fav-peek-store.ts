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
// (`pendingFocus`): the press opens the card in this window (platform/lib/
// last-chat `setFavOpen`), and the card is drawn a frame or more later — its
// chat's chunk may still be loading. The rail leaves the key here; the card
// takes it (`takeFavFocus`) once it stands on that key — however long that took, up to
// PENDING_FOCUS_MS, past which a stale wish must not steal the keyboard from
// whatever the reader did since.
//
// WRITES COALESCE, READERS HEAR ONLY A REAL CHANGE: the card's box write
// re-renders the rail, whose own writes would re-render the card (React
// #185 the first time round, with ring refs in the loop). So a write only
// marks the store dirty; a microtask later the live state is compared with
// the last one the readers saw, and they hear of it only if something is
// actually different.
//
// THE PAGE'S OWN CANVAS rides with a rail (`registerRail`'s `here`): the
// favourite's card has a where-chip too, and a chip naming a node of the
// canvas the page stands on selects it in place, as the rail's own card's
// does (claude-rail-lib `chipDoor`). Read at the press (`railHere`), never
// held: the page's selection moves without the store hearing of it.
import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import type { TaskPulseTask } from "@platform/lib/api";
import { clearFavOpen, favOpenedAtMs, useFavOpen } from "@platform/lib/last-chat";
import type { RailHere } from "./claude-rail-lib";
import { FAV_SETTLE_MS, favOpenVerdict, type Box } from "./rail-peek-lib";

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

/** Each rail's page canvas, as its rail answers it (header). */
const heres = new WeakMap<HTMLElement, () => RailHere | null>();

/** A rail mounted: it goes on top of the stack (header). The answer takes it
 *  off again — itself only, wherever it stands in the stack. `here`: the
 *  page's canvas, where the page can select a node in place (header). */
export function registerRail(el: HTMLElement, here?: () => RailHere | null): () => void {
  rails = [...rails.filter((r) => r !== el), el];
  if (here) heres.set(el, here);
  else heres.delete(el);
  settleRail();
  return () => {
    rails = rails.filter((r) => r !== el);
    heres.delete(el);
    settleRail();
  };
}

/** The page canvas `rail` registered with (header), now; null for a rail
 *  with none (a folder's, a file's) or no rail. */
export function railHere(rail: HTMLElement | null): RailHere | null {
  return (rail && heres.get(rail)?.()) ?? null;
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

/** The card stands on `key`: true (and the wish spent) when a press asked
 *  for the keyboard there, recently enough to still mean it. */
export function takeFavFocus(key: string): boolean {
  if (live.pendingFocus !== key) return false;
  const fresh = Date.now() - pendingAt <= PENDING_FOCUS_MS;
  requestFavFocus(null);
  return fresh;
}

/** THE READER MOVED ON (CodeRabbit, PR #2100): a pending wish is withdrawn
 *  when the keyboard lands on a control that is neither inside `card`, nor
 *  the ring that asked (its `data-task-key`), nor the rail itself (where
 *  `focusRail` parks the keyboard) — a × taking the keyboard back seconds
 *  later would undo where the reader went meanwhile. The body (the
 *  removed-ring case: focus fell to nowhere) and a non-element target
 *  withdraw nothing. */
export function withdrawFavFocusOn(target: EventTarget | null, card: Element | null): void {
  const key = live.pendingFocus;
  if (!key || !target) return;
  const el = target as Element;
  if (typeof el.closest !== "function") return;
  if (el.tagName === "BODY" || el.tagName === "HTML") return;
  if (el === live.rail || rails.includes(el as HTMLElement)) return;
  if (card?.contains(el)) return;
  const ring = el.closest<HTMLElement>(".claude-rail-chat[data-task-key]");
  if (ring?.dataset.taskKey === key) return;
  requestFavFocus(null);
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

/** THIS WINDOW'S OPEN FAVOURITE, checked against the pulse's stars
 *  (FavouritePeek; rail-peek-lib `favOpenVerdict`): the pulse row of the
 *  open key while its card should show, else null — and the key CLEARED
 *  once the verdict says close (an unstar, an erase or an archive, here or
 *  in another window). `seenLive` is whether the pulse has said the key is a
 *  live favourite since it was opened — once it has, its row turning
 *  unstarred, archived or gone closes the card. A star pressed a moment ago
 *  shows ahead of the pulse for FAV_SETTLE_MS at most (the open time is the
 *  window's, platform/lib/last-chat `favOpenedAtMs`); the verdict is asked
 *  again when that runs out. `on` false: nothing shows, nothing is cleared. */
export function useFavOpenRow(on: boolean, rows: readonly TaskPulseTask[]): TaskPulseTask | null {
  const openKey = useFavOpen();
  const openRow = useMemo(() => (openKey ? rows.find((t) => t.key === openKey) : undefined), [openKey, rows]);
  const seenLive = useRef<{ key: string | null; seen: boolean }>({ key: null, seen: false });
  if (seenLive.current.key !== openKey) seenLive.current = { key: openKey, seen: false };
  if (openRow?.starred && openRow.status !== "archived") seenLive.current.seen = true;
  const [, setSettled] = useState(0);
  useEffect(() => {
    const at = openKey ? favOpenedAtMs(openKey) : null;
    if (at === null) return;
    const ms = at + FAV_SETTLE_MS - Date.now();
    if (ms <= 0) return;
    const id = setTimeout(() => setSettled((n) => n + 1), ms + 20);
    return () => clearTimeout(id);
  }, [openKey]);
  const verdict =
    on && openKey
      ? favOpenVerdict(openRow, rows.length > 0, seenLive.current.seen, favOpenedAtMs(openKey), Date.now())
      : null;
  useEffect(() => {
    if (verdict === "close" && openKey) clearFavOpen(openKey);
  }, [verdict, openKey]);
  return verdict === "show" ? (openRow ?? null) : null;
}
