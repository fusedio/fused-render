// Reading the background-job registry (fused_render/jobs.py) — the model the
// download manager draws (SPEC §36, D244).
//
// A page reports long-running work through `fused.job()` in the injected
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

export interface Job {
  id: string;
  title: string;
  detail: string;
  kind: JobKind;
  state: JobState;
  // null means "no number to show": an indeterminate bar, not zero progress.
  done: number | null;
  total: number | null;
  unit: string; // "bytes" | "" — decides how done/total are formatted
  message: string; // the error text, when state is "error"
  page: string; // the .html that raised it (attribution)
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

// "1.2 / 8.1 GB", "412 MB", "3 / 12" — or "" when there is no number at all.
// Both sides are scaled by the LARGER of the two so the pair reads as one
// measurement instead of "1200 MB / 8.1 GB".
export function jobAmount(job: Job): string {
  const { done, total, unit } = job;
  if (done === null && total === null) return "";
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
  if (job.cancel_requested) return "Cancelling…";
  if (job.stalled) return "No longer reporting — the page that started it was closed";
  return job.detail;
}

// The collapsed header line: what the manager says when its rows are folded
// away. Counts what is HAPPENING, and falls back to describing what finished
// only when nothing is — a header reading "2 running" next to a list of four
// finished rows is the common case, and the running ones are the news.
export function jobsSummary(jobs: Job[]): string {
  const running = jobs.filter(isRunning);
  if (running.length === 0) {
    const failed = jobs.filter((j) => j.state === "error").length;
    if (failed > 0) return failed === 1 ? "1 failed" : `${failed} failed`;
    return jobs.length === 1 ? "1 finished" : `${jobs.length} finished`;
  }
  const downloads = running.filter((j) => j.kind === "download").length;
  const noun = downloads === running.length ? "downloading" : "running";
  return running.length === 1 ? `1 ${noun}` : `${running.length} ${noun}`;
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
