// THE RAIL'S HOVER CARD, the parts that need no DOM (RailPeek.tsx draws it;
// ClaudeRail.tsx wires the rings). Behind `canvas_rail_peek_enabled`
// (rail-peek-flag.ts).
//
// Three rulings live here so they can be proved without a browser:
//
//   - WHERE THE CARD STANDS (`peekPlacement`): beside the rail, its right edge
//     a gap off the rail's left edge, centred on the ring under the pointer
//     and clamped inside the workspace row — so a ring at the top of a short
//     window gets a card that starts at the row's top, not one hanging off
//     the window. Never wider than the row leaves it.
//   - WHEN IT OPENS AND CLOSES (`PeekIntent`): a hover is not a click. The
//     card waits PEEK_OPEN_MS before it shows, so a pointer crossing the rail
//     on its way somewhere opens nothing; once up, moving to another ring
//     swaps the chat after a shorter PEEK_SWAP_MS without closing and
//     reopening; leaving both the ring and the card gives PEEK_CLOSE_MS of
//     grace, long enough to cross the gap between them; and a card the reader
//     is USING — one that holds the keyboard focus, because they are typing —
//     does not close on a pointer leave at all. Escape, a press outside, or
//     the pane opening dismiss it at once. The pointer and the keyboard focus
//     are tracked apart: a ring that loses focus while the pointer still
//     rests on it is still hovered.
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
/** …and for a seat with no conversation (a draft, a scheduled task): what it
 *  is, its words and its door need no transcript's worth of card. */
export const PEEK_PLAIN_HEIGHT = 220;
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

/** Where the card stands for a ring at `anchor`, beside a rail at `rail`,
 *  inside the workspace row `bounds` (all viewport rects). Null when the row
 *  leaves no room at all. */
export function peekPlacement(anchor: Box, rail: Box, bounds: Box, maxHeight = PEEK_MAX_HEIGHT): Box | null {
  const room = rail.left - bounds.left - PEEK_GAP - PEEK_INSET;
  const width = Math.min(PEEK_WIDTH, room);
  const height = Math.min(maxHeight, bounds.height - 2 * PEEK_INSET);
  if (width < 120 || height < 120) return null;
  const left = rail.left - PEEK_GAP - width;
  const centre = anchor.top + anchor.height / 2;
  const minTop = bounds.top + PEEK_INSET;
  const maxTop = bounds.top + bounds.height - PEEK_INSET - height;
  const top = Math.min(maxTop, Math.max(minTop, centre - height / 2));
  return { left: Math.round(left), top: Math.round(top), width: Math.round(width), height: Math.round(height) };
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
  const [, top, bottom] = safeConePoints(apex, card, pad);
  return pointInTriangle(p, apex, top, bottom);
}

/** The cone's three corners — apex, then the card edge's padded top and
 *  bottom — the one place its shape is written (the hit test above and the
 *  debug overlay in RailPeek both read it). */
export function safeConePoints(apex: Point, card: Box, pad = PEEK_CONE_PAD): [Point, Point, Point] {
  const edgeX = card.left + card.width;
  return [apex, { x: edgeX, y: card.top - pad }, { x: edgeX, y: card.top + card.height + pad }];
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

  constructor(
    private readonly sink: IntentSink,
    private readonly timers: Timers = windowTimers,
  ) {}

  /** What the card shows now, if anything. */
  get current(): string | null {
    return this.shown;
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

  /** A pointer went down inside the card: hold it through the gesture. */
  pressStart(): void {
    this.pressing = true;
    this.cancelClose();
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

  /** Show `key` now, no wait — the keyboard's way in (ArrowLeft on a ring). */
  showNow(key: string): void {
    this.muteFocus = false;
    this.cancelOpen();
    this.cancelClose();
    this.sink.warm(key);
    this.set(key);
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

  private get overRing(): boolean {
    return this.pointerKey !== null || this.focusKey !== null;
  }

  private set(key: string | null): void {
    if (this.shown === key) return;
    this.shown = key;
    this.sink.show(key);
  }

  private get kept(): boolean {
    return this.overRing || this.overCard || this.inCone || this.held || this.pressing;
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
