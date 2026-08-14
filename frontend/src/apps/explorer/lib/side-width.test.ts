import { describe, expect, it } from "bun:test";
import {
  COMPANION_FRAC,
  defaultSideWidth,
  CONTENT_MIN_W,
  MIN_W,
} from "@apps/explorer/lib/side-width";
import { PANE_DEFAULT_FRAC } from "@apps/explorer/listing/pane-math";

// ONE SHARE FOR BOTH COMPANION COLUMNS (D282): a file's sidebar and a folder's
// preview pane are the same concept, so they take the same 30% — and the number
// lives in one place, because two literals are how they drifted apart (the pane
// was on 50%). The small-container 50% step that used to sit beside this is gone
// with the pane's tiers: no width decides the SHARE any more, only the floors
// below decide what a share is worth.
describe("COMPANION_FRAC", () => {
  it("is 30%, and is what the listing's pane uses too", () => {
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

  it("floors at MIN_W when 30% would be narrower than the column allows", () => {
    // A normal-but-not-wide container: 0.3 × 900 = 270, under the 280 floor.
    expect(defaultSideWidth(900)).toBe(MIN_W);
    // ...and the content column still clears its own floor at that width.
    expect(900 - defaultSideWidth(900)).toBeGreaterThanOrEqual(CONTENT_MIN_W);
  });

  it("gives the content column its floor back when the column's floor is greedy", () => {
    // At 600px the column's own 280px floor beats 30% (=180), and 600−280 = 320
    // is exactly the content floor: the two meet, and neither is starved.
    expect(defaultSideWidth(600)).toBe(600 - CONTENT_MIN_W);
    expect(defaultSideWidth(600)).toBe(280);
  });

  it("answers MIN_W when the two floors cannot both fit", () => {
    // 500 - 320 = 180, well under MIN_W: there is no split to express, so the
    // CSS min-widths hold and this reports the column's own floor.
    expect(defaultSideWidth(500)).toBe(MIN_W);
    expect(defaultSideWidth(MIN_W + CONTENT_MIN_W - 1)).toBe(MIN_W);
  });

  it("never returns below MIN_W", () => {
    for (const w of [1, 100, 320, 500, 599, 600, 601, 720, 900, 1920, 4000]) {
      expect(defaultSideWidth(w)).toBeGreaterThanOrEqual(MIN_W);
    }
  });

  it("never starves the content column when there is room for both floors", () => {
    for (let w = MIN_W + CONTENT_MIN_W; w <= 2400; w += 7) {
      expect(w - defaultSideWidth(w)).toBeGreaterThanOrEqual(CONTENT_MIN_W);
    }
  });

  it("answers MIN_W for an unmeasured container", () => {
    expect(defaultSideWidth(0)).toBe(MIN_W);
    expect(defaultSideWidth(NaN)).toBe(MIN_W);
    expect(defaultSideWidth(-100)).toBe(MIN_W);
  });
});
