// The rules for the queue's rows in the one bottom-right activity card — the one
// global surface for work that is about to run or running now. Everything that
// could be got subtly wrong here is about WHICH work that is: past due only, one
// row per entry, one row per unit of work across BOTH halves of the card, and a
// Cancel all that counts only what it can actually withdraw.
import { describe, expect, it } from "bun:test";
import type { ScheduledMessage } from "@platform/lib/api";
import {
  foldedJobRows,
  jobRows,
  jobsSummary,
  rowsShown,
  SCHEDULE_JOB_PREFIX,
  type Job,
} from "@platform/lib/jobs";
import {
  drawnIds,
  openRows,
  queueCount,
  queueRows,
  roleText,
  rowCancelKind,
  scheduleRunsEnded,
  scheduleRunsStarted,
  showCancelAll,
  withdrawableCount,
  type QueueRow,
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

  it("names the running and the waiting halves separately in a mix", () => {
    const mixed = queueCount(queueRows([entry({ id: "a", state: "sent" })], [], [entry({ id: "c" })]));
    // One live turn + one running download are underway; the past-due message is
    // not. Adding all three into "3 running" claimed the queue had already begun
    // — the same lie the pure-queue case above exists to avoid.
    expect(jobsSummary([job({ id: "d", kind: "download" })], mixed)).toBe("2 running · 1 queued");
  });

  it("keeps 'downloading' for a card whose work really is only downloads", () => {
    const jobs = [job({ id: "d", kind: "download" })];
    expect(jobsSummary(jobs, { waiting: 0, running: 0 })).toBe("1 downloading");
    // one scheduled message alongside it and the noun has to generalise
    expect(jobsSummary(jobs, { waiting: 1, running: 0 })).toBe("1 running · 1 queued");
  });

  it("describes what finished only when nothing is happening at all", () => {
    const done = [job({ id: "a", state: "done" }), job({ id: "b", state: "done" })];
    expect(jobsSummary(done, { waiting: 0, running: 0 })).toBe("2 finished");
    // a queued message outranks the finished rows: it is the news
    expect(jobsSummary(done, { waiting: 1, running: 0 })).toBe("1 queued");
  });
});

describe("the fold hides detail, never a control", () => {
  // The collapse is a PERSISTED preference, and it was set against the card as it
  // used to be: a download history worth folding away. Merging the queue into that
  // card put every queue row behind the same fold — and a queue row's ✕ is the only
  // cancel a queued message or a live turn has, so a card folded weeks ago left
  // scheduled work arriving with no reachable way to stop it. The rule that fixes
  // it is jobs.ts `rowsShown`, and these are the cases it has to get right.
  it("keeps a single queued row on screen while the card is folded — with its ✕", () => {
    // The sharpest case: ONE waiting message, so Cancel all is (rightly) not
    // offered — the row's own ✕ is the same action with a better name on it. That
    // ✕ is therefore the only cancel in the card, and folding must not eat it.
    const rows = queueRows([], [], [entry({ id: "c" })]);
    expect(showCancelAll(rows)).toBe(false);
    expect(rowsShown(true, queueCount(rows))).toEqual({ queue: true, jobs: false });
    expect(rowCancelKind(rows[0])).toBe("queued");
  });

  it("keeps a live turn's stop reachable while folded", () => {
    // Not a queued row at all — a running process, whose ✕ is the job registry's
    // cancelJob("sys:schedule:<id>"). Cancel all never covers it (it withdraws
    // messages that have not gone), so the row is the only way to stop it.
    const rows = queueRows([entry({ id: "a", state: "sent" })], [], []);
    expect(rowsShown(true, queueCount(rows))).toEqual({ queue: true, jobs: false });
    expect(rowCancelKind(rows[0])).toBe("job");
  });

  it("still offers a claimed row nothing at all, folded or open", () => {
    // Reachability is not "a button everywhere": the server refuses `sending`
    // every time, so this row shows the spinner and says why, in both states.
    const rows = queueRows([], [entry({ id: "b", state: "sending" })], []);
    const count = queueCount(rows);
    expect([rowsShown(true, count).queue, rowsShown(false, count).queue]).toEqual([true, true]);
    expect(rowCancelKind(rows[0])).toBe("none");
    expect(roleText(rows[0], "")).toBe("Starting… · too late to cancel");
  });

  it("folds the job rows away — which is what the preference was set for", () => {
    // A downloads-only card, collapsed, is exactly what it always was: no list,
    // just the header and its overall bar. Open, the job rows are back.
    const nothingQueued = { waiting: 0, running: 0 };
    expect(rowsShown(true, nothingQueued)).toEqual({ queue: false, jobs: false });
    expect(rowsShown(false, nothingQueued)).toEqual({ queue: false, jobs: true });
    // and a folded card with queue work shows the queue half only
    expect(rowsShown(true, { waiting: 1, running: 2 })).toEqual({ queue: true, jobs: false });
  });
});

