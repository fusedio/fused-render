// `show more` says WHEN (Akshil, 2026-10-04: "how long the job took").
import { describe, expect, test } from "bun:test";

import type { Segment, ToolSegment } from "../protocol/types";
import { runSpan, runWhenWords, spanWords } from "./run-when";

const tool = (id: string, ts?: number, ended?: number): ToolSegment => ({
  kind: "tool",
  id,
  name: "Bash",
  input: { command: "ls" },
  status: ended ? "ok" : "running",
  output: ended ? "" : null,
  images: [],
  ...(ts !== undefined ? { ts } : {}),
  ...(ended !== undefined ? { ended } : {}),
});
const think = (ts?: number): Segment => ({ kind: "thinking", text: "hmm", ...(ts !== undefined ? { ts } : {}) });

// A fixed "now" 3 minutes after the run began, in ms as `Date.now` reports it.
const T0 = 1_791_108_000; // 2026-10-04T10:00:00Z
const now = () => (T0 + 180) * 1000;

describe("spanWords", () => {
  test("floors at every unit and drops a zero second unit", () => {
    expect(spanWords(0.9)).toBe("0s");
    expect(spanWords(4)).toBe("4s");
    expect(spanWords(130)).toBe("2m 10s");
    expect(spanWords(120)).toBe("2m");
    expect(spanWords(3780)).toBe("1h 3m");
    expect(spanWords(7200)).toBe("2h");
  });
});

describe("runSpan", () => {
  test("is null when not one member carries a clock", () => {
    expect(runSpan([tool("a"), think()])).toBeNull();
    expect(runSpan([])).toBeNull();
  });
  test("reads the earliest ts and the latest ts-or-ended, skipping unstamped members", () => {
    expect(runSpan([think(T0 + 1), tool("a", T0 + 2, T0 + 130), tool("b")])).toEqual({
      started: T0 + 1,
      finished: T0 + 130,
    });
  });
  test("a lone stamp has no span", () => {
    expect(runSpan([tool("a", T0)])).toEqual({ started: T0, finished: T0 });
  });
  test("zero and NaN are not clocks", () => {
    expect(runSpan([tool("a", 0), tool("b", Number.NaN)])).toBeNull();
  });
});

describe("the hover words (Akshil, 2026-10-06: the RUN's number, Claude's shape)", () => {
  test("run from the run's first stamp to its last result", () => {
    expect(runWhenWords([think(T0 + 1), tool("a", T0 + 2, T0 + 130)], now)).toBe("Worked for 2m 9s · just now");
  });
  test("measure 'ago' from the END, not the start", () => {
    expect(runWhenWords([tool("a", T0 - 420, T0)], now)).toBe("Worked for 7m · 3m ago");
  });
  test("say only when, for a run whose stamps all agree", () => {
    expect(runWhenWords([tool("a", T0)], now)).toBe("3m ago");
  });
  test("say nothing for a run still streaming, with no clocks", () => {
    expect(runWhenWords([tool("a"), think()], now)).toBeNull();
  });
});
