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
 * Whether a terminal job is specifically a FAILURE, as opposed to a `done` or
 * `cancelled` one — the one thing `isTerminal` does not distinguish. All
 * three terminal states route to Notifications and leave Jobs the same tick
 * (D657, broadened from D586's original `error`-only route: "running
 * activities are shown in jobs and after done, a completed message goes to
 * notifications" never meant only failures). What this narrower question is
 * still used for is `.is-failure`'s red tint in Notifications — a `done` or
 * `cancelled` row belongs there too, but neither is a failure and must not
 * turn the chip red.
 *
 * (C7: this doc used to describe D586's original error-only routing —
 * "only `error` moves", `done`/`cancelled` "aging out" via `FINISHED_TTL_S`
 * — none of which has been true since D657 stopped sweeping any terminal
 * row until dismissed and started routing all three states the same way.)
 */
export function isFailure(job: Job): boolean {
  return job.state === "error";
}

/**
 * A job that has stopped and is not coming back — the three states that used
 * to be handled one at a time (`isFailure` for D586's failures-only route) are
 * now one question, because Notifications draws all three the same way and
 * Activity must lose all three the same tick they land there (user: "running
 * activities are shown in jobs and after done, a completed message goes to
 * notifications" — the "after done" half never distinguished which terminal
 * state, only D586's `error` half ever got built).
 */
export function isTerminal(job: Job): boolean {
  return job.state === "done" || job.state === "error" || job.state === "cancelled";
}

/** Every job that has finished, one way or another — what Notifications draws
 *  alongside its repo rows. */
export function terminalJobs(jobs: Job[]): Job[] {
  return jobs.filter(isTerminal);
}

/** What the Jobs section draws: work still in progress, i.e. not terminal.
 *  `waiting` stays in (a row parked on a question the user has not answered
 *  yet is not finished — losing it here would make it vanish everywhere,
 *  since it is not terminal either and so `terminalJobs` would not pick it
 *  up); every terminal state leaves in the same tick it reaches
 *  Notifications. */
export function inFlightJobs(jobs: Job[]): Job[] {
  return jobs.filter((j) => !isTerminal(j));
}

/** Server-owned jobs (a pull, a load) keyed by the model id the caller's own
 *  card matches on (`title`, which the supervisor sets to the model id, so a
 *  card never re-derives it) — for a card to ask "is there a job for me" and
 *  get back an ANSWER, not just a fact about history.
 *
 *  D657 keeps a finished job's row until it is dismissed rather than
 *  sweeping it a few seconds after its first read, so a map built from every
 *  `server` row regardless of state stayed non-null for a model for the rest
 *  of the session once its pull or load finished — every consumer that
 *  gated on PRESENCE (`RepoCard.tsx`'s `!!job` disables and
 *  "Downloading…"/"Loading…" labels, `PlaygroundTab.tsx`'s `jobForSelected`)
 *  read that as "still busy" forever, active again the moment anything else
 *  on the page was, which is exactly when this page polls (`isBusy`,
 *  `aiRuntime.ts`). Filtering here — the one place this map is built —
 *  means presence in it means what it always should have: an active job,
 *  matching this file's own `inFlightJobs` for the identical reason (Part A
 *  item 1 / C3 fix). */
export function activeJobByModel(jobs: Job[]): Map<string, Job> {
  return new Map(
    jobs.filter((j) => j.owner === "server" && !isTerminal(j)).map((j) => [j.title, j]),
  );
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

/** A scheduled message's own run, never drawn as an Activity row (user: "a
 *  task is not something I even want in the activity. that was added
 *  unintentionally"). The "Task finished:"/"Task failed:"/"Scheduled message
 *  ran:" toast (platform/lib/schedule-toast.ts) is the one surface for these
 *  now; the job-registry write behind it stays untouched server-side, because
 *  `schedule.py`'s poll loop reads its own report back to notice a live
 *  cancel request. */
export function isScheduleJob(job: Job): boolean {
  return job.id.startsWith(SCHEDULE_JOB_PREFIX);
}

/** Which jobs get a row of their own in Activity: every job the registry
 *  knows about, except a scheduled run's — those never draw a row here,
 *  regardless of state (see `isScheduleJob`). */
export function jobRows(jobs: Job[]): Job[] {
  return jobs.filter((j) => !isScheduleJob(j));
}

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
  // NOT `job.detail || jobDetail(job)` here: a running job can carry a real
  // progress AMOUNT (jobAmount, a sibling fact this function does not see)
  // with no phase text at all — a bare download row is exactly that case.
  // Falling back to `jobDetail` from inside this function would win over
  // that amount and stamp "Task · started 3m ago" onto a row that already
  // had something true to say. The last-resort fallback belongs at the call
  // site instead, once status AND amount are both known to be empty
  // (DownloadManager.tsx's `statusLine`, `repoStatusText`'s job branch).
  return job.detail || "";
}

/** A COARSE duration, in the largest unit that still says something true:
 *  seconds under a minute, whole minutes under an hour, then `2h 5m`. Shared
 *  by the engine rows (whose poll is every 10s, so counting seconds would be
 *  wrong between ticks more often than right) and `jobDetail` below (whose
 *  input is a job's own `started_at`, at the same coarseness). */
export function engineDuration(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  const rest = m % 60;
  return rest === 0 ? `${h}h` : `${h}h ${rest}m`;
}

const JOB_KIND_TEXT: Record<JobKind, string> = { download: "Download", task: "Task" };

/**
 * `jobStatusLine`'s last resort (D659) — no card may render a single line of
 * text (title alone, no status line beneath it). Engine rows already
 * guarantee a `.dl-status` line (`engineDetail`); a `running` job with no
 * server detail, no byte/step amount and no local action failure fell
 * through to a bare title, which is the gap this closes.
 *
 * Built only from facts every job always carries, so it can never itself be
 * empty: what kind of work this is, and how long it has been going. `stalled`
 * is folded in because a job with nothing else to say and no reporter left is
 * exactly the case most likely to need this fallback at all.
 *
 * `nowS` (C4 fix) is the SERVER's clock (`JobsSnapshot.now`), the same rule
 * `JobsSnapshot.now`'s own doc states and every other age in this file
 * follows — never the browser's `Date.now()`. `job.started_at` is a server
 * timestamp; measuring it against the browser's clock instead reads wrong
 * by however far the two have drifted, which after a tab throttle or a
 * laptop sleep is not a rounding error — it is the same "finished in 3
 * seconds' time" symptom `JobsSnapshot.now` exists to prevent, just for a
 * duration instead of an age.
 */
export function jobDetail(job: Job, nowS: number): string {
  const kind = JOB_KIND_TEXT[job.kind] ?? "Job";
  const started = `started ${engineDuration(nowS - job.started_at)} ago`;
  return job.stalled ? `${kind} · ${started} · not reporting` : `${kind} · ${started}`;
}

// Poll cadence. Fast while anything is live — a progress bar that steps once a
// second reads as stuck — and slow otherwise, where the only thing a poll can
// discover is a job started with no ping behind it: one reported from another
// same-origin document (a page in another browser tab), or one a server-side
// process reports on its own with no browser ever POSTing anything (a
// scheduled message's timer tick, `schedule.py`'s `_report` — runs no JS, so
// writes no ping). A row a page's own JS causes — the env-install path
// included, even though the row itself is created server-side inside
// `envinstall.start()` — is pinged the moment the triggering POST resolves.
// This floor is what covers the cases a ping can't reach.
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