// ---------------------------------------------------- one row, never two, never none

/**
 * The ONE list as the card would actually draw it, for one moment in time: the queue
 * half's rows (`q:<entry id>`) followed by the job rows it draws beside them
 * (`j:<job id>`). Mirrors QueueDock and DownloadManager exactly — the same calls in
 * the same order — because the property under test is about the two halves TOGETHER,
 * and neither half alone can be wrong or right about it.
 *
 * `queueJobs` is the fourth argument and the reason this helper exists at all: the two
 * halves do not have to be looking at the same job snapshot. The card polls
 * /api/jobs about once a second and hands the result up (`QueueSlot.onJobs`), so the
 * queue half is at worst one render behind — and it was six seconds behind when it had
 * its own poll, which is the window this bug lived in. Defaults to `reported` (the
 * halves agreed); pass an older list to sit inside the window on purpose.
 */
function shownRows(
  rows: QueueRow[],
  reported: Job[],
  collapsed = false,
  queueJobs: Job[] = reported,
): string[] {
  const open = openRows(rows, queueJobs);
  const jobs = jobRows(reported, drawnIds(open));
  const shown = rowsShown(collapsed, queueCount(open));
  const listed = shown.jobs ? jobs : foldedJobRows(jobs);
  return [
    ...(shown.queue ? open.map((r) => `q:${r.entry.id}`) : []),
    ...listed.map((j) => `j:${j.id}`),
  ];
}

const LIVE_JOB = `${SCHEDULE_JOB_PREFIX}e1`;
const liveJob = (over: Partial<Job> = {}) =>
  job({ id: LIVE_JOB, kind: "task", state: "running", cancellable: true, ...over });

