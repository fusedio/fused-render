// THE RAIL'S PREVIEW CARD, the parts that need no DOM (RailPeek.tsx draws it;
// ClaudeRail.tsx wires the rings). Behind `canvas_rail_peek_enabled`
// (rail-peek-flag.ts).
//
// Three rulings live here so they can be proved without a browser:
//
//   - WHERE THE CARD STANDS (`peekPlacement`): beside the rail, its right edge
//     a gap off the rail's left edge, its TOP LEVEL WITH THE RING'S TOP
//     (owner, 2026-10-31: "the top of the card and the top of item should be
//     aligned … unless there is not enough space in bottom") — pushed up only
//     as far as the row's bottom needs, and never above the row's top. Never
//     wider than the row leaves it.
//   - WHEN IT OPENS AND CLOSES (`PeekIntent`): a hover is not a click. The
//     card waits PEEK_OPEN_MS before it shows, so a pointer crossing the rail
//     on its way somewhere opens nothing; once up, moving to another ring
//     swaps the chat after a shorter PEEK_SWAP_MS without closing and
//     reopening; leaving both the ring and the card gives PEEK_CLOSE_MS of
//     grace, long enough to cross the gap between them; and a card the reader
//     is USING — one that holds the keyboard focus, because they are typing —
//     does not close on a pointer leave at all. Escape, a press outside, or
//     the pane opening dismiss it at once. A CLICK PINS IT (owner,
//     2026-10-31): a press on a ring (`press`) or anywhere in the card
//     (`pressStart`) — the hover rules then stop closing it, and only a
//     press outside, Escape, its × or its Open does. A pinned card does
//     NOTHING when the pointer rests on another ring — only that ring's
//     press swaps it (owner, 2026-11-02: "click only"; the hover-swap
//     trigger of 2026-10-31 is gone). The pointer and the keyboard focus
//     are tracked apart: a ring that loses focus while the pointer still
//     rests on it is still hovered.
//   - HOW BIG IT IS (`peekPlacement`, `dragResize`): 420×640 for a hover; a
//     PINNED card takes the size the reader last dragged it to from its left
//     corners or its left, top and bottom edges (RailPeek, ui-layout
//     `canvasRailPeek`; the favourite's card its own, `canvasRailFav`, from
//     the same grips — a bottom drag keeps its top), both clamped to the
//     row, the width also to the chat's reading column (`PEEK_MAX_WIDTH`).
//     The drag itself moves the box as the pointer does; when it ends the
//     card is placed again — top level with the ring — at its new size.
//   - WHAT STAYS WARM (`keepAlive`): the chats the reader has previewed stay
//     mounted (hidden) after the card closes, newest first, up to PEEK_KEEP,
//     so coming back to one is instant — the "load fast" half of the ask.
//     The mount starts on the pointer's ARRIVAL (`warm`), ahead of the
//     PEEK_OPEN_MS wait, so the transcript read overlaps the hover delay.
import type { TaskPulseTask } from "@platform/lib/api";
import { inFlight, liveJobRow, taskColumn } from "@shell/tasks-lib";

/** The pointer rests this long on a ring before its chat is MOUNTED (the
 *  warm-up): a sweep down the rail must not mount every chat it crosses. */
export const PEEK_WARM_MS = 60;
/** The hover waits this long before the card shows (a pass-through hover
 *  shows nothing). */
export const PEEK_OPEN_MS = 150;
/** With the card up, a hover on another ring swaps the chat after this. */
export const PEEK_SWAP_MS = 80;
/** Leaving both the ring and the card: the card closes after this, unless
 *  the pointer comes back onto either. */
export const PEEK_CLOSE_MS = 250;
/** How long a pointer may HANG in the safe cone or the bridge — heading for
 *  the card but not arriving — before the card stops waiting for it (owner,
 *  2026-10-30: "there should be a timeout to hang out in that invisible area
 *  … some reasonable amount of time"). Then the ordinary grace applies. */
export const PEEK_CONE_MS = 1200;
/** The safe cone's base reaches this far past the card's top and bottom. */
export const PEEK_CONE_PAD = 32;
/** The card's width, where the row leaves room for it. */
export const PEEK_WIDTH = 420;
/** The card's height cap; shorter rows shrink it. */
export const PEEK_MAX_HEIGHT = 640;
/** A pinned card can be dragged no smaller than this (RailPeek's corner
 *  and edge grips), and `peekPlacement` draws no card in a row with less room: the
 *  chat column's own floor is 340px (apps/claude/styles/chat.css `.c-chat`
 *  `min-width`, measured off legacy), under which the transcript and the
 *  composer stop shrinking and the card CLIPS them at its edge — the head's
 *  Open and ⋮ and the words of every turn cut off (owner, 2026-11-03,
 *  "content getting clipped"; it was 280). 340 + the card's two 1px borders
 *  + a hand's width for the head's seats. */
export const PEEK_MIN_WIDTH = 360;
export const PEEK_MIN_HEIGHT = 240;
/** …and no wider than the chat's own reading column (owner, 2026-11-02:
 *  "max width in resize should be the max width I can have for Claude chat
 *  … after which we show spaces left and right"): the transcript and the
 *  composer are capped at 720px (transcript.css, composer.css
 *  `.c-composer-chat`), so a card past 720 + its two 1px borders only adds
 *  gutters. The height has no such cap: the row's is the limit. */
