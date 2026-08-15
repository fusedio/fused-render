// describeRepeats: the human reading of the cron shapes the New job form
// itself writes — and, just as load-bearing, verbatim passthrough for anything
// it does not understand. A wrong-but-confident English reading would be worse
// than showing the expression.
import { describe, expect, it } from "bun:test";
import { describeRepeats } from "./schedule-lib";

describe("describeRepeats", () => {
  it("reads the form's own presets", () => {
    expect(describeRepeats("0 * * * *")).toBe("hourly at :00");
    expect(describeRepeats("30 * * * *")).toBe("hourly at :30");
    expect(describeRepeats("30 9 * * *")).toBe("daily at 09:30");
    expect(describeRepeats("0 10 * * 1")).toBe("Mondays at 10:00");
    // Both cron spellings of Sunday.
    expect(describeRepeats("0 8 * * 0")).toBe("Sundays at 08:00");
    expect(describeRepeats("0 8 * * 7")).toBe("Sundays at 08:00");
  });

  it("shows anything else verbatim rather than guessing", () => {
    expect(describeRepeats("*/15 9-17 * * 1-5")).toBe("*/15 9-17 * * 1-5");
    expect(describeRepeats("0 0 1 * *")).toBe("0 0 1 * *"); // dom-restricted
    expect(describeRepeats("0 9 * 2 *")).toBe("0 9 * 2 *"); // month-restricted
    expect(describeRepeats("not cron at all")).toBe("not cron at all");
    expect(describeRepeats("")).toBe("");
  });
});

// ---- calendarEvents: what the week grid shows -------------------------------
// The rules that broke in QA 2026-08-14: skipped runs of a replaced (edited)
// schedule lingered forever with an Unskip that could only 404.
import { calendarEvents } from "./schedule-lib";
import type { ScheduledMessage } from "@platform/lib/api";

const NOW = new Date("2026-08-14T12:00:00Z");

function entry(over: Partial<ScheduledMessage>): ScheduledMessage {
  return {
    id: "e1", target: "/tmp/x", message: "m", due: "2026-08-15T09:30:00Z",
    session_id: "", permission_mode: "auto", state: "pending",
    created: "", fired: "", run_id: "", error: "",
    ...over,
  } as ScheduledMessage;
}

describe("calendarEvents", () => {
  it("hides skipped runs of a dead (cancelled/replaced) schedule", () => {
    const dead = calendarEvents([
      entry({ id: "t1", state: "cancelled", repeats: "30 9 * * *" }),
      entry({ id: "o1", state: "cancelled", template_id: "t1" }),
    ], NOW);
    expect(dead).toEqual([]);
  });

  it("keeps a live schedule's skipped runs; Unskip only before their time", () => {
    const events = calendarEvents([
      entry({ id: "t1", state: "recurring", repeats: "30 9 * * *", upcoming: [] }),
      entry({ id: "future", state: "cancelled", template_id: "t1",
              due: "2026-08-15T09:30:00Z" }),
      entry({ id: "past", state: "cancelled", template_id: "t1",
              due: "2026-08-13T09:30:00Z" }),
    ], NOW);
    const byId = Object.fromEntries(events.map((e) => [e.entry.id, e]));
    expect(byId.future.unskippable).toBe(true);
    expect(byId.past.unskippable).toBe(false);
    expect(events.length).toBe(2);
  });

  it("hides a cancelled one-shot entirely", () => {
    expect(calendarEvents([entry({ state: "cancelled" })], NOW)).toEqual([]);
  });

  it("dedupes the stored next occurrence out of the ghost projection", () => {
    const events = calendarEvents([
      entry({ id: "t1", state: "recurring", repeats: "30 9 * * *",
              upcoming: ["2026-08-15T09:30:00Z", "2026-08-16T09:30:00Z"] }),
      entry({ id: "o1", state: "pending", template_id: "t1",
              due: "2026-08-15T09:30:00Z" }),
    ], NOW);
    const ghosts = events.filter((e) => e.ghost);
    expect(ghosts.length).toBe(1);
    expect(ghosts[0].iso).toBe("2026-08-16T09:30:00Z");
    expect(events.some((e) => !e.ghost && e.entry.id === "o1")).toBe(true);
  });

  it("places handled runs where they acted, waiting runs at their due", () => {
    const events = calendarEvents([
      entry({ id: "s", state: "sent", due: "2026-08-14T09:00:00Z",
              fired: "2026-08-14T10:07:00Z" }),
      entry({ id: "p", state: "pending", due: "2026-08-15T09:00:00Z" }),
      entry({ id: "m", state: "missed", due: "2026-08-13T09:00:00Z",
              fired: "" }),
    ], NOW);
    const byId = Object.fromEntries(events.map((e) => [e.entry.id, e]));
    expect(byId.s.iso).toBe("2026-08-14T10:07:00Z");
    expect(byId.p.iso).toBe("2026-08-15T09:00:00Z");
    expect(byId.m.iso).toBe("2026-08-13T09:00:00Z");
  });
});