describe("jobRows: which half owns a scheduled run", () => {
  it("gives a live run one row, not one in each half of the card", () => {
    // The queue row carries the link to the session and prints this very job's
    // status line. A job row beside it is the same run twice, each saying half.
    const rows = queueRows([entry({ id: "e1", state: "sent" })], [], []);
    const reported = [liveJob(), job({ id: "sys:ai-model:repo" })];
    expect(jobRows(reported, drawnIds(rows)).map((j) => j.id)).toEqual(["sys:ai-model:repo"]);
    expect(shownRows(rows, reported)).toEqual(["q:e1", "j:sys:ai-model:repo"]);
  });

  it("keeps the run when the queue read FAILED — one row, with its stop", () => {
    // The hole this closes. `GET /api/schedule/queue` errors or times out, so the
    // queue half has no rows (after a failed first read there is no last snapshot to
    // keep). The job half used to drop the run anyway, on the assumption that a row
    // it cannot see was drawing it, and a turn that was genuinely executing then had
    // no row anywhere and no reachable cancelJob("sys:schedule:<id>").
    const reported = [liveJob()];
    expect(jobRows(reported, drawnIds([])).map((j) => j.id)).toEqual([LIVE_JOB]);
    expect(shownRows([], reported)).toEqual([`j:${LIVE_JOB}`]);
  });

  it("keeps the run when the card is mounted BARE, with no queue slot at all", () => {
    // Same outcome by construction rather than by failure: NotificationHost falls
    // back to a plain <DownloadManager /> when no shell composed one, so nothing
    // fills the slot. Told nothing means "draw it yourself".
    expect(jobRows([liveJob()]).map((j) => j.id)).toEqual([LIVE_JOB]);
    expect(jobRows([liveJob()], undefined).map((j) => j.id)).toEqual([LIVE_JOB]);
    expect(jobRows([liveJob()], null).map((j) => j.id)).toEqual([LIVE_JOB]);
  });

  it("drops only the runs it was TOLD about — a second live turn keeps its row", () => {
    // The old rule was per-CATEGORY ("anything running whose id looks scheduled"),
    // which is why one missing row cost every run its row. This one is per-run.
    const rows = queueRows([entry({ id: "e1", state: "sent" })], [], []);
    const reported = [liveJob(), liveJob({ id: `${SCHEDULE_JOB_PREFIX}e2` })];
    expect(jobRows(reported, drawnIds(rows)).map((j) => j.id)).toEqual([
      `${SCHEDULE_JOB_PREFIX}e2`,
    ]);
  });

  it("draws a finished run's outcome as soon as it OWNS it, and not one instant before", () => {
    // queued → starting → running → finished/failed, in one list: the entry has left
    // the server's queue by now, so the job row is all there is to say what happened.
    //
    // But it is drawn by whichever half owns the run, not by whichever half has news.
    // A terminal row used to be exempt from `drawn` on the theory that a finished run
    // cannot also be a queue row — true of the server, false of two clocks reading it
    // (this list is polled about every second, the queue's every six), so the outcome
    // row appeared beside a live queue row for the same run. `drawn` therefore wins
    // whatever the state, and it is `openRows` — reading this very snapshot — that
    // hands the run over promptly rather than a poll later.
    const jobs = [
      job({ id: LIVE_JOB, state: "error", message: "boom" }),
      job({ id: `${SCHEDULE_JOB_PREFIX}e2`, state: "done" }),
    ];
    expect(jobRows(jobs)).toHaveLength(2);
    expect(jobRows(jobs, ["e1", "e2"])).toHaveLength(0);
  });

  it("covers every role, so a re-queued entry is not drawn in two tenses", () => {
    // run-now / resend can put an entry back in the queue while the previous run's
    // job row is still `running`. drawnIds names queued and sending rows too, so the
    // job half stays quiet and the queue's row — the current tense — is the one.
    const rows = queueRows([], [], [entry({ id: "e1" })]);
    expect(drawnIds(rows)).toEqual(["e1"]);
    expect(shownRows(rows, [liveJob()])).toEqual(["q:e1"]);
  });
});

