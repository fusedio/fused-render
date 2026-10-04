// The user message's hover stamp (design.md §C).
import { describe, expect, test } from "bun:test";

import { formatStamp, stampTitle } from "./stamp";

/** A local-time instant, in epoch SECONDS — the unit agent.py's `_history`
 *  writes. Built through the Date constructor so the test reads in whatever
 *  zone the machine is in, exactly as the stamp is drawn. */
const at = (y: number, m: number, d: number, h: number, min: number): number =>
  Math.floor(new Date(y, m - 1, d, h, min, 0, 0).getTime() / 1000);

describe("formatStamp", () => {
  const now = new Date(2026, 8, 14, 23, 30, 0, 0).getTime(); // Sep 14 2026, local

  test("under a day old → one relative unit", () => {
    expect(formatStamp(at(2026, 9, 14, 23, 29), now)).toBe("1m ago");
    expect(formatStamp(at(2026, 9, 14, 23, 25), now)).toBe("5m ago");
    expect(formatStamp(at(2026, 9, 14, 18, 30), now)).toBe("5h ago");
    // Yesterday but still inside 24h: relative, not the date.
    expect(formatStamp(at(2026, 9, 14, 0, 0), now)).toBe("23h ago");
    expect(formatStamp(Math.floor(now / 1000) - 10, now)).toBe("just now");
  });

  test("a day or older → day, month, clock", () => {
    expect(formatStamp(at(2026, 9, 13, 21, 59), now)).toBe("13 Sep, 21:59");
    expect(formatStamp(at(2026, 1, 2, 7, 4), now)).toBe("2 Jan, 07:04");
    expect(formatStamp(at(2025, 9, 14, 21, 59), now)).toBe("14 Sep, 21:59");
  });

  test("milliseconds are taken as milliseconds", () => {
    // The history half of this field is epoch seconds; the live half is a
    // composer's `Date.now()`. 1e11 seconds is the year 5138, so nothing real
    // is ambiguous.
    const ms = new Date(2026, 8, 14, 21, 59, 0, 0).getTime();
    expect(formatStamp(ms, now)).toBe("1h ago");
  });

  test("nothing usable → no stamp at all", () => {
    expect(formatStamp(undefined, now)).toBeNull();
    expect(formatStamp(null, now)).toBeNull();
    expect(formatStamp(0, now)).toBeNull();
    expect(formatStamp(Number.NaN, now)).toBeNull();
    expect(formatStamp("1757000000" as unknown as number, now)).toBeNull();
  });
});

describe("stampTitle", () => {
  test("the full instant, readable, for the tooltip", () => {
    const ts = at(2026, 9, 14, 21, 59);
    expect(stampTitle(ts)).toBe("14 Sep 2026, 21:59:00");
    expect(stampTitle(0)).toBeNull();
  });
});
