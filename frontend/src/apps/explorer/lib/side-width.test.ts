import { describe, expect, it } from "bun:test";
import {
  COMPANION_FRAC,
  clampSideWidth,
  defaultSideWidth,
  openingSideWidth,
  CONTENT_MIN_W,
  MIN_W,
} from "@apps/explorer/lib/side-width";
import { PANE_DEFAULT_FRAC } from "@apps/explorer/listing/pane-math";

// ONE RULE FOR BOTH COMPANION COLUMNS (D282): a file's sidebar and a folder's
// preview pane are the same concept, so they read the same function — and it lives
// in one place, because two literals are how they drifted apart (the pane was on
// 50%, tiering to 70%).
//
// `COMPANION_FRAC` is the GENERAL share. A container of 1000px or less takes 50%
// instead (`companionFrac`, D283 — the step D282 deleted on a false argument and
// the owner asked back), so a width in that band is testing the other regime and
// every case below says which one it is in.
describe("COMPANION_FRAC", () => {
  it("is the general 30% share, and is what the listing's pane uses too", () => {
    expect(COMPANION_FRAC).toBe(0.3);
    expect(PANE_DEFAULT_FRAC).toBe(COMPANION_FRAC);
  });
});

describe("defaultSideWidth", () => {
  it("opens at 30% of a normal container", () => {
    expect(defaultSideWidth(1440)).toBe(432);
    expect(defaultSideWidth(1600)).toBe(480);
  });

  it("opens at 50% of a small container — 1000 counts as small", () => {
    // D283 restored this step, and the boundary is inclusive (`<=`), matching CSS
    // `(max-width: 1000px)`. The assertion this replaces claimed the OLD 280px
    // floor reached the same answer without a step, which was simply false: 280
    // of 720 is 39%, not 50%. The boundary itself moved from 720 to 1000 on the
    // owner's window, which was on the 30% side of it and wanted half.
    expect(defaultSideWidth(1000)).toBe(500);
    // 50% of 720 is 360, under the 380px floor (MIN_W raised for the claude
    // template's composer row, side-width.ts), so the floor answers here now.
    expect(defaultSideWidth(720)).toBe(380);
    // 680 no longer has room for both floors at all (680 - CONTENT_MIN_W = 360 <
    // MIN_W), so this is the no-split branch below, not the 50% share.
    expect(defaultSideWidth(680)).toBe(380);
    // Just over the boundary is the general (30%) case, but 30% of 1001 is only
    // 300 — still under the 380px floor, so the floor keeps answering until the
    // share itself clears it (~1267px, see the 1600 case above).
    expect(defaultSideWidth(1001)).toBe(380);
  });

  it("never needs the MIN_W floor once both floors fit — the SHARE always clears it", () => {
    // This case used to read "floors at MIN_W when 30% would be narrower than the
    // column allows", exercising it at 900px where 0.3 × 900 = 270 was under the
    // 280px floor. **D283 did not move that scenario, it ABOLISHED it.** With 50%
    // below 1000px and 30% above, the smallest share any container with room for
    // both floors (≥ 600px) can produce is 300px — 50% at 600, and 30% of 1001 —
    // so the `Math.max(MIN_W, …)` clause can no longer decide an answer here. 900
    // is now a 50% container and opens at 450.
    //
    // So the case asserts the invariant that replaced it, which is the stronger
    // statement: across the whole both-floors-fit range the share is never under
    // the column's floor. The floor's remaining job is the no-room branch below
    // 600px, which has its own case.
    expect(defaultSideWidth(900)).toBe(450);
    for (let w = MIN_W + CONTENT_MIN_W; w <= 2400; w += 1) {
      expect(defaultSideWidth(w)).toBeGreaterThanOrEqual(MIN_W);
    }
  });

  it("the two floors meet exactly at their sum, and MIN_W answers there", () => {
    // This used to be 600px demonstrating the CONTENT column's floor clawing
    // room back from a greedy 50% share (300 → 280). Raising MIN_W to 380
    // (side-width.ts) retired that scenario outright: with MIN_W(380) +
    // CONTENT_MIN_W(320) = 700, no container between the "no room for both
    // floors" cutoff and the point where the SHARE itself clears 380 ever
    // asks for more than `max` — the arithmetic no longer has a width where
    // the content floor is the one doing the clipping. 700 is now the exact
    // seam: one px narrower and there is no split to express at all (the
    // "answers MIN_W when the two floors cannot both fit" case below), one at
    // or past it and MIN_W answers directly before the share ever gets a say.
    expect(defaultSideWidth(MIN_W + CONTENT_MIN_W)).toBe(MIN_W);
  });

  it("answers MIN_W when the two floors cannot both fit", () => {
    // 500 - 320 = 180, well under MIN_W: there is no split to express, so the
    // CSS min-widths hold and this reports the column's own floor. Share-agnostic
    // by construction — this branch returns before any share is applied, which is
    // why 500 and 599 being 50% containers now changes nothing here.
    expect(defaultSideWidth(500)).toBe(MIN_W);
    expect(defaultSideWidth(MIN_W + CONTENT_MIN_W - 1)).toBe(MIN_W);
  });

  it("never returns below MIN_W", () => {
    // Deliberately spans both regimes: 500/599/600/601/720/900 are 50% containers
    // and 1920/4000 are 30% ones, with 1000/1001 covering the boundary itself.
    for (const w of [1, 100, 320, 500, 599, 600, 601, 720, 900, 1000, 1001, 1920, 4000]) {
      expect(defaultSideWidth(w)).toBeGreaterThanOrEqual(MIN_W);
    }
  });

  it("never starves the content column when there is room for both floors", () => {
    // Steps across the 1000px boundary, so it covers the 50% band (where the share
    // is the greedy one) and the 30% band alike. Step 1 rather than 7: the boundary
    // is a discontinuity now, and a stride can walk straight over it.
    for (let w = MIN_W + CONTENT_MIN_W; w <= 2400; w += 1) {
      expect(w - defaultSideWidth(w)).toBeGreaterThanOrEqual(CONTENT_MIN_W);
    }
  });

  it("answers MIN_W for an unmeasured container", () => {
    expect(defaultSideWidth(0)).toBe(MIN_W);
    expect(defaultSideWidth(NaN)).toBe(MIN_W);
    expect(defaultSideWidth(-100)).toBe(MIN_W);
  });
});

