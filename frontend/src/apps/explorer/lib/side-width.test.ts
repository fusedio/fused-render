import { describe, expect, it } from "bun:test";
import {
  COMPANION_FRAC,
  clampSideWidth,
  defaultSideWidth,
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
    expect(defaultSideWidth(1200)).toBe(360);
  });

  it("opens at 50% of a small container — 1000 counts as small", () => {
    // D283 restored this step, and the boundary is inclusive (`<=`), matching CSS
    // `(max-width: 1000px)`. The assertion this replaces claimed the 280px FLOOR
    // reached the same answer without a step, which was simply false: 280 of 720
    // is 39%, not 50%. The boundary itself moved from 720 to 1000 on the owner's
    // window, which was on the 30% side of it and wanted half.
    expect(defaultSideWidth(1000)).toBe(500);
    expect(defaultSideWidth(720)).toBe(360);
    expect(defaultSideWidth(680)).toBe(340);
    // Just over the boundary is the general case: 30% of 1001 is 300, over the
    // 280px floor, so the share itself answers.
    expect(defaultSideWidth(1001)).toBe(300);
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

  it("gives the content column its floor back when the SHARE is greedy", () => {
    // 600px is a 50% container, so the share wants 300 — and the content column's
    // 320px floor claws 20 of it back, leaving 280. The two floors meet exactly
    // here, which makes 600 the tightest point in the range for the content side.
    //
    // It is the CONTENT cap that answers, not the column's own MIN_W: this case
    // read "the column's own 280px floor beats 30% (=180)" and passed for that
    // reason until D283 put 600 on the 50% side. Same number, different mechanism —
    // which is exactly the kind of green worth distrusting.
    expect(defaultSideWidth(600)).toBe(600 - CONTENT_MIN_W);
    expect(defaultSideWidth(600)).toBe(280);
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
