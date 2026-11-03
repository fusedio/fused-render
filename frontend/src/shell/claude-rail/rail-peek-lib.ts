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
//     corners (RailPeek, ui-layout `canvasRailPeek`), both clamped to the
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
 *  grips): room for the head's seats and a usable composer. */
export const PEEK_MIN_WIDTH = 280;
export const PEEK_MIN_HEIGHT = 240;
/** …and no wider than the chat's own reading column (owner, 2026-11-02:
 *  "max width in resize should be the max width I can have for Claude chat
 *  … after which we show spaces left and right"): the transcript and the
 *  composer are capped at 720px (transcript.css, composer.css
 *  `.c-composer-chat`), so a card past 720 + its two 1px borders only adds
 *  gutters. The height has no such cap: the row's is the limit. */
export const PEEK_MAX_WIDTH = 722;
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
  if (width < 120 || height < 120) return null;
  const left = rail.left - PEEK_GAP - width;
  const minTop = bounds.top + PEEK_INSET;
  const maxTop = bounds.top + bounds.height - PEEK_INSET - height;
  const top = Math.max(minTop, Math.min(maxTop, anchor.top));
  return { left: Math.round(left), top: Math.round(top), width: Math.round(width), height: Math.round(height) };
}

/** The box a corner drag asks for: `corner` is which of the card's LEFT
 *  corners is held (the right edge stands against the rail), `start` the box
 *  when the pointer went down, `dx`/`dy` how far it has moved since. The
 *  width and height are clamped to the minimums and to the row: a top-left
 *  drag keeps the bottom edge where it was, a bottom-left drag the top. */
export function dragResize(corner: "tl" | "bl", start: Box, dx: number, dy: number, bounds: Box): Box {
  // The row wins over the minimums: a row with less room than PEEK_MIN_*
  // (peekPlacement accepts down to 120) must not be pushed past its inset.
  const clamp = (want: number, min: number, max: number) => Math.round(Math.min(Math.max(want, Math.min(min, max)), max));
  const right = start.left + start.width;
  const maxWidth = Math.min(PEEK_MAX_WIDTH, right - (bounds.left + PEEK_INSET));
  const width = clamp(start.width - dx, PEEK_MIN_WIDTH, maxWidth);
  const minTop = bounds.top + PEEK_INSET;
  const maxBottom = bounds.top + bounds.height - PEEK_INSET;
  let top: number;
  let height: number;
  if (corner === "tl") {
    const bottom = start.top + start.height;
    height = clamp(start.height - dy, PEEK_MIN_HEIGHT, bottom - minTop);
    top = bottom - height;
  } else {
    height = clamp(start.height + dy, PEEK_MIN_HEIGHT, maxBottom - start.top);
    top = start.top;
  }
  return { left: Math.round(right - width), top: Math.round(top), width, height };
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

  /** The pointer (or the keyboard focus) arrived on ring `key`. */
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
    if (via === "pointer") this.pointerKey = null;
    else {
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