export const PEEK_MAX_WIDTH = 722;
/** The favourite's card (FavouritePeek) until the reader drags it: a bit
 *  under the list card's. */
export const FAV_WIDTH = 380;
export const FAV_HEIGHT = 520;
/** THE FAVOURITE'S CARD NEVER STANDS TALLER THAN HALF THE WINDOW (owner,
 *  2026-11-05: a taller one pushed the list's hover card, which keeps clear
 *  of it, to the far left): on open, on every placement as the window
 *  shrinks (`favCardWish`), and through a drag (`dragResize`'s `maxHeight`). */
export const FAV_MAX_SHARE = 0.5;

/** The favourite card's ceiling in a window `viewportH` tall. */
export function favMaxHeight(viewportH: number): number {
  return Math.floor(viewportH * FAV_MAX_SHARE);
}

/** A card's size as the reader last dragged it (ui-layout), or null. */
export interface StoredSize {
  width: number;
  height: number;
}

/** WHAT SIZE A LIST CARD ASKS FOR (RailPeek `wishFor`). A seat with no
 *  conversation: the short card. A chat: the remembered size once the card
 *  is GROWN (a pinned card, after its press) — and, with favourites on
 *  (`oneSize`; owner, 2026-11-05: "the hover preview and the click preview
 *  must be the same size"), whatever its state, so a hover is the size a
 *  click would be. Nothing remembered: the default. */
export function listCardWish(o: {
  hasSession: boolean;
  stored: StoredSize | null;
  grown: boolean;
  oneSize: boolean;
}): PeekWish {
  if (!o.hasSession) return { maxHeight: PEEK_PLAIN_HEIGHT };
  const s = o.oneSize || o.grown ? o.stored : null;
  return s ? { width: s.width, maxHeight: s.height } : { maxHeight: PEEK_MAX_HEIGHT };
}

/** WHAT SIZE THE FAVOURITE'S CARD ASKS FOR (RailPeek `wishFor`, `size` +
 *  `resizeKey`): its own remembered size over its fixed wish `base`, its
 *  height capped at half the window (`favMaxHeight`). */
export function favCardWish(base: PeekWish, stored: StoredSize | null, viewportH: number): PeekWish {
  const w = stored ? { ...base, width: stored.width, maxHeight: stored.height } : base;
  return { ...w, maxHeight: Math.min(w.maxHeight ?? PEEK_MAX_HEIGHT, favMaxHeight(viewportH)) };
}

/** …and for a seat with no conversation (a draft, a scheduled task): what it
 *  is, its words and its door need no transcript's worth of card. */
export const PEEK_PLAIN_HEIGHT = 250;
/** Between the card's right edge and the rail. */
export const PEEK_GAP = 8;
/** The card keeps this far inside the row's edges. */
export const PEEK_INSET = 8;
/** How many previewed chats stay mounted after their card closed. */
export const PEEK_KEEP = 3;

export interface Box {
  left: number;
  top: number;
  width: number;
  height: number;
}

/** What a placement may ask for beyond the defaults. */
export interface PeekWish {
  /** The height to aim for (the default cap, a plain card's, or the size
   *  the reader dragged a pinned card to). */
  maxHeight?: number;
  /** The width to aim for (the reader's dragged width for a pinned card). */
  width?: number;
  /** Which edge of the card lines up with the ring: its top (the default —
   *  a list ring's card hangs down from it) or its bottom (the favourite's
   *  card, whose ring stands at the rail's foot, grows up from it). */
  align?: "top" | "bottom";
}

/** Where the card stands for a ring at `anchor`, beside a rail at `rail`,
 *  inside the workspace row `bounds` (all viewport rects). Null when the row
 *  leaves no room at all. `wish` is a number for the height cap alone, or
 *  the fuller `PeekWish`. */
export function peekPlacement(anchor: Box, rail: Box, bounds: Box, wish: number | PeekWish = PEEK_MAX_HEIGHT): Box | null {
  const w: PeekWish = typeof wish === "number" ? { maxHeight: wish } : wish;
  const room = rail.left - bounds.left - PEEK_GAP - PEEK_INSET;
  const width = Math.min(w.width ?? PEEK_WIDTH, PEEK_MAX_WIDTH, room);
  const height = Math.min(w.maxHeight ?? PEEK_MAX_HEIGHT, bounds.height - 2 * PEEK_INSET);
  // Under the chat's floor the card would clip its chat (PEEK_MIN_WIDTH):
  // no card, and the ring's press opens the pane instead (ClaudeRail).
  if (width < PEEK_MIN_WIDTH || height < 120) return null;
  const left = rail.left - PEEK_GAP - width;
  const minTop = bounds.top + PEEK_INSET;
  const maxTop = bounds.top + bounds.height - PEEK_INSET - height;
  const want = w.align === "bottom" ? anchor.top + anchor.height - height : anchor.top;
  const top = Math.max(minTop, Math.min(maxTop, want));
  return { left: Math.round(left), top: Math.round(top), width: Math.round(width), height: Math.round(height) };
}

