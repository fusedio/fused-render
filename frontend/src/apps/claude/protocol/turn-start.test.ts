import { describe, expect, test } from "bun:test";

import { sliceStartedAt } from "./turn-start";

const T0 = 1_791_108_000;

describe("sliceStartedAt", () => {
  test("slice j starts at the j-th echo", () => {
    const poll = { turn_ts: T0 + 20, turn_starts: [T0, T0 + 20] };
    expect(sliceStartedAt(poll, 0, 1)).toBe(T0);
    expect(sliceStartedAt(poll, 1, 1)).toBe(T0 + 20);
  });
  test("a follow-up folded into the first reply keeps that reply's start at the FIRST echo", () => {
    // Two echoes, one slice, no seam (Bugbot, PR #1430).
    const poll = { turn_ts: T0 + 20, turn_starts: [T0, T0 + 20] };
    expect(sliceStartedAt(poll, 0, 0)).toBe(T0);
  });
  test("slices the base already accounts for shift the echo index", () => {
    const poll = { turn_ts: T0 + 40, turn_starts: [T0, T0 + 20, T0 + 40] };
    // One leading slice dropped: the page's slice 0 is the window's reply 1.
    expect(sliceStartedAt(poll, 0, 1, 1)).toBe(T0 + 20);
    expect(sliceStartedAt(poll, 1, 1, 1)).toBe(T0 + 40);
  });
  test("an echo with no clock is undefined, never 0 and never its neighbour's", () => {
    const poll = { turn_ts: T0 + 20, turn_starts: [null, T0 + 20] };
    expect(sliceStartedAt(poll, 0, 1)).toBeUndefined();
    expect(sliceStartedAt(poll, 1, 1)).toBe(T0 + 20);
  });
  test("an older server with only turn_ts answers the last slice alone", () => {
    const poll = { turn_ts: T0 + 20 };
    expect(sliceStartedAt(poll, 1, 1)).toBe(T0 + 20);
    expect(sliceStartedAt(poll, 0, 1)).toBeUndefined();
    expect(sliceStartedAt({ turn_ts: null }, 0, 0)).toBeUndefined();
    expect(sliceStartedAt({}, 0, 0)).toBeUndefined();
  });
});
