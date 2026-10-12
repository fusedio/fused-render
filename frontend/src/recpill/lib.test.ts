import { describe, expect, test } from "bun:test";
import { BAR_COUNT, BAR_MIN, bars, formatElapsed, iconFor } from "./lib";

describe("bars", () => {
  test("silence and pause rest at the minimum", () => {
    expect(bars(0)).toEqual(Array(BAR_COUNT).fill(BAR_MIN));
    expect(bars(1, true)).toEqual(Array(BAR_COUNT).fill(BAR_MIN));
  });
  test("the middle bar leads and the heights stay in range", () => {
    const b = bars(0.6);
    expect(b[2]).toBeGreaterThan(b[0]);
    expect(b[0]).toBeCloseTo(b[4]);
    for (const h of bars(5)) { expect(h).toBeLessThanOrEqual(1); expect(h).toBeGreaterThanOrEqual(BAR_MIN); }
  });
  test("garbage levels read as silence", () => {
    expect(bars(NaN)).toEqual(bars(0));
    expect(bars(-3)).toEqual(bars(0));
  });
});

describe("formatElapsed", () => {
  test("minutes, hours, garbage", () => {
    expect(formatElapsed(0)).toBe("0:00");
    expect(formatElapsed(65.9)).toBe("1:05");
    expect(formatElapsed(3600)).toBe("1:00:00");
    expect(formatElapsed(3725)).toBe("1:02:05");
    expect(formatElapsed(-4)).toBe("0:00");
    expect(formatElapsed(NaN)).toBe("0:00");
  });
});

describe("iconFor", () => {
  test("the app's icon wins, else the flavor's card", () => {
    expect(iconFor({ icon: "/x.svg", flavor: "bot" })).toEqual({ kind: "app", src: "/x.svg", scale: "100%" });
    expect(iconFor({ icon: null, flavor: "render" }).scale).toBe("113.7%");
    expect(iconFor({ icon: null, flavor: "bot" }).scale).toBe("124.3%");
  });
});
