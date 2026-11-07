// THE +'s NEW TASK CARD: HOVER PREVIEWS, CLICK PINS (owner, 2026-11-07:
// "hover on plus icon should show the card as well, and click does cement it
// unless you click on the cross icon. hover gives you quick preview"). The
// rail's chat-ring card's rules (rail-peek-lib `PeekIntent`), its timings,
// for the + — the parts that need no DOM.
//
//   - A HOVER: the pointer resting on the + starts reading what the card needs
//     (`seed`, the schedule) at once and shows the card UNPINNED after
//     PEEK_OPEN_MS — both must be done. Leaving the + first opens nothing and
//     drops the read (`cancelSeed`).
//   - THE GRACE: an unpinned card closes once the pointer has been off both
//     the + and the card for PEEK_CLOSE_MS — long enough to cross the gap
//     between them.
//   - A PRESS PINS: on the + (a preview pins in place, no re-read; nothing up
//     opens pinned as soon as the read is in; a pinned card stays as it is),
//     or anything the reader does inside the card (`pinInside`: a press, the
//     focus on a field). A pinned card closes by its × only — the chassis
//     (Modal `popover.pinned`) ignores a press outside and Escape.
//   - A DRAFT'S CARD (a draft ring's press) is the same popover, pinned:
//     `adopt` takes it as the +'s own.
//   - The card closing by ANY path (×, Create, Escape or a press outside a
//     preview, the grace) is reported back (`closed`), and the + starts over.
import { PEEK_CLOSE_MS, PEEK_OPEN_MS, type Timers } from "./rail-peek-lib";

export type PlusPhase = "closed" | "pending" | "preview" | "pinned";

export interface PlusPopSink {
  /** Start reading what the card needs; answer with `seeded()`. */
  seed(): void;
  /** The read started by `seed` is no longer wanted. */
  cancelSeed(): void;
  /** Show the card, pinned or as a preview. */
  open(pinned: boolean): void;
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
  private phase: PlusPhase = "closed";
  /** The read is in. */
  private ready = false;
  /** The hover delay has run (or a press made it moot). */
  private due = false;
  /** A press is waiting on the read: open pinned. */
  private wantPin = false;
  private overPlus = false;
  private overCard = false;
  private openTimer = 0;
  private closeTimer = 0;

  constructor(
    private readonly sink: PlusPopSink,
    private readonly timers: Timers = windowTimers,
  ) {}

  get state(): PlusPhase {
    return this.phase;
  }

  /** The pointer arrived on the +. */
  enterPlus(): void {
    this.overPlus = true;
    this.cancelClose();
    if (this.phase !== "closed") return;
    this.begin(false);
    this.openTimer = this.timers.set(() => {
      this.openTimer = 0;
      this.due = true;
      this.maybeOpen();
    }, PEEK_OPEN_MS);
  }

  /** The pointer left the +. */
  leavePlus(): void {
    this.overPlus = false;
    if (this.phase === "pending" && !this.wantPin) {
      this.reset();
      this.sink.cancelSeed();
      return;
    }
    if (this.phase === "preview") this.scheduleClose();
  }

  /** The pointer arrived on the card. */
  enterCard(): void {
    this.overCard = true;
    this.cancelClose();
  }

  /** The pointer left the card. */
  leaveCard(): void {
    this.overCard = false;
    if (this.phase === "preview") this.scheduleClose();
  }

  /** The + pressed (a click, Enter, Space). */
  press(): void {
    switch (this.phase) {
      case "pinned":
        return;
      case "preview":
        this.pinNow();
        return;
      case "pending":
        this.wantPin = true;
        this.due = true;
        this.clearOpen();
        this.maybeOpen();
        return;
      case "closed":
        this.begin(true);
        return;
    }
  }

  /** The reader acted inside the card (a press, the focus on a field). */
  pinInside(): void {
    if (this.phase === "preview") this.pinNow();
  }

  /** The read `seed` started is in. */
  seeded(): void {
    if (this.phase !== "pending") return;
    this.ready = true;
    this.maybeOpen();
  }

  /** A card another door opened (a draft's ring, pinned): the + treats it
   *  as its own pinned card — a hover shows nothing over it, a press keeps
   *  it — until it closes (`closed`). A read of the +'s own still out is
   *  dropped. */
  adopt(): void {
    const reading = this.phase === "pending";
    this.reset();
    this.phase = "pinned";
    if (reading) this.sink.cancelSeed();
  }

  /** The card closed, whichever way. */
  closed(): void {
    this.reset();
    this.overCard = false;
  }

  /** The rail unmounts. */
  dispose(): void {
    this.clearOpen();
    this.cancelClose();
  }

  private begin(pinned: boolean): void {
    this.phase = "pending";
    this.ready = false;
    this.due = pinned;
    this.wantPin = pinned;
    this.sink.seed();
  }

  private maybeOpen(): void {
    if (this.phase !== "pending" || !this.ready || !this.due) return;
    const pinned = this.wantPin;
    this.phase = pinned ? "pinned" : "preview";
    this.wantPin = false;
    this.sink.open(pinned);
    if (!pinned && !this.overPlus && !this.overCard) this.scheduleClose();
  }

  private pinNow(): void {
    this.cancelClose();
    this.phase = "pinned";
    this.sink.pin();
  }

  private reset(): void {
    this.clearOpen();
    this.cancelClose();
    this.phase = "closed";
    this.ready = false;
    this.due = false;
    this.wantPin = false;
  }

  private scheduleClose(): void {
    if (this.overPlus || this.overCard) return;
    this.cancelClose();
    this.closeTimer = this.timers.set(() => {
      this.closeTimer = 0;
      if (this.phase !== "preview" || this.overPlus || this.overCard) return;
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