/** WHERE THE RAIL'S CARD STANDS WITH THE FAVOURITE'S CARD UP (`avoid`, its
 *  box; FavouritePeek): the two must not cover each other. In order:
 *    1. ABOVE IT — the row ends a gap over the favourite's top — when that
 *       leaves a usable card (PEEK_MIN_HEIGHT);
 *    2. BESIDE IT, TO ITS LEFT — the favourite's left edge stands in for the
 *       rail's, the top level with the ring as always — when the row leaves
 *       a card at least PEEK_MIN_WIDTH wide there (a window under ~880px
 *       tall, the common laptop);
 *    3. OVER IT, the last resort — the plain placement, the favourite's card
 *       on top (claude-rail.css z-index) — rather than "no room" and every
 *       ring press opening the pane.
 *  With no favourite's card up, or one wholly below the row, the plain
 *  placement. */
/** THE ANCHOR STAYS IN ITS SCROLLED BOX (owner, 2026-11-05: "when I open a
 *  click preview in the collapsed rail of something that can scroll past
 *  10 items, it should stick at the 1st item and the 10th item — top
 *  shouldn't go all the way to the bottom or all the way to the top"): a
 *  ring scrolled out of the list's box (ClaudeRail `.claude-rail-chats`)
 *  would anchor the card above or below the box; instead the anchor is
 *  held at the box's first or last seat, so the card stands beside the
 *  list's edge and follows the ring only while it is in view. No box
 *  (a favourite's ring, the foot): the ring itself. */
export function clampAnchor(anchor: Box, scroller: Box | null): Box {
  if (!scroller) return anchor;
  const lo = scroller.top;
  const hi = Math.max(lo, scroller.top + scroller.height - anchor.height);
  const top = Math.min(hi, Math.max(lo, anchor.top));
  return top === anchor.top ? anchor : { ...anchor, top };
}

export function placeClear(anchor: Box, rail: Box, row: Box, avoid: Box | null, wish: number | PeekWish): Box | null {
  if (avoid && avoid.top < row.top + row.height) {
    const above = avoid.top - PEEK_GAP - row.top;
    if (above >= PEEK_MIN_HEIGHT + 2 * PEEK_INSET) {
      return peekPlacement(anchor, rail, { left: row.left, top: row.top, width: row.width, height: above }, wish);
    }
    const beside = peekPlacement(anchor, { ...rail, left: avoid.left }, row, wish);
    if (beside) return beside;
  }
  return peekPlacement(anchor, rail, row, wish);
}

/** Which grip a resize drag holds: one of the card's LEFT corners (`tl`,
 *  `bl`), or one of its edges — the LEFT (`l`, the width alone), the TOP
 *  (`t`, the height, growing up) or the BOTTOM (`b`, the height, growing
 *  down). The right edge stands against the rail: no grip there. */
export type ResizeEdge = "tl" | "bl" | "l" | "t" | "b";

/** The box a resize drag asks for: `edge` is the grip held, `start` the box
 *  when the pointer went down, `dx`/`dy` how far it has moved since. The
 *  width and height are clamped to the minimums and to the row: a drag that
 *  holds the top (`tl`, `t`) keeps the bottom edge where it was, one that
 *  holds the bottom (`bl`, `b`) the top, and one that holds no left (`t`,
 *  `b`) the width. `maxBottom` (viewport y) is a floor the bottom may not
 *  pass short of the row's own — the favourite's card's top, for the rail's
 *  card standing above it (RailPeek `avoid`). */
export function dragResize(
  edge: ResizeEdge,
  start: Box,
  dx: number,
  dy: number,
  bounds: Box,
  maxBottom?: number,
  /** A ceiling on the height (the favourite's card: `favMaxHeight`). */
  maxHeight?: number,
): Box {
  // The row wins over the minimums: a row with less room than PEEK_MIN_*
  // (peekPlacement accepts down to 120) must not be pushed past its inset.
  const clamp = (want: number, min: number, max: number) => Math.round(Math.min(Math.max(want, Math.min(min, max)), max));
  const right = start.left + start.width;
  const maxWidth = Math.min(PEEK_MAX_WIDTH, right - (bounds.left + PEEK_INSET));
  const width = edge === "t" || edge === "b" ? start.width : clamp(start.width - dx, PEEK_MIN_WIDTH, maxWidth);
  const minTop = bounds.top + PEEK_INSET;
  const floor = Math.min(bounds.top + bounds.height - PEEK_INSET, maxBottom ?? Infinity);
  let top: number;
  let height: number;
  if (edge === "tl" || edge === "t") {
    const bottom = start.top + start.height;
    height = clamp(start.height - dy, PEEK_MIN_HEIGHT, Math.min(bottom - minTop, maxHeight ?? Infinity));
    top = bottom - height;
  } else if (edge === "bl" || edge === "b") {
    height = clamp(start.height + dy, PEEK_MIN_HEIGHT, Math.min(floor - start.top, maxHeight ?? Infinity));
    top = start.top;
  } else {
    height = start.height;
    top = start.top;
  }
  return { left: Math.round(right - width), top: Math.round(top), width, height };
}

/** The bottom the rail's card may be dragged down to with the favourite's
 *  card up (`avoid`): where `placeClear` would let it end when it stands
 *  above the favourite (its first choice — the two share the rail's column):
 *  a gap and the inset over the favourite's top, so the release does not
 *  pull it back up; none when it stands beside it or (the last resort) over
 *  it. */
export function bottomClear(box: Box, avoid: Box | null): number | undefined {
  if (!avoid) return undefined;
  const sideBySide = box.left >= avoid.left + avoid.width || box.left + box.width <= avoid.left;
  if (sideBySide || box.top + box.height > avoid.top) return undefined;
  return avoid.top - PEEK_GAP - PEEK_INSET;
}

