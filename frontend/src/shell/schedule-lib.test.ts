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
import {
  boardColumn,
  isLive,
  stateLabel as stateLabelOf,
  stateTone as stateToneOf,
} from "./schedule-lib";

describe("boardColumn", () => {
  it("routes by the same collapse the pill shows", () => {
    expect(boardColumn(entry({ state: "pending" }))).toBe("upcoming");
    expect(boardColumn(entry({ state: "recurring", repeats: "0 * * * *" }))).toBe("upcoming");
    // In flight is work happening NOW — the Inbox's In Progress, exactly.
    expect(boardColumn(entry({ state: "sending" }))).toBe("in_progress");
    expect(boardColumn(entry({ state: "sent", turn: "" , fired: "x"}))).toBe("in_progress");
    // Settled WELL is Done; settled badly has had its own word since
    // 2026-08-17, and every view says it rather than showing a red Done.
    expect(boardColumn(entry({ state: "sent", turn: "ok", fired: "x" }))).toBe("done");
    expect(boardColumn(entry({ state: "sent", turn: "failed", fired: "x" }))).toBe("failed");
    expect(boardColumn(entry({ state: "missed" }))).toBe("failed");
    expect(boardColumn(entry({ state: "error" }))).toBe("failed");
    expect(boardColumn(entry({ state: "cancelled" }))).toBe("archived");
    expect(boardColumn(entry({ state: "cancelled", template_id: "t" }))).toBe("archived");
  });
});

// ---- sessionColumn: the chat half of the same board --------------------------
// The board shows scheduled tasks and Claude sessions side by side, so a
// session has to answer the same question a task does. The interesting case is
// the one the type system cannot see: a status this client does not know.
import { sessionColumn } from "./schedule-lib";

describe("sessionColumn", () => {
  it("passes the three the board already speaks straight through", () => {
    expect(sessionColumn("in_progress")).toBe("in_progress");
    expect(sessionColumn("done")).toBe("done");
    expect(sessionColumn("archived")).toBe("archived");
  });

  it("files anything it does not recognise under Done, never Upcoming", () => {
    // A session that happened is never UPCOMING — the one column it must not
    // land in — and Archive would hide it behind a collapsed lane.
    expect(sessionColumn("compacted")).toBe("done");
    expect(sessionColumn("")).toBe("done");
    expect(sessionColumn("upcoming")).toBe("done");
  });
});

