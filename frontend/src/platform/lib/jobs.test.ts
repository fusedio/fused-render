// The download manager's reading of a job record (lib/jobs) — the decisions
// that are wrong in ways a screenshot doesn't show: a bar drawn full because
// the total was zero, a pair of byte counts scaled to two different units, a
// header counting finished work as running.
import { expect, test } from "bun:test";
import {
  clearableCount,
  GRACE_MS,
  jobAmount,
  jobElapsedLabel,
  jobFraction,
  jobsAfterClear,
  jobStatusLine,
  mergedRows,
  pollInterval,
  POLL_ACTIVE_MS,
  POLL_IDLE_MS,
  trackSeenIds,
  type Job,
} from "@platform/lib/jobs";

function job(over: Partial<Job> = {}): Job {
  return {
    id: "j1",
    title: "FLUX.2-klein-4B",
    detail: "",
    model: "",
    kind: "download",
    state: "running",
    done: null,
    total: null,
    total_scope: "phase",
    unit: "bytes",
    message: "",
    page: "/tmp/index.html",
    owner: "page",
    cancellable: true,
    cancel_requested: false,
    started_at: 1000,
    updated_at: 1000,
    finished_at: null,
    stalled: false,
    waiting_for: "",
    ...over,
  };
}

// ------------------------------------------------------------------ fraction

test("no total is indeterminate, not zero", () => {
  expect(jobFraction(job({ done: 1024, total: null }))).toBe(null);
});

test("a total of zero is indeterminate — a size not learned yet is not a full bar", () => {
  expect(jobFraction(job({ done: 0, total: 0 }))).toBe(null);
});

test("a reporter over-counting past its own total clamps to full", () => {
  expect(jobFraction(job({ done: 120, total: 100 }))).toBe(1);
});

test("a done job reads as complete even if its last numbers never caught up", () => {
  expect(jobFraction(job({ state: "done", done: 7, total: 10 }))).toBe(1);
});

// -------------------------------------------------------------------- amount

test("both sides of a byte pair are scaled by the same unit", () => {
  const text = jobAmount(job({ done: 1.2e9, total: 8.1e9 }));
  // One "GB", not "1200 MB / 8.1 GB".
  expect(text.match(/GB/g)?.length).toBe(1);
  expect(text).toBe("1.12 / 7.54 GB");
});

test("bytes with no total still say how much has arrived", () => {
  expect(jobAmount(job({ done: 512 * 1024 * 1024, total: null }))).toBe("512 MB");
});

test("a non-byte unit counts plainly", () => {
  expect(jobAmount(job({ unit: "", done: 3, total: 12 }))).toBe("3 / 12");
});

test("seconds of audio read as a CLOCK, not as a bare pair of numbers", () => {
  // A transcription reports seconds (SPEC AI-10a), and "720 / 5400" is the
  // number a reader takes for segments or steps — the one unit where the bare
  // pair actively misinforms. h:mm:ss appears only once there are hours, so a
  // short clip does not read as a long one.
  expect(jobAmount(job({ unit: "s", done: 720, total: 5400 }))).toBe("12:00 / 1:30:00");
  expect(jobAmount(job({ unit: "s", done: 9, total: 185 }))).toBe("0:09 / 3:05");
});

test("seconds with no total still say how far in we are", () => {
  // The window before the decoder knows the duration — it must not read as 0.
  expect(jobAmount(job({ unit: "s", done: 42, total: null }))).toBe("0:42");
});

test("nothing reported reads as nothing, not as 0", () => {
  expect(jobAmount(job({ done: null, total: null }))).toBe("");
});

// ------------------------------------------------------------------ elapsed

test("a running job reads its elapsed time as a clock, against the given clock", () => {
  expect(jobElapsedLabel(job({ started_at: 1000 }), 1042_000)).toBe("0:42");
  expect(jobElapsedLabel(job({ started_at: 0 }), 4530_000)).toBe("1:15:30");
});

test("a job that has not started yet by this clock reads as 0:00, not negative", () => {
  // Server clock drift (a throttled/suspended tab's own `nowMs` running
  // behind) must not print a negative clock.
  expect(jobElapsedLabel(job({ started_at: 1000 }), 999_000)).toBe("0:00");
});

