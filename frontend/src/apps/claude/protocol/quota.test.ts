import { describe, expect, test } from "bun:test";

import {
  CONTINUE_GRACE_S,
  clockText,
  continueDue,
  continueNote,
  limitExplain,
  limitHit,
  untilText,
} from "./quota";
import type { Quota } from "./types";

// 2026-09-10T18:30:00Z — the reset off the real 429 (run 20260910-195317).
const RESET = 1789066800;

function q(over: Partial<Quota> = {}): Quota {
  return {
    status: "allowed",
    type: "five_hour",
    resets_at: RESET,
    utilization: null,
    windows: {
      five_hour: { utilization: 0.17, resets_at: RESET },
      seven_day: { utilization: 0.06, resets_at: RESET + 6 * 86400 },
    },
    ...over,
  };
}

describe("limitHit", () => {
  test("only a rejected window with a reset counts", () => {
    expect(limitHit(q({ status: "rejected" }))).toBe(true);
    expect(limitHit(q({ status: "allowed_warning" }))).toBe(false);
    expect(limitHit(q({ status: "rejected", resets_at: 0 }))).toBe(false);
    expect(limitHit(null)).toBe(false);
    expect(limitHit(undefined)).toBe(false);
  });
});

describe("continueDue", () => {
  test("is the reset plus the grace, as an ISO instant", () => {
    expect(continueDue(q())).toBe(new Date((RESET + CONTINUE_GRACE_S) * 1000).toISOString());
  });
});

describe("clockText", () => {
  test("spells the CLI's own clock: h:mmam/pm, no leading zero, local zone", () => {
    const d = new Date(RESET * 1000);
    const h = d.getHours() % 12 || 12;
    const mm = String(d.getMinutes()).padStart(2, "0");
    const ap = d.getHours() >= 12 ? "pm" : "am";
    expect(clockText(RESET)).toBe(`${h}:${mm}${ap}`);
  });
  test("a reset more than a day out carries its weekday", () => {
    const now = RESET * 1000;
    expect(clockText(RESET + 5 * 86400, now)).toMatch(/^(Sun|Mon|Tue|Wed|Thu|Fri|Sat) \d{1,2}:\d{2}[ap]m$/);
    expect(clockText(RESET + 3600, now)).toMatch(/^\d{1,2}:\d{2}[ap]m$/);
  });
  test("midnight is 12:xxam, noon 12:xxpm", () => {
    const local = (h: number) => new Date(2026, 0, 1, h, 5).getTime() / 1000;
    expect(clockText(local(0))).toBe("12:05am");
    expect(clockText(local(12))).toBe("12:05pm");
  });
});

describe("untilText", () => {
  const now = RESET * 1000;
  test("under a minute is 'any moment now'", () => {
    expect(untilText(RESET, now - 30_000)).toBe("any moment now");
    expect(untilText(RESET, now + 60_000)).toBe("any moment now");
  });
  test("minutes, then hours and minutes", () => {
    expect(untilText(RESET, now - 3 * 60_000)).toBe("in 3m");
    expect(untilText(RESET, now - (2 * 3600 + 14 * 60) * 1000)).toBe("in 2h 14m");
    expect(untilText(RESET, now - 3 * 3600 * 1000)).toBe("in 3h");
  });
});

describe("limitExplain", () => {
  const now = RESET * 1000 - 3 * 60_000;
  test("names the reset and, once scheduled, the follow-up row", () => {
    const plain = limitExplain(q({ status: "rejected" }), now, false);
    expect(plain).toContain("It resets at " + clockText(RESET) + " (in 3m).");
    expect(plain).not.toContain("follow-up");
    const scheduled = limitExplain(q({ status: "rejected" }), now, true);
    expect(scheduled).toContain("A follow-up is scheduled to continue this task then");
  });
});

describe("continueNote", () => {
  test("is the CLI's wait line", () => {
    expect(continueNote(q())).toBe(
      "Usage limit reached · continuing automatically at " + clockText(RESET),
    );
  });
});
