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
//
// …AND WHERE THE PAGE STANDS (`page`, `setRailPage`): its place's root and
// its selected node, as the top rail last said. The favourite's where-chip
// goes nowhere — plain text, no fill — when it names that very place
// (claude-rail-lib `chipDoor`'s `pageRoot`), so the card hears a hop the
// rail makes in place (the explorer keeps one rail from folder to file) and
// a node selected on the canvas.
//
// HOW MANY PAGE RAILS ARE MOUNTED (`registerPageRail`, `usePageRailCount`):
// the shell's fallback rail (FallbackRail; owner, 2026-11-05) stands only
// while no page draws a rail of its own — one count, read in one place, so
// the two never stand together. Every page rail counts from its mount to its
// unmount, shown or hidden (a hidden page rail has no favourite to draw, and
// neither would the fallback). Said SYNCHRONOUSLY, not coalesced like the
// rest of this store, and from the rails' layout phase: a hop unmounts one
// page and mounts the next in one commit, and the fallback's re-render lands
// before that commit paints — no frame with two rails, or none.
import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import type { TaskPulseTask } from "@platform/lib/api";
import { clearFavOpen, favOpenedAtMs, useFavOpen } from "@platform/lib/last-chat";
import type { RailHere } from "./claude-rail-lib";
import { FAV_SETTLE_MS, PeekIntent, favLike, favOpenVerdict, type Box } from "./rail-peek-lib";

/** Where the page a rail stands on is (header): its `placeRoot`, and the
 *  canvas's selected node file (null: none, or not a canvas). */
export interface RailPage {
  root: string;
  node: string | null;
}

export interface FavPeekState {
  rail: HTMLElement | null;
  /** The top rail's page (header); null with no rail, or one that never said. */
  page: RailPage | null;
  favBox: Box | null;
  /** The favourite whose card should take the keyboard when it stands. */
  pendingFocus: string | null;
}

/** How long a keyboard press's wish to focus the card outlives the press. */
export const PENDING_FOCUS_MS = 10_000;

let live: FavPeekState = { rail: null, page: null, favBox: null, pendingFocus: null };
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

function samePage(a: RailPage | null, b: RailPage | null): boolean {
  if (a === b) return true;
  if (!a || !b) return false;
  return a.root === b.root && a.node === b.node;
}

function flush(): void {
  dirty = false;
  if (
    live.rail === seen.rail &&
    samePage(live.page, seen.page) &&
    sameBox(live.favBox, seen.favBox) &&
    live.pendingFocus === seen.pendingFocus
  ) {
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

/** Each rail's page canvas, as its rail answers it (header). */
const heres = new WeakMap<HTMLElement, () => RailHere | null>();
/** Each rail's page, as its rail last said (header). */
const pages = new WeakMap<HTMLElement, RailPage>();

function settleRail(): void {
  const top = topRail();
  const page = (top && pages.get(top)) ?? null;
  if (live.rail !== top || !samePage(live.page, page)) mark({ ...live, rail: top, page });
}

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
    pages.delete(el);
    settleRail();
  };
}

/** Where `el`'s page stands now (header): a rail says so as it mounts and on
 *  every hop or node selection it makes in place. Null forgets it. */
export function setRailPage(el: HTMLElement, page: RailPage | null): void {
  if (page) pages.set(el, page);
  else pages.delete(el);
  settleRail();
}

/** The page canvas `rail` registered with (header), now; null for a rail
 *  with none (a folder's, a file's) or no rail. */
export function railHere(rail: HTMLElement | null): RailHere | null {
  return (rail && heres.get(rail)?.()) ?? null;
}

/** What the favourite's card tells the rail's own card (`avoid`): its box
 *  when OPEN, nothing for a hover — a hover overlaps, never shrinks it. */
