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
  EMPTY_GROUP_POPUP_STATE,
  effectiveTier,
  GRACE_MS,
  GROUP_GAP_MS,
  groupEffectiveTier,
  groupJobs,
  groupPopupTick,
  isGroupRecentOnly,
  isGroupTerminal,
  jobAmount,
  jobDetail,
  jobFraction,
  isRecentOnly,
  jobRows,
  jobsAfterClear,
  jobStatusLine,
  mergedRows,
  pollInterval,
  POLL_ACTIVE_MS,
  POLL_IDLE_MS,
  popupJobs,
  popupTick,
  recentJobs,
  recentNotifications,
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
    origin: "",
    owner: "page",
    cancellable: true,
    cancel_requested: false,
    started_at: 1000,
    updated_at: 1000,
    finished_at: null,
    stalled: false,
    waiting_for: "",
    tier: "trail",
    group: over.id ?? "j1",
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

// `recentNotifications` is `terminalNotifications`'s own complement — same
// `mergedRows`-first composition (ActivityDock.tsx feeds both from the same
// full snapshot), but selecting exactly what `terminalNotifications`
// excludes for presence reasons rather than what it keeps.

test("recentNotifications withholds a load's completion while its merged waiter is still running, same as terminalNotifications", () => {
  const jobs = [
    job({ id: "waiter", state: "running", waiting_for: "load" }),
    job({ id: "load", state: "done", page: "/ai-models/local" }),
  ];
  expect(recentNotifications(jobs, openHere("/ai-models/local"))).toEqual([]);
});

