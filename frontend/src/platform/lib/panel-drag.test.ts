import { describe, expect, it } from "bun:test";
import {
  closeOverdrag,
  committedWidth,
  OPEN_PULL,
  reopenWidth,
  resizeWidth,
} from "@platform/lib/panel-drag";

// The two seams that read this module use different floors (the global sidebar's
// 180, the preview column's 280) and sit on opposite edges of the window, so
// every case below names its own numbers rather than importing either surface's
// constants. What is being tested is the RULE, not one panel's arithmetic.
const MIN = 180;
const MAX = 400;

describe("resizeWidth — the seam of an OPEN panel", () => {
  it("tracks the pointer between the floor and the ceiling", () => {
    expect(resizeWidth(180, MIN, MAX)).toBe(180);
    expect(resizeWidth(240, MIN, MAX)).toBe(240);
    expect(resizeWidth(400, MIN, MAX)).toBe(400);
  });

  it("clamps at the ceiling instead of closing — a panel only shuts inward", () => {
    expect(resizeWidth(900, MIN, MAX)).toBe(MAX);
  });

  // The resistance band, and the whole reason this is not a plain clamp: the
  // panel holds at its floor while the cursor keeps going, so the close is
  // something the user drags THROUGH rather than something the floor does to
  // them the moment they touch it.
  it("sticks at the floor while the pointer overshoots, and does not close", () => {
    expect(resizeWidth(179, MIN, MAX)).toBe(MIN);
    expect(resizeWidth(150, MIN, MAX)).toBe(MIN);
    expect(resizeWidth(MIN - closeOverdrag(MIN) + 1, MIN, MAX)).toBe(MIN);
  });

  it("closes once the overshoot is spent", () => {
    expect(resizeWidth(MIN - closeOverdrag(MIN), MIN, MAX)).toBeNull();
    expect(resizeWidth(0, MIN, MAX)).toBeNull();
    // Past the far edge of the window — a fast drag overshoots into negatives,
    // and that is a close, not an error.
    expect(resizeWidth(-300, MIN, MAX)).toBeNull();
  });
});

// The threshold SCALES with the panel (the workbench's `allotment` rule, whose
// snap is `floor(minimumSize / 2)` of travel past the floor), so the two panels
// in this app resist in proportion to what each of them is worth losing.
describe("closeOverdrag", () => {
  it("is half the floor, so a wider panel resists further", () => {
    expect(closeOverdrag(180)).toBe(90); // the global sidebar
    expect(closeOverdrag(280)).toBe(140); // the preview's companion column
  });

  it("stays an integer on an odd floor", () => {
    expect(closeOverdrag(181)).toBe(90);
    expect(Number.isInteger(closeOverdrag(999))).toBe(true);
  });
});

describe("reopenWidth — the edge of a SHUT panel", () => {
  // The global sidebar shuts to a 44px icon rail; the preview column shuts to
  // nothing at all. The pull is measured from wherever the edge actually is.
  const RAIL = 44;

  it("stays shut until the pull is worth acting on", () => {
    expect(reopenWidth(RAIL, RAIL, MIN, MAX)).toBeNull();
    expect(reopenWidth(RAIL + OPEN_PULL - 1, RAIL, MIN, MAX)).toBeNull();
    expect(reopenWidth(0, 0, MIN, MAX)).toBeNull();
    expect(reopenWidth(OPEN_PULL - 1, 0, MIN, MAX)).toBeNull();
  });

  it("opens at the floor the moment the pull is met, not at the pull's width", () => {
    // 76px of travel, but 76px is not a width this panel may rest at.
    expect(reopenWidth(RAIL + OPEN_PULL, RAIL, MIN, MAX)).toBe(MIN);
    expect(reopenWidth(OPEN_PULL, 0, MIN, MAX)).toBe(MIN);
  });

  it("tracks the pointer once it has passed the floor, and clamps at the ceiling", () => {
    expect(reopenWidth(300, RAIL, MIN, MAX)).toBe(300);
    expect(reopenWidth(900, RAIL, MIN, MAX)).toBe(MAX);
  });

  // Opening is a restore and closing is a loss, so they are guarded to different
  // depths on purpose (see the module header). Equal thresholds would make a shut
  // panel feel stuck.
  it("is eager where the close is deliberate", () => {
    expect(OPEN_PULL).toBe(32);
    expect(OPEN_PULL).toBeLessThan(closeOverdrag(MIN));
  });
});

// CLOSING NEVER RECORDS A WIDTH. The trap this guards is that a close is only
// reachable by dragging THROUGH the resistance band, so the last width the seam
// rendered is always the floor — and a seam that commits "wherever the drag
// ended" files the floor as a choice the user never made.
describe("committedWidth — what a finished drag remembers", () => {
  it("records the width an ordinary resize settled at", () => {
    expect(committedWidth(320, 240)).toBe(320);
    expect(committedWidth(MIN, null)).toBe(MIN);
  });

  it("hands back the pre-gesture width when the drag CLOSED the panel", () => {
    expect(committedWidth(null, 520)).toBe(520);
  });

  it("keeps 'no width chosen yet' as a close's answer, rather than inventing one", () => {
    // Never dragged, then dragged shut: the panel still has no remembered width
    // and must reopen at the container's share, not at a pixel count.
    expect(committedWidth(null, null)).toBeNull();
  });

  // The whole bug, as one sequence: a gesture that walks a wide column inward
  // through the floor and out the far side of the resistance band. Every move but
  // the last answers MIN (the stick), the last answers null (the close) — and what
  // gets remembered is neither, it is the 520 the column had before the pointer
  // went down.
  it("close-drag then reopen restores the pre-gesture width, not the floor", () => {
    const preGesture = 520;
    let outcome: number | null | undefined;
    for (const implied of [520, 400, MIN + 1, MIN, MIN - 1, MIN - closeOverdrag(MIN)]) {
      outcome = resizeWidth(implied, MIN, MAX);
      if (outcome === null) break;
    }
    expect(outcome).toBeNull(); // the gesture ended in a close
    expect(committedWidth(outcome ?? null, preGesture)).toBe(preGesture);
    expect(committedWidth(outcome ?? null, preGesture)).not.toBe(MIN);
  });
});

// The two directions have to compose: a panel closed by a drag must be
// re-openable by the reverse of that drag, at every width in between.
describe("the round trip", () => {
  it("closes and reopens across the same seam", () => {
    const closed = resizeWidth(MIN - closeOverdrag(MIN), MIN, MAX);
    expect(closed).toBeNull();
    expect(reopenWidth(MIN, 0, MIN, MAX)).toBe(MIN);
  });

  it("leaves no width that is neither a legal width nor a close", () => {
    for (let implied = -100; implied <= 500; implied++) {
      const w = resizeWidth(implied, MIN, MAX);
      if (w !== null) expect(w).toBeGreaterThanOrEqual(MIN);
      if (w !== null) expect(w).toBeLessThanOrEqual(MAX);
    }
  });
});
