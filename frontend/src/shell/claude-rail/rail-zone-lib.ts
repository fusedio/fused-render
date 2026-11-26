// THE RAIL'S COLD AND HOT ZONES (owner, 2026-11-25: "the rail blocks
// stuff"): a pointer crossing INTO the rail from the page — on its way to
// the window's edge, a scrollbar, or overshooting — must not pop a ring's
// card over what the reader was looking at; a pointer already IN the rail,
// moving ring to ring, must not wait. The parts that need no DOM (rail-zone.ts
// feeds the pointer in; every door's hover machine asks it how long to wait).
//
//   - COLD (the pointer came from outside): a ring's card waits until the
//     pointer has been in the zone RAIL_COLD_OPEN_MS — anywhere in it, a ring
//     or the rail's empty area (owner, 2026-11-25: "if I am on the rail for
//     more than the threshold, even not on a button, it should be hot"). A
//     pointer that crosses the rail faster than that opens nothing.
//   - HOT (the pointer has been in the zone RAIL_COLD_OPEN_MS, a card has
//     shown while it is in the zone, or a press inside it): every door's
//     card shows at once (RAIL_HOT_OPEN_MS) — the list's rings, the
//     favourites, the bots, the doors, the +, a draft.
//   - THE ZONE is the rail and its own cards (rail-zone `RAIL_ZONE_SELECTOR`):
//     reading a card and coming back to a ring is still "in the rail".
//   - LEAVING keeps the heat for RAIL_COOL_MS: a brief overshoot off the
//     zone and back stays hot; a longer absence is cold again. A cold leave
//     keeps nothing.
//   - THE KEYBOARD is not a pointer crossing anything: a focus keeps its own
//     timing (`HoverPace` is asked for pointer arrivals only).

/** In the zone this long, the rail is hot (a cold ring's card waits out
 *  what is left of it). 500 ms (owner, 2026-11-26: "increase from 300 to
 *  500"). */
export const RAIL_COLD_OPEN_MS = 500;
/** A hot ring's card: no wait. */
export const RAIL_HOT_OPEN_MS = 0;
/** Off the zone this long, a hot rail is cold again. */
export const RAIL_COOL_MS = 300;

/** What a door's hover machine asks before it waits on a pointer (the
 *  rail-peek-lib `PeekIntent`s, plus-pop-lib `PlusPopIntent`). */
export interface HoverPace {
  /** How long a pointer resting on a ring waits before its card shows;
   *  `usual` is the machine's own wait, kept where no zone is watched. */
  openMs(usual: number): number;
  /** A card showed. */
  shown(): void;
}

/** No zone: every machine keeps its own wait. */
export const usualPace: HoverPace = { openMs: (ms) => ms, shown: () => {} };

/** The zone's state machine (header). `now` is a clock in ms. */
export class RailZone {
  private inside = false;
  /** Inside: the rail is hot. */
  private hot = false;
  /** Outside: when a HOT pointer left (null: it left cold, or never was). */
  private hotLeftAt: number | null = null;
  /** Inside: when the pointer came in. */
  private enteredAt = 0;

  constructor(private readonly now: () => number) {}

  /** The pointer is in the zone. */
  get isInside(): boolean {
    return this.inside;
  }

  /** The rail is hot (header) — inside, or within the cool-off. */
  get isHot(): boolean {
    if (this.inside) return this.hot || this.dwelt() >= RAIL_COLD_OPEN_MS;
    return this.hotLeftAt !== null && this.now() - this.hotLeftAt < RAIL_COOL_MS;
  }

  /** The pointer arrived in the zone (from outside). */
  enter(): void {
    if (this.inside) return;
    this.hot = this.isHot;
    this.inside = true;
    this.hotLeftAt = null;
    this.enteredAt = this.now();
  }

  /** Inside: how long the pointer has been in the zone. */
  private dwelt(): number {
    return this.inside ? this.now() - this.enteredAt : 0;
  }

  /** The pointer left the zone (the page, another frame, the window). */
  leave(): void {
    if (!this.inside) return;
    this.hotLeftAt = this.isHot ? this.now() : null;
    this.inside = false;
    this.hot = false;
  }

  /** A card showed, or a press landed in the zone: hot — if the pointer is
   *  here (a card the keyboard opened from outside heats nothing). */
  heat(): void {
    if (this.inside) this.hot = true;
  }

  /** How long a ring's card waits for the pointer resting on it. A ring's
   *  arrival proves the pointer is in the zone, whatever the watcher saw. */
  openMs(): number {
    this.enter();
    if (this.isHot) return RAIL_HOT_OPEN_MS;
    return Math.ceil(RAIL_COLD_OPEN_MS - this.dwelt());
  }

  /** Forget everything (tests). */
  reset(): void {
    this.inside = false;
    this.hot = false;
    this.hotLeftAt = null;
    this.enteredAt = 0;
  }
}
