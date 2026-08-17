// The rules for the queue's rows in the one bottom-right activity card — the one
// global surface for work that is about to run or running now. Everything that
// could be got subtly wrong here is about WHICH work that is: past due only, one
// row per entry, one row per unit of work across BOTH halves of the card, and a
// Cancel all that counts only what it can actually withdraw.
import { describe, expect, it } from "bun:test";
import type { ScheduledMessage } from "@platform/lib/api";
import { jobRows, jobsSummary, SCHEDULE_JOB_PREFIX, type Job } from "@platform/lib/jobs";
import {
  queueCount,
  queueRows,
  roleText,
  rowCancelKind,
  showCancelAll,
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

  it("names the claimed state as its own, and says why it has no cancel", () => {
    // The row loses its ✕ at this point, so the line has to explain the absence —
    // a row that goes quiet with no control and no sentence reads as stuck.
    expect(roleText({ entry: entry(), role: "sending" }, "")).toBe(
      "Starting… · too late to cancel",
    );
  });
});

describe("cancel", () => {
  it("routes a live turn to the job registry and a queued one to the queue", () => {
    // Different promises: un-sending a message the sender has not taken yet, and
    // stopping a process that is running.
    expect(rowCancelKind({ entry: entry(), role: "live" })).toBe("job");
    expect(rowCancelKind({ entry: entry(), role: "queued" })).toBe("queued");
  });

  it("offers a claimed row no cancel at all — the server refuses every one", () => {
    // `schedule.cancel_queued` allows exactly pending -> cancelled and refuses
    // `sending` on purpose ("the helper is already away"). A button whose only
    // possible outcome is a refusal is worse than no button.
    expect(rowCancelKind({ entry: entry({ state: "sending" }), role: "sending" })).toBe(
      "none",
    );
  });

  it("counts for Cancel all only what Cancel all can take", () => {
    // live, claimed, and two waiting: Cancel all can withdraw the two.
    const rows = queueRows(
      [entry({ id: "a", state: "sent" })],
      [entry({ id: "b", state: "sending" })],
      [entry({ id: "c" }), entry({ id: "d" })],
    );
    expect(withdrawableCount(rows)).toBe(2);
    expect(showCancelAll(rows)).toBe(true);
  });

  it("hides Cancel all when nothing on the card can be withdrawn", () => {
    // A dock full of work that has already gone: "all" would name zero messages.
    const gone = queueRows(
      [entry({ id: "a", state: "sent" })],
      [entry({ id: "b", state: "sending" }), entry({ id: "c", state: "sending" })],
      [],
    );
    expect(withdrawableCount(gone)).toBe(0);
    expect(showCancelAll(gone)).toBe(false);
    // And for one withdrawable row the row's own ✕ is the same action, named.
    const one = queueRows([], [entry({ id: "b", state: "sending" })], [entry({ id: "c" })]);
    expect(showCancelAll(one)).toBe(false);
  });

  it("changes the control set as one entry moves queued → sending → live", () => {
    // The three snapshots the dock actually polls, for the SAME entry. What must
    // not happen is a control decided once when the row appeared: waiting offers
    // a withdrawal, claimed offers nothing, in flight offers the job stop.
    const id = "e1";
    const waiting = queueRows([], [], [entry({ id })]);
    const claimed = queueRows([], [entry({ id, state: "sending" })], []);
    const flight = queueRows([entry({ id, state: "sent" })], [], []);
    expect([waiting, claimed, flight].map((rows) => rowCancelKind(rows[0]))).toEqual([
      "queued",
      "none",
      "job",
    ]);
    // and only the waiting snapshot counts toward Cancel all
    expect([waiting, claimed, flight].map(withdrawableCount)).toEqual([1, 0, 0]);
  });
});

describe("queueCount", () => {
  it("calls only an unclaimed message waiting — a claimed one has already gone", () => {
    // The header count's whole job is to not overstate. `sending` means the
    // scheduler took the entry and the helper is away (which is why the row has no
    // cancel at all), so counting it as "queued" would describe a message that is
    // no longer withdrawable as if it were.
    const rows = queueRows(
      [entry({ id: "a", state: "sent" })],
      [entry({ id: "b", state: "sending" })],
      [entry({ id: "c" }), entry({ id: "d" })],
    );
    expect(queueCount(rows)).toEqual({ waiting: 2, running: 2 });
    expect(queueCount([])).toEqual({ waiting: 0, running: 0 });
  });
});

describe("one card, one count", () => {
  // The header the merged card prints. Its counts come from the queue's rows and
  // its job rows together, because there is one list and one header over it.
  it("says queued, not running, when nothing has actually begun", () => {
    const waiting = queueCount(queueRows([], [], [entry({ id: "c" }), entry({ id: "d" })]));
    expect(jobsSummary([], waiting)).toBe("2 queued");
    expect(jobsSummary([], { waiting: 1, running: 0 })).toBe("1 queued");
  });

  it("counts the queue and the jobs as one number once anything is running", () => {
    const mixed = queueCount(queueRows([entry({ id: "a", state: "sent" })], [], [entry({ id: "c" })]));
    // one live turn + one waiting message + one running download = three
    expect(jobsSummary([job({ id: "d", kind: "download" })], mixed)).toBe("3 running");
  });

  it("keeps 'downloading' for a card whose work really is only downloads", () => {
    const jobs = [job({ id: "d", kind: "download" })];
    expect(jobsSummary(jobs, { waiting: 0, running: 0 })).toBe("1 downloading");
    // one scheduled message alongside it and the noun has to generalise
    expect(jobsSummary(jobs, { waiting: 1, running: 0 })).toBe("2 running");
  });

  it("describes what finished only when nothing is happening at all", () => {
    const done = [job({ id: "a", state: "done" }), job({ id: "b", state: "done" })];
    expect(jobsSummary(done, { waiting: 0, running: 0 })).toBe("2 finished");
    // a queued message outranks the finished rows: it is the news
    expect(jobsSummary(done, { waiting: 1, running: 0 })).toBe("1 queued");
  });
});

describe("jobRows", () => {
  it("gives a live scheduled run one row, not one in each half of the card", () => {
    // Its queue row is directly above, carries the link to the session and prints
    // this very job's status line. A job row beside it is the same run twice.
    const jobs = [
      job({ id: `${SCHEDULE_JOB_PREFIX}e1`, state: "running" }),
      job({ id: "sys:ai-model:repo", state: "running" }),
    ];
    expect(jobRows(jobs).map((j) => j.id)).toEqual(["sys:ai-model:repo"]);
  });

  it("keeps a finished scheduled run — that row is the end of the lifecycle", () => {
    // queued → starting → running → finished/failed, in one list: the entry has
    // left the server's queue by now, so this row is all there is to say so.
    const jobs = [
      job({ id: `${SCHEDULE_JOB_PREFIX}e1`, state: "error", message: "boom" }),
      job({ id: `${SCHEDULE_JOB_PREFIX}e2`, state: "done" }),
    ];
    expect(jobRows(jobs)).toHaveLength(2);
  });
});
