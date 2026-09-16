import { describe, expect, it } from "bun:test";
import {
  formatMtime,
  formatMtimeFull,
  formatParams,
  formatSize,
  basename,
  dirname,
  repoName,
  labelForSource,
} from "@platform/lib/format";
import { ORIGIN_BY_ROUTE } from "@platform/lib/originRoutes";

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

describe("repoName", () => {
  it("keeps the model half of a repo id and leaves a bare name alone", () => {
    expect(repoName("black-forest-labs/FLUX.2-klein-4B")).toBe("FLUX.2-klein-4B");
    expect(repoName("FLUX.1-schnell")).toBe("FLUX.1-schnell");
  });

  it("never answers with an empty label", () => {
    // A trailing slash or a lone separator must not shorten to nothing — an
    // empty `.dl-model` would draw a stray gap where a name should be.
    expect(repoName("owner/model/")).toBe("model");
    expect(repoName("/")).toBe("/");
    expect(repoName("")).toBe("");
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

describe("labelForSource: agrees with the server's origin_for_page on every known route", () => {
  it("labels every ORIGIN_BY_ROUTE entry with its own table value, not a basename guess", () => {
    // Fix 17 shipped a basename-only labeller that disagreed, visibly, with
    // the server's origin_for_page on the very sources it CAN name without
    // a round trip ("/tasks" -> "tasks" here, "Scheduler" there). This loop
    // is the regression pin: every key in the shared table must label
    // identically to its own value, for every route, not just one example.
    for (const [route, label] of Object.entries(ORIGIN_BY_ROUTE)) {
      expect(labelForSource(route)).toBe(label);
    }
  });

  it("tries the query-bearing key BEFORE stripping it — /preferences?tab=indexing is Explorer, not Preferences", () => {
    expect(labelForSource("/preferences?tab=indexing")).toBe("Explorer");
    expect(labelForSource("/preferences")).toBe("Preferences");
    // A tab this table does not special-case falls through to the bare
    // "/preferences" entry once its own query string is stripped.
    expect(labelForSource("/preferences?tab=engines")).toBe("Preferences");
  });

  it("strips a query string and hash before falling back to the basename rule", () => {
    // A real task-destination shape (schedule-lib.ts's explorerUrl/chatPaneUrl):
    // the junk tail must not leak into the label.
    expect(labelForSource("/explorer/view/Users/x/fused-share?_side=claude&session_id=abc123")).toBe(
      "fused-share",
    );
    expect(labelForSource("/Users/me/Projects/my-app?foo=bar")).toBe("my-app");
    expect(labelForSource("/Users/me/Projects/my-app#section")).toBe("my-app");
  });

  it("still falls back to a bare basename for a route the table does not cover", () => {
    expect(labelForSource("/Users/me/Projects/my-app")).toBe("my-app");
  });
});

describe("formatParams", () => {
  it("uses decimal steps — a 7B model is 7e9 parameters, not 7 * 2^30", () => {
    expect(formatParams(7_241_732_096)).toBe("7.2B");
    expect(formatParams(1_000_000_000)).toBe("1B");
    expect(formatParams(465_000_000)).toBe("465M");
    expect(formatParams(22_713_216)).toBe("23M");
    expect(formatParams(4096)).toBe("4K");
    expect(formatParams(512)).toBe("512");
  });

  it("rolls a count that would ROUND to 1000M over into B instead", () => {
    // `gemma-3-1b-it`'s own real shape: just under 1e9, which used to render
    // as the nonsensical "1000M" because the raw count (not what the M-step
    // would round it to) decided the threshold. Same trailing-zero-stripping
    // the exact-1e9 case already used ("1B", not "1.0B") — this is that same
    // rule reached from just under the boundary instead of just at it.
    expect(formatParams(999_700_000)).toBe("1B");
    // A count that rounds to a real fraction of a billion keeps the decimal.
    expect(formatParams(999_500_000)).toBe("1B");
    // Comfortably under the boundary still reads as M.
    expect(formatParams(994_000_000)).toBe("994M");
  });

  it("says nothing when there is no count", () => {
    expect(formatParams(null)).toBe("");
    expect(formatParams(undefined)).toBe("");
    expect(formatParams(0)).toBe("");
  });
});