export interface Point {
  x: number;
  y: number;
}

/**
 * THE SAFE CONE (Amazon's mega-menu triangle, Floating UI's `safePolygon`,
 * menu-aim): the triangle from the hovered ring's centre (the apex, at the
 * rail) to the card's NEAR edge — its right side, padded top and bottom —
 * widening from right to left. A pointer inside it is heading for the card
 * and may cross other rings or the canvas on the way without the card
 * closing or swapping; the rail column itself (x at or past the apex) and
 * the card's own box are not the cone's — the rings and the card speak for
 * those.
 */
export function inSafeCone(p: Point, apex: Point, card: Box, pad = PEEK_CONE_PAD): boolean {
  const edgeX = card.left + card.width;
  if (p.x >= apex.x || p.x < edgeX) return false;
  const top: Point = { x: edgeX, y: card.top - pad };
  const bottom: Point = { x: edgeX, y: card.top + card.height + pad };
  return pointInTriangle(p, apex, top, bottom);
}

/** Barycentric sign test. */
export function pointInTriangle(p: Point, a: Point, b: Point, c: Point): boolean {
  const sign = (p1: Point, p2: Point, p3: Point) => (p1.x - p3.x) * (p2.y - p3.y) - (p2.x - p3.x) * (p1.y - p3.y);
  const d1 = sign(p, a, b);
  const d2 = sign(p, b, c);
  const d3 = sign(p, c, a);
  const neg = d1 < 0 || d2 < 0 || d3 < 0;
  const pos = d1 > 0 || d2 > 0 || d3 > 0;
  return !(neg && pos);
}

/** The kept-alive set after `key` was previewed: most recent first, at most
 *  `max` long. A key already at the front is the same set (same array, so a
 *  React state write is a no-op). */
export function keepAlive(kept: readonly string[], key: string, max = PEEK_KEEP): readonly string[] {
  if (kept[0] === key) return kept;
  return [key, ...kept.filter((k) => k !== key)].slice(0, Math.max(1, max));
}

/** A chat ring's state IN WORDS — the ring's accessible name on the rail and
 *  the card's status line. Same precedence as the ring itself: a background
 *  job, then a run waiting on the reader, then one in flight; settled rows
 *  say their lane and whether they are unread. */
export function statusWords(t: TaskPulseTask, failed: boolean): string {
  if (liveJobRow(t)) return "running in the background";
  const column = taskColumn(t);
  if (column === "needs_attention") return "waiting for you";
  if (inFlight(column)) return "running";
  const base = failed ? "blocked" : String(column).replace(/_/g, " ");
  return t.unread > 0 ? `${base}, unread` : base;
}

/** Timers, injectable so the intent's clock can be driven by a test. */
export interface Timers {
  set(fn: () => void, ms: number): number;
  clear(id: number): void;
}

const windowTimers: Timers = {
  set: (fn, ms) => window.setTimeout(fn, ms),
  clear: (id) => window.clearTimeout(id),
};

export interface IntentSink {
  /** The card should show `key`, or hide (`null`). Called only on change. */
  show(key: string | null): void;
  /** The card became pinned (`true`) or stopped being pinned. Called only on
   *  change. */
  pin?(on: boolean): void;
  /** `key` is about to be wanted: mount it now. Called on every arrival. */
  warm(key: string): void;
}

/** How a ring came to be "entered": the pointer resting on it, or the
 *  keyboard focus. The two leave independently. */
export type Via = "pointer" | "focus";

/**
 * The hover-intent state machine (header). One per rail; the rings and the
 * card report their pointer and focus, and the sink hears what to draw.
 */
export class PeekIntent {
  private shown: string | null = null;
  private pendingKey: string | null = null;
  private openTimer = 0;
  private warmTimer = 0;
  private closeTimer = 0;
  /** Which ring the pointer / the focus rests on (null = none). */
  private pointerKey: string | null = null;
  private focusKey: string | null = null;
  private overCard = false;
  /** The pointer is in the safe cone or on the bridge: on its way to the
   *  card. Holds the card and defers a swap, for PEEK_CONE_MS at most. */
  private inCone = false;
  private coneTimer = 0;
  /** The card holds the keyboard focus. */
  private held = false;
  /** A pointer is down inside the card (a text selection, a scrollbar drag)
   *  — it may leave the card mid-gesture and must not close it. */
  private pressing = false;
  /** Set by `dismiss(true)`: the focus that comes straight back to the ring
   *  (Escape hands it there) must not reopen what Escape just closed. Cleared
   *  by the next pointer arrival, the next focus LEAVING a ring (Tab moving
   *  on), or `showNow`. */
  private muteFocus = false;
  /** Pinned by a press (`press`, `pressStart`, `showNow(key, true)`): the
   *  card stays up whatever the pointer and the focus do, until `dismiss`. */
  private pinned = false;

  constructor(
    private readonly sink: IntentSink,
    private readonly timers: Timers = windowTimers,
  ) {}

  /** What the card shows now, if anything. */
  get current(): string | null {
    return this.shown;
  }

  /** Whether the card is pinned (header). */
  get isPinned(): boolean {
    return this.pinned;
  }