describe("openRows: when this half hands a run over", () => {
  // The other half of the one-row rule. `jobRows` makes a duplicate impossible by
  // dropping every drawn run, terminal or not; this is what stops that costing the
  // outcome row a poll — or, with the queue read failing and its last snapshot kept,
  // costing it forever.
  it("retires a live row whose run the registry says has ENDED", () => {
    const rows = queueRows([entry({ id: "e1", state: "sent" })], [], []);
    expect(openRows(rows, [liveJob({ state: "done", detail: "finished" })])).toEqual([]);
    expect(openRows(rows, [liveJob({ state: "error", message: "boom" })])).toEqual([]);
    expect(openRows(rows, [liveJob({ state: "cancelled" })])).toEqual([]);
  });

  it("keeps a live row while the turn is still going — or has not reported yet", () => {
    // Absent is not ended: the spawn writes the first report a moment after the entry
    // becomes `sent`, and a row that vanished in that gap would be a running turn with
    // no stop anywhere. Nor does STALLED mean ended — that is the app admitting it has
    // stopped hearing, and the queue row is where this run's stop lives.
    const rows = queueRows([entry({ id: "e1", state: "sent" })], [], []);
    expect(openRows(rows, [])).toEqual(rows);
    expect(openRows(rows, [job({ id: "sys:ai-model:repo", state: "done" })])).toEqual(rows);
    expect(openRows(rows, [liveJob()])).toEqual(rows);
    expect(openRows(rows, [liveJob({ stalled: true })])).toEqual(rows);
  });

  it("never retires a queued or sending row, whatever its job says", () => {
    // A re-queued entry (run-now, resend): the terminal job belongs to the PREVIOUS
    // run and this row is the current tense. The job half is already dropping that
    // stale row because the entry is drawn, so retiring this one as well would leave
    // the entry with no row in either half.
    const done = [liveJob({ state: "done" })];
    const waiting = queueRows([], [], [entry({ id: "e1" })]);
    const claimed = queueRows([], [entry({ id: "e1", state: "sending" })], []);
    expect(openRows(waiting, done)).toEqual(waiting);
    expect(openRows(claimed, done)).toEqual(claimed);
    expect(shownRows(waiting, done)).toEqual(["q:e1"]);
    expect(shownRows(claimed, done)).toEqual(["q:e1"]);
  });

  it("retires only the run that ended, and leaves the count to match the rows", () => {
    // Two live turns, one finished. The header count comes off the same filtered
    // array, so it cannot describe a row that is no longer there.
    const rows = queueRows(
      [entry({ id: "e1", state: "sent" }), entry({ id: "e2", state: "sent" })],
      [],
      [entry({ id: "e3" })],
    );
    const jobs = [liveJob({ state: "done" }), liveJob({ id: `${SCHEDULE_JOB_PREFIX}e2` })];
    const open = openRows(rows, jobs);
    expect(open.map((r) => r.entry.id)).toEqual(["e2", "e3"]);
    expect(queueCount(open)).toEqual({ waiting: 1, running: 1 });
    expect(shownRows(rows, jobs)).toEqual(["q:e2", "q:e3", `j:${LIVE_JOB}`]);
  });
});

