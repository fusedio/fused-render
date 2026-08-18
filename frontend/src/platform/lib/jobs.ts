// Reading the background-job registry (fused_render/jobs.py) — the model the
// download manager draws (SPEC §36, D244).
//
// A page reports long-running work through `fused.trackJob()` in the injected
// runtime; the record lives on the server so it outlives the reporting
// document, and the shell reads it back here. Everything the manager decides
// from a record — how far along it is, what to call it, whether it can still be
// cancelled — lives in this module as a pure function with a test, rather than
// inline in the component, because those are the parts that are wrong in ways a
// screenshot doesn't show (a bar at 0% because `total` was absent rather than
// zero; a row that says "stalled" for work that finished).
import { getJson, postJson } from "@platform/lib/api";

export type JobState = "running" | "done" | "error" | "cancelled";
export type JobKind = "download" | "task";
// Who is running the work, which decides what ✕ can do (SPEC BG-4). "page" —
// only the page knows what stopping means, so cancel is a request it honours.
// "server" — this app owns the process and really stops it.
export type JobOwner = "page" | "server";

export interface Job {
  id: string;
  title: string;
  detail: string;
  kind: JobKind;
  state: JobState;
  // null means "no number to show": an indeterminate bar, not zero progress.
  done: number | null;
  total: number | null;
  unit: string; // "bytes" | "s" | "" — decides how done/total are formatted
  message: string; // the error text, when state is "error"
  page: string; // the .html that raised it (attribution)
  owner: JobOwner;
  cancellable: boolean;
  cancel_requested: boolean;
  started_at: number;
  updated_at: number;
  finished_at: number | null;
  // Server-computed: running, but nothing has reported in a while. The reporter
  // is gone (its page was closed); the work may well be carrying on.
  stalled: boolean;
}

export interface JobsSnapshot {
  jobs: Job[];
  // The SERVER's clock at the moment of the read. Ages are measured against
  // this, never against the browser's Date.now(): the two disagree after a tab
  // throttle or a suspend, and the visible symptom is a job that finished "in
  // 3 seconds' time".
  now: number;
}

export function fetchJobs(signal?: AbortSignal): Promise<JobsSnapshot> {
  return getJson<JobsSnapshot>("/api/jobs", { signal });
}

export function cancelJob(id: string): Promise<Job> {
  return postJson<Job>(`/api/jobs/${encodeURIComponent(id)}/cancel`, {});
}

export function dismissJob(id: string): Promise<{ dismissed: string }> {
  return postJson<{ dismissed: string }>(`/api/jobs/${encodeURIComponent(id)}/dismiss`, {});
}

export function clearFinishedJobs(): Promise<{ cleared: number }> {
  return postJson<{ cleared: number }>("/api/jobs/clear", {});
}

// The cross-document nudge runtime.js writes when it reports (see its
// JOB_PING_KEY comment). Keep the two spellings in step — tests/test_jobs_api.py
// pins them together.
export const JOB_PING_KEY = "fused-render:jobs-ping";

export function isRunning(job: Job): boolean {
  return job.state === "running";
}

// A scheduled message's job row, by id (fused_render/schedule.py `_JOB_PREFIX`).
export const SCHEDULE_JOB_PREFIX = "sys:schedule:";

/** The entry id inside a scheduled run's job id, or "" for every other job — the
 *  one place the `sys:schedule:<entry id>` spelling is taken apart. */
function scheduleEntryId(jobId: string): string {
  return jobId.startsWith(SCHEDULE_JOB_PREFIX) ? jobId.slice(SCHEDULE_JOB_PREFIX.length) : "";
}

