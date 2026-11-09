// THE NEW TASK POPOVER'S DOORS: HOVER PREVIEWS, CLICK PINS (owner,
// 2026-11-07: "hover on plus icon should show the card as well, and click
// does cement it unless you click on the cross icon. hover gives you quick
// preview" — and a draft's ring "behaves exactly like the +", on that
// draft's record). The rail's chat-ring card's rules (rail-peek-lib
// `PeekIntent`), its timings, for the popover's doors — the parts that need
// no DOM. A DOOR is the + (`PLUS_DOOR`) or a draft ring's key.
//
//   - A HOVER: the pointer resting on a door starts reading what its card
//     needs (`seed`: the schedule; a draft's fresh record too) at once and
//     shows the card UNPINNED after PEEK_OPEN_MS — both must be done.
//     Leaving the door first opens nothing and drops the read
//     (`cancelSeed`). A read that finds nothing (`failed`) shows nothing.
//   - THE GRACE: an unpinned card closes once the pointer has been off every
//     door and the card for PEEK_CLOSE_MS — long enough to cross the gap.
//     A hover on ANOTHER door swaps the card once that door's card is ready.
//   - A PRESS PINS: on a door (its preview pins in place, no re-read; any
//     other door's card opens pinned as soon as its read is in, replacing
//     what is up; a pinned card's own door keeps it), or anything the reader
//     does inside the card (`pinInside`: a press, the focus on a field). A
//     pinned card closes by its × only — the chassis (Modal
//     `popover.pinned`) ignores a press outside and Escape — and a hover on
//     another door shows nothing over it.
//   - The card closing by ANY path (×, Create, Escape or a press outside a
//     preview, the grace) is reported back (`closed`).
import { PEEK_CLOSE_MS, PEEK_OPEN_MS, type Timers } from "./rail-peek-lib";

/** The +'s own door name; a draft's door is its ring's key. */
export const PLUS_DOOR = "+";

export type PlusPhase = "closed" | "pending" | "preview" | "pinned";

export interface PlusPopSink {
  /** Start reading what `door`'s card needs; answer with `seeded(door)` or
   *  `failed(door)`. */
  seed(door: string): void;
  /** The read started by `seed` is no longer wanted. */
  cancelSeed(): void;
  /** Show `door`'s card, pinned or as a preview (replacing what is up). */
  open(door: string, pinned: boolean): void;
  /** The preview that is up became pinned. */
  pin(): void;
  /** Close the preview that is up (the chassis' own way, with its fade). */
  close(): void;
}

const windowTimers: Timers = {
  set: (fn, ms) => window.setTimeout(fn, ms),
  clear: (id) => window.clearTimeout(id),
};

export class PlusPopIntent {
  /** The door whose card is up, and whether it is pinned. */
  private shown: string | null = null;
  private pinnedNow = false;
  /** The door whose card is being readied. */
  private pending: string | null = null;
  /** Its read is in. */
  private ready = false;
  /** Its hover delay has run (or a press made it moot). */
  private due = false;
  /** A press is waiting on its read: open pinned. */
  private wantPin = false;
  private overDoor: string | null = null;
  private overCard = false;
  private openTimer = 0;
  private closeTimer = 0;

  constructor(
    private readonly sink: PlusPopSink,
    private readonly timers: Timers = windowTimers,
  ) {}

  /** What is up (or coming): the card's phase. */
  get state(): PlusPhase {
    if (this.shown !== null) return this.pinnedNow ? "pinned" : "preview";
    return this.pending !== null ? "pending" : "closed";
  }

  /** The door whose card is up, if any. */
  get current(): string | null {
    return this.shown;
  }

  /** The pointer arrived on a door. */
  enterPlus(door: string = PLUS_DOOR): void {
    this.overDoor = door;
    this.cancelClose();
    // A pinned card: a hover shows nothing over it (header).
    if (this.shown !== null && this.pinnedNow) return;
    if (this.shown === door) {
      this.cancelPending();
      return;
    }
    if (this.pending === door) return;
    this.cancelPending();
    this.begin(door, false);
    this.openTimer = this.timers.set(() => {
      this.openTimer = 0;
      this.due = true;
      this.maybeOpen();
    }, PEEK_OPEN_MS);
  }

