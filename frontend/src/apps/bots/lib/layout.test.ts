import { describe, expect, test } from "bun:test";
import { FIT_HYST, LAYOUT_DEF, LIM, MID_MIN, R_MIN, dragStep, fitFlags, hyst } from "./layout";

describe("layout", () => {
  test("hyst: on below zero, off from FIT_HYST, else holds", () => {
    expect(hyst(false, -1)).toBe(true);
    expect(hyst(true, FIT_HYST)).toBe(false);
    expect(hyst(true, FIT_HYST - 1)).toBe(true);
    expect(hyst(false, FIT_HYST - 1)).toBe(false);
  });
  test("fitFlags: wide window fits everything", () => {
    expect(fitFlags(1600, LAYOUT_DEF, { lfit: false, rfit: false })).toEqual({ lfit: false, rfit: false });
  });
  test("fitFlags: the sidebar collapses before the preview hides", () => {
    // 280 sidebar + 12 + 450 + 280 = 1022 needed; at 900 the sidebar folds to 72 and 72+12+450+280 = 814 still fits.
    expect(fitFlags(900, LAYOUT_DEF, { lfit: false, rfit: false })).toEqual({ lfit: true, rfit: false });
    // Too narrow even for the rail: the preview hides as well.
    expect(fitFlags(72 + 12 + MID_MIN + R_MIN - 1, LAYOUT_DEF, { lfit: false, rfit: false })).toEqual({ lfit: true, rfit: true });
  });
  test("fitFlags: a user-collapsed sidebar never sets lfit", () => {
    expect(fitFlags(700, { ...LAYOUT_DEF, lcol: true }, { lfit: false, rfit: false }).lfit).toBe(false);
  });
  test("dragStep: snap shut below snap, clamp to min/max/room", () => {
    expect(dragStep(LAYOUT_DEF, "l", LIM.l.snap - 1, 999).lcol).toBe(true);
    expect(dragStep(LAYOUT_DEF, "l", 150, 999)).toEqual({ ...LAYOUT_DEF, lw: LIM.l.min });
    expect(dragStep(LAYOUT_DEF, "l", 2000, 999).lw).toBe(LIM.l.max);
    expect(dragStep(LAYOUT_DEF, "r", 700, 500).rw).toBe(500);
    expect(dragStep({ ...LAYOUT_DEF, rcol: true }, "r", 400, 999)).toEqual({ ...LAYOUT_DEF, rw: 400 });
  });
});