/**
 * Which jobs get a row of their own, which is not every job the card knows.
 *
 * ONE ROW PER UNIT OF WORK, and since the queue and the jobs now share a single
 * card (Akshil, 2026-08-17 — "this queue and notification thing should be same
 * no?") that rule is enforced inside one list rather than between two cards. A
 * scheduled message whose turn is live gets a row at the top of this card from the
 * queue half: it carries the link to the session it is running in and a cancel that
 * knows what it is cancelling, and it prints THIS job's status line under its title
 * (queue-dock-lib `roleText`). A job row beside it would be the same run twice,
 * each half saying half of the same thing.
 *
 * `drawn` IS WHICH RUNS THAT HALF ACTUALLY HAS A ROW FOR, by entry id, and it is
 * passed in rather than assumed. It used to be assumed: every running
 * `sys:schedule:*` job was dropped on the theory that something above was drawing
 * it, and the theory is false two ways. `GET /api/schedule/queue` can fail or time
 * out — after a failed FIRST read the queue half has no rows at all — and this card
 * can be mounted bare, with nothing filling the `queue` slot by construction. In
 * both cases a turn that was genuinely executing had no row in EITHER half: no
 * title, no status line, and no reachable `cancelJob("sys:schedule:<id>")` in any
 * fold state. That is the invisible-and-unreachable run this whole surface was asked
 * for, made worse — the earlier bug at least left a row saying "waiting for
 * permission".
 *
 * So being told nothing means "draw it yourself", never "somebody else has it": no
 * ids ⇒ one row, in the job half, without the Explorer link but with its stop. Told
 * an id ⇒ the queue half owns that run and this half keeps quiet, which is exact
 * (the ids come off the very array the rows are rendered from, queue-dock-lib
 * `drawnIds`) rather than a guess about a category of job.
 *
 * `drawn` WINS WHATEVER THE JOB'S STATE, and the terminal rows are the ones that
 * taught us why. They used to be exempt — "once the turn has ended the entry leaves
 * the server's queue, so a terminal row cannot be a duplicate" — and the premise is
 * true of the SERVER and false of the two clocks reading it. This half polls
 * /api/jobs about once a second and the queue half polls /api/schedule/queue every
 * six, so for as long as several seconds this half can know the turn ended while the
 * other is still painting the same run as live: a terminal job row and a live queue
 * row, two rows for one run, which is the single lifecycle this card was built to
 * be. One row per run AT EVERY INSTANT is the invariant, and a rule that holds only
 * when two independent timers agree is not one.
 *
 * The cost is that the outcome row waits for the queue half to let go of the run,
 * and that cost is paid where it can be made small rather than here: the queue half
 * retires a live row the moment the job registry says the run ended (queue-dock-lib
 * `openRows`), reading the SAME fast snapshot this half polls (DownloadManager hands
 * it up through the slot), so the handover is a render apart rather than a poll
 * apart. Nothing about this function depends on that being quick — it is what keeps
 * a duplicate impossible; `openRows` is what keeps the outcome prompt.
 */
export function jobRows(jobs: Job[], drawn?: Iterable<string> | null): Job[] {
  const ids = drawn instanceof Set ? drawn : new Set(drawn ?? []);
  return jobs.filter((j) => {
    const entry = scheduleEntryId(j.id);
    return entry === "" || !ids.has(entry);
  });
}

/**
 * The job rows the FOLD does not take — pass it what `jobRows` returned.
 *
 * A live scheduled run that reaches the job half is there because the queue half is
 * not drawing it (that is exactly what `jobRows` filtered on), so this row is the
 * only one that run has anywhere, and its ✕ is the only way to stop it. Folding it
 * away would re-create the bug `rowsShown` exists to prevent, one level down: a card
 * collapsed weeks ago, a turn running unattended, and nothing on screen to stop it
 * or to say that expanding would help.
 *
 * Nothing else survives. A download's ✕ is a cancel REQUEST for work the user
 * started themselves and is one expand away, which is what the preference was set
 * to fold; and a terminal scheduled row is a report, not a control, so it folds with
 * the rest of the history. Only work that is still running and still unattended
 * earns a place through the fold.
 */
export function foldedJobRows(jobs: Job[]): Job[] {
  return jobs.filter((j) => isRunning(j) && scheduleEntryId(j.id) !== "");
}

// Fraction complete in 0..1, or null when there is nothing honest to draw.
//
// `total` of 0 is null, not 1: a reporter that has not learned the size yet
// sends 0, and painting that as a full bar says the opposite of the truth. A
// `done` past `total` is clamped rather than dropped — an over-count is a
// reporter rounding, and a bar past its own end is worse than a full one.
export function jobFraction(job: Job): number | null {
  if (job.state === "done") return 1;
  if (job.total === null || job.total <= 0 || job.done === null) return null;
  return Math.max(0, Math.min(1, job.done / job.total));
}

// A byte count as the manager shows it: 3 significant-ish digits, binary
// units, no more precision than the number deserves. Deliberately local rather
// than lib/format's file-size helper — this one has to render a partial count
// against a total in the SAME unit ("1.2 / 8.1 GB"), which a standalone
// formatter can't do without picking two different units.
const BYTE_UNITS = ["B", "KB", "MB", "GB", "TB"];

export function byteScale(max: number): { div: number; unit: string } {
  let div = 1;
  let i = 0;
  while (i < BYTE_UNITS.length - 1 && max >= div * 1024) {
    div *= 1024;
    i += 1;
  }
  return { div, unit: BYTE_UNITS[i] };
}