  /** The pointer (or the keyboard focus) arrived on ring `key`. A pinned card
   *  moves for no hover: only a ring's press does (owner, 2026-11-05: a
   *  favourite's ring is the OTHER window's — its hover opens that one). */
  enterRing(key: string, via: Via = "pointer"): void {
    if (via === "pointer") {
      this.pointerKey = key;
      this.muteFocus = false;
      // Crossing a ring INSIDE the cone is not a hover of it: the swap waits
      // until the pointer leaves the cone still on that ring (`leaveCone`).
      if (this.inCone && this.shown !== null && key !== this.shown) {
        this.cancelOpen();
        return;
      }
    } else {
      this.focusKey = key;
      if (this.muteFocus) return;
    }
    // A pinned card: a hover (or a Tab) on another ring moves nothing — that
    // ring's press does (header).
    if (this.pinned) return;
    this.cancelClose();
    if (this.shown === key) {
      this.cancelOpen();
      return;
    }
    if (this.pendingKey === key) return;
    this.cancelOpen();
    this.pendingKey = key;
    // The mount: at once for the keyboard (a focus is deliberate), after a
    // short dwell for the pointer (a sweep is not).
    if (via === "focus") this.sink.warm(key);
    else this.warmTimer = this.timers.set(() => {
      this.warmTimer = 0;
      this.sink.warm(key);
    }, PEEK_WARM_MS);
    this.openTimer = this.timers.set(
      () => {
        this.openTimer = 0;
        this.pendingKey = null;
        this.set(key);
      },
      this.shown === null ? PEEK_OPEN_MS : PEEK_SWAP_MS,
    );
  }

  /** The pointer (or focus) left the ring it was on. */
  leaveRing(via: Via = "pointer"): void {
    if (via === "pointer") {
      this.pointerKey = null;
    } else {
      this.focusKey = null;
      this.muteFocus = false;
    }
    // A pending open survives only while the ring it is for is still the one
    // the pointer or the focus rests on: the pointer leaving ring B while the
    // focus still sits on ring A must not open B.
    if (this.pendingKey !== null && this.pendingKey !== this.pointerKey && this.pendingKey !== this.focusKey) {
      this.cancelOpen();
    }
    this.scheduleClose();
  }

  /** A pointer went down inside the card: hold it through the gesture —
   *  and pin it, since a click on the card is a click (header). */
  pressStart(): void {
    this.pressing = true;
    this.cancelClose();
    if (this.shown !== null) this.setPinned(true);
  }

  /** …and came up (anywhere). */
  pressEnd(): void {
    if (!this.pressing) return;
    this.pressing = false;
    this.scheduleClose();
  }

  /** The rail's seats changed: forget any ring that is gone (no leave or
   *  blur ever fires for an unmounted element), drop a pending open on it,
   *  and close a card whose chat left the rail. */
  prune(live: ReadonlySet<string>): void {
    if (this.pointerKey !== null && !live.has(this.pointerKey)) this.pointerKey = null;
    if (this.focusKey !== null && !live.has(this.focusKey)) this.focusKey = null;
    if (this.pendingKey !== null && !live.has(this.pendingKey)) this.cancelOpen();
    if (this.shown !== null && !live.has(this.shown)) {
      this.cancelClose();
      // The card's composer may have held the focus; its unmount fires no
      // blur in every engine, so the hold is dropped here.
      this.held = false;
        this.pressing = false;
        this.setPinned(false);
      this.set(null);
      return;
    }
    this.scheduleClose();
  }

  /** The pointer arrived on the card. */
  enterCard(): void {
    this.overCard = true;
    this.cancelClose();
    this.leaveCone(false);
  }

  /** The pointer entered the safe cone (or the bridge) on its way to the
   *  card: hold, and start the hang clock. */
  enterCone(): void {
    if (this.inCone) return;
    this.inCone = true;
    this.cancelClose();
    this.coneTimer = this.timers.set(() => {
      this.coneTimer = 0;
      // Hung too long: the cone stops speaking for the pointer.
      this.leaveCone(true);
    }, PEEK_CONE_MS);
  }

  /** The pointer left the cone — onto the card, back to the rail, or away.
   *  `settle` re-arms whatever the pointer rests on: a swap to the ring under
   *  it, else the grace close. */
  leaveCone(settle = true): void {
    if (this.coneTimer) this.timers.clear(this.coneTimer);
    this.coneTimer = 0;
    if (!this.inCone) return;
    this.inCone = false;
    if (!settle) return;
    if (this.pointerKey !== null && this.pointerKey !== this.shown && this.pendingKey !== this.pointerKey) {
      this.enterRing(this.pointerKey, "pointer");
      return;
    }
    this.scheduleClose();
  }

  /** The pointer left the card. */
  leaveCard(): void {
    this.overCard = false;
    this.scheduleClose();
  }

  /** The card holds (`true`) or lost (`false`) the keyboard focus. Held, it
   *  never closes on a pointer leave. */
  hold(on: boolean): void {
    this.held = on;
    if (on) this.cancelClose();
    else this.scheduleClose();
  }

  /** The keyboard was HANDED to ring `key` (fav-peek-store `focusRail`, a
   *  card shutting under it): it rests there, muted like Escape's return —
   *  no card opens until the pointer arrives or the focus moves on. */
  quietFocus(key: string): void {
    this.focusKey = key;
    this.muteFocus = true;
    if (this.pendingKey === key) this.cancelOpen();
  }