describe("one scheduled run, one row, at every step of its life", () => {
  // queued → sending → live → finished, as the two halves see it. What must not
  // happen at ANY step is two rows (the same turn twice in one list) or none (a
  // running turn with no title and no stop). The steps are the server's:
  // schedule.queue() lists `pending` past due and `sending`, the router adds `sent`
  // with no turn as `live`, and the job registry's row is written when the helper
  // spawns and finishes with the turn.
  const queued = queueRows([], [], [entry({ id: "e1" })]);
  const sending = queueRows([], [entry({ id: "e1", state: "sending" })], []);
  const live = queueRows([entry({ id: "e1", state: "sent" })], [], []);

  it("has exactly one owner at each step, healthy queue read", () => {
    // No job row exists before the spawn (`_send` writes the first one), and the
    // entry has left every queue list by the time the row goes terminal.
    expect(shownRows(queued, [])).toEqual(["q:e1"]);
    expect(shownRows(sending, [])).toEqual(["q:e1"]);
    expect(shownRows(live, [liveJob()])).toEqual(["q:e1"]);
    expect(shownRows([], [liveJob({ state: "done", detail: "finished" })])).toEqual([
      `j:${LIVE_JOB}`,
    ]);
  });

  it("has exactly one owner at each step with the queue read FAILING throughout", () => {
    // No queue rows at all. The first two steps are invisible either way — there is
    // no job row yet, and nothing this card can do about a queue it cannot read —
    // but the step that MATTERS, a turn in flight, keeps its row and its stop.
    expect(shownRows([], [])).toEqual([]);
    expect(shownRows([], [liveJob()])).toEqual([`j:${LIVE_JOB}`]);
    expect(shownRows([], [liveJob({ state: "done" })])).toEqual([`j:${LIVE_JOB}`]);
  });

  it("draws one row in the TRANSITION WINDOW — the job half knows, the queue half does not", () => {
    // The step that was missing, and the bug. The turn ends: this half's fast poll
    // (~1s) has a terminal job row while the queue half is still holding a snapshot
    // that calls the same entry live. With terminal rows exempt from `drawn` that was
    // an outcome row AND a live row for one run, for as long as the queue half took to
    // catch up — the two-rows-one-run this card exists not to do.
    const ended = [liveJob({ state: "done", detail: "finished" })];
    // Inside the window: the queue half has not seen the terminal job yet (`[liveJob()]`
    // is its older snapshot), so it still owns the run and draws it — once.
    expect(shownRows(live, ended, false, [liveJob()])).toEqual(["q:e1"]);
    expect(shownRows(live, ended, true, [liveJob()])).toEqual(["q:e1"]);
    // The instant it sees the same snapshot this half is acting on — one render later,
    // not one poll later — it retires its row and the outcome row is the one. Note the
    // QUEUE READ is still stale here (`live` still lists the entry): the handover does
    // not wait on it, so a failing queue endpoint cannot strand the outcome.
    expect(shownRows(live, ended)).toEqual([`j:${LIVE_JOB}`]);
    // and folded it is history like the rest of it — one row or none, never two
    expect(shownRows(live, ended, true)).toEqual([]);
  });

  it("draws one row in the MIRROR image — the queue says gone, the job says running", () => {
    // The other direction, and it is not a race the server can lose: schedule.py
    // writes the entry's `turn` verdict BEFORE reporting the job terminal, and
    // /api/schedule/queue calls live exactly "sent with no turn" — so an entry leaves
    // the queue lists while its job row is still `running`, every single run. It is
    // also what a failed queue read looks like. Either way the job half owns it: one
    // row, with the ✕ that really stops the process, through the fold as well.
    expect(shownRows([], [liveJob()])).toEqual([`j:${LIVE_JOB}`]);
    expect(shownRows([], [liveJob()], true)).toEqual([`j:${LIVE_JOB}`]);
    // and the reverse skew of the same moment — this half's snapshot still says live
    // while the queue read has already dropped the entry — is the same one row.
    expect(shownRows([], [liveJob()], false, [])).toEqual([`j:${LIVE_JOB}`]);
  });

  it("never draws the same run twice — every step, healthy or degraded, folded or not", () => {
    // The other failure mode, and the one the old unconditional drop was protecting
    // against. Every moment the pair of halves can actually be in, times both fold
    // states: exactly one row for this run, never two and never zero.
    // `inFlight` — work still going, so its row must survive the fold as well. A
    // FINISHED run is history and folds with the rest of it; what it must never do
    // is appear twice, which is the half of the property that holds everywhere.
    // The fifth field is the QUEUE half's job snapshot when it differs from this
    // half's — the transition window, where the two halves disagree about whether the
    // run is over and the duplicate used to appear.
    const ended = liveJob({ state: "done", detail: "finished" });
    const steps: Array<[string, QueueRow[], Job[], boolean, Job[]?]> = [
      ["queued", queued, [], true],
      ["sending", sending, [], true],
      ["live", live, [liveJob()], true],
      ["finished", [], [ended], false],
      // the in-flight moment with the queue read failing
      ["live, no queue read", [], [liveJob()], true],
      // and a stale running row against a queue row in each earlier tense, which is
      // what a re-queued entry (run-now, resend) looks like mid-transition
      ["queued, stale job row", queued, [liveJob()], true],
      ["sending, stale job row", sending, [liveJob()], true],
      // THE TRANSITION WINDOW, both ways round. The job half knows the turn ended and
      // the queue half is a snapshot behind (the queue row is the one row); then it
      // catches up while its READ is still stale (the outcome row is the one row).
      ["ended, queue half behind", live, [ended], true, [liveJob()]],
      ["ended, queue read still stale", live, [ended], false],
      // the mirror image: the entry has left every queue list — which is what the
      // server does first, every run — while the job row is still running
      ["still running, entry already gone", [], [liveJob()], true, []],
    ];
    for (const [what, rows, reported, inFlight, queueJobs] of steps) {
      // Paired with the label so a failure names the step rather than the number.
      expect([what, shownRows(rows, reported, false, queueJobs ?? reported).length]).toEqual([
        what,
        1,
      ]);
      expect([what, shownRows(rows, reported, true, queueJobs ?? reported).length]).toEqual([
        what,
        inFlight ? 1 : 0,
      ]);
    }
  });

  it("keeps the stop reachable while the card is FOLDED, in both halves", () => {
    // The fold is a persisted preference set against a download history. A queue row
    // goes through it (rowsShown), and so must the job row standing in for one when
    // the queue read failed — otherwise the hole reopens for anyone whose card has
    // been collapsed since before any of this existed.
    expect(shownRows(live, [liveJob()], true)).toEqual(["q:e1"]);
    expect(shownRows([], [liveJob()], true)).toEqual([`j:${LIVE_JOB}`]);
  });

  it("still folds away everything the preference was set to fold", () => {
    // A download and a scheduled run that ENDED are history, not unattended work:
    // one ✕ is a request for work the user started, the other row is a report.
    const history = [
      job({ id: "sys:ai-model:repo", kind: "download" }),
      liveJob({ state: "done" }),
    ];
    expect(foldedJobRows(history)).toEqual([]);
    expect(shownRows([], history, true)).toEqual([]);
    expect(shownRows([], history, false)).toEqual(["j:sys:ai-model:repo", `j:${LIVE_JOB}`]);
  });

  it("counts the stand-in row in the one header, and leaves no empty card", () => {
    // The count comes off the same two numbers as the rows. With the queue read
    // failing, the run is a job row — so the header must say "1 running" from the
    // job side rather than falling through to "0 finished" and drawing a card with
    // no rows in it. Nothing anywhere ⇒ nothing at all, exactly as before.
    const failed = jobRows([liveJob()], drawnIds([]));
    expect(jobsSummary(failed, queueCount([]))).toBe("1 running");
    expect(shownRows([], []).length).toBe(0);
    expect(jobRows([], drawnIds([]))).toEqual([]);
  });
});

