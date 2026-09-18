// The pure decision behind the home-page focus-change-detection trigger.
import { describe, expect, it } from "bun:test";
import { MIN_HIDDEN_MS, hiddenSeconds, shouldNoteFocus } from "./focus-detect";

describe("shouldNoteFocus", () => {
  it("is false with no recorded hidden-since (never observed going hidden)", () => {
    expect(shouldNoteFocus(null)).toBe(false);
  });

  it("is false when hidden for less than the floor", () => {
    expect(shouldNoteFocus(MIN_HIDDEN_MS - 1)).toBe(false);
  });

  it("is true exactly at the floor", () => {
    expect(shouldNoteFocus(MIN_HIDDEN_MS)).toBe(true);
  });

  it("is true well past the floor", () => {
    expect(shouldNoteFocus(MIN_HIDDEN_MS * 100)).toBe(true);
  });

  it("is false for a brief alt-tab between this app's own windows", () => {
    expect(shouldNoteFocus(500)).toBe(false);
  });
});

describe("hiddenSeconds", () => {
  it("rounds milliseconds to the nearest whole second", () => {
    expect(hiddenSeconds(45_000)).toBe(45);
    expect(hiddenSeconds(45_400)).toBe(45);
    expect(hiddenSeconds(45_600)).toBe(46);
  });
});
