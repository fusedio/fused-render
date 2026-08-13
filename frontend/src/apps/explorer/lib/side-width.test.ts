import { describe, expect, it } from "bun:test";
import {
  defaultSideFrac,
  defaultSideWidth,
  CONTENT_MIN_W,
  MIN_W,
  SMALL_W,
} from "@apps/explorer/lib/side-width";

describe("defaultSideFrac", () => {
  it("gives a normal container a third", () => {
    expect(defaultSideFrac(1440)).toBe(0.3);
    expect(defaultSideFrac(SMALL_W + 1)).toBe(0.3);
  });

  it("gives a small container half, breakpoint inclusive", () => {
    expect(defaultSideFrac(SMALL_W)).toBe(0.5);
    expect(defaultSideFrac(600)).toBe(0.5);
  });
});

describe("defaultSideWidth", () => {
  it("opens at 30% of a normal container", () => {
    expect(defaultSideWidth(1440)).toBe(432);
    expect(defaultSideWidth(1200)).toBe(360);
  });

  it("opens at 50% of a small container", () => {
    // 720 is the breakpoint itself, and counts as small.
    expect(defaultSideWidth(SMALL_W)).toBe(360);
    expect(defaultSideWidth(680)).toBe(340);
  });

  it("floors at MIN_W when 30% would be narrower than the column allows", () => {
    // A normal-but-not-wide container: 0.3 × 900 = 270, under the 280 floor.
    expect(defaultSideWidth(900)).toBe(MIN_W);
    // ...and the content column still clears its own floor at that width.
    expect(900 - defaultSideWidth(900)).toBeGreaterThanOrEqual(CONTENT_MIN_W);
  });

  it("gives the content column its floor back when the share is too greedy", () => {
    // Small, so 50% = 300 — but that leaves the content column 300px, under
    // its 320 floor, so the column gives the difference back.
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