test("a finished job has no elapsed clock — that is not what this line is for", () => {
  for (const state of ["done", "error", "cancelled"] as const) {
    expect(jobElapsedLabel(job({ state, started_at: 0 }), 60_000)).toBe("");
  }
});

// --------------------------------------------------------------- status line

test("an error's message outranks whatever detail was last set", () => {
  const line = jobStatusLine(job({ state: "error", detail: "downloading", message: "disk full" }));
  expect(line).toBe("disk full");
});

test("a waiting row's message names the question, not a generic label", () => {
  const line = jobStatusLine(
    job({ state: "waiting", message: "waiting for your approval to compile foolib" })
  );
  expect(line).toBe("waiting for your approval to compile foolib");
});

test("a waiting row without a message still says something, not the raw detail", () => {
  expect(jobStatusLine(job({ state: "waiting", detail: "installing" }))).toBe("Waiting for you");
});

test("a requested cancel says so — the ✕ must not read as broken", () => {
  expect(jobStatusLine(job({ cancel_requested: true, detail: "shard 3/8" }))).toBe("Cancelling…");
});

test("a stalled row explains itself instead of showing a stale detail", () => {
  expect(jobStatusLine(job({ stalled: true, detail: "shard 3/8" }))).toContain("No longer reporting");
});

test("a stalled row blames the right reporter", () => {
  // A page-owned row means a tab was closed. A server-owned one — a model
  // download (SPEC §40) — means the app's own worker went quiet, and telling
  // someone their page was closed when no page was involved sends them to look
  // in the wrong place.
  expect(jobStatusLine(job({ stalled: true }))).toContain("the page that started it was closed");
  const server = jobStatusLine(job({ stalled: true, owner: "server" }));
  expect(server).toContain("the process running it stopped reporting");
  expect(server).not.toContain("page");
});

test("stalled outranks a pending cancel, and says both", () => {
  // "Cancelling…" claims something is working on the request. If the reporter
  // died before honoring it, that claim would stand for the whole ten-minute
  // stale-drop window while nothing at all was happening.
  const line = jobStatusLine(job({ stalled: true, cancel_requested: true }));
  expect(line).toContain("Cancel requested");
  expect(line).toContain("nothing is reporting");
});

// NO SUMMARY TESTS ANY MORE — `jobsSummary` is deleted (code review finding 8).
// Nothing has rendered its sentence since D579 moved the idle line into the
// panel and D588/D590 reduced the chip to a label plus one circle; it stayed on
// as a fully-tested function with no caller, which reads to the next person like
// something load-bearing. Its whole test block goes with it rather than pinning
// a rule the app no longer has.

// --------------------------------------------------------------- auto-expand

test("trackSeenIds flags a genuinely new id and folds it into the returned set", () => {
  const { seen, hasNew } = trackSeenIds(["a", "b"], new Set(["a"]));
  expect(hasNew).toBe(true);
  expect(Array.from(seen).sort()).toEqual(["a", "b"]);
});

test("trackSeenIds does not flag an id already in the seen set", () => {
  const { seen, hasNew } = trackSeenIds(["a"], new Set(["a", "b"]));
  expect(hasNew).toBe(false);
  // dropped from seen: "b" is no longer present in currentIds
  expect(Array.from(seen)).toEqual(["a"]);
});

test("an id that changes state but stays present never re-reads as new", () => {
  const first = trackSeenIds(["job-1"], new Set());
  expect(first.hasNew).toBe(true);
  // simulate a progress tick / running -> done: same id, still present
  const second = trackSeenIds(["job-1"], first.seen);
  expect(second.hasNew).toBe(false);
});

test("an id that disappears and later reappears counts as new again", () => {
  const arrived = trackSeenIds(["job-1"], new Set());
  const gone = trackSeenIds([], arrived.seen);
  expect(gone.hasNew).toBe(false);
  expect(gone.seen.size).toBe(0);
  const back = trackSeenIds(["job-1"], gone.seen);
  expect(back.hasNew).toBe(true);
});

