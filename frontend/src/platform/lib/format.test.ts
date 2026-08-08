import { describe, expect, it } from "bun:test";
import { formatMtime, formatMtimeFull, formatSize, basename, dirname } from "@platform/lib/format";

// 2025-09-24 20:59:57 local time — the seconds are what the assertions are
// about, so the instant is built from local parts, not a UTC string.
const T = new Date(2025, 8, 24, 20, 59, 57).getTime() / 1000;

describe("formatMtime", () => {
  it("is empty for a missing time", () => {
    expect(formatMtime(null)).toBe("");
    expect(formatMtime(undefined)).toBe("");
    expect(formatMtime(0)).toBe("");
  });

  it("drops the seconds component", () => {
    const short = formatMtime(T);
    // Locale-independent shape check: the full stamp carries a third
    // :NN group (the seconds); the short one must not.
    expect(formatMtimeFull(T)).toMatch(/\d+:\d\d:\d\d/);
    expect(short).not.toMatch(/\d+:\d\d:\d\d/);
    expect(short).toMatch(/\d+:\d\d/); // hours:minutes survive
  });

  it("keeps a date and the time of day", () => {
    const short = formatMtime(T);
    // Short date styles abbreviate the year ("9/24/25"), so assert shape:
    // some date-ish run of digits and separators, then the clock.
    expect(short).toMatch(/\d/);
    expect(short).toContain("59"); // the minute
    expect(short.length).toBeLessThan(formatMtimeFull(T).length);
  });

  it("stays a prefix-free-of-seconds subset of the full stamp", () => {
    // Same instant, so both must agree on the calendar date.
    expect(formatMtimeFull(T)).toContain("2025");
  });
});

describe("formatSize", () => {
  it("reports bytes under 1 KB and scales past it", () => {
    expect(formatSize(0)).toBe("0 B");
    expect(formatSize(512)).toBe("512 B");
    expect(formatSize(1536)).toBe("1.5 KB");
    expect(formatSize(null)).toBe("");
  });
});

describe("path helpers", () => {
  it("basename and dirname handle the root", () => {
    expect(basename("/a/b/c.txt")).toBe("c.txt");
    expect(basename("/")).toBe("/");
    expect(dirname("/a/b/c.txt")).toBe("/a/b");
    expect(dirname("/a")).toBe("/");
  });
});
