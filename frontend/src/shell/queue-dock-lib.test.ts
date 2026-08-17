// The queue dock's rules. The card is the one global surface for work that is
// about to run or running now, and everything that could be got subtly wrong
// here is about WHICH work that is: past due only, one row per entry, and a
// Cancel all that counts only what it can actually withdraw.
import { describe, expect, it } from "bun:test";
import type { ScheduledMessage } from "@platform/lib/api";
import { dockJobs, SCHEDULE_JOB_PREFIX, type Job } from "@platform/lib/jobs";
import {
  queueRows,
  roleText,
  rowCancelKind,
  withdrawableCount,
} from "./queue-dock-lib";

const entry = (over: Partial<ScheduledMessage> = {}): ScheduledMessage =>
  ({
    id: "e1",
    target: "/Users/x/proj",
    message: "pull today's news",
    due: new Date(Date.now() - 120_000).toISOString(),
    session_id: "",
    permission_mode: "",
    state: "pending",
    created: "",
    fired: "",
    run_id: "",
    error: "",
    ...over,
  }) as ScheduledMessage;

const job = (over: Partial<Job> = {}): Job =>
  ({
    id: "sys:ai-model:repo",
    title: "Downloading",
    detail: "",
    kind: "task",
    state: "running",
    done: null,
    total: null,
    unit: "",
    message: "",
    page: "",
    owner: "server",
    cancellable: true,
    cancel_requested: false,
    stalled: false,
    started_at: 0,
    updated_at: 0,
    finished_at: null,
    ...over,
  }) as Job;

describe("queueRows", () => {
  it("draws only what the server handed over — nothing is promoted here", () => {
    // The whole past-due-only rule is the server's (`GET /api/schedule/queue`).
    // What this proves is that the card adds nothing: three lists in, the same
    // three entries out, in the order work has already got to.
    const rows = queueRows(
      [entry({ id: "live", state: "sent" })],
      [entry({ id: "claimed", state: "sending" })],
      [entry({ id: "waiting" })],
    );
    expect(rows.map((r) => r.entry.id)).toEqual(["live", "claimed", "waiting"]);
    expect(rows.map((r) => r.role)).toEqual(["live", "sending", "queued"]);
  });

  it("shows an entry once, in the state it has actually reached", () => {
    // The claim races the read: an entry can be in `queued` from one list and
    // `running` from the next. Two rows for one message, in two tenses, is the
    // failure — and the row that survives must be the later of the two.
    const rows = queueRows([], [entry({ id: "e1", state: "sending" })], [entry({ id: "e1" })]);
    expect(rows).toHaveLength(1);
    expect(rows[0].role).toBe("sending");
  });

  it("has nothing to draw when nothing is queued or running", () => {
    expect(queueRows([], [], [])).toEqual([]);
    expect(queueRows(undefined, undefined, undefined)).toEqual([]);
  });
});

describe("roleText", () => {
  it("prefers the job registry's line for a live turn — the reason it exists", () => {
    const row = { entry: entry({ state: "sent" }), role: "live" as const };
    expect(roleText(row, "waiting for permission")).toBe("waiting for permission");
    // No tick yet: still honest, just less useful.
    expect(roleText(row, "")).toBe("Running");
  });

  it("says how long a queued message has been waiting", () => {
    const row = { entry: entry(), role: "queued" as const };
    expect(roleText(row, "")).toBe("Queued · due 2m ago");
  });

  it("names the claimed state as its own", () => {
    expect(roleText({ entry: entry(), role: "sending" }, "")).toBe("Starting…");
  });
});

describe("cancel", () => {
  it("routes a live turn to the job registry and everything else to the queue", () => {
    // Different promises: un-sending a message the sender has not taken yet, and
    // stopping a process that is running.
    expect(rowCancelKind({ entry: entry(), role: "live" })).toBe("job");
    expect(rowCancelKind({ entry: entry(), role: "sending" })).toBe("queued");
    expect(rowCancelKind({ entry: entry(), role: "queued" })).toBe("queued");
  });

  it("counts for Cancel all only what Cancel all can take", () => {
    const rows = queueRows(
      [entry({ id: "a" })],
      [entry({ id: "b" })],
      [entry({ id: "c" }), entry({ id: "d" })],
    );
    expect(withdrawableCount(rows)).toBe(3);
  });
});

describe("dockJobs", () => {
  it("leaves the live scheduled run to the dock above, and keeps everything else", () => {
    const jobs = [
      job({ id: `${SCHEDULE_JOB_PREFIX}e1`, state: "running" }),
      job({ id: "sys:ai-model:repo", state: "running" }),
    ];
    expect(dockJobs(jobs).map((j) => j.id)).toEqual(["sys:ai-model:repo"]);
  });

  it("keeps a finished scheduled run — that row is the outcome report", () => {
    const jobs = [
      job({ id: `${SCHEDULE_JOB_PREFIX}e1`, state: "error", message: "boom" }),
      job({ id: `${SCHEDULE_JOB_PREFIX}e2`, state: "done" }),
    ];
    expect(dockJobs(jobs)).toHaveLength(2);
  });
});