// THE RESIZE CLAMP, which has to answer two questions at once: never starve the
// content column, and never lose the width the user actually dragged. It used to
// answer only the first — `w > max ? max : w`, monotonically narrowing — so a
// window narrowed and widened again left the column at the narrow width while the
// STORE still held the dragged one, and the next file→file remount (which re-seeds
// from the store) snapped it back. One number in two places, disagreeing.
describe("clampSideWidth", () => {
  it("narrows to fit the content column's floor", () => {
    expect(clampSideWidth(900, null, 1000)).toBe(1000 - CONTENT_MIN_W);
  });

  it("leaves a width that already fits alone", () => {
    expect(clampSideWidth(400, null, 1400)).toBe(400);
  });

  it("RESTORES the dragged width when the room comes back", () => {
    // Dragged to 700 in a wide window, then narrowed to a 900px container (max
    // 580), then widened again: the column returns to 700 instead of sitting at
    // 580 until the next remount.
    expect(clampSideWidth(580, 700, 900)).toBe(580);
    expect(clampSideWidth(580, 700, 1400)).toBe(700);
  });

  it("never grows past the standing choice", () => {
    // Not a "fill the room" rule: an undragged column keeps the width the layout
    // measured for it, and a dragged one never exceeds what was dragged.
    expect(clampSideWidth(400, 700, 4000)).toBe(700);
    expect(clampSideWidth(400, null, 4000)).toBe(400);
  });

  it("holds still when there is no room for both floors", () => {
    // CSS min-widths take over below the floor sum; acting here would describe the
    // container rather than a choice (the pane's rule, FS-12).
    expect(clampSideWidth(400, null, MIN_W + CONTENT_MIN_W - 1)).toBe(400);
    expect(clampSideWidth(400, 700, 100)).toBe(400);
    expect(clampSideWidth(400, null, 0)).toBe(400);
    expect(clampSideWidth(400, null, NaN)).toBe(400);
  });

  it("never returns below MIN_W where it acts at all", () => {
    expect(clampSideWidth(MIN_W, 5, 2000)).toBeGreaterThanOrEqual(MIN_W);
  });
});

// WHAT THE COLUMN OPENS AT, decided once against the MEASURED container — the
// pre-paint answer, so nothing reaches the screen at one width and jumps to
// another. A stored width used to skip this step entirely (the mount effect
// returned early when one existed), so the column painted at the raw stored pixels
// and only met the floors in the post-paint resize effect: drag wide, navigate to a
// folder so the column unmounts, shrink the window, open a file — and the column
// overflowed for a frame before snapping back.
describe("openingSideWidth", () => {
  it("takes the container's share when nothing was dragged", () => {
    expect(openingSideWidth(null, 2000)).toBe(defaultSideWidth(2000));
  });

  it("CLAMPS a stored width to the container it is opening in", () => {
    // Dragged to 900 in a big window, reopened in a 800px one: the content column's
    // floor wins on the first paint, not one frame later.
    expect(openingSideWidth(900, 800)).toBe(800 - CONTENT_MIN_W);
    // ...and a stored width that still fits opens exactly as it was left.
    expect(openingSideWidth(900, 2000)).toBe(900);
  });

  it("never widens a stored width to the share", () => {
    expect(openingSideWidth(MIN_W, 4000)).toBe(MIN_W);
  });

  it("answers null for an unmeasurable container", () => {
    // Null means "keep the seed": detached, display:none, or a state initialiser
    // running before there is any layout — the caller's viewport guess is the
    // honest answer there and this must not overwrite it with a floor.
    expect(openingSideWidth(null, 0)).toBe(null);
    expect(openingSideWidth(900, 0)).toBe(null);
    expect(openingSideWidth(900, NaN)).toBe(null);
  });

  it("leaves a stored width alone when both floors cannot fit", () => {
    // Same rule as the clamp: CSS min-widths hold below the floor sum.
    expect(openingSideWidth(900, MIN_W + CONTENT_MIN_W - 1)).toBe(900);
  });
});