// ---------------------------------------------------------- overall fraction

// ---------------------------------------------------------------- poll pacing

test("the poll goes fast while anything runs, regardless of elapsed time", () => {
  expect(pollInterval([job()], 0)).toBe(POLL_ACTIVE_MS);
  expect(pollInterval([job()], GRACE_MS + 1)).toBe(POLL_ACTIVE_MS);
});

test("the poll stays fast through a grace window after the last running job disappears", () => {
  expect(pollInterval([job({ state: "done" })], 0)).toBe(POLL_ACTIVE_MS);
  expect(pollInterval([job({ state: "done" })], GRACE_MS - 1)).toBe(POLL_ACTIVE_MS);
});

test("the poll idles once the grace window has elapsed", () => {
  expect(pollInterval([job({ state: "done" })], GRACE_MS)).toBe(POLL_IDLE_MS);
  expect(pollInterval([job({ state: "done" })], GRACE_MS + 1)).toBe(POLL_IDLE_MS);
  expect(pollInterval([], GRACE_MS + 1)).toBe(POLL_IDLE_MS);
});

// --------------------------------------------------------------------- clear
//
// Mirrors the server's rule (jobs.py `clear_finished`, D558): Clear takes
// TERMINAL records only. A stalled-but-RUNNING row used to be swept too —
// the work does not actually stop when its record does, so that silently
// orphaned live work behind a Clear press. The per-row ✕ (`dismiss`) still
// takes a stalled row on purpose; only the bulk sweep changed.

test("clearableCount counts terminal rows but not a stalled running one", () => {
  const jobs = [
    job({ id: "run", state: "running", stalled: false }),
    job({ id: "stalled", state: "running", stalled: true }),
    job({ id: "done", state: "done" }),
    job({ id: "err", state: "error" }),
  ];
  expect(clearableCount(jobs)).toBe(2);
});

test("jobsAfterClear keeps every running row, stalled included", () => {
  const jobs = [
    job({ id: "run", state: "running", stalled: false }),
    job({ id: "stalled", state: "running", stalled: true }),
    job({ id: "done", state: "done" }),
  ];
  expect(jobsAfterClear(jobs).map((j) => j.id)).toEqual(["run", "stalled"]);
});

// -------------------------------------------------------------- mergedRows
//
// SPEC §36: a waiter and the model load it is blocked on used to open two
// rows saying the same thing (`fused_render/ai/supervisor.py` `_wait_ready`'s
// old "Two rows, two truths" behaviour). The merge mirrors the load's
// progress onto the waiter's row and marks it `waiting_for`; `mergedRows` is
// what makes the manager actually draw one row instead of two.

test("mergedRows hides the row another RUNNING row is waiting on", () => {
  const jobs = [
    job({ id: "waiter", state: "running", waiting_for: "load" }),
    job({ id: "load", title: "black-forest-labs/FLUX.2-klein-4B", state: "running" }),
  ];
  expect(mergedRows(jobs).map((j) => j.id)).toEqual(["waiter"]);
});

test("mergedRows keeps the referenced row once the waiter has gone terminal", () => {
  // A wait that ends in a real failure has to show up as two rows again —
  // one for the waiter's own failure, one for the load's, if it also failed
  // (D266). A stale `waiting_for` from a wait that already ended must not
  // keep hiding the load's row.
  const jobs = [
    job({ id: "waiter", state: "error", waiting_for: "load" }),
    job({ id: "load", state: "error" }),
  ];
  expect(mergedRows(jobs).map((j) => j.id).sort()).toEqual(["load", "waiter"]);
});

test("mergedRows leaves unrelated rows alone", () => {
  const jobs = [job({ id: "a" }), job({ id: "b" })];
  expect(mergedRows(jobs).map((j) => j.id).sort()).toEqual(["a", "b"]);
});

test("mergedRows is a no-op when nothing has waiting_for set", () => {
  const jobs = [job({ id: "a" }), job({ id: "b", waiting_for: "" })];
  expect(mergedRows(jobs)).toEqual(jobs);
});