function num(value: number, div: number): string {
  const scaled = value / div;
  if (div === 1) return String(Math.round(scaled));
  return scaled >= 100 ? scaled.toFixed(0) : scaled >= 10 ? scaled.toFixed(1) : scaled.toFixed(2);
}

// Seconds as a clock: "0:09", "12:00", "1:30:00". The hours field appears only
// when there are hours, so a short clip is not dressed up as a long one.
function clock(seconds: number): string {
  const whole = Math.max(0, Math.round(seconds));
  const s = whole % 60;
  const m = Math.floor(whole / 60) % 60;
  const h = Math.floor(whole / 3600);
  const pad = (n: number) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

// "1.2 / 8.1 GB", "412 MB", "12:00 / 1:30:00", "3 / 12" — or "" when there is
// no number at all. Bytes scale both sides by the LARGER of the two so the pair
// reads as one measurement instead of "1200 MB / 8.1 GB".
//
// SECONDS get the clock, and that is not decoration. A transcription reports
// seconds of audio (SPEC AI-10a) and every non-byte unit used to fall through
// to a bare pair, so a 90-minute recording read "720 / 5400" — a number a user
// takes for segments or steps. A unit that is only ever right by accident is
// worse than one that is absent, since the bare pair looks deliberate.
export function jobAmount(job: Job): string {
  const { done, total, unit } = job;
  if (done === null && total === null) return "";
  if (unit === "s") {
    if (done === null) return "";
    return total === null || total <= 0
      ? clock(done)
      : `${clock(done)} / ${clock(total)}`;
  }
  if (unit !== "bytes") {
    if (done === null) return "";
    return total === null || total <= 0
      ? String(Math.round(done))
      : `${Math.round(done)} / ${Math.round(total)}`;
  }
  const scale = byteScale(Math.max(done ?? 0, total ?? 0));
  if (total === null || total <= 0) {
    return done === null ? "" : `${num(done, scale.div)} ${scale.unit}`;
  }
  if (done === null) return `${num(total, scale.div)} ${scale.unit}`;
  return `${num(done, scale.div)} / ${num(total, scale.div)} ${scale.unit}`;
}

// The one line under the title. In priority order, because the states overlap:
// an error's message beats everything (it is the thing to act on), a cancel
// that has been asked for but not yet honored has to say so or the ✕ reads as
// broken, and a stalled row must not keep showing a stale detail as if it were
// live.
export function jobStatusLine(job: Job): string {
  if (job.state === "error") return job.message || "Failed";
  if (job.state === "cancelled") return job.detail || "Cancelled";
  if (job.state === "done") return job.detail || "Done";
  // Stalled outranks a pending cancel, and says so explicitly when both hold.
  // "Cancelling…" claims something is working on the request; if the reporter
  // died before honoring it, that claim would stand for the whole ten-minute
  // stale-drop window while nothing at all was happening.
  if (job.stalled) {
    // WHOSE reporter went quiet decides what to say. A page-owned row means a
    // tab was closed. A server-owned one (a model download, SPEC §40) means the
    // app's own worker stopped reporting — and telling someone their page was
    // closed when no page was involved sends them to look in the wrong place.
    const why =
      job.owner === "server"
        ? "the process running it stopped reporting"
        : "the page that started it was closed";
    return job.cancel_requested
      ? `Cancel requested, but nothing is reporting any more — ${why}`
      : `No longer reporting — ${why}`;
  }
  if (job.cancel_requested) return "Cancelling…";
  return job.detail;
}

/** The queue's contribution to the one header count: how many scheduled entries
 *  the card is showing above the job rows, split by whether their turn has
 *  actually begun. `waiting` is past-due-and-unclaimed plus claimed-and-spawning;
 *  `running` is a turn in flight. Zero of both = a card with no queue half (a
 *  platform-only mount, or simply nothing scheduled). */
export interface QueueCount {
  waiting: number;
  running: number;
}

const NO_QUEUE: QueueCount = { waiting: 0, running: 0 };

/** Which of the card's two kinds of row are on screen (see `rowsShown`). */
export interface RowsShown {
  /** The queue's rows — work about to run or running now. */
  queue: boolean;
  /** The job rows — everything a page reported, and the outcome of a run. */
  jobs: boolean;
}

/**
 * What the FOLD takes, which is not the whole list — and this is the one place
 * that decides it, because getting it wrong hides a control rather than a detail.
 *
 * The collapse is a persisted preference (`fused-render:jobs-collapsed`), and it
 * was set against the card as it used to be: a download history that grows all
 * session and is worth folding away. Then the queue moved into this same card, and
 * folding the whole list started taking the ONLY cancel a queued message or a live
 * turn has with it — a card someone collapsed weeks ago left scheduled work
 * arriving with no reachable way to stop it, and nothing on screen to say that
 * expanding would give them one. That is the bug this split fixes.
 *
 * So the fold applies to the JOB rows only:
 *
 * * **queue rows always show** while there are any. They are bounded (past due
 *   and about to run, not a session's history), each one is a claim on attention
 *   the user did not make, and each carries the only control that can stop it —
 *   a queued message's withdrawal, or a live turn's stop. Three rows about to run
 *   is not what anybody folded a downloads card to avoid.
 * * **job rows fold**, which is what the preference was set for. A download's ✕ is
 *   a cancel REQUEST for work the user started themselves, one expand away; the
 *   header still counts it and the overall bar still says it is running.
 *
 * `jobs: false` is still not quite "no job rows": `foldedJobRows` keeps the one kind
 * that must not be folded — a live scheduled run the queue half is not drawing,
 * which is the same argument as above applied to the row that stands in for a queue
 * row when the queue read failed. Everything the preference was set for still folds.
 *
 * Nothing here writes the preference: the stored fold is only ever changed by the
 * user pressing the header, so a card folded on purpose stays folded — it just
 * stops swallowing the queue's controls. Both false means no list at all, which
 * with an empty queue is exactly the collapsed download card as it always was.
 */
export function rowsShown(collapsed: boolean, queue: QueueCount = NO_QUEUE): RowsShown {
  return { queue: queue.waiting + queue.running > 0, jobs: !collapsed };
}

// The one header line for the whole card — the queue's rows and the job rows
// under a single count, because they are one list of one kind of thing and two
// headers over one corner is what this replaced.
//
// Counts what is HAPPENING, and falls back to describing what finished only when
// nothing is: a header reading "2 running" over a list of four finished rows is
// the common case, and the running ones are the news. Terminal rows are still
// reachable — they are in the list, and Clear counts them.
//
// The NOUN is the honest one for the mix. Nothing has actually begun yet ⇒
// "queued", never "running": a past-due message the scheduler has not claimed is
// waiting, and calling that running is the kind of small lie that makes a user
// stop believing the corner. "downloading" survives only for a card whose active
// work is entirely downloads, which is what it always meant.
//
// A MIX names both halves instead of adding them up, for that same reason: one
// live turn beside two unclaimed messages reads "1 running · 2 queued", never
// "3 running". The sum was the same lie in a shorter sentence — it inflated how
// much work is underway at exactly the moment a reader glances at the corner to
// judge that.
export function jobsSummary(jobs: Job[], queue: QueueCount = NO_QUEUE): string {
  const running = jobs.filter(isRunning);
  const live = running.length + queue.running;
  const active = live + queue.waiting;
  if (active === 0) {
    const failed = jobs.filter((j) => j.state === "error").length;
    if (failed > 0) return failed === 1 ? "1 failed" : `${failed} failed`;
    return jobs.length === 1 ? "1 finished" : `${jobs.length} finished`;
  }
  if (live === 0) return active === 1 ? "1 queued" : `${active} queued`;
  const downloads = running.filter((j) => j.kind === "download").length;
  const pureDownloads =
    queue.waiting === 0 && queue.running === 0 && downloads === running.length;
  const noun = pureDownloads ? "downloading" : "running";
  const head = `${live} ${noun}`;
  return queue.waiting === 0 ? head : `${head} · ${queue.waiting} queued`;
}

// Overall progress across the running jobs, for the collapsed header's bar —
// or null when not every one of them can say how far along it is. Averaged over
// jobs rather than summed over bytes on purpose: summing lets one 8GB model
// download swallow a 40MB one entirely, so the bar would sit still while a
// whole other job ran start to finish.
export function overallFraction(jobs: Job[]): number | null {
  const running = jobs.filter(isRunning);
  if (running.length === 0) return null;
  const fractions = running.map(jobFraction);
  if (fractions.some((f) => f === null)) return null;
  return (fractions as number[]).reduce((a, b) => a + b, 0) / running.length;
}

// Poll cadence. Fast while anything is live — a progress bar that steps once a
// second reads as stuck — and slow otherwise, where the only thing a poll can
// discover is a job STARTED by some other document (a page in another browser
// tab, or a Python worker reporting straight to the API, which runs no JS and
// so writes no ping). The ping cuts the usual latency of that discovery to
// nothing; this floor is what covers the cases it can't reach.
export const POLL_ACTIVE_MS = 1000;
export const POLL_IDLE_MS = 5000;

export function pollInterval(jobs: Job[]): number {
  return jobs.some(isRunning) ? POLL_ACTIVE_MS : POLL_IDLE_MS;
}