// ---- boardColumn: which Board column a task sits in --------------------------
import { boardColumn, stateLabel as stateLabelOf, stateTone as stateToneOf } from "./schedule-lib";

describe("boardColumn", () => {
  it("routes by the same collapse the pill shows", () => {
    expect(boardColumn(entry({ state: "pending" }))).toBe("upcoming");
    expect(boardColumn(entry({ state: "recurring", repeats: "0 * * * *" }))).toBe("upcoming");
    // In flight counts as news, not a plan — it sits with Ran.
    expect(boardColumn(entry({ state: "sending" }))).toBe("ran");
    expect(boardColumn(entry({ state: "sent", turn: "ok", fired: "x" }))).toBe("ran");
    expect(boardColumn(entry({ state: "sent", turn: "" , fired: "x"}))).toBe("ran");
    expect(boardColumn(entry({ state: "sent", turn: "failed", fired: "x" }))).toBe("attention");
    expect(boardColumn(entry({ state: "missed" }))).toBe("attention");
    expect(boardColumn(entry({ state: "error" }))).toBe("attention");
    expect(boardColumn(entry({ state: "cancelled" }))).toBe("cancelled");
    expect(boardColumn(entry({ state: "cancelled", template_id: "t" }))).toBe("cancelled");
  });
});

// ---- the loop's own skip verdict reads as Skipped, not a fault ---------------
describe("missed recurring runs", () => {
  it("label, tone, and board column all say skipped", () => {
    const missedOcc = entry({ state: "missed", template_id: "t1" });
    expect(stateLabelOf(missedOcc)).toBe("Skipped");
    expect(stateToneOf(missedOcc)).toBe("skipped");
    expect(boardColumn(missedOcc)).toBe("cancelled");
    // A missed ONE-SHOT stays a fault — the day-long catch-up genuinely failed.
    const missedOneShot = entry({ state: "missed" });
    expect(stateLabelOf(missedOneShot)).toBe("Missed");
    expect(boardColumn(missedOneShot)).toBe("attention");
  });
});

// ---- dead-schedule skips retire from the grid, whichever skip they were ------
describe("dead-schedule skipped runs", () => {
  it("hides loop-missed occurrences of a cancelled/replaced schedule", () => {
    const dead = calendarEvents([
      entry({ id: "t1", state: "cancelled", repeats: "30 9 * * *" }),
      entry({ id: "o1", state: "missed", template_id: "t1" }),
    ], NOW);
    expect(dead).toEqual([]);
  });

  it("keeps a live schedule's loop-missed run, without Unskip", () => {
    const events = calendarEvents([
      entry({ id: "t1", state: "recurring", repeats: "30 9 * * *", upcoming: [] }),
      entry({ id: "o1", state: "missed", template_id: "t1",
              due: "2026-08-15T09:30:00Z" }),
    ], NOW);
    expect(events.length).toBe(1);
    expect(events[0].unskippable).toBe(false);
  });

  it("still shows a missed one-shot — that one is a fault", () => {
    const events = calendarEvents([entry({ state: "missed" })], NOW);
    expect(events.length).toBe(1);
  });
});

// ---- lane packing reuses freed lanes -----------------------------------------
import { assignLanes } from "./schedule-lib";

describe("assignLanes", () => {
  const at = (h: number, m: number) => ({ time: new Date(2026, 7, 14, h, m) });
  it("slides back to a freed lane instead of widening the cluster", () => {
    // 9:00 and 9:20 overlap; 9:40 only overlaps 9:20 — it reuses lane 0.
    const packed = assignLanes([at(9, 0), at(9, 20), at(9, 40)]);
    expect(packed.map((p) => p.lane)).toEqual([0, 1, 0]);
    expect(packed.every((p) => p.lanes === 2)).toBe(true);
  });
  it("separate clusters do not share width", () => {
    const packed = assignLanes([at(9, 0), at(9, 10), at(14, 0)]);
    expect(packed[2].lane).toBe(0);
    expect(packed[2].lanes).toBe(1);
    expect(packed[0].lanes).toBe(2);
  });
  it("truly simultaneous chips fan out", () => {
    const packed = assignLanes([at(9, 0), at(9, 0), at(9, 0)]);
    expect(new Set(packed.map((p) => p.lane)).size).toBe(3);
    expect(packed.every((p) => p.lanes === 3)).toBe(true);
  });
});
