// `show more` says WHEN (Akshil, 2026-10-04: "how long the job took").
import { describe, expect, test } from "bun:test";

import type { Segment, ToolSegment } from "../protocol/types";
import { chipHint, chipWhen, runSpan, runWhenWords, spanWords } from "./run-when";

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

const T0 = 1_791_108_000; // 2026-10-04T10:00:00Z

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

describe("the hover words (Akshil, 2026-10-06: the RUN's number, in the chip hover's order)", () => {
  test("say when the first call was made, then how long the run took", () => {
    expect(runWhenWords([think(T0 + 1), tool("a", T0 + 2, T0 + 130)])).toMatch(
      /^\d{1,2} [A-Z][a-z]{2} \d{4}, \d\d:\d\d:\d\d · ran 2m 9s$/,
    );
  });
  test("say only the instant, for a run whose stamps all agree", () => {
    expect(runWhenWords([tool("a", T0)])).toMatch(/^\d{1,2} [A-Z][a-z]{2} \d{4}, \d\d:\d\d:\d\d$/);
  });
  test("say nothing for a run still streaming, with no clocks", () => {
    expect(runWhenWords([tool("a"), think()])).toBeNull();
  });
});

describe("the chip's lane stamp and its hover (Akshil, 2026-10-06)", () => {
  const nowMs = (T0 + 180) * 1000;
  test("speaks the message stamp's words, and a date only once it is a day old", () => {
    expect(chipWhen(T0 + 150, nowMs)).toBe("just now");
    expect(chipWhen(T0 - 120, nowMs)).toBe("5m ago");
    expect(chipWhen(T0 - 7200, nowMs)).toBe("2h ago");
    expect(chipWhen(T0 - 3 * 86400, nowMs)).toMatch(/^\d{1,2} [A-Z][a-z]{2}$/);
    expect(chipWhen(undefined, nowMs)).toBeNull();
    expect(chipWhen(0, nowMs)).toBeNull();
  });
  test("the hover is the exact instant and how long the call ran", () => {
    expect(chipHint({ ts: T0, ended: T0 + 130, status: "ok" })).toMatch(/^\d{1,2} [A-Z][a-z]{2} \d{4}, \d\d:\d\d:\d\d · ran 2m 10s$/);
    expect(chipHint({ ts: T0, status: "running" })).toMatch(/ · still running$/);
    expect(chipHint({ ts: T0, status: "ok" })).toMatch(/\d\d:\d\d:\d\d$/);
    expect(chipHint({ status: "ok" })).toBeNull();
  });
});
