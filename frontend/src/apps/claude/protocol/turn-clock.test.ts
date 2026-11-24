import { describe, expect, test } from "bun:test";

import { advanceTurnClock } from "./turn-clock";

describe("advanceTurnClock", () => {
  test("the first stamp only moves the clock earlier", () => {
    expect(advanceTurnClock({ startedAt: 5_000, stamp: 0 }, 4_000, 9_000)).toEqual({ startedAt: 4_000, stamp: 4_000 });
    expect(advanceTurnClock({ startedAt: 5_000, stamp: 0 }, 6_000, 9_000)).toEqual({ startedAt: 5_000, stamp: 6_000 });
  });
  test("the same stamp again changes nothing", () => {
    const c = { startedAt: 4_000, stamp: 4_000 };
    expect(advanceTurnClock(c, 4_000, 20_000)).toEqual(c);
  });
  test("a different stamp is a new turn: the clock restarts, even later", () => {
    expect(advanceTurnClock({ startedAt: 4_000, stamp: 4_000 }, 60_000, 61_000)).toEqual({ startedAt: 60_000, stamp: 60_000 });
  });
  test("no stamp, or a stale one, leaves the clock alone", () => {
    const c = { startedAt: 4_000, stamp: 4_000 };
    expect(advanceTurnClock(c, 0, 9_000)).toBe(c);
    expect(advanceTurnClock(c, 1, 10 * 24 * 3600 * 1000)).toBe(c);
  });
});
