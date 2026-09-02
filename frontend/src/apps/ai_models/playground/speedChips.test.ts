import { describe, expect, it } from "bun:test";
import { SERVER_STEPS, middleSteps } from "./speedChips";

describe("the image rail's middle step count", () => {
  it("triples a small curated count", () => {
    // The klein rows, curated at 4: Quick 4 / Finer 12 / Max 28.
    expect(middleSteps(4)).toBe(12);
  });

  it("takes the midpoint where tripling would hit the ceiling", () => {
    // tiny-sd, curated at 16: 3x is 48, which clamps to 28 and would print the
    // same number as the Max chip. The midpoint of 16 and 28 is 22.
    expect(middleSteps(16)).toBe(22);
  });

  it("never returns the ceiling itself", () => {
    for (let n = 1; n < SERVER_STEPS; n += 1) {
      const middle = middleSteps(n);
      if (middle != null) expect(middle).toBeLessThan(SERVER_STEPS);
    }
  });

  it("never returns the model's own count", () => {
    for (let n = 1; n < SERVER_STEPS; n += 1) {
      const middle = middleSteps(n);
      if (middle != null) expect(middle).toBeGreaterThan(n);
    }
  });

  it("has no middle rung to offer at or above the ceiling", () => {
    // A model curated one step below the ceiling, at it, or past it has no
    // distinct number to put between the two — the caller shows two chips.
    expect(middleSteps(SERVER_STEPS - 1)).toBeNull();
    expect(middleSteps(SERVER_STEPS)).toBeNull();
    expect(middleSteps(40)).toBeNull();
  });
});