describe("scheduleRunsEnded: the moment the two surfaces sync", () => {
  // A scheduled run's status lives on two surfaces at two cadences: this card's
  // job snapshot (~1s behind the turn) and the Tasks page's own poll (20s, plus
  // the server's liveness window on top). "If finished in one, finished in the
  // other" (Akshil, 2026-08-19) — so the running→terminal flip detected here is
  // what QueueDock pokes the shared tasks store with (tasksPulse.pokeTasks).
  const running = () => job({ id: `${SCHEDULE_JOB_PREFIX}e1` });

  it("fires on a run flipping running → terminal", () => {
    for (const state of ["done", "error", "cancelled"] as const) {
      expect(scheduleRunsEnded([running()], [job({ id: `${SCHEDULE_JOB_PREFIX}e1`, state })])).toBe(
        true,
      );
    }
  });

  it("fires on a running run vanishing — a Clear between polls still ended it", () => {
    expect(scheduleRunsEnded([running()], [])).toBe(true);
  });

  it("does not fire on history: a job first seen already terminal is not news", () => {
    // The card mounts onto whatever the registry still displays. Poking on that
    // would refetch the tasks on every mount for a run that ended an hour ago.
    expect(scheduleRunsEnded([], [job({ id: `${SCHEDULE_JOB_PREFIX}e1`, state: "done" })])).toBe(
      false,
    );
    expect(scheduleRunsEnded([], [])).toBe(false);
  });

  it("stays quiet while the run is still going, stalled included", () => {
    expect(scheduleRunsEnded([running()], [running()])).toBe(false);
    // Stalled is the app admitting it stopped hearing, not the turn ending.
    expect(
      scheduleRunsEnded([running()], [job({ id: `${SCHEDULE_JOB_PREFIX}e1`, stalled: true })]),
    ).toBe(false);
  });

  it("only scheduled runs count — a download finishing is not a task event", () => {
    expect(
      scheduleRunsEnded([job({ id: "sys:ai-model:repo" })], [
        job({ id: "sys:ai-model:repo", state: "done" }),
      ]),
    ).toBe(false);
  });
});

