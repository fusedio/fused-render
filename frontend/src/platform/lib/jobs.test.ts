// The download manager's reading of a job record (lib/jobs) — the decisions
// that are wrong in ways a screenshot doesn't show: a bar drawn full because
// the total was zero, a pair of byte counts scaled to two different units, a
// header counting finished work as running.
import { expect, test } from "bun:test";
import {
  aggregateProgress,
  jobTypeLabel,
  SCHEDULE_JOB_PREFIX,
  activeJobByModel,
  effectiveTier,
  GRACE_MS,
  jobAmount,
  jobDetail,
  jobFraction,
  jobRows,
  jobsAfterClear,
  jobStatusLine,
  mergedRows,
  pollInterval,
  POLL_ACTIVE_MS,
  POLL_IDLE_MS,
  popupJobs,
  popupTick,
  terminalNotifications,
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
    total_estimated: false,
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
    tier: "trail",
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

test("a done that outgrows an ESTIMATED total (an index rescan's tree grew since the last scan) still clamps to full, never past it or backwards", () => {
  // The index-job bridge (D724+) sets `total` to the last completed scan's
  // file count as an ESTIMATE for a rescan's denominator — a real but
  // possibly-stale number, since the tree can have grown. `jobFraction` is
  // where every job's bar gets clamped (there is no bridge-side clamp; a
  // reporter honestly stating `done` past a stale `total` is real data, not
  // something to hide at the source), so this is the one place a fast-
  // growing rescan's bar is guaranteed not to render past full.
  expect(jobFraction(job({ state: "running", done: 700_000, total: 672_424 }))).toBe(1);
  // Still a normal fraction well before the estimate is exceeded.
  expect(jobFraction(job({ state: "running", done: 10_856, total: 672_424 }))).toBeCloseTo(
    10856 / 672424
  );
});

test("a done job draws no bar — jobFraction is not consulted for terminal jobs, and Bar returns null for them directly", () => {
  expect(jobFraction(job({ state: "done", done: 7, total: 10 }))).toBeCloseTo(0.7);
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

test("a counted unit gets locale thousands separators and its own word", () => {
  // `unit: "files"` (index scans, D724) and `unit: "tokens"` (text
  // generation) used to fall through to a bare, unformatted number — no
  // separators, no word — which is why an in-progress scan read "10856"
  // instead of "10,856 files". `toLocaleString()`, not a hard-coded comma:
  // the Preferences panel's own count renders in the browser's locale (e.g.
  // Indian digit grouping), and this has to agree with it.
  expect(jobAmount(job({ unit: "files", done: 10856, total: null }))).toBe("10,856 files");
  expect(jobAmount(job({ unit: "tokens", done: 512, total: null }))).toBe("512 tokens");
});

test("a counted unit with a total renders both sides, unit word once", () => {
  expect(jobAmount(job({ unit: "files", done: 10856, total: 672424 }))).toBe(
    "10,856 / 672,424 files"
  );
});

test("a done that outgrows an estimated total clamps the printed numerator, matching the bar (D733)", () => {
  // jobFraction already clamps the BAR at full when done > total (an index
  // rescan whose tree grew since the last scan). The printed amount must
  // not disagree with a bar already pinned at 100% — "700,000 / 672,424"
  // beside a full bar claims the walk overran what it promised. Clamping
  // the numerator to the total (not dropping the denominator) keeps the
  // total's honest information ("what the walk expected") on the row.
  expect(jobAmount(job({ unit: "files", done: 700_000, total: 672_424 }))).toBe(
    "672,424 / 672,424 files"
  );
  // Still unclamped, still agrees with a non-full bar.
  expect(jobAmount(job({ unit: "files", done: 10_856, total: 672_424 }))).toBe(
    "10,856 / 672,424 files"
  );
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

test("a running job with no detail and no message reads as empty — the fallback is the call site's job (jobDetail)", () => {
  // `jobStatusLine` no longer folds `jobDetail` in itself (Change 3): a
  // running download can carry a real progress AMOUNT with no phase text at
  // all, a fact this function never sees, so falling back here would win
  // over that amount at the render site. Callers apply `jobDetail` only once
  // BOTH the status line and the amount are known to be empty.
  expect(jobStatusLine(job({ state: "running", detail: undefined }))).toBe("");
});

test("a running job's phase (message) leads, its detail follows — both reach the line", () => {
  // The index-scan bridge (D724) puts its run's phase in `message`
  // ("writing index" / "writing signatures") and its root in `detail` — but
  // this function used to read `message` only for `error`/`waiting`, so a
  // running row's phase was written to the job record and never rendered.
  // Every other running reporter still sends `message: ""`, so this is
  // additive for them (see the plain-`detail` case just below).
  expect(jobStatusLine(job({ state: "running", message: "writing index", detail: "~" }))).toBe(
    "writing index · ~"
  );
});

test("a running job with only a detail (message empty) is unchanged from before", () => {
  expect(jobStatusLine(job({ state: "running", message: "", detail: "shard 3/8" }))).toBe(
    "shard 3/8"
  );
});

test("jobDetail names the kind and how long it has been running, from facts every job always carries", () => {
  const now = 10_000;
  const started_at = now - 125; // 2m 5s ago
  expect(jobDetail(job({ kind: "download", started_at, stalled: false }), now)).toBe(
    "Download · started 2m ago"
  );
  expect(jobDetail(job({ kind: "task", started_at, stalled: false }), now)).toBe(
    "Task · started 2m ago"
  );
});

test("jobDetail folds in stalled, since a job with nothing else to say and no reporter left needs it most", () => {
  const now = 10_000;
  const started_at = now - 5;
  expect(jobDetail(job({ kind: "download", started_at, stalled: true }), now)).toBe(
    "Download · started 5s ago · not reporting"
  );
});

test("jobDetail measures against the SERVER's clock, not the browser's (C4)", () => {
  // `started_at` is a server timestamp; a browser clock that has drifted
  // hours from the server's must not leak into what "started X ago" says.
  const serverNow = 1_000_000;
  const started_at = serverNow - 30; // 30s ago, by the SERVER's clock
  expect(jobDetail(job({ started_at, stalled: false }), serverNow)).toBe(
    "Download · started 30s ago"
  );
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

// ------------------------------------------------------------- activeJobByModel

test("activeJobByModel (Part A item 1 / C3) drops a done job — a finished pull must not read as still busy forever", () => {
  const done = job({ id: "j1", title: "FLUX.2-klein-4B", owner: "server", state: "done" });
  expect(activeJobByModel([done]).get("FLUX.2-klein-4B")).toBeUndefined();
});

test("activeJobByModel keeps a running server job, keyed by its title", () => {
  const running = job({ id: "j1", title: "FLUX.2-klein-4B", owner: "server", state: "running" });
  expect(activeJobByModel([running]).get("FLUX.2-klein-4B")).toBe(running);
});

test("activeJobByModel keeps a waiting server job — parked on a question is not finished", () => {
  const waiting = job({ id: "j1", title: "FLUX.2-klein-4B", owner: "server", state: "waiting" });
  expect(activeJobByModel([waiting]).get("FLUX.2-klein-4B")).toBe(waiting);
});

test("activeJobByModel ignores a page-owned job — a card only asks about its own model's server-side job", () => {
  const pageJob = job({ id: "j1", title: "FLUX.2-klein-4B", owner: "page", state: "running" });
  expect(activeJobByModel([pageJob]).size).toBe(0);
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

// --------------------------------------------------------- terminalNotifications
//
// `ActivityDock.tsx`'s `onJobsReported` — what actually reaches Notifications
// from a full snapshot. `mergedRows` has to run FIRST, on the unfiltered
// snapshot, or a load that has already gone terminal but whose waiter has not
// yet cleared its own `waiting_for` (the one-tick gap `_wait_ready`'s poll
// loop leaves between the load finishing and the waiter noticing) reaches
// Notifications on its own — a second completion entry for what Activity is,
// at that very moment, still drawing as one row.

test("terminalNotifications withholds a load's completion while its merged waiter is still running", () => {
  const jobs = [
    job({ id: "waiter", state: "running", waiting_for: "load" }),
    job({ id: "load", state: "done" }),
  ];
  expect(terminalNotifications(jobs)).toEqual([]);
});

test("terminalNotifications surfaces the load once the waiter itself has gone terminal", () => {
  const jobs = [
    job({ id: "waiter", state: "done", waiting_for: "load" }),
    job({ id: "load", state: "done" }),
  ];
  expect(terminalNotifications(jobs).map((j) => j.id).sort()).toEqual(["load", "waiter"]);
});

test("terminalNotifications still drops a scheduled run's own job", () => {
  const jobs = [job({ id: "sys:schedule:e1", state: "done", tier: "transient" })];
  expect(terminalNotifications(jobs)).toEqual([]);
});

test("terminalNotifications leaves an ordinary terminal job alone", () => {
  const jobs = [job({ id: "dl", state: "done" })];
  expect(terminalNotifications(jobs).map((j) => j.id)).toEqual(["dl"]);
});

// An index scan's own job (fused_render/server/routers/index.py's
// mirror_index_jobs_once, "sys:index:<run_id>") stays a live Activity row
// while running (default "trail" tier) — unlike a scheduled run's job
// (`SCHEDULE_JOB_PREFIX`), which `jobRows` excludes in every state (D661: a
// scheduled message's own row is never Activity's business, regardless of
// what its declared tier would otherwise say).
test("an index scan's job is not caught by the exclusion a scheduled run's job is", () => {
  const jobs = [
    job({ id: "sys:index:20260907-1200-ab12", state: "running" }),
    job({ id: `${SCHEDULE_JOB_PREFIX}e1`, state: "running", tier: "transient" }),
  ];
  expect(jobRows(jobs).map((j) => j.id)).toEqual(["sys:index:20260907-1200-ab12"]);
});

test("a scheduled run's job is excluded from Activity in every state, not only while transient-and-terminal", () => {
  const jobs = [
    job({ id: `${SCHEDULE_JOB_PREFIX}e1`, state: "running", tier: "transient" }),
    job({ id: `${SCHEDULE_JOB_PREFIX}e2`, state: "done", tier: "transient" }),
    job({ id: `${SCHEDULE_JOB_PREFIX}e3`, state: "error", tier: "transient" }),
  ];
  expect(jobRows(jobs)).toEqual([]);
});

// A model load's own row (fused_render/ai/supervisor.py, "sys:ai-model:<repo>")
// stays a live Activity row while it runs, but never becomes a stored
// Notification once it succeeds — a live watcher (`_wait_ready`'s row-merge,
// `fused.ai.models.load(wait=True)`) only ever reads it while it is RUNNING,
// so nothing downstream needs the terminal row to survive. `job.tier` (set
// server-side, only by the load's own success report, to `"silent"` — the
// user's own call: loading a model is not news) is what says so — NOT the
// id prefix plus `state === "done"` alone, because `job_id_for(model)` is
// the SAME id a weights-only download or an unload of that model reports
// through, and both of those are real news (see the two tests below).
test("a model load's row disappears from Notifications once it succeeds", () => {
  const jobs = [job({ id: "sys:ai-model:org/fake-model", state: "done", tier: "silent" })];
  expect(jobRows(jobs)).toEqual([]);
});

test("a model load's row still shows while it is running", () => {
  const jobs = [job({ id: "sys:ai-model:org/fake-model", state: "running" })];
  expect(jobRows(jobs).map((j) => j.id)).toEqual(["sys:ai-model:org/fake-model"]);
});

// Same rule, exercised on the running form of `silent` too — `tier` only
// ever governs a TERMINAL row (`JobTier`'s own doc comment), so a running
// silent load still shows, Cancel included.
test("a running model load stays visible even though it will report silent — a running row is never filtered, only a terminal one", () => {
  const jobs = [job({ id: "sys:ai-model:org/fake-model", state: "running", tier: "silent" })];
  expect(jobRows(jobs).map((j) => j.id)).toEqual(["sys:ai-model:org/fake-model"]);
});

// The tier's own documented meaning is "shown while running, never kept
// once terminal" — a transient row that HAS NOT reached a terminal state
// yet must still show, Cancel included. Every fixture above that exercises
// a running transient row leaves `tier` at its "trail" default (`job()`'s
// own default), which is why a `jobRows` that filtered transient
// unconditionally still passed them all: an index scan's running row, a
// scheduled run's running row, and this one all need `tier: "transient"`
// stated explicitly, in the running state, to close that gap.
test("a running index scan stays visible even when it declares transient — a running row is never filtered, only a terminal one", () => {
  const jobs = [job({ id: "sys:index:20260907-1200-ab12", state: "running", tier: "transient" })];
  expect(jobRows(jobs).map((j) => j.id)).toEqual(["sys:index:20260907-1200-ab12"]);
});

test("a running text generation stays visible even when it declares transient", () => {
  const jobs = [job({ id: "ai-text:1", state: "running", tier: "transient" })];
  expect(jobRows(jobs).map((j) => j.id)).toEqual(["ai-text:1"]);
});

// `effectiveTier`'s override: a load that declares itself silent but ends in
// error/cancelled is treated as attention, not silent, so it still gets a
// row — a failed load is exactly the kind of news the override exists for.
test("a failed or cancelled model load's row still shows", () => {
  const jobs = [
    job({ id: "sys:ai-model:org/fake-model", state: "error", tier: "silent" }),
    job({ id: "sys:ai-model:org/other-model", state: "cancelled", tier: "silent" }),
  ];
  expect(jobRows(jobs).map((j) => j.id)).toEqual([
    "sys:ai-model:org/fake-model",
    "sys:ai-model:org/other-model",
  ]);
});

// The bug `job.tier` replaces: matching the id prefix plus `state === "done"`
// alone would also have matched a finished weights-only DOWNLOAD and an
// unload/eviction — both report through the exact same `sys:ai-model:` id
// family (`job_id_for(model)`), and neither is a resident load succeeding.
// `tier` defaults to "trail", so a row that never had it set to "silent" by
// the server always shows, regardless of id or state.
test("a finished DOWNLOAD sharing the model-load id family still shows — only a resident load is silent", () => {
  const jobs = [job({ id: "sys:ai-model:org/fake-model", state: "done", kind: "download" })];
  expect(jobRows(jobs).map((j) => j.id)).toEqual(["sys:ai-model:org/fake-model"]);
});

test("an unload's finished row still shows, even sharing the model-load id family", () => {
  const jobs = [
    job({ id: "sys:ai-model:org/fake-model", state: "done", detail: "Unloaded", tier: "silent" }),
  ];
  // An unload declares `tier: "silent"` too (nothing survives it either, and
  // unloading is no more news than the load it undoes), so it drops out of
  // Notifications just like the load.
  expect(jobRows(jobs)).toEqual([]);
});

// ---- effectiveTier's error/cancelled override --------------------------

test("effectiveTier overrides a declared-transient row that ends in error", () => {
  const j = job({ state: "error", tier: "transient" });
  expect(j.tier).toBe("transient");
  expect(effectiveTier(j)).toBe("attention");
});

test("effectiveTier overrides a declared-silent row that ends in error", () => {
  const j = job({ state: "error", tier: "silent" });
  expect(j.tier).toBe("silent");
  expect(effectiveTier(j)).toBe("attention");
});

test("effectiveTier overrides a declared-transient row that is cancelled", () => {
  expect(effectiveTier(job({ state: "cancelled", tier: "transient" }))).toBe("attention");
});

test("effectiveTier leaves a done transient row alone", () => {
  expect(effectiveTier(job({ state: "done", tier: "transient" }))).toBe("transient");
});

test("effectiveTier leaves a done silent row alone", () => {
  expect(effectiveTier(job({ state: "done", tier: "silent" }))).toBe("silent");
});

test("effectiveTier leaves a running row's declared tier alone", () => {
  expect(effectiveTier(job({ state: "running", tier: "trail" }))).toBe("trail");
});

// ---- the chip's one word and one line (D673, statusbar redesign) ------------

test("a single job's chip word is its title's own -ing verb, capitalised", () => {
  expect(jobTypeLabel(job({ title: "erasing text using flux" }))).toBe("Erasing");
  expect(jobTypeLabel(job({ title: "Transcribing meeting.mp3" }))).toBe("Transcribing");
});

test("the live phase in `detail` wins over the title — the bar says what the page says", () => {
  expect(
    jobTypeLabel(job({ title: "Studio photograph of a polished chrome robot", detail: "Denoising · 0 / 4" })),
  ).toBe("Denoising");
  expect(jobTypeLabel(job({ title: "Erasing text", detail: "Decoding" }))).toBe("Decoding");
  expect(jobTypeLabel(job({ title: "Erasing text", detail: "step 3 of 9" }))).toBe("Erasing");
});

test("a title with no leading verb falls back to the kind", () => {
  expect(jobTypeLabel(job({ title: "FLUX.2-klein-4B", kind: "download" }))).toBe("Downloading");
  expect(jobTypeLabel(job({ title: "FLUX.2-klein-4B", kind: "task" }))).toBe("Working");
  expect(jobTypeLabel(job({ title: "Ring sizing", kind: "task" }))).toBe("Working");
});

test("a queued Claude call says Queued", () => {
  expect(jobTypeLabel(job({ title: "Claude", detail: "Queued — another Claude call is in flight" }))).toBe("Queued");
  expect(jobTypeLabel(job({ title: "FLUX.2-klein-4B", kind: "download", detail: "Preparing MLX…" }))).toBe("Preparing");
});

test("a job parked on a question says Waiting whatever its title", () => {
  expect(jobTypeLabel(job({ title: "Erasing text", state: "waiting" }))).toBe("Waiting");
});

// ------------------------------------------------------------------ popups
//
// The floating pop-up card (SPEC actionable-notifications, "the latest
// notification always pops up"): `tier` governs RETENTION for
// `attention`/`trail`/`transient` only (whether a row survives in the
// panel), never whether a terminal job is shown at all, so `popupJobs`
// reads none of that for those three — a `transient` job pops exactly like
// an `attention`/`trail` one. `silent` is the one tier that also suppresses
// the pop itself on a clean finish (a resident model load/unload: the
// running row already said as much, so "done" is not news) — see the
// dedicated tests below for the gate on stored `tier` + `state === "done"`.

test("popupJobs pops every terminal job regardless of tier, transient included", () => {
  const jobs = [
    job({ id: "a", state: "done", tier: "transient" }),
    job({ id: "b", state: "done", tier: "trail" }),
    job({ id: "c", state: "running", tier: "attention" }),
  ];
  expect(popupJobs(jobs).map((j) => j.id)).toEqual(["a", "b"]);
});

test("popupJobs pops nothing for a silent job that finishes done", () => {
  const jobs = [job({ id: "sys:ai-model:org/fake-model", state: "done", tier: "silent" })];
  expect(popupJobs(jobs)).toEqual([]);
});

// Silence is a property of SUCCESS only. A silent job's row can end up
// `error` while its last-written STORED tier is still `silent` — a manager
// process dying mid-report is exactly the case where the failure path never
// gets to restate `tier=jobs.TRAIL` the way `_bring_up`'s own error branch
// normally does — so `popupJobs` must still pop it. The filter is written
// against `state === "done"`, not `effectiveTier(j) !== "silent"`, precisely
// so this case (stored tier still "silent", state "error") is not
// mistakenly treated as the done-and-silent case it is gating against.
test("popupJobs still pops an error job whose stored tier is still silent", () => {
  const jobs = [job({ id: "sys:ai-model:org/fake-model", state: "error", tier: "silent" })];
  expect(popupJobs(jobs).map((j) => j.id)).toEqual(["sys:ai-model:org/fake-model"]);
});

test("popupJobs excludes a scheduled run's own job by id, same as jobRows (D661)", () => {
  const jobs = [job({ id: `${SCHEDULE_JOB_PREFIX}e1`, state: "done", tier: "transient" })];
  expect(popupJobs(jobs)).toEqual([]);
});

test("popupJobs hides the underlying job a running waiter merges over it (mergedRows)", () => {
  const jobs = [
    job({ id: "load", state: "done", tier: "transient" }),
    job({ id: "render", state: "running", waiting_for: "load" }),
  ];
  expect(popupJobs(jobs)).toEqual([]);
});

// The first-tick backlog problem (SPEC actionable-notifications): a poller's
// very first read after a page load or refresh sees every already-terminal
// job at once. `popupTick` must seed its `seen` set from that first read
// without popping any of it — the frontend twin of `_seen_running`
// (fused_render/server/routers/index.py).
test("popupTick seeds the first tick's already-terminal jobs with no popup", () => {
  const jobs = [job({ id: "a", state: "done", finished_at: 100 })];
  const { seen, popped } = popupTick(jobs, new Set(), true);
  expect(popped).toBe(null);
  // A second call with the exact same snapshot must still not pop — proof
  // the seeded key really covers this terminal event, not just its bare id.
  expect(popupTick(jobs, seen, false).popped).toBe(null);
});

test("popupTick pops a job that crosses into terminal on a later tick", () => {
  const first = popupTick([job({ id: "a", state: "running" })], new Set(), true);
  const second = popupTick([job({ id: "a", state: "done" })], first.seen, false);
  expect(second.popped?.id).toBe("a");
});

test("popupTick does not re-pop an id it has already popped", () => {
  const first = popupTick([job({ id: "a", state: "done" })], new Set(), false);
  expect(first.popped?.id).toBe("a");
  const second = popupTick([job({ id: "a", state: "done" })], first.seen, false);
  expect(second.popped).toBe(null);
});

// "Latest wins; do not stack" — one card at a time, never a queue of them.
test("popupTick pops only the latest of several jobs turning terminal in the same tick", () => {
  const running = popupTick(
    [job({ id: "a", state: "running" }), job({ id: "b", state: "running" })],
    new Set(),
    true,
  );
  const { popped } = popupTick(
    [
      job({ id: "a", state: "done", finished_at: 100 }),
      job({ id: "b", state: "done", finished_at: 200 }),
    ],
    running.seen,
    false,
  );
  expect(popped?.id).toBe("b");
});

// `list_jobs` sorts by `(started_at, id)` (fused_render/jobs.py), so array
// order is STARTED order, not finished order. "load" is listed first here
// (it started first) but "render" — listed after it, having started
// second — is the one that finishes LAST, with the newer `finished_at`. The
// pick must follow `finished_at`, not the array's tail, which in this case
// is "load".
test("popupTick picks the job with the newest finished_at, not the array's tail", () => {
  const running = popupTick(
    [job({ id: "render", state: "running" }), job({ id: "load", state: "running" })],
    new Set(),
    true,
  );
  const { popped } = popupTick(
    [
      job({ id: "render", state: "done", finished_at: 200 }),
      job({ id: "load", state: "done", finished_at: 100 }),
    ],
    running.seen,
    false,
  );
  expect(popped?.id).toBe("render");
});

// `job_id_for(model)` (fused_render/ai/supervisor.py) mints one id shared by
// a resident model's load, its weights-only download and its unload — so
// the SAME id can go terminal twice in a card's lifetime (a completed load,
// later followed by an unload finishing on that identical id). Each of
// those is its own notification and must pop on its own, so "have I popped
// this?" cannot be keyed on the bare id alone.
test("popupTick pops a second terminal event that lands on an id already popped once", () => {
  const loading = popupTick([job({ id: "m", state: "running" })], new Set(), true);
  const loaded = popupTick(
    [job({ id: "m", state: "done", finished_at: 100 })],
    loading.seen,
    false,
  );
  expect(loaded.popped?.id).toBe("m");

  // The model then unloads — same id, a later `finished_at` — without ever
  // leaving the candidate set null in between (a resident model's row stays
  // present, just no longer terminal, while it's loaded).
  const unloaded = popupTick(
    [job({ id: "m", state: "done", finished_at: 200 })],
    loaded.seen,
    false,
  );
  expect(unloaded.popped?.id).toBe("m");
});

test("aggregate progress: nothing running draws no line, no totals sweep, else the mean", () => {
  expect(aggregateProgress([])).toBeUndefined();
  expect(aggregateProgress([job({ state: "done", done: 1, total: 1 })])).toBeUndefined();
  expect(aggregateProgress([job({ state: "running", done: null, total: null })])).toBeNull();
  expect(
    aggregateProgress([
      job({ state: "running", done: 25, total: 100 }),
      job({ state: "running", done: 75, total: 100 }),
      job({ state: "running", done: null, total: null }),
    ]),
  ).toBeCloseTo(0.5);
});
