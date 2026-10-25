// `show more` says WHEN (Akshil, 2026-10-04: "how long the job took").
import { describe, expect, test } from "bun:test";

import type { Segment, ToolSegment } from "../protocol/types";
import { chipHint, chipWhen, runSpan, runWhenWords, spanWords, turnWorkedWords } from "./run-when";

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

describe("the hover words (Akshil, 2026-10-06: the RUN's number, time first then span)", () => {
  const now = () => (T0 + 180) * 1000;
  test("say how long ago it finished, then how long it ran", () => {
    expect(runWhenWords([think(T0 + 1), tool("a", T0 + 2, T0 + 130)], now)).toBe("just now · ran 2m 9s");
    expect(runWhenWords([tool("a", T0 - 420, T0)], now)).toBe("3m ago · ran 7m");
  });
  test("say only when, for a run whose stamps all agree", () => {
    expect(runWhenWords([tool("a", T0)], now)).toBe("3m ago");
  });
  test("say nothing for a run still streaming, with no clocks", () => {
    expect(runWhenWords([tool("a"), think()], now)).toBeNull();
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

describe("turnWorkedWords", () => {
  test("measures from the user turn to the latest segment clock", () => {
    const segs = [think(T0 + 5), tool("a", T0 + 6, T0 + 130)];
    expect(turnWorkedWords(T0, segs)).toBe("Worked for 2m 10s");
  });
  test("falls back to the first segment stamp when there is no user ts", () => {
    expect(turnWorkedWords(undefined, [think(T0), tool("a", T0 + 1, T0 + 12)])).toBe("Worked for 12s");
  });
  test("a live turn with no segment clocks uses the client end stamp", () => {
    expect(turnWorkedWords(T0, [{ kind: "text", text: "hi" }], T0 + 65)).toBe("Worked for 1m 5s");
  });
  test("segment clocks win over the client end stamp", () => {
    expect(turnWorkedWords(T0, [tool("a", T0 + 1, T0 + 30)], T0 + 999)).toBe("Worked for 30s");
  });
  test("a wait on the user is split out of the span", () => {
    const segs = [{ ...tool("a", T0 + 6, T0 + 2530), waited: [T0 + 10, T0 + 2410] as [number, number] }];
    expect(turnWorkedWords(T0, segs)).toBe("Worked for 2m 10s · waited 40m on you");
  });
  test("a wait longer than the span is clamped to it", () => {
    const segs = [{ ...tool("a", T0 + 1, T0 + 30), waited: [T0 - 500, T0 + 999] as [number, number] }];
    expect(turnWorkedWords(T0, segs)).toBe("Worked for 0s · waited 30s on you");
  });
  test("a nested child's wait counts", () => {
    const child = { ...tool("c", T0 + 2, T0 + 100), waited: [T0 + 2, T0 + 62] as [number, number] };
    const segs = [{ ...tool("a", T0 + 1, T0 + 130), children: [child] }];
    expect(turnWorkedWords(T0, segs)).toBe("Worked for 1m 10s · waited 1m on you");
  });
  test("two overlapping waits count their shared time once", () => {
    const segs = [
      { ...tool("a", T0 + 1, T0 + 50), waited: [T0 + 10, T0 + 40] as [number, number] },
      { ...tool("b", T0 + 2, T0 + 50), waited: [T0 + 15, T0 + 40] as [number, number] },
    ];
    expect(turnWorkedWords(T0, segs)).toBe("Worked for 20s · waited 30s on you");
  });
  test("disjoint waits sum", () => {
    const segs = [
      { ...tool("a", T0 + 1, T0 + 50), waited: [T0 + 5, T0 + 15] as [number, number] },
      { ...tool("b", T0 + 2, T0 + 50), waited: [T0 + 30, T0 + 40] as [number, number] },
    ];
    expect(turnWorkedWords(T0, segs)).toBe("Worked for 30s · waited 20s on you");
  });
  test("a wait partly outside the span is clipped", () => {
    const segs = [{ ...tool("a", T0 + 1, T0 + 50), waited: [T0 + 40, T0 + 90] as [number, number] }];
    expect(turnWorkedWords(T0, segs)).toBe("Worked for 40s · waited 10s on you");
  });
  test("a sub-second wait is not shown", () => {
    const segs = [{ ...tool("a", T0 + 1, T0 + 30), waited: [T0 + 5, T0 + 5.4] as [number, number] }];
    expect(turnWorkedWords(T0, segs)).toBe("Worked for 30s");
  });
  test("null when nothing can be computed or under a second", () => {
    expect(turnWorkedWords(undefined, [{ kind: "text", text: "hi" }])).toBeNull();
    expect(turnWorkedWords(T0, [])).toBeNull();
    expect(turnWorkedWords(T0, [think(T0)])).toBeNull();
    expect(turnWorkedWords(T0, [], T0 + 0.5)).toBeNull();
  });
});