  /** Chat `key` went elsewhere (the favourite's card took it): a hover or a
   *  focus still waiting to open it here is dropped — else this card opens
   *  on the same chat a beat after the favourite's (two composers, one
   *  draft). A card already showing it is the caller's to dismiss. */
  forget(key: string): void {
    if (this.pendingKey === key) this.cancelOpen();
  }

  /** Show `key` now, no wait — the keyboard's way in (ArrowLeft on a ring).
   *  `pin`: the card stays up until dismissed (`press`). */
  showNow(key: string, pin = false): void {
    this.setPinned(pin);
    this.muteFocus = false;
    this.cancelOpen();
    this.cancelClose();
    this.sink.warm(key);
    this.set(key);
  }

  /** A press on ring `key`: show it now, pinned — over a
   *  hover card of the same ring, it only pins; over another ring's card, it
   *  swaps without a close. A press never closes the card (owner,
   *  2026-10-31: it "stays unless I click outside, or click on cross or
   *  click open"). */
  press(key: string): void {
    this.showNow(key, true);
  }

  /** Close now: Escape, a press outside, the pane opening. `returningFocus`
   *  says the host is about to put the focus back on the ring (Escape, the
   *  close button) — that focus must not reopen the card. */
  dismiss(returningFocus = false): void {
    this.cancelOpen();
    this.cancelClose();
    this.leaveCone(false);
    this.held = false;
    this.pressing = false;
    this.setPinned(false);
    // The card goes pointer-events: none under the pointer; no leave fires.
    this.overCard = false;
    this.muteFocus = returningFocus;
    this.set(null);
  }

  /** Stop every timer (unmount). */
  dispose(): void {
    this.cancelOpen();
    this.cancelClose();
    this.leaveCone(false);
  }

  private setPinned(on: boolean): void {
    if (this.pinned === on) return;
    this.pinned = on;
    this.sink.pin?.(on);
  }

  private set(key: string | null): void {
    if (this.shown === key) return;
    this.shown = key;
    this.sink.show(key);
  }

  private get kept(): boolean {
    // The focus holds only the card it is FOR: a ring Escape handed the focus
    // back to (muted) must not hold another ring's hover card open.
    const focusHolds = this.focusKey !== null && this.focusKey === this.shown;
    return (
      this.pinned || this.pointerKey !== null || focusHolds || this.overCard || this.inCone || this.held || this.pressing
    );
  }

  private scheduleClose(): void {
    if (this.kept || this.shown === null) return;
    this.cancelClose();
    this.closeTimer = this.timers.set(() => {
      this.closeTimer = 0;
      if (this.kept) return;
      this.set(null);
    }, PEEK_CLOSE_MS);
  }

  private cancelOpen(): void {
    if (this.openTimer) this.timers.clear(this.openTimer);
    if (this.warmTimer) this.timers.clear(this.warmTimer);
    this.openTimer = 0;
    this.warmTimer = 0;
    this.pendingKey = null;
  }

  private cancelClose(): void {
    if (this.closeTimer) this.timers.clear(this.closeTimer);
    this.closeTimer = 0;
  }
}

// ---- THE STAR'S ANSWER ------------------------------------------------------

/** THE STAR SHOWS THE ANSWER AT ONCE (RailPeek `starMark`): the server's
 *  answer for one key, held over the pulse's `starred` until the pulse
 *  agrees — or until it has had its chance to. `pulse` is the pulse row the
 *  mark last saw for its key, `seen` how many DISTINCT pulse rows have come
 *  since it was set: the first may have left before the write landed, the
 *  second cannot have — so a pulse that still disagrees then is not lagging,
 *  another window flipped the star, and the pulse is the truth. */
export interface StarMark {
  key: string;
  on: boolean;
  pulse: TaskPulseTask;
  seen: number;
  /** Which press set it (RailPeek's timeout fallback is per press). */
  seq: number;
}

/** How long a mark may stand at most, pulses or none (a pulse loop that
 *  stalls must not hold a star the server no longer agrees with). */
export const STAR_MARK_MS = 6000;

/** The mark after the pulse row `t` for its key (undefined: the row left):
 *  null when the pulse agrees, the row is gone, or this is the second
 *  distinct pulse row since the mark was set (header); the same mark for the
 *  same row; else the mark with this row counted. */
export function starMarkStep(mark: StarMark, t: TaskPulseTask | undefined): StarMark | null {
  if (!t || !!t.starred === mark.on) return null;
  if (t === mark.pulse) return mark;
  if (mark.seen >= 1) return null;
  return { ...mark, pulse: t, seen: mark.seen + 1 };
}

/** WINDOW B'S HOVER ROW (FavouritePeek hover mode; owner, 2026-11-05): the
 *  pulse row of the hovered favourite — only while NO favourite's card is
 *  open in this window (an open card is pressed, and its other rings speak
 *  by their caption alone), only for a live star with a conversation. */
export function favHoverRow(
  hoverKey: string | null,
  openKey: string | null,
  rows: readonly TaskPulseTask[],
): TaskPulseTask | null {
  if (!hoverKey || openKey) return null;
  const t = rows.find((r) => r.key === hoverKey);
  return t && t.starred && t.status !== "archived" && t.session_id ? t : null;
}

/** What a card does once the server has answered its star (RailPeek
 *  `toggleStar`): the rail hears it (`onStarred`), and an UNSTAR shuts the
 *  card only where the card is the favourite's own (`dismissOnUnstar`,
 *  FavouritePeek) and still on that chat. The rail's card may show a
 *  favourite on a hover of its ring (2026-11-04) and unstar it there; that
 *  card stays — the chat is back in the list. */
