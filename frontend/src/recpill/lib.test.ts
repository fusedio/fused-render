import { describe, expect, test } from "bun:test";
import { BAR_COUNT, BAR_REST, bars, combineStates, formatElapsed, iconFor, MAX_ICONS, shapeLevel, smoothLevel, visibleApps } from "./lib";

describe("bars", () => {
  test("silence rests as five 2px dots of a 24px max", () => {
    const r = bars(0);
    expect(r).toEqual(BAR_REST);
    expect(r).toHaveLength(BAR_COUNT);
    r.forEach((w) => expect(w).toBeCloseTo(2 / 24));
  });
  test("normal speech brings the middle bar to full width", () => {
    const b = bars(0.6);
    expect(b[2]).toBeCloseTo(1);
    expect(b[2]).toBeGreaterThan(b[0]);
    expect(b[0]).toBeLessThan(1);
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
    expect(loud[2]).toBeGreaterThanOrEqual(loud[0]);
    expect(loud[2]).toBeGreaterThanOrEqual(loud[4]);
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

const pi = (id: string) => ({ id, name: id, app: "", icon: null, flavor: "render" as const, canPause: true, mode: "audio" });

describe("visibleApps", () => {
  test("up to MAX_ICONS show all, newest first", () => {
    expect(visibleApps([pi("a")])).toEqual({ shown: [pi("a")], extra: 0 });
    expect(visibleApps([pi("a"), pi("b"), pi("c")]).shown.map((x) => x.id)).toEqual(["c", "b", "a"]);
    expect(visibleApps([pi("a"), pi("b"), pi("c")]).extra).toBe(0);
  });
  test("over MAX_ICONS the last slot becomes a +N chip", () => {
    const r4 = visibleApps([pi("a"), pi("b"), pi("c"), pi("d")]);
    expect(r4.shown.map((x) => x.id)).toEqual(["d", "c"]);
    expect(r4.extra).toBe(2);
    const r6 = visibleApps(["a", "b", "c", "d", "e", "f"].map(pi));
    expect(r6.shown.map((x) => x.id)).toEqual(["f", "e"]);
    expect(r6.extra).toBe(4);
    expect(MAX_ICONS).toBe(3);
  });
});

describe("combineStates", () => {
  test("level is the max over non-paused states", () => {
    expect(combineStates([{ level: 0.2, seconds: 3 }, { level: 0.7, paused: true, seconds: 9 }, { level: 0.5, seconds: 1 }]))
      .toEqual({ level: 0.5, paused: false, seconds: 9 });
  });
  test("all paused", () => {
    expect(combineStates([{ level: 0.4, paused: true, seconds: 2 }, { paused: true, seconds: 5 }]))
      .toEqual({ level: 0, paused: true, seconds: 5 });
  });
  test("empty and undefined entries", () => {
    expect(combineStates([])).toEqual({ level: 0, paused: false, seconds: 0 });
    expect(combineStates([undefined, undefined])).toEqual({ level: 0, paused: false, seconds: 0 });
    expect(combineStates([undefined, { paused: true }])).toEqual({ level: 0, paused: true, seconds: 0 });
    expect(combineStates([undefined, { level: 0.3 }]).level).toBe(0.3);
  });
});