// ---- the loop's own skip verdict reads as Skipped, not a fault ---------------
describe("missed recurring runs", () => {
  it("label, tone, and board column all say skipped", () => {
    const missedOcc = entry({ state: "missed", template_id: "t1" });
    expect(stateLabelOf(missedOcc)).toBe("Skipped");
    expect(stateToneOf(missedOcc)).toBe("skipped");
    expect(boardColumn(missedOcc)).toBe("archived");
    // A missed ONE-SHOT stays a fault — the day-long catch-up genuinely failed,
    // and since 2026-08-17 the board has a word for that.
    const missedOneShot = entry({ state: "missed" });
    expect(stateLabelOf(missedOneShot)).toBe("Missed");
    expect(boardColumn(missedOneShot)).toBe("failed");
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

// describeRule / repeatChoicesFor: the Google wording is the CONTRACT — the
// select's labels, the cards and the popover must all say these exact words,
// so the words themselves are pinned, not paraphrased.
import { describeRule, entryRepeatText, nthOfMonth, repeatChoicesFor } from "./schedule-lib";

// Wednesday, August 12 2026 — the date Google's own screenshots use.
const AUG12 = new Date(2026, 7, 12, 10, 0);

describe("describeRule", () => {
  it("says Google's presets verbatim", () => {
    expect(describeRule({ freq: "hour" }, AUG12)).toBe("Hourly");
    expect(describeRule({ freq: "day" }, AUG12)).toBe("Daily");
    expect(describeRule({ freq: "week", byday: [3] }, AUG12)).toBe("Weekly on Wednesday");
    expect(describeRule({ freq: "month", monthly: "nth-weekday" }, AUG12))
      .toBe("Monthly on the second Wednesday");
    expect(describeRule({ freq: "year" }, AUG12)).toBe("Annually on August 12");
    expect(describeRule({ freq: "week", byday: [1, 2, 3, 4, 5] }, AUG12))
      .toBe("Every weekday (Monday to Friday)");
  });
  it("reads intervals, day sets and ends", () => {
    // The one frequency with nothing to read off the anchor: an hourly rule
    // says only how often, so its sentence is the interval and the ends.
    expect(describeRule({ freq: "hour", interval: 6 }, AUG12)).toBe("Every 6 hours");
    expect(describeRule({ freq: "hour", until: "2026-11-11" }, AUG12))
      .toBe("Hourly, until Nov 11, 2026");
    expect(describeRule({ freq: "hour", interval: 3, count: 13 }, AUG12))
      .toBe("Every 3 hours, 13 times");
    expect(describeRule({ freq: "week", interval: 2, byday: [1, 3] }, AUG12))
      .toBe("Every 2 weeks on Monday, Wednesday");
    expect(describeRule({ freq: "month", monthly: "day" }, AUG12)).toBe("Monthly on day 12");
    expect(describeRule({ freq: "day", until: "2026-11-11" }, AUG12))
      .toBe("Daily, until Nov 11, 2026");
    expect(describeRule({ freq: "day", count: 13 }, AUG12)).toBe("Daily, 13 times");
  });
});

describe("repeatChoicesFor", () => {
  it("is Google's list, derived from the picked date", () => {
    expect(repeatChoicesFor(AUG12).map((c) => c.label)).toEqual([
      "Does not repeat",
      // Shortest first: Hourly sits above Daily, the order recur.FREQUENCIES
      // is written in.
      "Hourly",
      "Daily",
      "Weekly on Wednesday",
      "Monthly on the second Wednesday",
      "Annually on August 12",
      "Every weekday (Monday to Friday)",
      "Custom…",
    ]);
  });
  it("nthOfMonth counts the way the label reads", () => {
    expect(nthOfMonth(new Date(2026, 7, 1))).toBe(1);
    expect(nthOfMonth(new Date(2026, 7, 12))).toBe(2);
    expect(nthOfMonth(new Date(2026, 7, 29))).toBe(5);
  });
});

describe("entryRepeatText", () => {
  const base = { id: "x", target: "", message: "", due: AUG12.toISOString(),
    session_id: "", permission_mode: "auto", state: "recurring", created: "",
    fired: "", run_id: "", error: "" } as never;
  it("prefers the rule's wording, falls back to cron's", () => {
    expect(entryRepeatText({ ...(base as object), rule: { freq: "day" } } as never)).toBe("Daily");
    expect(entryRepeatText({ ...(base as object), repeats: "30 9 * * *" } as never)).toBe("daily at 09:30");
  });
});

// ---- The calendar's layout maths --------------------------------------------
// Everything the grid gets wrong when it is wrong lives in these functions:
// which chip anchors a day, what the `+N` counts, how the 4-day window steps,
// and the two dates a year where a day is not 24 hours long. None of it needs a
// DOM, so none of it is tested through one.
import {
  GHOST_PREFIX,
  RANGE_DAYS,
  addDays,
  cancelOutcome,
  columnLabel,
  dayKey,
  dayStatus,
  dayTone,
  firstLine,
  lateBy,
  lateText,
  messageTone,
  minutesOfDay,
  popoverPos,
  projectedMessages,
  queueRole,
  queueRoles,
  rangeDays,
  rangeLabel,
  rangeStart,
  runStatus,
  startOfWeek,
  stepRange,
  taskChips,
  taskColour,
  threadForDay,
  turnPhase,
} from "./schedule-lib";
// The one-vocabulary mapping is deliberately NOT re-derived in schedule-lib —
// tasks-lib.messageTone is the app's single answer to "which column", and
// runStatus consumes it. So the test composes exactly what the component
// composes, rather than a fixture that could agree with neither.
import { messageTone as taskMessageTone } from "./tasks-lib";
import type { Task, TaskMessage } from "@platform/lib/api";

// Local wall-clock seconds — the whole point of the day maths is that it reads
// the LOCAL clock, so the fixtures have to be built off it too.
const at = (y: number, mo: number, d: number, h: number, mi = 0) =>
  Math.floor(new Date(y, mo, d, h, mi, 0, 0).getTime() / 1000);

function msg(over: Partial<TaskMessage> & { at: number }): TaskMessage {
  return {
    message_id: `MSG-${over.at}`,
    kind: "scheduled",
    body: "pull today's news",
    // Never run unless a case says so: `at` is what a message was scheduled
    // for, `ran_at` is when it went, and 0 is "it has not".
    ran_at: 0,
    state: "pending",
    unread: false,
    entry_id: "e1",
    template_id: "",
    turn: "",
    anchor: "",
    ...over,
  };
}

function task(over: Partial<Task> & { key: string }): Task {
  return {
    task_id: "TASK-001",
    project: "/Users/x",
    target: "/Users/x",
    session_id: "sess",
    title: "Pull news",
    title_source: "ai",
    description: "",
    status: "upcoming",
    failed: false,
    live: false,
    unread: 0,
    last_active: 0,
    message_count: 1,
    messages: [],
    ...over,
  };
}

// ---- ranges -------------------------------------------------------------------

describe("calendar ranges", () => {
  it("the week snaps to Monday; the 4-day range starts on the day you are on", () => {
    // Wednesday 19 August 2026.
    const wed = new Date(2026, 7, 19, 15, 30);
    expect(dayKey(rangeStart(wed, "week"))).toBe("2026-08-17"); // the Monday
    // Today-leftmost — the whole point of the 4-day view.
    expect(dayKey(rangeStart(wed, "4day"))).toBe("2026-08-19");
  });

  it("lays out the right number of consecutive days", () => {
    const start = rangeStart(new Date(2026, 7, 19), "4day");
    expect(rangeDays(start, "4day").map(dayKey)).toEqual([
      "2026-08-19", "2026-08-20", "2026-08-21", "2026-08-22",
    ]);
    expect(rangeDays(rangeStart(new Date(2026, 7, 19), "week"), "week").length).toBe(7);
    expect(RANGE_DAYS).toEqual({ week: 7, "4day": 4 });
  });

  it("the arrows step a whole window — seven days, or FOUR", () => {
    const monday = rangeStart(new Date(2026, 7, 19), "week");
    expect(dayKey(stepRange(monday, "week", 1))).toBe("2026-08-24");
    expect(dayKey(stepRange(monday, "week", -1))).toBe("2026-08-10");
    const wed = rangeStart(new Date(2026, 7, 19), "4day");
    expect(dayKey(stepRange(wed, "4day", 1))).toBe("2026-08-23");
    expect(dayKey(stepRange(wed, "4day", -1))).toBe("2026-08-15");
    // Stepping is reversible — a page forward and back is where you started.
    expect(dayKey(stepRange(stepRange(wed, "4day", 3), "4day", -3))).toBe("2026-08-19");
  });

  it("Today snaps the window back to today-leftmost", () => {
    const now = new Date(2026, 7, 19, 9, 0);
    expect(dayKey(rangeStart(now, "4day"))).toBe(dayKey(now));
    expect(rangeDays(rangeStart(now, "4day"), "4day")[0].getDate()).toBe(19);
  });

  it("startOfWeek is Monday-first, including on a Sunday", () => {
    expect(dayKey(startOfWeek(new Date(2026, 7, 23)))).toBe("2026-08-17"); // Sunday
    expect(dayKey(startOfWeek(new Date(2026, 7, 17)))).toBe("2026-08-17"); // Monday
  });

  it("labels the window, spanning when it straddles a month", () => {
    expect(rangeLabel(rangeDays(rangeStart(new Date(2026, 7, 17), "week"), "week")))
      .toContain("2026");
    const straddle = rangeDays(rangeStart(new Date(2026, 7, 30), "4day"), "4day");
    expect(rangeLabel(straddle)).toContain("–");
  });
});

// ---- DST ------------------------------------------------------------------------
// A day is 23 or 25 hours long twice a year. Day arithmetic done in
// milliseconds slides a chip a whole column on those two days; done in local
// calendar fields it cannot. These run in whatever zone the machine is in, so
// they assert the INVARIANT (one column per day, chip on its own local day)
// rather than a zone-specific pixel.

describe("DST days", () => {
  // US spring forward 2026-03-08 (23h) and fall back 2026-11-01 (25h).
  for (const [name, y, mo, d] of [
    ["spring forward (23h)", 2026, 2, 8],
    ["fall back (25h)", 2026, 10, 1],
  ] as const) {
    it(`${name}: still exactly one column per day`, () => {
      const start = rangeStart(new Date(y, mo, d - 1), "4day");
      const days = rangeDays(start, "4day").map(dayKey);
      expect(days.length).toBe(4);
      expect(new Set(days).size).toBe(4);
      // Consecutive calendar dates, not "24h apart".
      expect(days[1]).toBe(dayKey(new Date(y, mo, d)));
      expect(days[2]).toBe(dayKey(new Date(y, mo, d + 1)));
    });

    it(`${name}: a 9am message lands on the 9am line of its own day`, () => {
      const t = task({
        key: "k", messages: [msg({ at: at(y, mo, d, 9), state: "sent", turn: "done" })],
      });
      const days = rangeDays(rangeStart(new Date(y, mo, d), "4day"), "4day");
      const chips = taskChips([t], days);
      const mine = chips.get(dayKey(new Date(y, mo, d)))!;
      expect(mine.length).toBe(1);
      // 9am local is the 9am line, whether the day was 23 or 25 hours long.
      expect(minutesOfDay(mine[0].time)).toBe(9 * 60);
    });

    it(`${name}: stepping over it stays on whole days`, () => {
      const before = rangeStart(new Date(y, mo, d - 2), "4day");
      expect(dayKey(stepRange(before, "4day", 1))).toBe(dayKey(new Date(y, mo, d + 2)));
      expect(minutesOfDay(addDays(before, 1))).toBe(0);
    });
  }
});

// ---- one chip per task per day -------------------------------------------------

describe("taskChips", () => {
  const days = rangeDays(rangeStart(new Date(2026, 7, 17), "week"), "week");

  it("anchors the day at the task's EARLIEST message and counts the rest", () => {
    const t = task({
      key: "news",
      messages: [
        msg({ message_id: "MSG-3", at: at(2026, 7, 17, 19) }),
        msg({ message_id: "MSG-1", at: at(2026, 7, 17, 5) }),
        msg({ message_id: "MSG-2", at: at(2026, 7, 17, 12) }),
      ],
    });
    const chips = taskChips([t], days).get("2026-08-17")!;
    expect(chips.length).toBe(1); // ONE chip, not three
    expect(chips[0].anchor.message_id).toBe("MSG-1");
    expect(minutesOfDay(chips[0].time)).toBe(5 * 60);
    expect(chips[0].extra).toBe(2); // the +N
    // The later runs are still IN the chip — that is what the popover lists.
    expect(chips[0].messages.map((m) => m.message_id)).toEqual(["MSG-1", "MSG-2", "MSG-3"]);
  });

  it("an hourly rule is ONE chip carrying +23, not 24 chips", () => {
    const t = task({
      key: "hourly",
      messages: Array.from({ length: 24 }, (_, h) =>
        msg({ message_id: `MSG-${h}`, at: at(2026, 7, 18, h), template_id: "t1" }),
      ),
    });
    const chips = taskChips([t], days).get("2026-08-18")!;
    expect(chips.length).toBe(1);
    expect(chips[0].extra).toBe(23);
    expect(minutesOfDay(chips[0].time)).toBe(0); // the midnight run anchors it
    expect(chips[0].recurring).toBe(true);
  });

  it("23:50 and 00:10 are two chips on two days, not one", () => {
    const t = task({
      key: "midnight",
      messages: [
        msg({ message_id: "late", at: at(2026, 7, 17, 23, 50) }),
        msg({ message_id: "early", at: at(2026, 7, 18, 0, 10) }),
      ],
    });
    const byDay = taskChips([t], days);
    expect(byDay.get("2026-08-17")!.length).toBe(1);
    expect(byDay.get("2026-08-18")!.length).toBe(1);
    expect(byDay.get("2026-08-17")![0].extra).toBe(0);
    expect(minutesOfDay(byDay.get("2026-08-17")![0].time)).toBe(23 * 60 + 50);
    expect(minutesOfDay(byDay.get("2026-08-18")![0].time)).toBe(10);
  });

  it("unrelated tasks are unrelated chips", () => {
    const a = task({ key: "a", messages: [msg({ at: at(2026, 7, 19, 9) })] });
    const b = task({ key: "b", messages: [msg({ at: at(2026, 7, 19, 14) })] });
    const c = task({ key: "c", messages: [msg({ at: at(2026, 7, 19, 18) })] });
    const chips = taskChips([a, b, c], days).get("2026-08-19")!;
    expect(chips.length).toBe(3);
    // Sorted by time, so the column reads top to bottom.
    expect(chips.map((ch) => minutesOfDay(ch.time))).toEqual([540, 840, 1080]);
  });

  it("a daily task is one chip a day, all five sharing one colour", () => {
    const t = task({
      key: "daily",
      messages: [17, 18, 19, 20, 21].map((d) => msg({ message_id: `d${d}`, at: at(2026, 7, d, 9) })),
    });
    const byDay = taskChips([t], days);
    const mine = days.map((d) => byDay.get(dayKey(d))!).flat();
    expect(mine.length).toBe(5);
    expect(new Set(mine.map((ch) => ch.colour)).size).toBe(1);
  });

  it("a chat message never places a chip — it has no schedule", () => {
    const t = task({
      key: "chat",
      messages: [
        msg({ message_id: "c1", kind: "chat", at: at(2026, 7, 19, 9), entry_id: "" }),
        msg({ message_id: "s1", at: at(2026, 7, 19, 11) }),
      ],
    });
    const chips = taskChips([t], days).get("2026-08-19")!;
    expect(chips.length).toBe(1);
    expect(chips[0].anchor.message_id).toBe("s1");
    expect(chips[0].extra).toBe(0);
  });

  it("ignores messages outside the window and never invents a column", () => {
    const t = task({ key: "far", messages: [msg({ at: at(2026, 9, 1, 9) })] });
    const byDay = taskChips([t], days);
    expect([...byDay.keys()].length).toBe(7);
    expect([...byDay.values()].every((v) => v.length === 0)).toBe(true);
  });

  it("a full thread supplied by the caller wins over the task's last-N tail", () => {
    const t = task({ key: "k", messages: [msg({ message_id: "tail", at: at(2026, 7, 17, 9) })] });
    const chips = taskChips([t], days, {
      k: [
        msg({ message_id: "full-1", at: at(2026, 7, 17, 5) }),
        msg({ message_id: "full-2", at: at(2026, 7, 17, 9) }),
      ],
    }).get("2026-08-17")!;
    expect(chips[0].anchor.message_id).toBe("full-1");
    expect(chips[0].extra).toBe(1);
  });
});

// ---- tone: `turn` means the same thing in both views ---------------------------
// The one vocabulary the calendar and the list share, and the one they had
// silently forked: schedule-lib.messageTone named only "done" as finished, so
// "idle" — a turn that ended and reported — painted as Running here while
// tasks-lib.messageTone already said Ran. Both now read turnPhase.

describe("turnPhase", () => {
  it("calls only an EMPTY turn still running", () => {
    // The field is written once, when the turn ends. That is the whole rule,
    // and it holds across both spellings of the field.
    expect(turnPhase("")).toBe("running");
    expect(turnPhase(undefined)).toBe("running");
    expect(turnPhase("unknown")).toBe("unreported");
    // TaskMessage's words…
    expect(turnPhase("done")).toBe("ended");
    expect(turnPhase("idle")).toBe("ended");
    // …ScheduledMessage's…
    expect(turnPhase("ok")).toBe("ended");
    expect(turnPhase("failed")).toBe("ended");
    expect(turnPhase("cancelled")).toBe("ended");
    // …and a word neither of them has invented yet. It ENDED: the server did
    // not write the field until it did, so guessing "in flight" is the one
    // wrong answer.
    expect(turnPhase("settled")).toBe("ended");
  });
});

describe("messageTone", () => {
  it("reads an idle turn as ran, the same as the list does", () => {
    expect(messageTone(msg({ at: 1, state: "sent", turn: "idle" }))).toBe("ran");
    expect(messageTone(msg({ at: 1, state: "sent", turn: "done" }))).toBe("ran");
  });

  it("keeps `sending` for the one case that is genuinely in flight", () => {
    expect(messageTone(msg({ at: 1, state: "sent", turn: "" }))).toBe("sending");
    expect(messageTone(msg({ at: 1, state: "sending" }))).toBe("sending");
    // A turn that stopped reporting is a failure, not work in progress.
    expect(messageTone(msg({ at: 1, state: "sent", turn: "unknown" }))).toBe("error");
    // And an unheard-of word is a turn that finished.
    expect(
      messageTone(msg({ at: 1, state: "sent", turn: "settled" as TaskMessage["turn"] })),
    ).toBe("ran");
  });

  it("still tells the calendar's six tones apart", () => {
    // Why this function is not tasks-lib's: `missed` and `error` are different
    // colours here, and `cancelled` files under skipped rather than archived.
    expect(messageTone(msg({ at: 1, state: "pending" }))).toBe("upcoming");
    expect(messageTone(msg({ at: 1, state: "missed" }))).toBe("missed");
    expect(messageTone(msg({ at: 1, state: "error" }))).toBe("error");
    expect(messageTone(msg({ at: 1, state: "cancelled" }))).toBe("skipped");
    expect(messageTone(msg({ at: 1, state: "skipped" }))).toBe("skipped");
  });
});

// The same audit on the OTHER shape's turn vocabulary — the schedule store's
// "" | "ok" | "failed" | "cancelled" | "unknown".
describe("stateLabel / isLive on an unheard-of turn", () => {
  it("does not report a finished turn as running forever", () => {
    const future = entry({
      state: "sent",
      fired: "x",
      turn: "settled" as ScheduledMessage["turn"],
    });
    expect(stateLabelOf(future)).toBe("Sent");
    expect(stateToneOf(future)).not.toBe("sending");
    expect(isLive(future)).toBe(false);
    // The empty turn is the one that is still going, in all three.
    const live = entry({ state: "sent", fired: "x", turn: "" });
    expect(stateLabelOf(live)).toBe("Running…");
    expect(stateToneOf(live)).toBe("sending");
    expect(isLive(live)).toBe(true);
  });
});

// ---- tone: a day answers for all of its messages -------------------------------

describe("dayTone", () => {
  it("a failure in the day beats a clean run — the chip must not read green", () => {
    expect(dayTone([
      msg({ at: 1, state: "sent", turn: "done" }),
      msg({ at: 2, state: "error" }),
    ])).toBe("error");
    expect(dayTone([msg({ at: 1, state: "sent", turn: "done" })])).toBe("ran");
    expect(dayTone([msg({ at: 1, state: "missed" })])).toBe("missed");
    // A skipped run alongside one still to come reads as still to come.
    expect(dayTone([
      msg({ at: 1, state: "cancelled" }),
      msg({ at: 2, state: "pending" }),
    ])).toBe("upcoming");
    // A sent message whose turn has not reported yet is work in flight.
    expect(dayTone([msg({ at: 1, state: "sent", turn: "" })])).toBe("sending");
    // "unknown" is the watch ending with no verdict — a failure, not a run.
    expect(dayTone([msg({ at: 1, state: "sent", turn: "unknown" })])).toBe("error");
  });
});

// ---- colour --------------------------------------------------------------------

describe("taskColour", () => {
  it("is stable, in range, and does not put every task on one hue", () => {
    expect(taskColour("sess-abc")).toBe(taskColour("sess-abc"));
    const keys = Array.from({ length: 40 }, (_, i) => `pending:entry-${i}`);
    for (const k of keys) {
      const c = taskColour(k);
      expect(c).toBeGreaterThanOrEqual(0);
      expect(c).toBeLessThan(8);
    }
    // Not a perfect spread — a hash never is — but every hue gets used.
    expect(new Set(keys.map(taskColour)).size).toBe(8);
  });

  it("does not collide on the anagrams task keys are full of", () => {
    expect(taskColour("ab")).not.toBe(taskColour("ba"));
  });
});

// ---- projections ----------------------------------------------------------------

describe("projectedMessages", () => {
  const entryOf = (over: Partial<import("@platform/lib/api").ScheduledMessage>) =>
    entry({ state: "recurring", ...over });
  // Pinned, never `new Date()`: projections are decided against the clock now
  // (nothing is drawn into the past), so a test that read the real one would
  // start failing on a date nobody chose.
  const NOW = new Date(2026, 7, 17, 12, 0);

  it("hangs a rule's future occurrences on the task that owns it", () => {
    const t = task({
      key: "k",
      messages: [msg({ message_id: "MSG-1", at: at(2026, 7, 17, 9), template_id: "t1" })],
    });
    const out = projectedMessages([t], [
      entryOf({ id: "t1", upcoming: [
        new Date(2026, 7, 18, 9).toISOString(),
        new Date(2026, 7, 19, 9).toISOString(),
      ] }),
    ], {}, NOW);
    expect(out.k.length).toBe(3);
    const days = rangeDays(rangeStart(new Date(2026, 7, 17), "week"), "week");
    const byDay = taskChips([t], days, out);
    expect(byDay.get("2026-08-18")!.length).toBe(1);
    expect(byDay.get("2026-08-18")![0].projected).toBe(true);
    // The already-materialized run is NOT projected.
    expect(byDay.get("2026-08-17")![0].projected).toBe(false);
  });

  it("never draws the next run twice — projections dedupe to the minute", () => {
    const t = task({
      key: "k",
      messages: [msg({ message_id: "MSG-1", at: at(2026, 7, 18, 9), template_id: "t1" })],
    });
    const out = projectedMessages([t], [
      entryOf({ id: "t1", upcoming: [new Date(2026, 7, 18, 9).toISOString()] }),
    ], {}, NOW);
    expect(out.k.length).toBe(1);
  });

  it("finds a rule that has never run by its pending task key", () => {
    const t = task({ key: "pending:t1", messages: [] });
    const out = projectedMessages([t], [
      entryOf({ id: "t1", upcoming: [new Date(2026, 7, 20, 7).toISOString()] }),
    ], {}, NOW);
    expect(out["pending:t1"].length).toBe(1);
    expect(out["pending:t1"][0].template_id).toBe("t1");
  });

  it("drops a rule no task claims rather than inventing one", () => {
    expect(projectedMessages(
      [], [entryOf({ id: "t1", upcoming: ["2026-08-20T07:00:00Z"] })], {}, NOW,
    )).toEqual({});
  });

  // A repeat anchored BEHIND us: catch-up runs the most recent missed slot only,
  // at that slot's own time, and the ones before it are dropped for good (§9,
  // Akshil 2026-08-17). Drawing them would be the grid promising work that has
  // already been decided against.
  it("never projects into the past — the past shows what happened", () => {
    const t = task({ key: "pending:t1", messages: [] });
    const out = projectedMessages([t], [
      entryOf({ id: "t1", upcoming: [
        new Date(2026, 7, 15, 9).toISOString(), // two days behind NOW
        new Date(2026, 7, 17, 9).toISOString(), // this morning, already gone
        new Date(2026, 7, 18, 9).toISOString(), // tomorrow — the only forecast
      ] }),
    ], {}, NOW);
    expect(out["pending:t1"].map((m) => m.at)).toEqual([at(2026, 7, 18, 9)]);
  });

  it("leaves the run that DID happen alone — only ghosts are suppressed", () => {
    // The most recent missed slot, materialized by catch-up: a real message at
    // its own past time. It is history, and history stays on the grid.
    const ran = msg({
      message_id: "MSG-1",
      at: at(2026, 7, 15, 9),
      ran_at: at(2026, 7, 17, 8),
      state: "sent",
      turn: "done",
      template_id: "t1",
    });
    const t = task({ key: "k", messages: [ran] });
    const out = projectedMessages([t], [
      entryOf({ id: "t1", upcoming: [
        new Date(2026, 7, 15, 9).toISOString(),
        new Date(2026, 7, 18, 9).toISOString(),
      ] }),
    ], {}, NOW);
    expect(out.k.map((m) => m.at)).toEqual([at(2026, 7, 15, 9), at(2026, 7, 18, 9)]);
    // The week the run was DUE in — Saturday the 15th, not the week it was
    // caught up in.
    const days = rangeDays(rangeStart(new Date(2026, 7, 15), "week"), "week");
    const byDay = taskChips([t], days, out);
    // The chip stays on the day it was due, and says how late it ran.
    expect(byDay.get("2026-08-15")!.length).toBe(1);
    expect(byDay.get("2026-08-15")![0].projected).toBe(false);
    expect(lateText(ran)).toBe("ran 2 days late");
  });
});

// ---- the popover's thread order ---------------------------------------------------

describe("threadForDay", () => {
  it("that day first and earliest-first; the rest newest-first", () => {
    const messages = [
      msg({ message_id: "y", at: at(2026, 7, 16, 9) }),
      msg({ message_id: "d2", at: at(2026, 7, 17, 19) }),
      msg({ message_id: "d1", at: at(2026, 7, 17, 5) }),
      msg({ message_id: "x", at: at(2026, 7, 15, 9) }),
    ];
    const { today, rest } = threadForDay(messages, "2026-08-17");
    // The 7pm run has no chip of its own — this is where it stays reachable.
    expect(today.map((m) => m.message_id)).toEqual(["d1", "d2"]);
    expect(rest.map((m) => m.message_id)).toEqual(["y", "x"]);
  });
});

// ---- the queue, inside the thread ----------------------------------------------------
// The Queued strip across the top of the grid is gone (Akshil, 2026-08-17):
// queued work is a MESSAGE, and it is now marked on the thread row it already
// had. What has to survive the move is the cancel and its honest refusal.

describe("queued and running, on the row", () => {
  const q = (id: string, message: string) =>
    entry({ id, message, state: "pending" });
  const NOW = at(2026, 7, 17, 12, 0);

  it("takes the server's word for it, by entry id", () => {
    const roles = queueRoles([q("e1", "waiting")], [q("e2", "going")]);
    // Due in the FUTURE, so nothing about the message itself says queued — the
    // queue endpoint is the only thing that knows, and it is believed.
    expect(queueRole(msg({ at: NOW + 3600, entry_id: "e1" }), roles, NOW)).toBe("queued");
    expect(queueRole(msg({ at: NOW + 3600, entry_id: "e2" }), roles, NOW)).toBe("running");
  });

  it("an entry in both lists reads as running — the half that limits Cancel", () => {
    const roles = queueRoles([q("e1", "x")], [q("e1", "x")]);
    expect(roles.get("e1")).toBe("running");
  });

  it("falls back to the message when the queue feed said nothing", () => {
    const none = new Map<string, string>() as ReturnType<typeof queueRoles>;
    // Past due and still pending IS the definition of queued.
    expect(queueRole(msg({ at: NOW - 60 }), none, NOW)).toBe("queued");
    expect(queueRole(msg({ at: NOW + 60 }), none, NOW)).toBe("");
    expect(queueRole(msg({ at: NOW - 60, state: "sending" }), none, NOW)).toBe("running");
    // Already ran: the queue is not holding it.
    expect(queueRole(msg({ at: NOW - 60, state: "sent", turn: "done" }), none, NOW)).toBe("");
  });

  it("never calls a projection queued — nothing has been written down", () => {
    const none = new Map<string, string>() as ReturnType<typeof queueRoles>;
    const ghost = msg({ at: NOW - 60, message_id: `${GHOST_PREFIX}2026-08-17T11:59:00` });
    expect(queueRole(ghost, none, NOW)).toBe("");
  });

  it("reads a pasted multi-line prompt by its first line", () => {
    expect(firstLine("\n\nPull today's news\nand summarise it")).toBe("Pull today's news");
    expect(firstLine("")).toBe("");
  });

  it("says out loud that a claimed entry was refused, never drops it", () => {
    expect(cancelOutcome(["a", "b"], [])).toBe("");
    expect(cancelOutcome([], ["a"])).toBe("Already running — too late to cancel.");
    expect(cancelOutcome(["a"], ["b"])).toBe("Cancelled 1; 1 was already running.");
    expect(cancelOutcome(["a"], ["b", "c"])).toBe("Cancelled 1; 2 were already running.");
    expect(cancelOutcome([], ["b", "c"]))
      .toBe("2 were already running — too late to cancel.");
  });
});

// ---- one status vocabulary -----------------------------------------------------------
// Board, List and Calendar say the SAME four words. The two distinctions that
// costs — a failed run, a projected one — are kept as visuals, and these are the
// tests that stop either of them quietly becoming a fifth word again.

describe("the one status vocabulary", () => {
  const status = (m: TaskMessage) => runStatus(m, taskMessageTone(m));

  it("speaks only the app's five words", () => {
    expect(columnLabel("upcoming")).toBe("Upcoming");
    expect(columnLabel("in_progress")).toBe("In Progress");
    expect(columnLabel("done")).toBe("Done");
    expect(columnLabel("failed")).toBe("Failed");
    expect(columnLabel("archived")).toBe("Archive");
    const words = [
      status(msg({ at: 1, state: "pending" })),
      status(msg({ at: 1, state: "sending" })),
      status(msg({ at: 1, state: "sent", turn: "done" })),
      status(msg({ at: 1, state: "error" })),
      status(msg({ at: 1, state: "missed" })),
      status(msg({ at: 1, state: "cancelled" })),
      status(msg({ at: 1, state: "skipped" })),
      status(msg({ at: 1, state: "sent", turn: "unknown" })),
    ].map((s) => s.label);
    expect(new Set(words)).toEqual(
      new Set(["Upcoming", "In Progress", "Done", "Failed", "Archive"]),
    );
  });

  it("a failed run SAYS Failed — the red is reinforcement, not the signal", () => {
    const failed = status(msg({ at: 1, state: "error" }));
    expect(failed.label).toBe("Failed");
    expect(failed.failed).toBe(true);
    const clean = status(msg({ at: 1, state: "sent", turn: "done" }));
    expect(clean.label).toBe("Done");
    expect(clean.failed).toBe(false);
    // A turn that stopped reporting is a failure too, and says so.
    expect(status(msg({ at: 1, state: "sent", turn: "unknown" })))
      .toMatchObject({ label: "Failed", failed: true });
    // A missed ONE-OFF is a fault: the run the user asked for never happened.
    expect(status(msg({ at: 1, state: "missed" }))).toMatchObject({ label: "Failed" });
  });

  it("a skipped occurrence reads Archive — filed away, never attempted", () => {
    expect(status(msg({ at: 1, state: "skipped" })))
      .toMatchObject({ label: "Archive", failed: false });
    expect(status(msg({ at: 1, state: "cancelled" })))
      .toMatchObject({ label: "Archive", failed: false });
    // The loop's own skip of a recurring occurrence is the same thing.
    expect(status(msg({ at: 1, state: "missed", template_id: "t1" })))
      .toMatchObject({ label: "Archive", failed: false });
  });

  it("a ghost reads Upcoming and is kept apart by its DASHES, not by a word", () => {
    const ghost = status(msg({ at: 1, message_id: `${GHOST_PREFIX}2026-08-17T09:00:00` }));
    const real = status(msg({ at: 1, state: "pending" }));
    expect(ghost.label).toBe("Upcoming");
    expect(real.label).toBe("Upcoming");
    // Same word, and still distinguishable — which is the whole bargain.
    expect(ghost.projected).toBe(true);
    expect(real.projected).toBe(false);
  });

  it("keeps the finer reading for a tooltip, never as a sixth word", () => {
    // "Missed" and "Stopped reporting" still say something a status cannot, so
    // they survive — in `detail`, which only ever reaches a title attribute.
    expect(status(msg({ at: 1, state: "missed" })).detail).toBe("Missed");
    expect(status(msg({ at: 1, state: "sent", turn: "unknown" })).detail)
      .toBe("Stopped reporting");
    expect(status(msg({ at: 1, state: "sent", turn: "done" })).detail).toBe("Ran");
  });

  it("a day's pill answers for its worst run", () => {
    const day = (...ms: TaskMessage[]) => dayStatus(ms.map(status));
    // 9am ran fine, 2pm died: the day is not clean.
    expect(day(
      msg({ at: 1, state: "sent", turn: "done" }),
      msg({ at: 2, state: "error" }),
    )).toMatchObject({ label: "Failed", failed: true });
    // Work in flight outranks work still coming.
    expect(day(
      msg({ at: 1, state: "pending" }),
      msg({ at: 2, state: "sending" }),
    )).toMatchObject({ label: "In Progress", failed: false });
    // Something still to come outranks something filed away.
    expect(day(
      msg({ at: 1, state: "skipped" }),
      msg({ at: 2, state: "pending" }),
    )).toMatchObject({ label: "Upcoming" });
    expect(day(msg({ at: 1, state: "sent", turn: "done" }))).toMatchObject({ label: "Done" });
  });

  it("a day is only projected when NOTHING in it is real", () => {
    const ghost = (n: number) =>
      msg({ at: n, message_id: `${GHOST_PREFIX}${n}` });
    expect(dayStatus([ghost(1), ghost(2)].map(status)).projected).toBe(true);
    // One materialized run makes the day a commitment, not a forecast.
    expect(dayStatus([ghost(1), msg({ at: 2, state: "pending" })].map(status)).projected)
      .toBe(false);
    expect(dayStatus([]).projected).toBe(false);
  });
});

// ---- late runs -----------------------------------------------------------------------
// `at` is what was ASKED FOR and never moves; `ran_at` is when it went. The chip
// stays on the day the user picked and the row says how far behind it ran —
// which is the whole fix for a task jumping to the day the app happened to open.

describe("a run that was late", () => {
  const NOON = at(2026, 7, 17, 12, 0);

  it("is derived from at vs ran_at, and nothing else", () => {
    expect(lateBy(msg({ at: NOON, ran_at: NOON + 7200, state: "sent", turn: "done" })))
      .toBe(7200);
    // Never ran: there is no lateness to report, only a state.
    expect(lateBy(msg({ at: NOON, ran_at: 0, state: "pending" }))).toBe(0);
    // Ran early — or the clocks disagreed. Not a fact worth a sentence.
    expect(lateBy(msg({ at: NOON, ran_at: NOON - 60, state: "sent" }))).toBe(0);
  });

  it("stays quiet about the scheduler's own granularity", () => {
    expect(lateText(msg({ at: NOON, ran_at: NOON + 40, state: "sent" }))).toBe("");
    expect(lateText(msg({ at: NOON, ran_at: NOON + 299, state: "sent" }))).toBe("");
    expect(lateText(msg({ at: NOON, ran_at: NOON + 300, state: "sent" })))
      .toBe("ran 5 minutes late");
  });

  it("says it in the unit a person would", () => {
    const late = (secs: number) => lateText(msg({ at: NOON, ran_at: NOON + secs, state: "sent" }));
    expect(late(3600)).toBe("ran 1 hour late");
    expect(late(3 * 3600)).toBe("ran 3 hours late");
    expect(late(2 * 86400)).toBe("ran 2 days late");
    // No unit is ever printed at the value the NEXT unit owns.
    expect(late(3599)).toBe("ran 1 hour late");
    expect(late(86399)).toBe("ran 1 day late");
  });

  it("does not move the chip: a caught-up run keeps the day it was due", () => {
    const days = [new Date(2026, 7, 17), new Date(2026, 7, 19)];
    const t = task({ key: "t1", messages: [] });
    const caughtUp = msg({
      // Due Monday, ran Wednesday — the case that used to jump columns.
      at: at(2026, 7, 17, 9),
      ran_at: at(2026, 7, 19, 10),
      state: "sent",
      turn: "done",
    });
    const chips = taskChips([t], days, { t1: [caughtUp] });
    expect(chips.get("2026-08-17")?.length).toBe(1);
    expect(chips.get("2026-08-19")?.length).toBe(0);
    expect(lateText(caughtUp)).toBe("ran 2 days late");
  });
});

// ---- keeping the popover on screen ---------------------------------------------------

describe("popoverPos", () => {
  const VW = 1000;
  const VH = 800;

  it("sits below-right of the click when there is room", () => {
    expect(popoverPos(100, 100, 360, 400, VW, VH)).toEqual({ left: 108, top: 108 });
  });

  it("flips to the other side rather than sliding under the edge", () => {
    // A click near the right edge: the panel goes LEFT of the pointer.
    expect(popoverPos(900, 100, 360, 400, VW, VH).left).toBe(900 - 360 - 8);
    // Near the bottom: ABOVE it. This is the Open in Explorer case.
    expect(popoverPos(100, 700, 360, 400, VW, VH).top).toBe(700 - 400 - 8);
  });

  it("clamps when neither side fits, and never goes off the top-left", () => {
    // Taller than the window: pinned to the margin, and the thread scrolls
    // inside it rather than the card running off the screen.
    expect(popoverPos(100, 700, 360, 900, VW, VH)).toMatchObject({ top: 8 });
    expect(popoverPos(0, 0, 360, 400, 200, 200)).toEqual({ left: 8, top: 8 });
  });
});

// ---- The chip's accessible name -------------------------------------------------
// A chip used to carry a bare `aria-label="Repeats"` on its ↻ glyph. That said
// nothing about HOW it repeats, and — a label being a global string — seventeen
// chips answered to the same name as the New task form's recurrence dropdown,
// so anything addressing that control by label hit a chip instead (Akshil,
// 2026-08-17, found in a real browser). The glyph is decorative now and the
// recurrence is spoken in the chip's own name, in the app's existing wording.
import { chipAccessibleName, repeatTextFor } from "./schedule-lib";

describe("repeatTextFor", () => {
  it("reads the rule's own wording — never a second dialect for it", () => {
    const rule = entry({ id: "t1", state: "recurring", rule: { freq: "day" } });
    expect(repeatTextFor("t1", [rule])).toBe("Daily");
    expect(repeatTextFor("t1", [
      entry({ id: "t1", state: "recurring", rule: { freq: "week", interval: 2, byday: [1, 3] } }),
    ])).toBe("Every 2 weeks on Monday, Wednesday");
    // The legacy cron reading still comes through the same door.
    expect(repeatTextFor("t1", [
      entry({ id: "t1", state: "recurring", repeats: "30 9 * * *" }),
    ])).toBe("daily at 09:30");
  });

  it("says nothing rather than guessing when there is no rule to read", () => {
    expect(repeatTextFor("", [])).toBe("");
    expect(repeatTextFor("t1", [])).toBe("");
    // A template row that carries neither a rule nor a cron line.
    expect(repeatTextFor("t1", [entry({ id: "t1", state: "recurring" })])).toBe("");
  });
});

describe("chipAccessibleName", () => {
  it("names the recurrence in words, then the time", () => {
    expect(chipAccessibleName("Pull today's news", "Daily", "5:00 AM"))
      .toBe("Pull today's news — Daily, 5:00 AM");
  });

  it("names the runs that have no chip of their own", () => {
    expect(chipAccessibleName("Pull today's news", "Daily", "5:00 AM", ["7:00 PM"]))
      .toBe("Pull today's news — Daily, 5:00 AM, also 7:00 PM");
    expect(chipAccessibleName("Hourly sweep", "Hourly", "12:00 AM", ["1:00 AM", "2:00 AM"]))
      .toBe("Hourly sweep — Hourly, 12:00 AM, also 1:00 AM, 2:00 AM");
  });

  it("drops the recurrence clause entirely for a one-off", () => {
    expect(chipAccessibleName("Review PRs", "", "9:00 AM")).toBe("Review PRs — 9:00 AM");
    // Never the word "Repeats" on its own — that was the whole defect.
    expect(chipAccessibleName("Review PRs", "", "9:00 AM")).not.toContain("Repeats");
  });

  it("survives a task with no title", () => {
    expect(chipAccessibleName("", "Daily", "5:00 AM")).toBe("Daily, 5:00 AM");
  });
});

describe("chip templateId", () => {
  it("finds the rule anywhere in the day, not just under the anchor", () => {
    // A one-off at 5am holds the chip; the DAILY rule runs at 9. The chip is
    // still a recurring one and must be able to say which rule it belongs to.
    const t = task({
      key: "k",
      messages: [
        msg({ message_id: "one-off", at: at(2026, 7, 17, 5) }),
        msg({ message_id: "daily", at: at(2026, 7, 17, 9), template_id: "t1" }),
      ],
    });
    const days = rangeDays(rangeStart(new Date(2026, 7, 17), "week"), "week");
    const chip = taskChips([t], days).get("2026-08-17")![0];
    expect(chip.anchor.message_id).toBe("one-off");
    expect(chip.recurring).toBe(true);
    expect(chip.templateId).toBe("t1");
    expect(repeatTextFor(chip.templateId, [
      entry({ id: "t1", state: "recurring", rule: { freq: "day" } }),
    ])).toBe("Daily");
  });

  it("a purely one-off day names no rule at all", () => {
    const t = task({ key: "k", messages: [msg({ at: at(2026, 7, 17, 9) })] });
    const days = rangeDays(rangeStart(new Date(2026, 7, 17), "week"), "week");
    const chip = taskChips([t], days).get("2026-08-17")![0];
    expect(chip.recurring).toBe(false);
    expect(chip.templateId).toBe("");
  });
});

// ---- The windowed feed ----------------------------------------------------------
// GET /api/tasks/scheduled — every scheduled message in the visible days. This
// is what stops the grid under-drawing: the task listing ships each task's three
// most recent messages, and a calendar draws a week, so a task whose runs fall
// outside that tail had no chips at all on those days.
import { calendarThreads, groupScheduled, isProjected, windowBounds } from "./schedule-lib";

describe("windowBounds", () => {
  const days = rangeDays(rangeStart(new Date(2026, 7, 17), "week"), "week");

  it("asks from local midnight to the midnight AFTER the last visible day", () => {
    const { from, to } = windowBounds(days);
    expect(from).toBe(Math.floor(new Date(2026, 7, 17).getTime() / 1000));
    // Monday the 17th through Sunday the 23rd — so the bound is the 24th, not
    // the 23rd. `to` is exclusive.
    expect(to).toBe(Math.floor(new Date(2026, 7, 24).getTime() / 1000));
  });

  it("keeps a 23:59 run on the last column inside the window", () => {
    const { from, to } = windowBounds(days);
    const lastMinute = at(2026, 7, 23, 23, 59);
    expect(lastMinute).toBeGreaterThanOrEqual(from);
    expect(lastMinute).toBeLessThan(to); // the boundary the endpoint is tested for
    // And the first instant of the day after is OUTSIDE it.
    expect(at(2026, 7, 24, 0, 0)).toBe(to);
  });

  it("spans a DST day without losing or double-counting an hour of window", () => {
    // 2026-11-01 is 25 hours long; the bounds are still whole local midnights.
    const dst = rangeDays(rangeStart(new Date(2026, 9, 30), "4day"), "4day");
    const { from, to } = windowBounds(dst);
    expect(from).toBe(Math.floor(new Date(2026, 9, 30).getTime() / 1000));
    expect(to).toBe(Math.floor(new Date(2026, 10, 3).getTime() / 1000));
    expect(at(2026, 10, 2, 23, 59)).toBeLessThan(to);
  });

  it("asks for nothing when there is nothing on screen", () => {
    expect(windowBounds([])).toEqual({ from: 0, to: 0 });
  });
});

describe("groupScheduled", () => {
  it("turns the endpoint's flat rows into the per-task threads the grid wants", () => {
    const a = msg({ message_id: "a", at: at(2026, 7, 17, 9) });
    const b = msg({ message_id: "b", at: at(2026, 7, 18, 9) });
    const c = msg({ message_id: "c", at: at(2026, 7, 17, 10) });
    expect(groupScheduled([
      { task_key: "t1", message: a },
      { task_key: "t2", message: c },
      { task_key: "t1", message: b },
    ])).toEqual({ t1: [a, b], t2: [c] });
    expect(groupScheduled([])).toEqual({});
  });
});

describe("calendarThreads", () => {
  const days = rangeDays(rangeStart(new Date(2026, 7, 17), "week"), "week");

  it("draws chips on days the listing's three messages could never reach", () => {
    // What the listing gives: the three most recent, all late in the week.
    const t = task({
      key: "news",
      message_count: 7,
      messages: [21, 22, 23].map((d) =>
        msg({ message_id: `m${d}`, at: at(2026, 7, d, 9), template_id: "t1" }),
      ),
    });

    // Without the window, Monday through Thursday are empty.
    const tail = taskChips([t], days, calendarThreads([t], [], null));
    expect(tail.get("2026-08-17")!.length).toBe(0);
    expect(tail.get("2026-08-21")!.length).toBe(1);

    // With it, every day of the run has its chip.
    const windowed = groupScheduled(
      [17, 18, 19, 20, 21, 22, 23].map((d) => ({
        task_key: "news",
        message: msg({ message_id: `m${d}`, at: at(2026, 7, d, 9), template_id: "t1" }),
      })),
    );
    const full = taskChips([t], days, calendarThreads([t], [], windowed));
    expect(days.every((d) => full.get(dayKey(d))!.length === 1)).toBe(true);
    // Still ONE chip a day, and still one colour across the week.
    expect(new Set(days.map((d) => full.get(dayKey(d))![0].colour)).size).toBe(1);
  });

  it("an hourly rule in-window is one chip with +23, from real windowed rows", () => {
    // The shape the endpoint actually returns: one row per (task, message).
    const items = Array.from({ length: 24 }, (_, h) => ({
      task_key: "hourly",
      message: msg({
        message_id: `MSG-${h}`,
        at: at(2026, 7, 18, h),
        template_id: "t1",
        state: h < 12 ? "sent" : "pending",
        turn: h < 12 ? "done" : "",
      }),
    }));
    // The listing itself only ever knew about the last three of them.
    const t = task({
      key: "hourly",
      message_count: 24,
      messages: items.slice(-3).map((i) => i.message),
    });
    const chips = taskChips([t], days, calendarThreads([t], [], groupScheduled(items)));
    const day = chips.get("2026-08-18")!;
    expect(day.length).toBe(1);
    expect(day[0].extra).toBe(23);
    expect(minutesOfDay(day[0].time)).toBe(0);
    expect(day[0].recurring).toBe(true);
    expect(day[0].templateId).toBe("t1");
    // The morning ran and the afternoon has not — the chip answers for both,
    // and "still work coming" is the honest reading.
    expect(day[0].tone).toBe("upcoming");
  });

  it("falls back to the task's own messages when the window fetch failed", () => {
    const t = task({ key: "k", messages: [msg({ at: at(2026, 7, 19, 9) })] });
    const chips = taskChips([t], days, calendarThreads([t], [], null));
    expect(chips.get("2026-08-19")!.length).toBe(1);
  });

  it("layers projections ON TOP of the window rather than clobbering it", () => {
    const past = msg({ message_id: "ran", at: at(2026, 7, 17, 9), template_id: "t1",
                       state: "sent", turn: "done" });
    const t = task({ key: "k", messages: [past] });
    const windowed = { k: [past] };
    const threads = calendarThreads([t], [
      entry({ id: "t1", state: "recurring", rule: { freq: "day" },
              upcoming: [new Date(2026, 7, 18, 9).toISOString()] }),
    ], windowed);
    // The real run survives, and the projection is added — not swapped in.
    expect(threads.k.map((m) => m.message_id)).toEqual(["ran", "GHOST-" +
      new Date(2026, 7, 18, 9).toISOString()]);
    const chips = taskChips([t], days, threads);
    expect(chips.get("2026-08-17")![0].projected).toBe(false);
    expect(chips.get("2026-08-18")![0].projected).toBe(true);
  });

  it("never double-draws a run the window knows about but the tail does not", () => {
    // The materialized next occurrence is in the WINDOW only — the listing's
    // three messages never mention it. Deduping against the tail alone would
    // have drawn it twice: once real, once projected.
    const soon = msg({ message_id: "next", at: at(2026, 7, 20, 9), template_id: "t1" });
    const t = task({
      key: "k",
      messages: [msg({ message_id: "old", at: at(2026, 6, 1, 9), template_id: "t1" })],
    });
    const threads = calendarThreads([t], [
      entry({ id: "t1", state: "recurring", rule: { freq: "day" },
              upcoming: [new Date(2026, 7, 20, 9).toISOString()] }),
    ], { k: [soon] });
    const chips = taskChips([t], days, threads).get("2026-08-20")!;
    expect(chips.length).toBe(1);
    expect(chips[0].messages.length).toBe(1);
    expect(chips[0].projected).toBe(false); // the REAL one won
  });

  it("finds a rule whose only occurrences live in the window, not the tail", () => {
    const t = task({ key: "k", messages: [] }); // listing knows nothing
    const threads = calendarThreads([t], [
      entry({ id: "t1", state: "recurring", rule: { freq: "day" },
              upcoming: [new Date(2026, 7, 22, 9).toISOString()] }),
    ], { k: [msg({ message_id: "w", at: at(2026, 7, 21, 9), template_id: "t1" })] });
    expect(threads.k.length).toBe(2);
    const chips = taskChips([t], rangeDays(rangeStart(new Date(2026, 7, 17), "week"), "week"), threads);
    expect(chips.get("2026-08-21")!.length).toBe(1);
    expect(chips.get("2026-08-22")!.length).toBe(1);
  });

  it("a task in neither feed keeps its own messages", () => {
    const a = task({ key: "a", messages: [msg({ at: at(2026, 7, 19, 9) })] });
    const b = task({ key: "b", messages: [msg({ at: at(2026, 7, 19, 14) })] });
    const threads = calendarThreads([a, b], [], { a: [msg({ at: at(2026, 7, 20, 9) })] });
    const chips = taskChips([a, b], days, threads);
    expect(chips.get("2026-08-20")!.length).toBe(1); // a, from the window
    expect(chips.get("2026-08-19")!.length).toBe(1); // b, from its own messages
    expect(chips.get("2026-08-19")![0].task.key).toBe("b");
  });
});

// ---- which task a projection hangs on -------------------------------------------
// Under "new task each run" (§6) every past occurrence of a rule is its OWN
// task, so several tasks reference the same template and picking "the first
// match" makes the grid depend on the listing's sort order. Found against live
// data, 2026-08-17: an hourly rule with seven historical tasks.
describe("projection ownership", () => {
  const upcoming = [
    new Date(2026, 7, 18, 9).toISOString(),
    new Date(2026, 7, 19, 9).toISOString(),
  ];
  const rule = () =>
    entry({ id: "t1", state: "recurring", rule: { freq: "day" }, upcoming });
  const ran = (key: string, session: string, day: number) =>
    task({
      key, session_id: session,
      messages: [msg({ message_id: "MSG-001", at: at(2026, 7, day, 9),
                       template_id: "t1", state: "sent", turn: "done" })],
    });
  // The shell §5 keeps for the runs that have not happened: no session yet.
  const shell = task({
    key: "pending:occ-9", session_id: "",
    messages: [msg({ message_id: "MSG-001", at: at(2026, 7, 20, 9), template_id: "t1" })],
  });

  it("hangs future runs on the task that has not run, whatever the sort order", () => {
    for (const tasks of [
      [shell, ran("s1", "sess-1", 16), ran("s2", "sess-2", 17)],
      [ran("s1", "sess-1", 16), ran("s2", "sess-2", 17), shell],
    ]) {
      const out = projectedMessages(tasks, [rule()]);
      expect(Object.keys(out)).toEqual(["pending:occ-9"]);
      expect(out["pending:occ-9"].filter(isProjected).length).toBe(2);
    }
  });

  it("falls back to the task that DID run when every occurrence has a session", () => {
    // The chained case: one task holds the whole thread, so it is the only
    // candidate and the projections belong to it.
    const chained = task({
      key: "sess-1", session_id: "sess-1",
      messages: [msg({ at: at(2026, 7, 17, 9), template_id: "t1" })],
    });
    const out = projectedMessages([chained], [rule()]);
    expect(Object.keys(out)).toEqual(["sess-1"]);
    expect(out["sess-1"].filter(isProjected).length).toBe(2);
  });

  it("a rule that has never run at all still finds its pending shell", () => {
    const never = task({ key: "pending:t1", session_id: "", messages: [] });
    const out = projectedMessages([never], [rule()]);
    expect(out["pending:t1"].length).toBe(2);
  });

  it("the already-run occurrence keeps its own chip and its own colour", () => {
    // Two chips on the day the rule both ran and is still due — they are two
    // TASKS, which is exactly what "unrelated tasks are unrelated chips" means.
    const days = rangeDays(rangeStart(new Date(2026, 7, 17), "week"), "week");
    const done = ran("sess-1", "sess-1", 17);
    const pending = task({
      key: "pending:occ-9", session_id: "",
      messages: [msg({ message_id: "MSG-001", at: at(2026, 7, 17, 21),
                       template_id: "t1" })],
    });
    const chips = taskChips([pending, done], days,
      calendarThreads([pending, done], [rule()], null)).get("2026-08-17")!;
    expect(chips.length).toBe(2);
    expect(chips.map((c) => c.task.key)).toEqual(["sess-1", "pending:occ-9"]);
    expect(chips[0].colour).not.toBe(chips[1].colour);
    expect(chips[0].tone).toBe("ran");
    expect(chips[1].tone).toBe("upcoming");
  });
});
