// THE LIST'S CHAT CARD IS THE SHELL'S, WITH FREE PINS (owner, 2026-11-18: "it
// should be smooth, no flicker, jumping"). The rail is a page's own and
// remounts with every page hop; a card it drew itself went down and up with
// it — and a PINNED list task, which outlives the hop, blinked out and faded
// back on every page change while the pinned favourite beside it (the shell's,
// FavouritePeek) stood still. So the rail still BUILDS its card's slots
// (rail-slots-store: the free one and one per pinned task — any number since
// 2026-11-24) — every rule about them stays where it was — but hands the
// elements here, and the shell draws them (ListCardHost, beside the + card's
// host, plus-card-store's own pattern): same component, same key (the slot's
// id), so a rail going and the next one coming keeps every pinned card
// mounted, its chat, its scroll and its words with it.
//
// A STACK OF RAILS, AS EVERYWHERE (fav-peek-store): the next page's rail can
// publish before the last one's cleanup runs, so a withdrawal counts only
// from the rail that published last.
//
// A RAIL GOING TAKES ITS NON-PIN ALONG AND LEAVES THE PINS: the free slot's
// card is that rail's hover or click and means nothing without it; each pinned
// slot's card stays as it was last built until the next rail builds it again
// (a page with no rail at all keeps it standing — it floats, and needs none).
import { useSyncExternalStore, type ReactNode } from "react";

/** One slot's card as the rail built it: the slot's id, the task it shows
 *  pinned (null: the free slot), and the element. */
export interface ListCard {
  id: string;
  pin: string | null;
  node: ReactNode;
}
export type ListCards = readonly ListCard[];

const NONE: ListCards = Object.freeze([]);
let cards: ListCards = NONE;
let owner: object | null = null;
let hosts = 0;
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

/** A rail's slots, as it built them this render. */
export function publishListCards(id: object, next: ListCards): void {
  owner = id;
  cards = next;
  notify();
}

/** The rail is gone (or no longer hands its card over): its non-pin goes, the
 *  pinned slots' cards stay (header). Only the last publisher's word counts. */
export function withdrawListCards(id: object): void {
  if (owner !== id) return;
  owner = null;
  cards = cards.filter((c) => c.pin !== null);
  notify();
}

/** The shell's host mounted: counted until the answer runs. */
export function registerListHost(): () => void {
  hosts += 1;
  notify();
  let done = false;
  return () => {
    if (done) return;
    done = true;
    hosts -= 1;
    notify();
  };
}

const hosted = () => hosts > 0;
/** Is a host there to draw the card? (None in an embed: the rail draws it.) */
export function useListHosted(): boolean {
  return useSyncExternalStore(subscribe, hosted, () => false);
}

const current = () => cards;
export function useListCards(): ListCards {
  return useSyncExternalStore(subscribe, current, () => NONE);
}

const orphaned = () => owner === null;
/** No rail stands behind the cards held here: what is drawn is the pinned
 *  slots as the last rail built them (header). Nobody rebuilds them then, so
 *  the host itself must let each go when its pin does (ListCardHost). */
export function useListOrphaned(): boolean {
  return useSyncExternalStore(subscribe, orphaned, () => true);
}

/** The two answers outside React (tests). */
export function listCardsNow(): ListCards {
  return cards;
}
export function listOrphanedNow(): boolean {
  return owner === null;
}

export function resetListHostForTests(): void {
  cards = NONE;
  owner = null;
  hosts = 0;
  notify();
}