test("recentNotifications surfaces a merged pair as Recent once both are terminal and the page is open", () => {
  const jobs = [
    job({ id: "waiter", state: "done", waiting_for: "load", page: "/ai-models/local" }),
    job({ id: "load", state: "done", page: "/ai-models/local" }),
  ];
  expect(
    recentNotifications(jobs, openHere("/ai-models/local")).map((j) => j.id).sort(),
  ).toEqual(["load", "waiter"]);
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

// ------------------------------------------------------- presence suppression
// SPEC-quiet-notifications.md §2b / D-A: a successful terminal job whose own
// page the user is already looking at does not pop and does not hold a seat
// in Notifications — it is routed to Recent (§4) instead.

const openHere = (page: string) => (source: string) => source === page;
const openNowhere = () => false;

test("isRecentOnly: a done, non-attention job whose page is open anywhere is recent-only", () => {
  const j = job({ state: "done", tier: "trail", page: "/ai-models/local" });
  expect(isRecentOnly(j, openHere("/ai-models/local"))).toBe(true);
});

test("isRecentOnly: false when nothing has that page open", () => {
  const j = job({ state: "done", tier: "trail", page: "/ai-models/local" });
  expect(isRecentOnly(j, openNowhere)).toBe(false);
});

test("isRecentOnly: an error is never recent-only even if its page is open (effectiveTier promotes to attention)", () => {
  const j = job({ state: "error", tier: "trail", page: "/ai-models/local" });
  expect(isRecentOnly(j, openHere("/ai-models/local"))).toBe(false);
});

test("isRecentOnly: a cancelled job is never recent-only for the same reason", () => {
  const j = job({ state: "cancelled", tier: "trail", page: "/ai-models/local" });
  expect(isRecentOnly(j, openHere("/ai-models/local"))).toBe(false);
});

test("isRecentOnly: an explicit tier: attention job is never recent-only", () => {
  const j = job({ state: "done", tier: "attention", page: "/ai-models/local" });
  expect(isRecentOnly(j, openHere("/ai-models/local"))).toBe(false);
});

test("isRecentOnly: a running job is never recent-only regardless of presence", () => {
  const j = job({ state: "running", tier: "trail", page: "/ai-models/local" });
  expect(isRecentOnly(j, openHere("/ai-models/local"))).toBe(false);
});

test("jobRows: a suppressed success drops out when isOpenAnywhere is supplied", () => {
  const jobs = [job({ state: "done", tier: "trail", page: "/ai-models/local" })];
  expect(jobRows(jobs, openHere("/ai-models/local"))).toEqual([]);
});

// ------------------------------------------------------------- recentJobs
// SPEC-quiet-notifications.md §4: a job `jobRows` drops for being
// recent-only does not vanish — it goes to the folded "Recent" section
// instead. `recentJobs` is the complement of `jobRows`'s own suppression
// check: exactly the rows `jobRows` excludes BECAUSE `isRecentOnly` said so,
// nothing more (a schedule job, or a transient/silent terminal job, is still
// never shown anywhere — those are unrelated exclusions, not presence-based).

test("recentJobs: a suppressed success is exactly what Recent holds", () => {
  const jobs = [job({ id: "dl", state: "done", tier: "trail", page: "/ai-models/local" })];
  expect(recentJobs(jobs, openHere("/ai-models/local")).map((j) => j.id)).toEqual(["dl"]);
});

test("recentJobs: empty when nothing has the job's page open", () => {
  const jobs = [job({ id: "dl", state: "done", tier: "trail", page: "/ai-models/local" })];
  expect(recentJobs(jobs, openNowhere)).toEqual([]);
});

test("recentJobs: an error is never recent, even with its page open (goes to Needs you, not Recent)", () => {
  const jobs = [job({ id: "dl", state: "error", tier: "trail", page: "/ai-models/local" })];
  expect(recentJobs(jobs, openHere("/ai-models/local"))).toEqual([]);
});

test("recentJobs: a running job never appears in Recent regardless of presence", () => {
  const jobs = [job({ id: "dl", state: "running", tier: "trail", page: "/ai-models/local" })];
  expect(recentJobs(jobs, openHere("/ai-models/local"))).toEqual([]);
});

test("recentJobs: a scheduled run's own job never appears in Recent (D661 still applies)", () => {
  const jobs = [
    job({ id: "sys:schedule:e1", state: "done", tier: "transient", page: "/tasks" }),
  ];
  expect(recentJobs(jobs, openHere("/tasks"))).toEqual([]);
});

test("recentJobs: an ordinary job whose page is NOT open stays out of Recent (it's in Worth keeping instead)", () => {
  const jobs = [job({ id: "dl", state: "done", tier: "trail", page: "/ai-models/local" })];
  expect(jobRows(jobs, openNowhere).map((j) => j.id)).toEqual(["dl"]);
  expect(recentJobs(jobs, openNowhere)).toEqual([]);
});

test("recentJobs: jobRows and recentJobs partition a mixed snapshot with no overlap and no loss", () => {
  const jobs = [
    job({ id: "seen", state: "done", tier: "trail", page: "/ai-models/local" }),
    job({ id: "unseen", state: "done", tier: "trail", page: "/claude-config" }),
    job({ id: "failed", state: "error", tier: "trail", page: "/ai-models/local" }),
    job({ id: "running", state: "running", tier: "trail", page: "/ai-models/local" }),
  ];
  const open = openHere("/ai-models/local");
  const rows = jobRows(jobs, open);
  const recent = recentJobs(jobs, open);
  expect(rows.map((j) => j.id).sort()).toEqual(["failed", "running", "unseen"]);
  expect(recent.map((j) => j.id)).toEqual(["seen"]);
});

test("jobRows: the same job still shows when nothing has its page open", () => {
  const jobs = [job({ state: "done", tier: "trail", page: "/ai-models/local" })];
  expect(jobRows(jobs, openNowhere)).toEqual(jobs);
});

test("jobRows: an error still shows even though its page is open — errors are never suppressed", () => {
  const jobs = [job({ state: "error", tier: "trail", page: "/ai-models/local" })];
  expect(jobRows(jobs, openHere("/ai-models/local"))).toEqual(jobs);
});

test("jobRows: omitting isOpenAnywhere entirely preserves today's behavior (no suppression)", () => {
  const jobs = [job({ state: "done", tier: "trail", page: "/ai-models/local" })];
  expect(jobRows(jobs)).toEqual(jobs);
});

test("popupJobs: a suppressed success does not pop when its page is open", () => {
  const jobs = [job({ state: "done", tier: "trail", page: "/ai-models/local", finished_at: 5000 })];
  expect(popupJobs(jobs, openHere("/ai-models/local"))).toEqual([]);
});

test("popupJobs: the same job still pops when nothing has its page open", () => {
  const jobs = [job({ state: "done", tier: "trail", page: "/ai-models/local", finished_at: 5000 })];
  expect(popupJobs(jobs, openNowhere).map((j) => j.id)).toEqual(["j1"]);
});

test("popupTick: a suppressed success raises no popup even on its first genuinely-new tick", () => {
  const jobs = [job({ state: "done", tier: "trail", page: "/ai-models/local", finished_at: 5000 })];
  const { popped } = popupTick(jobs, new Set(), false, openHere("/ai-models/local"));
  expect(popped).toBeNull();
});

test("popupTick (finding 2): a suppressed success's key is recorded even though it never pops", () => {
  // Before the fix, popupTick built its candidate list from
  // `popupJobs(jobs, isOpenAnywhere)` directly -- a suppressed job never
  // appeared in that filtered list, so its key never made it into `seen`.
  const jobs = [job({ state: "done", tier: "trail", page: "/ai-models/local", finished_at: 5000 })];
  const { seen, popped } = popupTick(jobs, new Set(), false, openHere("/ai-models/local"));
  expect(popped).toBeNull();
  expect(seen.size).toBe(1);
});

test("popupTick (finding 2): navigating away after a suppressed success does not pop it stale", () => {
  // The regression this finding describes: a job pops nothing while its page
  // is open (suppressed). The user then navigates away, so `isOpenAnywhere`
  // now returns false for that page. Without the fix, the job's key was
  // never in `seen` from the first tick, so this second tick treated it as
  // brand new and popped a stale card for a job the user never watched
  // finish live.
  const jobs = [job({ state: "done", tier: "trail", page: "/ai-models/local", finished_at: 5000 })];
  const tick1 = popupTick(jobs, new Set(), false, openHere("/ai-models/local"));
  expect(tick1.popped).toBeNull();
  const tick2 = popupTick(jobs, tick1.seen, false, openNowhere);
  expect(tick2.popped).toBeNull();
});

test("popupTick: an error on the same open page still pops — an error is never suppressed", () => {
  const jobs = [job({ state: "error", tier: "trail", page: "/ai-models/local", finished_at: 5000 })];
  const { popped } = popupTick(jobs, new Set(), false, openHere("/ai-models/local"));
  expect(popped?.id).toBe("j1");
});

// --------------------------------------------------------------- §3 grouping
// SPEC-quiet-notifications.md §3: rows are keyed by `(page, group)`, not by
// id. Every job's `group` defaults, server-side, to its own id when it has
// no `sys:<name>:` family prefix — so an ungrouped job is a group of one BY
// CONSTRUCTION, which is what makes "a lone job behaves exactly as today"
// true without any of `jobRows`/`recentJobs`/`groupJobs` special-casing
// group size 1. `job()`'s own default (`group: over.id ?? "j1"`) mirrors
// that server default, so every pre-existing test above — none of which set
// `group` explicitly — already IS the single-member regression suite: if
// grouping had broken lone-job behavior, they would have failed already.
// The tests below name that guarantee explicitly, then move on to what's new.

test("groupJobs: a lone job is its own group of one, keyed by its own id", () => {
  const jobs = [job({ id: "dl", page: "/ai-models/local" })];
  const groups = groupJobs(jobs);
  expect(groups).toHaveLength(1);
  expect(groups[0].group).toBe("dl");
  expect(groups[0].jobs.map((j) => j.id)).toEqual(["dl"]);
});

test("groupJobs: two jobs sharing (page, group) fold into one group, arrival order preserved", () => {
  const jobs = [
    job({ id: "sys:ai-image:a", page: "/ai-images", group: "sys:ai-image" }),
    job({ id: "sys:ai-image:b", page: "/ai-images", group: "sys:ai-image" }),
  ];
  const groups = groupJobs(jobs);
  expect(groups).toHaveLength(1);
  expect(groups[0].jobs.map((j) => j.id)).toEqual(["sys:ai-image:a", "sys:ai-image:b"]);
});

test("groupJobs: the same group id on two different pages is two groups, not one — grouping is per-page too", () => {
  const jobs = [
    job({ id: "a", page: "/one", group: "shared" }),
    job({ id: "b", page: "/two", group: "shared" }),
  ];
  expect(groupJobs(jobs)).toHaveLength(2);
});

// Finding 3 (code review 2026-09-16): grouping by `(page, group)` alone
// folds a family's ENTIRE history into one group, since terminal rows are
// kept until dismissed (D663). A group must mean one BURST of work — see
// `GROUP_GAP_MS`'s own doc comment in jobs.ts for the fix (cluster a family
// by activity gap before grouping). Both sides of the gap boundary, pinned:
test("groupJobs: two family members just inside GROUP_GAP_MS of each other's last activity are one burst", () => {
  const jobs = [
    job({
      id: "sys:ai-model:a",
      page: "/ai-models/local",
      group: "sys:ai-model",
      state: "done",
      started_at: 0,
      finished_at: 1000,
    }),
    job({
      id: "sys:ai-model:b",
      page: "/ai-models/local",
      group: "sys:ai-model",
      state: "done",
      started_at: 1000 + GROUP_GAP_MS - 1,
      finished_at: 1000 + GROUP_GAP_MS - 1 + 500,
    }),
  ];
  const groups = groupJobs(jobs);
  expect(groups).toHaveLength(1);
  expect(groups[0].jobs.map((j) => j.id)).toEqual(["sys:ai-model:a", "sys:ai-model:b"]);
});

test("groupJobs: a family member starting more than GROUP_GAP_MS after the burst's last activity starts its own group", () => {
  const jobs = [
    job({
      id: "sys:ai-model:a",
      page: "/ai-models/local",
      group: "sys:ai-model",
      state: "done",
      started_at: 0,
      finished_at: 1000,
    }),
    job({
      id: "sys:ai-model:b",
      page: "/ai-models/local",
      group: "sys:ai-model",
      state: "done",
      started_at: 1000 + GROUP_GAP_MS + 1,
      finished_at: 1000 + GROUP_GAP_MS + 1 + 500,
    }),
  ];
  const groups = groupJobs(jobs);
  expect(groups).toHaveLength(2);
  expect(groups.map((g) => g.jobs.map((j) => j.id))).toEqual([
    ["sys:ai-model:a"],
    ["sys:ai-model:b"],
  ]);
});

test("groupJobs: an old finished burst never absorbs a job that starts long after, keeping the new job popping/attention behavior independent", () => {
  // The concrete regression named by the finding: a page that has ever had
  // two model downloads used to fold every FUTURE download in that family
  // into the same permanent group — so a lone new download stopped popping
  // on completion (multi-member groups are excluded from `popupJobs`) the
  // moment it joined that group. Clustering by activity gap means the old,
  // long-finished burst and today's new download are different groups, so
  // today's download is a group of ONE and pops exactly like a fresh job.
  const oldBurst = [
    job({
      id: "sys:ai-model:old-a",
      page: "/ai-models/local",
      group: "sys:ai-model",
      state: "done",
      started_at: 0,
      finished_at: 100,
    }),
    job({
      id: "sys:ai-model:old-b",
      page: "/ai-models/local",
      group: "sys:ai-model",
      state: "done",
      started_at: 200,
      finished_at: 300,
    }),
  ];
  const today = job({
    id: "sys:ai-model:today",
    page: "/ai-models/local",
    group: "sys:ai-model",
    state: "done",
    started_at: 300 + GROUP_GAP_MS + 1,
    finished_at: 300 + GROUP_GAP_MS + 1 + 50,
  });
  const groups = groupJobs([...oldBurst, today]);
  expect(groups).toHaveLength(2);
  const todaysGroup = groups.find((g) => g.jobs.some((j) => j.id === "sys:ai-model:today"));
  expect(todaysGroup?.jobs).toHaveLength(1);
});

test("isGroupTerminal: false while any member is still running, however many siblings finished", () => {
  const members = [
    job({ id: "a", state: "done" }),
    job({ id: "b", state: "running" }),
  ];
  expect(isGroupTerminal(members)).toBe(false);
});

test("isGroupTerminal: true once every member is terminal", () => {
  const members = [job({ id: "a", state: "done" }), job({ id: "b", state: "error" })];
  expect(isGroupTerminal(members)).toBe(true);
});

test("isGroupRecentOnly: true only when the group is fully terminal AND every member is individually recent-only", () => {
  const members = [
    job({ id: "a", state: "done", tier: "trail", page: "/p" }),
    job({ id: "b", state: "done", tier: "trail", page: "/p" }),
  ];
  expect(isGroupRecentOnly(members, openHere("/p"))).toBe(true);
});

test("isGroupRecentOnly: one failing member keeps the whole group visible, per spec's own words", () => {
  const members = [
    job({ id: "a", state: "done", tier: "trail", page: "/p" }),
    job({ id: "b", state: "error", tier: "trail", page: "/p" }),
  ];
  expect(isGroupRecentOnly(members, openHere("/p"))).toBe(false);
});

test("isGroupRecentOnly: one still-running member keeps the whole group visible (not fully terminal)", () => {
  const members = [
    job({ id: "a", state: "done", tier: "trail", page: "/p" }),
    job({ id: "b", state: "running", tier: "trail", page: "/p" }),
  ];
  expect(isGroupRecentOnly(members, openHere("/p"))).toBe(false);
});

test("isGroupRecentOnly: false when only some members' page is open (one member not recent-only)", () => {
  const members = [
    job({ id: "a", state: "done", tier: "trail", page: "/p" }),
    job({ id: "b", state: "done", tier: "trail", page: "/p" }),
  ];
  // nothing has "/p" open in this variant
  expect(isGroupRecentOnly(members, openNowhere)).toBe(false);
});

test("groupEffectiveTier: one attention member promotes the whole group, regardless of the rest", () => {
  const members = [job({ id: "a", state: "error" }), job({ id: "b", tier: "silent", state: "done" })];
  expect(groupEffectiveTier(members)).toBe("attention");
});

test("groupEffectiveTier: the loudest non-attention tier present wins (trail over transient over silent)", () => {
  const members = [
    job({ id: "a", tier: "silent", state: "done" }),
    job({ id: "b", tier: "trail", state: "running" }),
    job({ id: "c", tier: "transient", state: "done" }),
  ];
  expect(groupEffectiveTier(members)).toBe("trail");
});

test("groupEffectiveTier: all silent stays silent", () => {
  const members = [job({ id: "a", tier: "silent", state: "done" })];
  expect(groupEffectiveTier(members)).toBe("silent");
});

// The trap named explicitly in this build's brief: a two-member group where
// one member is individually suppressible (done, its page open) and the
// other is failing must appear EXACTLY ONCE, in the attention/trail path —
// never split across jobRows and recentJobs, never dropped from both.
test("jobRows/recentJobs: a two-member group with one suppressed success and one failure appears exactly once, in jobRows", () => {
  const jobs = [
    job({ id: "sys:ai-image:ok", state: "done", tier: "trail", page: "/p", group: "sys:ai-image" }),
    job({ id: "sys:ai-image:boom", state: "error", tier: "trail", page: "/p", group: "sys:ai-image" }),
  ];
  const open = openHere("/p");
  const rows = jobRows(jobs, open).map((j) => j.id).sort();
  const recent = recentJobs(jobs, open).map((j) => j.id).sort();
  expect(rows).toEqual(["sys:ai-image:boom", "sys:ai-image:ok"]);
  expect(recent).toEqual([]);
});

test("jobRows/recentJobs: a two-member group where every member is individually recent-only is suppressed as a whole, into Recent", () => {
  const jobs = [
    job({ id: "sys:ai-image:a", state: "done", tier: "trail", page: "/p", group: "sys:ai-image" }),
    job({ id: "sys:ai-image:b", state: "done", tier: "trail", page: "/p", group: "sys:ai-image" }),
  ];
  const open = openHere("/p");
  expect(jobRows(jobs, open)).toEqual([]);
  expect(recentJobs(jobs, open).map((j) => j.id).sort()).toEqual(["sys:ai-image:a", "sys:ai-image:b"]);
});

test("jobRows: a two-member group with one member still running is never suppressed, even if its sibling's page is open and done", () => {
  const jobs = [
    job({ id: "sys:ai-image:a", state: "done", tier: "trail", page: "/p", group: "sys:ai-image" }),
    job({ id: "sys:ai-image:b", state: "running", tier: "trail", page: "/p", group: "sys:ai-image" }),
  ];
  const open = openHere("/p");
  expect(jobRows(jobs, open).map((j) => j.id).sort()).toEqual(["sys:ai-image:a", "sys:ai-image:b"]);
  expect(recentJobs(jobs, open)).toEqual([]);
});

// The single-member regression this build was told to pin explicitly: a
// job whose group is itself (the id-derived default) behaves byte-for-byte
// like today's ungrouped path — one running + one about-to-be-suppressed
// job that do NOT share a group must never be folded together, and the
// suppressed one is judged purely on its own.
test("jobRows: two UNRELATED single-member jobs are never folded into each other's group just because they share a page", () => {
  const jobs = [
    job({ id: "a", state: "done", tier: "trail", page: "/p" }),
    job({ id: "b", state: "running", tier: "trail", page: "/p" }),
  ];
  const open = openHere("/p");
  // "a" is its own group of one and is fully suppressible on its own;
  // "b" is a separate group of one, still running, so it should NOT keep
  // "a" visible the way a genuine shared-group sibling would.
  expect(jobRows(jobs, open).map((j) => j.id)).toEqual(["b"]);
  expect(recentJobs(jobs, open).map((j) => j.id)).toEqual(["a"]);
});

// --------------------------------------------------------- D-C pop rule
// SPEC-quiet-notifications.md §3: a multi-member group pops on START (no
// running members to some) and on FAILURE (any member error/cancelled), and
// on nothing else — never on an ordinary completion, never when the whole
// group finishes. A single-member group is unaffected: `popupTick`/
// `popupJobs` above already own its pop-on-every-terminal-event behavior
// unchanged (see the "popupJobs pops every terminal job" tests already in
// this file, all running with a default group-of-one).

test("popupJobs excludes a multi-member group's own members entirely — their popping is groupPopupTick's job now", () => {
  const jobs = [
    job({ id: "sys:g:a", state: "done", group: "sys:g", finished_at: 1000 }),
    job({ id: "sys:g:b", state: "running", group: "sys:g" }),
  ];
  expect(popupJobs(jobs).map((j) => j.id)).toEqual([]);
});

test("popupJobs still pops a SINGLE-member job's own terminal event unchanged, even sharing a page with an unrelated group", () => {
  const jobs = [
    job({ id: "lone", state: "done", finished_at: 1000, page: "/p" }),
    job({ id: "sys:g:a", state: "done", group: "sys:g", page: "/p", finished_at: 2000 }),
    job({ id: "sys:g:b", state: "running", group: "sys:g", page: "/p" }),
  ];
  expect(popupJobs(jobs).map((j) => j.id)).toEqual(["lone"]);
});

test("groupPopupTick: a group's first running member pops a START, once — not on the seeding first tick", () => {
  const jobs = [
    job({ id: "sys:g:a", state: "running", group: "sys:g", started_at: 500 }),
    job({ id: "sys:g:b", state: "running", group: "sys:g", started_at: 400 }),
  ];
  const first = groupPopupTick(jobs, EMPTY_GROUP_POPUP_STATE, true);
  expect(first.popped).toBeNull();
  const second = groupPopupTick(jobs, first.state, false);
  expect(second.popped).toBeNull(); // already running last tick too — no NEW start
});

test("groupPopupTick: a group crossing from 0 running to some pops a START on that tick", () => {
  const idle: Job[] = [job({ id: "sys:g:a", state: "done", group: "sys:g", finished_at: 100 })];
  const started: Job[] = [
    job({ id: "sys:g:a", state: "done", group: "sys:g", finished_at: 100 }),
    job({ id: "sys:g:b", state: "running", group: "sys:g", started_at: 700 }),
  ];
  const t0 = groupPopupTick(idle, EMPTY_GROUP_POPUP_STATE, true);
  const t1 = groupPopupTick(started, t0.state, false);
  expect(t1.popped?.id).toBe("sys:g:b");
});

test("groupPopupTick: a serialized handoff (members running one at a time within GROUP_GAP_MS) pops START exactly once", () => {
  // Regression for the predicate that asked "were the CURRENTLY running
  // members unseen" instead of "was ANY member of the group running last
  // tick" — a serialized burst has a different, newly-running member on
  // every tick, so the wrong predicate popped a duplicate START on every
  // handoff instead of once for the whole group.
  //
  // "waiting" here stands in for "queued, not yet started" — a1/a2 are both
  // already known members of the group before either one runs, the same
  // shape a real batch of queued downloads would have.
  const seed: Job[] = [
    job({ id: "sys:g:a1", state: "waiting", group: "sys:g" }),
    job({ id: "sys:g:a2", state: "waiting", group: "sys:g" }),
  ];
  // Tick 1: a1 starts running — the group's first running member.
  const tick1: Job[] = [
    job({ id: "sys:g:a1", state: "running", group: "sys:g", started_at: 100 }),
    job({ id: "sys:g:a2", state: "waiting", group: "sys:g" }),
  ];
  // Tick 2: a1 finishes, a2 takes over — a handoff, not a new group.
  const tick2: Job[] = [
    job({ id: "sys:g:a1", state: "done", group: "sys:g", started_at: 100, finished_at: 200 }),
    job({ id: "sys:g:a2", state: "running", group: "sys:g", started_at: 300 }),
  ];
  // Tick 3: a2 finishes, a3 (a brand-new member) takes over — still just a
  // handoff within the same running group.
  const tick3: Job[] = [
    job({ id: "sys:g:a1", state: "done", group: "sys:g", started_at: 100, finished_at: 200 }),
    job({ id: "sys:g:a2", state: "done", group: "sys:g", started_at: 300, finished_at: 400 }),
    job({ id: "sys:g:a3", state: "running", group: "sys:g", started_at: 500 }),
  ];

  const t0 = groupPopupTick(seed, EMPTY_GROUP_POPUP_STATE, true);
  expect(t0.popped).toBeNull(); // seeded on the first tick, same as popupTick's own backlog rule

  const t1 = groupPopupTick(tick1, t0.state, false);
  expect(t1.popped?.id).toBe("sys:g:a1"); // 0 running -> some running: a genuine START

  const t2 = groupPopupTick(tick2, t1.state, false);
  expect(t2.popped).toBeNull(); // a2 taking over from a1 is a handoff, not a new START

  const t3 = groupPopupTick(tick3, t2.state, false);
  expect(t3.popped).toBeNull(); // a3 taking over from a2 is likewise not a new START
});

test("groupPopupTick: ordinary member completion pops nothing (no start, no failure)", () => {
  const running: Job[] = [
    job({ id: "sys:g:a", state: "running", group: "sys:g", started_at: 100 }),
    job({ id: "sys:g:b", state: "running", group: "sys:g", started_at: 100 }),
  ];
  const oneDone: Job[] = [
    job({ id: "sys:g:a", state: "done", group: "sys:g", started_at: 100, finished_at: 900 }),
    job({ id: "sys:g:b", state: "running", group: "sys:g", started_at: 100 }),
  ];
  const t0 = groupPopupTick(running, EMPTY_GROUP_POPUP_STATE, true);
  const t1 = groupPopupTick(oneDone, t0.state, false);
  expect(t1.popped).toBeNull();
});

test("groupPopupTick: the whole group finishing pops nothing", () => {
  const running: Job[] = [
    job({ id: "sys:g:a", state: "running", group: "sys:g", started_at: 100 }),
    job({ id: "sys:g:b", state: "running", group: "sys:g", started_at: 100 }),
  ];
  const bothDone: Job[] = [
    job({ id: "sys:g:a", state: "done", group: "sys:g", started_at: 100, finished_at: 900 }),
    job({ id: "sys:g:b", state: "done", group: "sys:g", started_at: 100, finished_at: 950 }),
  ];
  const t0 = groupPopupTick(running, EMPTY_GROUP_POPUP_STATE, true);
  const t1 = groupPopupTick(bothDone, t0.state, false);
  expect(t1.popped).toBeNull();
});

test("groupPopupTick: any member failing pops a FAILURE, even mid-run", () => {
  const running: Job[] = [
    job({ id: "sys:g:a", state: "running", group: "sys:g", started_at: 100 }),
    job({ id: "sys:g:b", state: "running", group: "sys:g", started_at: 100 }),
  ];
  const oneFailed: Job[] = [
    job({ id: "sys:g:a", state: "error", group: "sys:g", started_at: 100, finished_at: 900 }),
    job({ id: "sys:g:b", state: "running", group: "sys:g", started_at: 100 }),
  ];
  const t0 = groupPopupTick(running, EMPTY_GROUP_POPUP_STATE, true);
  const t1 = groupPopupTick(oneFailed, t0.state, false);
  expect(t1.popped?.id).toBe("sys:g:a");
});

test("groupPopupTick: a cancelled member also pops a FAILURE (effectiveTier promotes it the same as error)", () => {
  const running: Job[] = [
    job({ id: "sys:g:a", state: "running", group: "sys:g", started_at: 100 }),
    job({ id: "sys:g:b", state: "running", group: "sys:g", started_at: 100 }),
  ];
  const cancelled: Job[] = [
    job({ id: "sys:g:a", state: "cancelled", group: "sys:g", started_at: 100, finished_at: 900 }),
    job({ id: "sys:g:b", state: "running", group: "sys:g", started_at: 100 }),
  ];
  const t0 = groupPopupTick(running, EMPTY_GROUP_POPUP_STATE, true);
  const t1 = groupPopupTick(cancelled, t0.state, false);
  expect(t1.popped?.id).toBe("sys:g:a");
});

test("groupPopupTick: a failure already popped is not popped again while it stays failed", () => {
  const oneFailed: Job[] = [
    job({ id: "sys:g:a", state: "error", group: "sys:g", started_at: 100, finished_at: 900 }),
    job({ id: "sys:g:b", state: "running", group: "sys:g", started_at: 100 }),
  ];
  const t0 = groupPopupTick(oneFailed, EMPTY_GROUP_POPUP_STATE, true);
  expect(t0.popped).toBeNull(); // seeded on the first tick, same as popupTick's own backlog rule
  const t1 = groupPopupTick(oneFailed, t0.state, false);
  expect(t1.popped).toBeNull();
});

test("groupPopupTick: latest wins when a start and a failure both land in the same tick, across different groups", () => {
  const before: Job[] = [
    job({ id: "sys:g1:a", state: "done", group: "sys:g1", finished_at: 100 }),
    job({ id: "sys:g2:a", state: "running", group: "sys:g2", started_at: 100 }),
  ];
  const after: Job[] = [
    job({ id: "sys:g1:a", state: "done", group: "sys:g1", finished_at: 100 }),
    job({ id: "sys:g1:b", state: "running", group: "sys:g1", started_at: 5000 }), // START at 5000
    job({ id: "sys:g2:a", state: "error", group: "sys:g2", started_at: 100, finished_at: 2000 }), // FAILURE at 2000
  ];
  const t0 = groupPopupTick(before, EMPTY_GROUP_POPUP_STATE, true);
  const t1 = groupPopupTick(after, t0.state, false);
  expect(t1.popped?.id).toBe("sys:g1:b"); // 5000 > 2000
});

test("groupPopupTick: single-member groups are ignored entirely — never a candidate here", () => {
  const jobs = [job({ id: "lone", state: "running", started_at: 100 })];
  const t0 = groupPopupTick(jobs, EMPTY_GROUP_POPUP_STATE, true);
  const t1 = groupPopupTick(jobs, t0.state, false);
  expect(t0.popped).toBeNull();
  expect(t1.popped).toBeNull();
});

// ------------------------- key-churn must never re-trigger a START ---------
// Finding (code review 2026-09-16): `clusterFamily` numbers clusters 0,1,2...
// purely by position in the CURRENT snapshot, and the old `runningGroups`
// tracked cluster-scoped KEYS (which bake that ordinal in). Losing an older
// cluster from the snapshot (Clear all, dismissal) or a late report landing
// out of `started_at` order can renumber a still-running cluster's ordinal
// out from under it, so a key-keyed `runningGroups` would forget it was
// already running and pop a duplicate START. Tracking RUNNING MEMBER IDS
// instead of group keys makes both cases inert.

test("groupPopupTick: removing an older, fully-terminal cluster from the snapshot does not re-pop the surviving running cluster", () => {
  const oldCluster = job({
    id: "sys:g:old",
    group: "sys:g",
    state: "done",
    started_at: 100,
    finished_at: 200,
  });
  // The surviving cluster needs 2+ members itself to be a tracked
  // multi-member group at all (a single-member cluster never reaches
  // `groupPopupTick`'s START logic).
  const b1 = job({
    id: "sys:g:b1",
    group: "sys:g",
    state: "running",
    // Comfortably more than GROUP_GAP_MS past the old cluster's activity —
    // its own cluster, per `clusterFamily`.
    started_at: 200 + GROUP_GAP_MS + 10_000,
  });
  const b2 = job({
    id: "sys:g:b2",
    group: "sys:g",
    state: "running",
    started_at: 200 + GROUP_GAP_MS + 10_100,
  });

  const t0 = groupPopupTick([oldCluster, b1, b2], EMPTY_GROUP_POPUP_STATE, true);
  const t1 = groupPopupTick([oldCluster, b1, b2], t0.state, false);
  expect(t1.popped).toBeNull(); // already running last tick — no NEW start

  // "Clear all" (or a sweep) drops the old, fully-terminal cluster from the
  // snapshot entirely. `clusterFamily` now renumbers the surviving cluster
  // from index 1 to index 0 — a key-keyed tracker would read this as a
  // brand-new group and pop a duplicate START for work already announced.
  const t2 = groupPopupTick([b1, b2], t1.state, false);
  expect(t2.popped).toBeNull();
});

test("groupPopupTick: a late-arriving report that splits an earlier cluster does not re-pop the already-seen running members", () => {
  const c1 = job({ id: "sys:g:c1", group: "sys:g", state: "running", started_at: 200_000 });
  const c2 = job({ id: "sys:g:c2", group: "sys:g", state: "running", started_at: 200_100 });

  const t0 = groupPopupTick([c1, c2], EMPTY_GROUP_POPUP_STATE, true);
  const t1 = groupPopupTick([c1, c2], t0.state, false);
  expect(t1.popped).toBeNull(); // already running last tick — no NEW start

  // A late report arrives whose `started_at` predates everything seen so
  // far by more than GROUP_GAP_MS — it splits what was cluster 0 ({c1, c2})
  // into two clusters, shifting {c1, c2} from ordinal 0 to ordinal 1.
  const lateReport = job({
    id: "sys:g:late",
    group: "sys:g",
    state: "done",
    started_at: 1_000,
    finished_at: 50_000,
  });
  const t2 = groupPopupTick([c1, c2, lateReport], t1.state, false);
  expect(t2.popped).toBeNull();
});

// -------------------------------------------------- finding 8: shrink-to-1
// A group shrinking to one member (a sibling dismissed/swept) must not
// re-pop an already-popped failure via popupTick's singleton path.

test("groupPopupTick: a failed member's key survives in failedSeen after its sibling disappears and the group shrinks to one", () => {
  const running: Job[] = [
    job({ id: "sys:g:a", state: "running", group: "sys:g", started_at: 100 }),
    job({ id: "sys:g:b", state: "running", group: "sys:g", started_at: 100 }),
  ];
  const oneFailed: Job[] = [
    job({ id: "sys:g:a", state: "error", group: "sys:g", started_at: 100, finished_at: 900 }),
    job({ id: "sys:g:b", state: "running", group: "sys:g", started_at: 100 }),
  ];
  const t0 = groupPopupTick(running, EMPTY_GROUP_POPUP_STATE, true);
  const t1 = groupPopupTick(oneFailed, t0.state, false);
  expect(t1.popped?.id).toBe("sys:g:a");

  // "b" is dismissed/swept — the group now has just one member, "a".
  const shrunk: Job[] = [job({ id: "sys:g:a", state: "error", group: "sys:g", started_at: 100, finished_at: 900 })];
  const t2 = groupPopupTick(shrunk, t1.state, false);
  expect(t2.popped).toBeNull();
  // The key must still be carried forward for popupTick to consult.
  expect(t2.state.failedSeen.has("sys:g:a:900")).toBe(true);
});

test("popupTick: a group failure already popped by groupPopupTick does not re-pop once its group shrinks to one member", () => {
  const running: Job[] = [
    job({ id: "sys:g:a", state: "running", group: "sys:g", started_at: 100 }),
    job({ id: "sys:g:b", state: "running", group: "sys:g", started_at: 100 }),
  ];
  const oneFailed: Job[] = [
    job({ id: "sys:g:a", state: "error", group: "sys:g", started_at: 100, finished_at: 900 }),
    job({ id: "sys:g:b", state: "running", group: "sys:g", started_at: 100 }),
  ];
  const g0 = groupPopupTick(running, EMPTY_GROUP_POPUP_STATE, true);
  const g1 = groupPopupTick(oneFailed, g0.state, false);
  expect(g1.popped?.id).toBe("sys:g:a");
  // While the group still has two members, popupJobs excludes "a" entirely
  // — popupTick has never seen its key.
  const p0 = popupTick(oneFailed, new Set(), false, undefined, g1.state.failedSeen);
  expect(p0.popped).toBeNull();
  expect(p0.seen.has("sys:g:a:900")).toBe(false);

  // "b" is dismissed/swept — "a" is now a group of one, and is for the
  // FIRST time ever a `popupJobs` candidate for popupTick. Without the
  // fix, popupTick would treat this as a brand-new terminal event (its
  // key is absent from `seen`) and pop it again.
  const shrunk: Job[] = [job({ id: "sys:g:a", state: "error", group: "sys:g", started_at: 100, finished_at: 900 })];
  const g2 = groupPopupTick(shrunk, g1.state, false);
  const p1 = popupTick(shrunk, p0.seen, false, undefined, g2.state.failedSeen);
  expect(p1.popped).toBeNull();
  // It is now recorded, so any later tick behaves normally too.
  expect(p1.seen.has("sys:g:a:900")).toBe(true);
});