  /** The pointer left the door it was on. */
  leavePlus(): void {
    this.overDoor = null;
    if (this.pending !== null && !this.wantPin) this.cancelPending();
    if (this.shown !== null && !this.pinnedNow) this.scheduleClose();
  }

  /** The pointer arrived on the card. */
  enterCard(): void {
    this.overCard = true;
    this.cancelClose();
  }

  /** The pointer left the card. */
  leaveCard(): void {
    this.overCard = false;
    if (this.shown !== null && !this.pinnedNow) this.scheduleClose();
  }

  /** A door pressed (a click, Enter, Space). */
  press(door: string = PLUS_DOOR): void {
    if (this.shown === door) {
      this.cancelPending();
      if (!this.pinnedNow) this.pinNow();
      return;
    }
    if (this.pending === door) {
      this.wantPin = true;
      this.due = true;
      this.clearOpen();
      this.maybeOpen();
      return;
    }
    this.cancelPending();
    this.begin(door, true);
  }

  /** ANOTHER WINDOW A CARD WAS PINNED (ClaudeRail; CodeRabbit on #2189): a
   *  hover's read still pending on a door must not open over it when its
   *  delay runs out — the pointer may rest on the + while an Enter pins the
   *  Tasks card. A press waiting on its read (`wantPin`) is a press: kept. */
  cancelPreview(): void {
    if (this.pending !== null && !this.wantPin) this.cancelPending();
  }

  /** The reader acted inside the card (a press, the focus on a field). */
  pinInside(): void {
    if (this.shown !== null && !this.pinnedNow) this.pinNow();
  }

  /** The read `seed(door)` started is in. */
  seeded(door: string): void {
    if (this.pending !== door) return;
    this.ready = true;
    this.maybeOpen();
  }

  /** The read `seed(door)` started found nothing to show: no card. Says
   *  whether a press was waiting on it (the caller's fallback door). */
  failed(door: string): boolean {
    if (this.pending !== door) return false;
    const pressed = this.wantPin;
    this.clearPending();
    return pressed;
  }

  /** The card closed, whichever way. */
  closed(): void {
    this.cancelClose();
    this.shown = null;
    this.pinnedNow = false;
    this.overCard = false;
  }

  /** The rail unmounts. */
  /** A card already up when this intent was made — one that came across a
   *  page hop (plus-card-store): this intent takes it as shown. */
  adopt(door: string, pinned: boolean): void {
    this.cancelPending();
    this.cancelClose();
    this.shown = door;
    this.pinnedNow = pinned;
  }

  dispose(): void {
    this.clearOpen();
    this.cancelClose();
  }

  private begin(door: string, pinned: boolean): void {
    this.pending = door;
    this.ready = false;
    this.due = pinned;
    this.wantPin = pinned;
    this.sink.seed(door);
  }

  private maybeOpen(): void {
    const door = this.pending;
    if (door === null || !this.ready || !this.due) return;
    const pinned = this.wantPin;
    this.clearPending();
    this.cancelClose();
    this.shown = door;
    this.pinnedNow = pinned;
    this.sink.open(door, pinned);
    if (!pinned) this.scheduleClose();
  }

  private pinNow(): void {
    this.cancelClose();
    this.pinnedNow = true;
    this.sink.pin();
  }

  private cancelPending(): void {
    if (this.pending === null) return;
    this.clearPending();
    this.sink.cancelSeed();
  }

  private clearPending(): void {
    this.clearOpen();
    this.pending = null;
    this.ready = false;
    this.due = false;
    this.wantPin = false;
  }

  private scheduleClose(): void {
    // Any door hovered keeps the card up: its own, or another's on its way.
    if (this.overDoor !== null || this.overCard) return;
    this.cancelClose();
    this.closeTimer = this.timers.set(() => {
      this.closeTimer = 0;
      if (this.shown === null || this.pinnedNow || this.overDoor !== null || this.overCard) return;
      this.sink.close();
    }, PEEK_CLOSE_MS);
  }

  private clearOpen(): void {
    if (this.openTimer) this.timers.clear(this.openTimer);
    this.openTimer = 0;
  }

  private cancelClose(): void {
    if (this.closeTimer) this.timers.clear(this.closeTimer);
    this.closeTimer = 0;
  }
}