export function publishFavBoxFor(hovering: boolean, box: Box | null): void {
  publishFavBox(hovering ? null : box);
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

/** A ring `focusRail` put the keyboard on: its focus is a hand-off, not a
 *  reader arriving, so it opens no hover card (ClaudeRail's ring `onFocus`
 *  asks `takeQuietFocus` once; owner, 2026-11-04: "when I unfavourite, a
 *  random task from above opens"). */
const quiet = new WeakSet<object>();

/** True — and spent — when `el` took the keyboard from `focusRail`. */
export function takeQuietFocus(el: object): boolean {
  return quiet.delete(el);
}

/** WHERE THE KEYBOARD GOES WHEN A CARD SHUTS UNDER IT — never the body: the
 *  ring for `key`, looked for first in the section `section` names — the
 *  rail's favourites (`"foot"`: the favourite's card, and the rail's own card
 *  letting go of a chat that was just starred or opened as a favourite,
 *  whose ring now lives there) or its list (`"list"`: an unstarred
 *  favourite, whose foot ring is about to go) — then in the other one (the
 *  pulse may not have moved the ring yet), skipping `key`'s foot ring when
 *  that is going (`unstarred`); else the rail itself (made focusable for the
 *  purpose) — NEVER another chat's ring: on another folder the unstarred
 *  chat has no list ring, and the rail's first ring is a stranger. The ring
 *  is focused QUIETLY (`takeQuietFocus`): a hand-off opens no card. Rings
 *  carry their key (ClaudeRail `data-task-key`). */
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
  if (target) quiet.add(target);
  else {
    if (!rail.hasAttribute("tabindex")) rail.setAttribute("tabindex", "-1");
    target = rail;
  }
  target.focus({ preventScroll: true });
  // Heard synchronously (focus events fire inside `focus()`); a focus that
  // did not land leaves no mark for a later, real one to be muted by.
  quiet.delete(target);
}

// WINDOW B'S HOVER (owner, 2026-11-05: two sections, two windows): a hover on
// a favourite's ring — with no favourite's card open in this window — shows
// the favourite's card (FavouritePeek) in a HOVER MODE, bottom right at the
// card's own size. Local, never persisted: `hoverKey` lives here and nowhere
// else. The timings are the rail's own (PeekIntent, one instance for the
// shell: only one rail stands at a time). A press on the ring, or inside the
// card, converts the hover to this window's open key (`openFavFromHover`).
let hoverKey: string | null = null;
const hoverSubs = new Set<() => void>();

function setHoverKey(key: string | null): void {
  if (hoverKey === key) return;
  hoverKey = key;
  for (const fn of [...hoverSubs]) fn();
}

/** The favourite whose hover card is up now, or null. */
export function favHoverKey(): string | null {
  return hoverKey;
}

function subscribeHover(fn: () => void): () => void {
  hoverSubs.add(fn);
  return () => {
    hoverSubs.delete(fn);
  };
}

export function useFavHoverKey(): string | null {
  return useSyncExternalStore(subscribeHover, favHoverKey, favHoverKey);
}

let hoverIntent: PeekIntent | null = null;

/** The hover-intent machine of window B (header): the favourites' rings
 *  report their pointer and focus to it, the card reports its own. */
export function favHoverIntent(): PeekIntent {
  if (!hoverIntent) hoverIntent = new PeekIntent({ show: setHoverKey, warm: () => {} });
  return hoverIntent;
}

/** Drop the hover now (a press converted it, the rail went, the card was
 *  closed): no grace, nothing left pending. */
export function dismissFavHover(): void {
  hoverIntent?.dismiss();
  setHoverKey(null);
}

export function resetFavHoverForTests(): void {
  hoverIntent?.dispose();
  hoverIntent = null;
  hoverKey = null;
}

let pageRails = 0;
const pageSubs = new Set<() => void>();

/** A page's rail mounted (header): counted until the answer runs, once. */
export function registerPageRail(): () => void {
  pageRails += 1;
  for (const fn of [...pageSubs]) fn();
  let done = false;
  return () => {
    if (done) return;
    done = true;
    pageRails -= 1;
    for (const fn of [...pageSubs]) fn();
  };
}

export function pageRailCount(): number {
  return pageRails;
}

function subscribePages(fn: () => void): () => void {
  pageSubs.add(fn);
  return () => {
    pageSubs.delete(fn);
  };
}

/** How many page rails are mounted now (header), re-rendering on each. */
export function usePageRailCount(): number {
  return useSyncExternalStore(subscribePages, pageRailCount, pageRailCount);
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
  live = { rail: null, page: null, favBox: null, pendingFocus: null };
  seen = live;
  dirty = false;
  rails = [];
  pendingAt = 0;
  pageRails = 0;
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
  if (favLike(openRow) && openRow!.status !== "archived") seenLive.current.seen = true;
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