export function starOutcome(starred: boolean, key: string, shownKey: string | null, dismissOnUnstar: boolean): { dismiss: boolean } {
  return { dismiss: !starred && dismissOnUnstar && shownKey === key };
}

/** The rail's side of a star (ClaudeRail `onStarred`): with favourites on,
 *  a starred chat leaves the list and lives on its ring in the favourites
 *  section — no duplicate — and a star you just pressed is a card you want
 *  to see, so its favourite's card opens IN THIS WINDOW (`open`, the
 *  per-window open key) and the rail's own card lets go of it AT ONCE,
 *  before the favourite's composer mounts, so one draft never has two
 *  composers. */
export function railOnStarred(
  favsOn: () => boolean,
  letGo: (key: string) => void,
  open: (key: string) => void = () => {},
): (key: string, on: boolean) => void {
  return (key, on) => {
    if (!on || !favsOn()) return;
    letGo(key);
    open(key);
  };
}

/** The rail's side of a WHERE-CHIP LEAVING THE PAGE (ClaudeRail
 *  `onChipLeave`): the rail's card, showing a live favourite's chat (a hover
 *  on its ring), hands it to the favourite's card in this window as the hop
 *  goes (`onHop`: only once the router's leave guard let it) — `letGo` first
 *  (the draft flushed, one composer per draft), then `open` (the per-window
 *  open key, sessionStorage: it survives the hop), told whether the press
 *  was the keyboard's (the favourite's card then takes the focus on arrival,
 *  as a ring's keyboard press does). Anything else (favourites off, a list
 *  chat, a favourite with no session) just goes. */
export function railOnChipLeave(
  favsOn: () => boolean,
  liveFav: (key: string) => boolean,
  letGo: (key: string) => void,
  open: (key: string, keyboard: boolean) => void,
): (key: string, keyboard?: boolean) => void {
  return (key, keyboard = false) => {
    if (!favsOn() || !liveFav(key)) return;
    letGo(key);
    open(key, keyboard);
  };
}

/** What `onHop` listens on (the window). */
export interface HopTarget {
  addEventListener(type: string, fn: () => void): void;
  removeEventListener(type: string, fn: () => void): void;
}

/** THE HOP, ONLY WHEN IT HAPPENS: `go` starts a navigation the router's
 *  leave guard may refuse (router `guarded`: a question first, the push only
 *  on a yes) and settles once it is decided (router `navigateUrl`: `true`
 *  pushed, `false` refused). `then` runs once the hop is real — on the
 *  router's navigate `event`, which the router dispatches synchronously
 *  inside the push (router `navigateUrl` → `notifyNavigate`, after
 *  `pushHistory`), so with no guard it runs before `go` returns, and on a
 *  guard's yes as the answer lands, before the page repaints. The listener
 *  lives exactly as long as the question: once `go` settles (the push, if
 *  any, has fired the event by then) it is removed — a refused hop hands
 *  nothing over, and never answers a later navigation either (Bugbot on
 *  #2122: a stale listener used to run its hand-over on the next hop to the
 *  same page, one more per refused press). `arrived` double-checks the
 *  event was this hop's. */
export function onHop(
  win: HopTarget,
  event: string,
  arrived: () => boolean,
  then: () => void,
  go: () => Promise<boolean>,
): void {
  const heard = () => {
    win.removeEventListener(event, heard);
    if (arrived()) then();
  };
  win.addEventListener(event, heard);
  const drop = () => win.removeEventListener(event, heard);
  go().then(drop, drop);
}

/** The words a refused favourite write says: the server's own `detail` (a
 *  FastAPI refusal, which the shared fetch helpers' message reads as a bare
 *  "HTTP 409"), else the error's message, else `fallback`. */
export function errorWords(e: unknown, fallback: string): string {
  const body = (e as { body?: unknown } | null)?.body;
  const detail = body && typeof body === "object" ? (body as { detail?: unknown }).detail : undefined;
  if (typeof detail === "string" && detail) return detail;
  const message = (e as Error | null)?.message;
  return message || fallback;
}

// ---- THE OPEN FAVOURITE, PER WINDOW ------------------------------------------

/** How long a favourite this document just opened (a star pressed on, whose
 *  `starred` the pulse has yet to carry — seconds on a loaded Mac) may stand
 *  before the pulse has said it is a favourite. */
export const FAV_SETTLE_MS = 15_000;

/** WHAT THE OPEN FAVOURITE'S CARD DOES (FavouritePeek; the open key is this
 *  window's own, platform/lib/last-chat `useFavOpen`): `"show"` it, `"wait"`
 *  for the pulse (no card yet, the key kept), or `"close"` — the key cleared.
 *  `row` is the pulse's row for the key (undefined: none), `loaded` whether
 *  the pulse has any rows yet, `seenLive` whether it has said this key is a
 *  live favourite since the key was opened, `openedAt` when THIS document
 *  opened it (null: a stored key from before a reload, or another document
 *  of the window). In order:
 *    - an ARCHIVED row never shows a card: close;
 *    - a starred row shows;
 *    - a key the pulse has said live and now does not (unstarred, erased —
 *      here or in another window): close;
 *    - a key just opened here, the pulse not caught up yet: show it if the
 *      row is there (a star pressed a moment ago), else wait — for at most
 *      FAV_SETTLE_MS;
 *    - otherwise wait for a first pulse, and close once it has come. */
