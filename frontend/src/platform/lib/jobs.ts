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

// "waiting" — the two NON-terminal states are "running" and "waiting". Work
// has stopped and is not coming back on its own: it is sitting on a QUESTION
// only the user can answer (today, the sole producer is
// `envinstall._mirror_into_jobs`'s `needs_build` branch: uv's "Install
// anyway" compile prompt). Not "running" — nothing is actually in flight, so
// a bar or a spinner would lie. Not terminal either: none of "done" / "error"
// / "cancelled" means "stopped, waiting on you" (see `fused_render/jobs.py`'s
// own state-machine comment for the fuller reasoning).
export type JobState = "running" | "waiting" | "done" | "error" | "cancelled";
export type JobKind = "download" | "task";
// Who is running the work, which decides what ✕ can do (SPEC BG-4). "page" —
// only the page knows what stopping means, so cancel is a request it honours.
// "server" — this app owns the process and really stops it.
export type JobOwner = "page" | "server";

export interface Job {
  id: string;
  title: string;
  detail: string;
  // The model running this row, as a dimmed suffix on the TITLE — never
  // folded into title or detail, because detail is a worker's progress
  // ticks' own line and a model name concatenated there would get
  // overwritten by the next tick. "" means no model to show (a download, a
  // scheduled run, a page's own `fused.trackJob()`) and JobRow renders
  // nothing for it, not an empty element.
  model: string;
  kind: JobKind;
  state: JobState;
  // null means "no number to show": an indeterminate bar, not zero progress.
  done: number | null;
  total: number | null;
  // Whether `total` prices the WHOLE download or only the phase currently in
  // flight (SPEC AI-5n, D498). "phase" — the default every plain reporter has
  // always sent without knowing it, correct as-is for a single-repo download.
  // "download" — an explicit claim only a multi-phase reporter
  // (`worker_base.download_plan`) is entitled to make. `shared/modelSize.ts`
  // is the one place this decides anything: a "download" total may win
  // outright over the catalog's constant; a "phase" total may only ever
  // raise it (never-understate).
  total_scope: "download" | "phase";
  unit: string; // "bytes" | "s" | "" — decides how done/total are formatted
  message: string; // the error text when state is "error"; the question's caption when state is "waiting"
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
  // The id of another row this row is blocked on, or "" for the ordinary
  // case — set server-side while an image/video render waits on a shared
  // model load (`fused_render/ai/supervisor.py` `_wait_ready`'s merge). See
  // `mergedRows` below for what the manager does with it.
  waiting_for: string;
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

/**
 * A FAILURE belongs to Notifications, not Jobs (D586, user: "maybe we can have
 * a flow like running activities are shown in jobs and after done, a completed
 * message goes to notifications?").
 *
 * `Jobs` claims to be work IN PROGRESS, and an `error` row is not in progress —
 * it sat there indefinitely while being the one thing that actually wanted
 * attention. Only `error` moves: a `cancelled` row is user-initiated (they
 * already know) and ages out on its own, and a `done` row has its artefact on
 * disk, so neither becomes a notification.
 *
 * This needs no notification store, and that is why the cheap version works:
 * `fused_render/jobs.py`'s `_sweep` already keeps `error` rows until they are
 * explicitly dismissed (where `done`/`cancelled` age out via `first_read_at` +
 * `FINISHED_TTL_S`), so the server-side lifetime, the dismissal endpoint and
 * its `X-Fused` guard all already exist. This is a client-side re-route of
 * rows that are already there.
 */
export function isFailure(job: Job): boolean {
  return job.state === "error";
}

/** What the Jobs section draws: everything EXCEPT failures. Deliberately not
 *  "only running" — `done` and `cancelled` rows keep behaving exactly as they
 *  did (vanish-on-success, then the server's TTL), because D586 moves failures
 *  and nothing else. */
export function inFlightJobs(jobs: Job[]): Job[] {
  return jobs.filter((j) => !isFailure(j));
}

/** What the Notifications section draws alongside its repo rows. */
export function failedJobs(jobs: Job[]): Job[] {
  return jobs.filter(isFailure);
}

/**
 * What the card's bulk "Clear" button would actually take, and what it
 * leaves — mirroring the server's own rule (`fused_render/jobs.py`
 * `clear_finished`) exactly, TERMINAL rows only. A stalled-but-`running`
 * row is deliberately NOT clearable in bulk any more: `is_stalled` only
 * means "no report in `STALE_AFTER_S`", and the work behind it does not
 * stop just because its row does — a Clear press used to silently orphan
 * a still-running AI job, telling the user it had been cancelled when
 * nothing had touched it. The per-row ✕ (`JobRow`'s `dismiss` path) still
 * takes a stalled row one at a time, unchanged: that is a user closing a
 * SPECIFIC row they usually recognize, not a bulk sweep that cannot know
 * what any of its rows are.
 *
 * (This block sat two functions further up until code review finding 4 —
 * `DownloadManager.tsx` cites "`clearableCount`'s own doc has the full
 * argument" twice, and the doc was over `isFailure`.)
 */
export function clearableCount(jobs: Job[]): number {
  return jobs.filter((j) => !isRunning(j)).length;
}

/** The jobs list after a Clear — every row Clear would NOT take, i.e. every
 *  `running` row, stalled included. Used to optimistically patch the local
 *  list the instant the server confirms a clear, without waiting for the
 *  next poll. */
export function jobsAfterClear(jobs: Job[]): Job[] {
  return jobs.filter(isRunning);
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
 * `jobRows`'s sibling for the other place two rows can describe ONE unit of
 * work (SPEC §36): an image/video render blocked on a shared model load used
 * to open a SECOND row — "Waiting for FLUX.2-klein-4B — Loading weights…" —
 * right beside the load's own "Loading weights…" row, both true, both saying
 * the same thing. `_wait_ready` (`fused_render/ai/supervisor.py`) now merges
 * the load's live progress onto the caller's row instead and marks it
 * `waiting_for` the load's job id; this is the client half that acts on it —
 * drop whichever row is NAMED by another row's `waiting_for`, for as long as
 * that reference is live.
 *
 * "Live" is `isRunning`, deliberately, not "present": a reference from a row
 * that has gone TERMINAL — most tellingly, an `error` — must not hide
 * anything, because that is exactly the case the merge exists to still get
 * right (D266) — a wait that ends in a real load failure has to show up as
 * two rows again, one for each side's own failure, not stay collapsed into
 * whichever tick last pointed at the other.
 *
 * `waiting_for` is server-only (`fused_render/jobs.py` `Job.waiting_for`), so
 * a page cannot use this filter to hide a row it does not own.
 */
export function mergedRows(jobs: Job[]): Job[] {
  const hidden = new Set(
    jobs.filter((j) => j.waiting_for && isRunning(j)).map((j) => j.waiting_for),
  );
  return jobs.filter((j) => !hidden.has(j.id));
}

// Fraction complete in 0..1, or null when there is nothing honest to draw.
//
// `total` of 0 is null, not 1: a reporter that has not learned the size yet
// sends 0, and painting that as a full bar says the opposite of the truth. A
// `done` past `total` is clamped rather than dropped — an over-count is a
// reporter rounding, and a bar past its own end is worse than a full one.
export function jobFraction(job: Job): number | null {
  if (job.state === "done") return 1;
  // `== null`, not `=== null` (D577): covers `undefined` as well, so a payload
  // missing these keys yields null rather than `undefined / undefined` ->
  // `NaN` -> a literal `NaN%` painted into the bar. Not user-reachable today
  // (fused_render/jobs.py serializes explicit nulls — `done: float | None =
  // None`), but the loose check costs nothing and removes the trap.
  if (job.total == null || job.total <= 0 || job.done == null) return null;
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
  // A question on the page, not a failure and not progress — the caption
  // names what it is waiting on (e.g. "waiting for your approval to compile
  // <pkg>"). Checked ahead of `stalled`/`cancel_requested` below: both of
  // those describe a REPORTER that has gone quiet or been asked to stop, and
  // a "waiting" row's reporter already exited on purpose the moment it wrote
  // this state (see `fused_render/jobs.py`'s own comment on `WAITING`).
  if (job.state === "waiting") return job.message || "Waiting for you";
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

// `jobsSummary` AND `NO_QUEUE` ARE DELETED (code review 2026-08-28, finding 8),
// along with `overallFraction`, `rowsShown`, `RowsShown`, `foldedJobRows` and
// `repoUpdatesSummary` before them. `jobsSummary` printed the card's one header
// line — "2 running · 1 queued", with a genuinely careful set of rules behind it
// (name both halves rather than summing them, since one live turn beside two
// unclaimed messages is not "3 running"; keep "downloading" only for a card
// whose active work really is all downloads; fall back to what FINISHED only
// when nothing is happening). D579 moved the idle sentence into the panel and
// D588/D590 reduced the chip to a label plus one circle, which left nothing in
// the bar for a sentence to render into. It survived that as a "pure, fully
// tested function" with no non-test caller, which is the shape a future reader
// mistakes for something load-bearing. `QueueCount` above STAYS — the card still
// computes it, and the queue rows still need it.
// Poll cadence. Fast while anything is live — a progress bar that steps once a
// second reads as stuck — and slow otherwise, where the only thing a poll can
// discover is a job STARTED by some other document (a page in another browser
// tab, or a Python worker reporting straight to the API, which runs no JS and
// so writes no ping). The ping cuts the usual latency of that discovery to
// nothing; this floor is what covers the cases it can't reach.
export const POLL_ACTIVE_MS = 1000;
export const POLL_IDLE_MS = 5000;

// How long to keep the ACTIVE cadence going after the last running job
// disappears. jobs.py sweeps a finished row after FINISHED_TTL_S (currently
// 3s) — a short TTL only actually shortens what the user sees if the client
// is still polling fast enough to catch the row landing AND catch it being
// swept. Dropping straight to POLL_IDLE_MS (5s) the instant nothing is
// running would mean a row could be missed on arrival, or sit for a ragged
// 0-5s after it dies depending on poll phase, instead of the clean ~3s the
// server now promises.
//
// GRACE_MS must comfortably outlive FINISHED_TTL_S plus a poll interval —
// this is the other half of that relationship, so a future change to
// FINISHED_TTL_S (fused_render/jobs.py) should come back here and check the
// margin still holds, and vice versa: shrinking GRACE_MS below
// FINISHED_TTL_S + POLL_ACTIVE_MS reopens the same lag this constant exists
// to close.
export const GRACE_MS = 6000;

/**
 * Poll cadence given the current jobs and how long ago a job was last seen
 * running. Pure — no clock of its own — so the caller (DownloadManager's
 * `useJobs`) is the one that owns `Date.now()` and remembers when it last
 * saw a running job; this function just decides what the elapsed time means.
 */
export function pollInterval(jobs: Job[], sinceLastRunningMs: number): number {
  if (jobs.some(isRunning)) return POLL_ACTIVE_MS;
  if (sinceLastRunningMs < GRACE_MS) return POLL_ACTIVE_MS;
  return POLL_IDLE_MS;
}

// ------------------------------------------------------------- auto-expand
//
// Shared by BOTH notification cards (DownloadManager.tsx's jobs/downloads
// card and shell/RepoUpdatesDock.tsx's repo-updates card, D562 follow-up —
// user call: "we can make the notifications 'un collapse' when a new one
// comes"). Lives here, not in repo-updates-lib.ts, because platform/ may not
// import shell/ (frontend/scripts/check-boundaries.mjs) — a helper both
// sides use has to live on the platform side, and shell is free to import
// it back.
//
// Pure and generic over the id: a job id for the jobs card, a repo root for
// the repo-updates card. `seen` in, `seen` out — the caller (a ref, one per
// card) owns the mutable state across renders/polls; this function only
// decides what one snapshot means against it.
//
// An id merely CHANGING (progress ticking, running -> done, ahead/behind
// moving) is not new — it was already in `seen` from an earlier snapshot and
// stays there, so `hasNew` stays false and the card does not re-open under a
// user who just folded it. An id that DISAPPEARS (cleared, dismissed,
// forgotten, the server no longer reporting it) falls out of the returned
// set — it is only ever repopulated from `currentIds` — so a genuinely
// re-arriving id later reads as new again, exactly like a first arrival.
export function trackSeenIds(
  currentIds: Iterable<string>,
  seen: ReadonlySet<string>
): { seen: Set<string>; hasNew: boolean } {
  const next = new Set<string>();
  let hasNew = false;
  for (const id of currentIds) {
    next.add(id);
    if (!seen.has(id)) hasNew = true;
  }
  return { seen: next, hasNew };
}