// ---- the live row's shimmer -----------------------------------------------------
// Akshil, 2026-08-19: a run in flight wore the same muted grey as a row merely
// waiting. The sentence of a LIVE row now wears the app's one "running"
// treatment — the In Progress yellow with the travelling band, the sidebar's
// recipe. Source pins, because the decision is one ternary in the view and the
// treatment one CSS block: the class must ride on the ROLE (the same fact that
// gives the row its job line and its Stop), and the reduced-motion arm must
// state the label flatly rather than letting the blanket rule park the sweep.
describe("the live row's shimmer", () => {
  const { readFileSync } = require("node:fs") as typeof import("node:fs");
  const { join } = require("node:path") as typeof import("node:path");
  const DOCK = readFileSync(join(import.meta.dir, "QueueDock.tsx"), "utf8");
  const CSS = readFileSync(
    join(import.meta.dir, "../styles/notifications.css"),
    "utf8",
  );

  it("rides on the row's role, and only on live", () => {
    expect(DOCK).toContain('"q-status" + (row.role === "live" ? " is-running" : "")');
  });

  it("is the sidebar's yellow text shimmer, with the flat reduced-motion arm", () => {
    expect(CSS).toContain(".q-status.is-running {");
    expect(CSS).toContain("color: var(--status-progress);");
    expect(CSS).toContain("animation: q-running-shimmer 2.2s linear infinite;");
    expect(CSS).toContain("@keyframes q-running-shimmer");
    // The words never fade and never vanish: in-range travel over a 300% image.
    expect(CSS).toContain("background-size: 300% 100%;");
    expect(CSS).toMatch(/q-running-shimmer \{\n  from \{ background-position: 100% 0; \}\n  to \{ background-position: 0% 0; \}/);
    // Motion off: no parked gradient — the flat status hue instead.
    const reduced = CSS.slice(CSS.lastIndexOf(".q-status.is-running"));
    expect(reduced).toContain("animation: none;");
    expect(reduced).toContain("-webkit-text-fill-color: var(--status-progress);");
  });
});

// PR #647: a run STARTING is news the same way one ending is (Akshil,
// 2026-08-19: the popover said "thinking" while the Tasks list sat on
// Upcoming until a reload) — same prefix rule, opposite transition.
describe("scheduleRunsStarted", () => {
  it("reads a schedule job newly running as a start", () => {
    const prev = [{ id: "sys:schedule:9", state: "queued" }] as never;
    const next = [{ id: "sys:schedule:9", state: "running" }] as never;
    expect(scheduleRunsStarted(prev, next)).toBe(true);
  });

  it("counts a job the registry only just learned about", () => {
    const next = [{ id: "sys:schedule:9", state: "running" }] as never;
    expect(scheduleRunsStarted([], next)).toBe(true);
  });

  it("stays quiet for still-running and for non-schedule jobs", () => {
    const running = [{ id: "sys:schedule:9", state: "running" }] as never;
    expect(scheduleRunsStarted(running, running)).toBe(false);
    const other = [{ id: "dl:1", state: "running" }] as never;
    expect(scheduleRunsStarted([], other)).toBe(false);
  });
});