export function favOpenVerdict(
  row: { starred?: boolean; status: string } | undefined,
  loaded: boolean,
  seenLive: boolean,
  openedAt: number | null,
  now: number,
): "show" | "wait" | "close" {
  if (row?.status === "archived") return "close";
  if (row?.starred) return "show";
  if (seenLive) return "close";
  if (openedAt !== null && now - openedAt < FAV_SETTLE_MS) return row ? "show" : "wait";
  return loaded ? "close" : "wait";
}

/** THE OPEN FAVOURITE AS THE RAIL COUNTS IT (ClaudeRail): this window's open
 *  key (`openKey`) when the pulse says it is a favourite (`favKeys`) — and,
 *  for FAV_SETTLE_MS after this window opened it (`openedAt`: a star pressed
 *  in the rail's card a moment ago, the pulse yet to say `starred`), even
 *  though its ring still stands in the LIST: the favourite's card is already
 *  up on it (FavouritePeek shows a just-opened key), so that ring must not
 *  open the rail's hover card on the same chat — two composers on one draft.
 *  Null otherwise (a key the pulse dropped, or one the settle ran out on). */
export function railOpenFav(
  openKey: string | null,
  favKeys: ReadonlySet<string>,
  openedAt: number | null,
  now: number,
): string | null {
  if (!openKey) return null;
  if (favKeys.has(openKey)) return openKey;
  return openedAt !== null && now - openedAt < FAV_SETTLE_MS ? openKey : null;
}

/** Where the favourite's chat already is, so its card keeps none of its own
 *  (one chat, one composer): `"pane"` when a mounted pane chat says it shows
 *  its session (`paneSessions`, platform/lib/peek-session — the Claude pane
 *  open on it, the explorer's sidebar), `"peek"` when the Tasks side peek
 *  shows it, else null. ONLY a mounted composer counts: a URL still naming
 *  the session with the pane shut shows it nowhere, and the card keeps it. */
export function favChatElsewhere(
  fav: { key: string; session_id?: string | null } | null,
  paneSessions: readonly string[],
  peekSession: string | null,
): "pane" | "peek" | null {
  if (!fav) return null;
  const names = (sid: string | null) => !!sid && (sid === fav.session_id || sid === fav.key);
  if (paneSessions.some(names)) return "pane";
  if (names(peekSession)) return "peek";
  return null;
}

/** WHAT A FAVOURITE'S RING DOES (ClaudeRail; owner, 2026-11-05: a card with
 *  only a note must never show): `"pane"` when the pane's composer already
 *  holds its chat (a press focuses that composer, no card, the hover shows
 *  the title only), `"door"` when the Tasks peek holds it (a press opens the
 *  chat, no card), else `"card"` (window B as always). */
export function favRingRoute(
  fav: { key: string; session_id?: string | null } | null,
  paneSessions: readonly string[],
  peekSession: string | null,
): "pane" | "door" | "card" {
  const where = favChatElsewhere(fav, paneSessions, peekSession);
  return where === "pane" ? "pane" : where === "peek" ? "door" : "card";
}

/** THE OPEN KEYS A FAVOURITE'S PRESS LETS GO (ClaudeRail): a press that goes
 *  to the pane or the door opens no card, and window B open on ANOTHER
 *  favourite would show the wrong task beside the pane — so both the pressed
 *  key and the open one are cleared. A card press clears nothing here. */
export function favPressClears(route: "pane" | "door" | "card", pressed: string, open: string | null): string[] {
  if (route === "card") return [];
  return open && open !== pressed ? [pressed, open] : [pressed];
}

/** THE ROW THE RAIL STANDS IN — the box the card is placed against and
 *  watched for resizes: the rail's nearest ancestor that HAS a box. The
 *  explorer's file view portals the rail into `.stat-side-slot`, which is
 *  `display: contents` (explorer.css) and so measures as nothing; the row
 *  there is the split behind it. Shared by RailPeek (placement) and
 *  FavouritePeek (its no-room reset: a sidebar folding widens the row, not
 *  the 48px rail). */
export function rowOf(rail: HTMLElement | null): HTMLElement | null {
  let el = rail?.parentElement ?? null;
  while (el && getComputedStyle(el).display === "contents") el = el.parentElement;
  return el;
}

/** THE RAIL BESIDE AN OPEN PANE (ClaudeRail `paneOpen`; owner, 2026-11-05):
 *  how much of `split`'s right edge a standing rail takes — 0 with none, or
 *  a hidden one. A pane's seam drag measures the pane as the distance from
 *  the pointer to the split's right edge; with the rail at that edge the pane
 *  ends 48px short of it, and the seam would trail the pointer by the rail.
 *  Only a rail AT the right edge counts (the explorer portals its rail into a
 *  `display: contents` slot, so it is a descendant, not always a child). */
export function railInset(split: Element | null | undefined): number {
  // A stand-in with no DOM behind it (a test's rect-only split) has no rail.
  if (!split || typeof split.querySelectorAll !== "function") return 0;
  const right = split.getBoundingClientRect().right;
  for (const rail of split.querySelectorAll<HTMLElement>(".claude-rail")) {
    if (rail.hidden) continue;
    const box = rail.getBoundingClientRect();
    if (box.width > 0 && Math.abs(box.right - right) <= 1) return box.width;
  }
  return 0;
}
