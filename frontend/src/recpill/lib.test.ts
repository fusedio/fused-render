import { describe, expect, test } from "bun:test";
import { BAR_COUNT, BAR_REST, bars, formatElapsed, iconFor, resolvePaused, shapeLevel, smoothLevel } from "./lib";

describe("bars", () => {
  test("silence rests on the uneven taper, widest second and third", () => {
    const r = bars(0);
    expect(r).toEqual(BAR_REST);
    expect(r).toHaveLength(BAR_COUNT);
    expect(r[1]).toBeGreaterThan(r[0]);
    expect(r[2]).toBeGreaterThan(r[1]);
    expect(r[2]).toBeGreaterThan(r[4]);
  });
  test("pause rests regardless of level", () => {
    expect(bars(1, true)).toEqual(BAR_REST);
  });
  test("widths grow monotonically with level, never shrink below rest, and the middle leads", () => {
    let prev = bars(0);
    for (const lv of [0.1, 0.3, 0.5, 0.8, 1]) {
      const b = bars(lv);
      b.forEach((w, i) => { expect(w).toBeGreaterThanOrEqual(prev[i]); expect(w).toBeGreaterThanOrEqual(BAR_REST[i]); expect(w).toBeLessThanOrEqual(1); });
      prev = b;
    }
    const loud = bars(1);
    expect(loud[2]).toBeCloseTo(1);
    expect(loud[2]).toBeGreaterThan(loud[0]);
    expect(loud[2]).toBeGreaterThan(loud[4]);
  });
  test("garbage and out-of-range levels clamp", () => {
    expect(bars(NaN)).toEqual(bars(0));
    expect(bars(-3)).toEqual(bars(0));
    expect(bars(Infinity)).toEqual(bars(0));
    expect(bars(5)).toEqual(bars(1));
  });
});

describe("shapeLevel", () => {
  test("sqrt curve lifts quiet speech and clamps", () => {
    expect(shapeLevel(0.1)).toBeCloseTo(0.316, 2);
    expect(shapeLevel(0.3)).toBeCloseTo(0.548, 2);
    expect(shapeLevel(NaN)).toBe(0);
    expect(shapeLevel(-1)).toBe(0);
    expect(shapeLevel(9)).toBe(1);
  });
});

describe("smoothLevel", () => {
  test("fast attack, slow release", () => {
    expect(smoothLevel(0.8, 0.2)).toBe(0.8);
    expect(smoothLevel(0, 1)).toBeCloseTo(0.85);
    expect(smoothLevel(0.9, 0.5)).toBe(0.9);
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

describe("resolvePaused", () => {
  const pending = { want: true, until: 1000 };
  test("no pending: the server wins", () => {
    expect(resolvePaused(false, null, 5)).toEqual({ paused: false, pending: null });
    expect(resolvePaused(undefined, null, 5)).toEqual({ paused: false, pending: null });
    expect(resolvePaused(true, null, 5)).toEqual({ paused: true, pending: null });
  });
  test("server lagging: the wanted value holds and pending stays", () => {
    expect(resolvePaused(false, pending, 500)).toEqual({ paused: true, pending });
  });
  test("server agreeing clears pending", () => {
    expect(resolvePaused(true, pending, 500)).toEqual({ paused: true, pending: null });
  });
  test("deadline expiry gives the server's value back", () => {
    expect(resolvePaused(false, pending, 1000)).toEqual({ paused: false, pending: null });
    expect(resolvePaused(false, pending, 2000)).toEqual({ paused: false, pending: null });
  });
});
