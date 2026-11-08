// THE PINNED BOT'S CARD IS THE SHELL'S (owner, 2026-11-08: "when I pin a task
// and change view the window doesn't flicker, but for bots it flickers — use
// the same window thingy"). The favourite's card (FavouritePeek) and the Tasks
// card (TasksPeek) are mounted once in the shell, so a page's view switch —
// which remounts the page's Claude rail — never touches them. The bot's card
// lived in the rail and went down and up with it. Now it is the shell's too
// (BotCardHost, beside FavouritePeek); the rings stay every rail's own. This
// store is where the two meet, as tasks-peek-store is for the Tasks card:
//   - ONE INTENT MACHINE (`botCardIntent`, rail-peek-lib's PeekIntent — the
//     rail's rings report pointer, focus and presses; the card its own);
//   - the card's state (`useBotCardState`: which bot, pinned, held, no room),
//     and the window's mark (last-chat `botOpen`) kept in step with a pin, so
//     a reload can pin it back (ClaudeRail's restore);
//   - the rings, by bot id (`setBotRing`), which the card's placement and the
//     keyboard's way back read whichever rail drew them;
//   - the card's handle (`registerBotCardHandle`), for the rail's press that
//     sends the keyboard into the card's composer;
//   - the press-inside flag and the pressed ring's href, which the rail's
//     outside-press rule and the no-room fallback read.
import { useSyncExternalStore } from "react";
import { setBotOpen } from "@platform/lib/last-chat";
import { setBotCard } from "./fav-peek-store";
import { PeekIntent, type Timers } from "./rail-peek-lib";

export interface BotCardState {
  /** The bot whose card is up (a hover's preview or pinned), by id. */
  key: string | null;
  /** Pinned by a press. */
  pinned: boolean;
  /** Held (favourites on): nothing but its × and Open close it. */
  held: boolean;
  /** The row left no room for a card: the rings are doors until it grows. */
  noRoom: boolean;
}

const EMPTY: BotCardState = { key: null, pinned: false, held: false, noRoom: false };
let state: BotCardState = EMPTY;
const subs = new Set<() => void>();

function write(patch: Partial<BotCardState>): void {
  const next = { ...state, ...patch };
  if (next.key === state.key && next.pinned === state.pinned && next.held === state.held && next.noRoom === state.noRoom) return;
  state = next;
  // ONE WINDOW B (fav-peek-store `setBotCard`): a pinned bot's card says so
  // to the favourite's card and rings.
  setBotCard(state.pinned && state.key ? state.key : null);
  for (const fn of [...subs]) fn();
}

/** Favourites on: a pin holds the card (the list card's rule). The rail says. */
let holdOnPin = true;
export function setBotCardHoldsOnPin(on: boolean): void {
  holdOnPin = on;
}

let warm: () => void = () => {};
/** What a ring's arrival warms (BotPeek `preloadBotChat`): set by the host. */
export function setBotCardWarm(fn: () => void): void {
  warm = fn;
}

let intent: PeekIntent | null = null;
let testTimers: Timers | undefined;

/** The shell's one intent machine for the bot card (header). */
export function botCardIntent(): PeekIntent {
  if (!intent) {
    intent = new PeekIntent(
      {
        // The machine pins before it shows (`showNow`): the mark follows both.
        show: (key) => {
          // A card that went has nothing to hold. Only a close says so: the
          // machine pins BEFORE it shows a new card (`showNow`), so a pin's
          // hold lands while the key is still the old one (or none).
          write(key === null ? { key, pinned: false, held: false } : { key });
          if (state.pinned) setBotOpen(key);
        },
        pin: (on) => {
          write({ pinned: on, ...(on && holdOnPin ? { held: true } : {}) });
          setBotOpen(on ? state.key : null);
        },
        warm: () => warm(),
      },
      testTimers,
    );
  }
  return intent;
}

export function botCardState(): BotCardState {
  return state;
}

function subscribe(fn: () => void): () => void {
  subs.add(fn);
  return () => {
    subs.delete(fn);
  };
}

export function useBotCardState(): BotCardState {
  return useSyncExternalStore(subscribe, botCardState, botCardState);
}

export function setBotCardHeld(held: boolean): void {
  write({ held });
}

export function setBotCardNoRoom(noRoom: boolean): void {
  write({ noRoom });
}

/** The rings, by bot id (header). A rail's ref callback sets its ring; the
 *  clear takes it off only if it is still that ring — a page switch can mount
 *  the next rail before the last one lets go. */
const rings = new Map<string, HTMLElement>();

export function setBotRing(id: string, el: HTMLElement): void {
  rings.set(id, el);
}

export function clearBotRing(id: string, el: HTMLElement): void {
  if (rings.get(id) === el) rings.delete(id);
}

export function botRing(id: string): HTMLElement | null {
  return rings.get(id) ?? null;
}

export interface BotCardHandle {
  focusComposer(): boolean;
  focusChat(): boolean;
}

let handle: BotCardHandle | null = null;

/** The host's card (BotPeek's handle), for the keyboard's way in. */
export function registerBotCardHandle(h: BotCardHandle): () => void {
  handle = h;
  return () => {
    if (handle === h) handle = null;
  };
}

export function botCardHandle(): BotCardHandle | null {
  return handle;
}

/** A press inside the card (the rail's outside-press rule reads it, a tick
 *  after its own pointerdown). */
export const botPress = { inside: false };

/** The ring a press just pinned, for one frame: a card that finds no room on
 *  that press takes the ring's door instead (`onNoRoom`). */
export const botPressed: { href: string | null } = { href: null };

export function resetBotCardForTests(timers?: Timers): void {
  intent?.dispose();
  intent = null;
  testTimers = timers;
  state = EMPTY;
  subs.clear();
  rings.clear();
  handle = null;
  holdOnPin = true;
  botPress.inside = false;
  botPressed.href = null;
  setBotCard(null);
}
