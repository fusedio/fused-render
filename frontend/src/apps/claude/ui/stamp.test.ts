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

  test("same day → the bare clock, zero-padded", () => {
    expect(formatStamp(at(2026, 9, 14, 21, 59), now)).toBe("21:59");
    expect(formatStamp(at(2026, 9, 14, 8, 5), now)).toBe("08:05");
    expect(formatStamp(at(2026, 9, 14, 0, 0), now)).toBe("00:00");
  });

  test("any other day → the date in front of it", () => {
    // Yesterday is another day: `21:59` on a message from before today is a lie
    // the reader would have to catch themselves.
    expect(formatStamp(at(2026, 9, 13, 21, 59), now)).toBe("Sep 13, 21:59");
    expect(formatStamp(at(2026, 1, 2, 7, 4), now)).toBe("Jan 2, 07:04");
    // ...including the same date in a different YEAR.
    expect(formatStamp(at(2025, 9, 14, 21, 59), now)).toBe("Sep 14, 21:59");
  });

  test("milliseconds are taken as milliseconds", () => {
    // The history half of this field is epoch seconds; the live half is a
    // composer's `Date.now()`. 1e11 seconds is the year 5138, so nothing real
    // is ambiguous.
    const ms = new Date(2026, 8, 14, 21, 59, 0, 0).getTime();
    expect(formatStamp(ms, now)).toBe("21:59");
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
  test("the full instant, for the tooltip", () => {
    const ts = at(2026, 9, 14, 21, 59);
    expect(stampTitle(ts)).toBe(new Date(ts * 1000).toISOString());
    expect(stampTitle(0)).toBeNull();
  });
});
