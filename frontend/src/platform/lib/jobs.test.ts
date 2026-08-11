// The download manager's reading of a job record (lib/jobs) — the decisions
// that are wrong in ways a screenshot doesn't show: a bar drawn full because
// the total was zero, a pair of byte counts scaled to two different units, a
// header counting finished work as running.
import { expect, test } from "bun:test";
import {
  jobAmount,
  jobFraction,
  jobStatusLine,
  jobsSummary,
  overallFraction,
  pollInterval,
  POLL_ACTIVE_MS,
  POLL_IDLE_MS,
  type Job,
} from "@platform/lib/jobs";

function job(over: Partial<Job> = {}): Job {
  return {
    id: "j1",
    title: "FLUX.2-klein-4B",
    detail: "",
    kind: "download",
    state: "running",
    done: null,
    total: null,
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

test("nothing reported reads as nothing, not as 0", () => {
  expect(jobAmount(job({ done: null, total: null }))).toBe("");
});

// --------------------------------------------------------------- status line

test("an error's message outranks whatever detail was last set", () => {
  const line = jobStatusLine(job({ state: "error", detail: "downloading", message: "disk full" }));
  expect(line).toBe("disk full");
});

test("a requested cancel says so — the ✕ must not read as broken", () => {
  expect(jobStatusLine(job({ cancel_requested: true, detail: "shard 3/8" }))).toBe("Cancelling…");
});

test("a stalled row explains itself instead of showing a stale detail", () => {
  expect(jobStatusLine(job({ stalled: true, detail: "shard 3/8" }))).toContain("No longer reporting");
});

test("stalled outranks a pending cancel, and says both", () => {
  // "Cancelling…" claims something is working on the request. If the reporter
  // died before honoring it, that claim would stand for the whole ten-minute
  // stale-drop window while nothing at all was happening.
  const line = jobStatusLine(job({ stalled: true, cancel_requested: true }));
  expect(line).toContain("Cancel requested");
  expect(line).toContain("nothing is reporting");
});

// ------------------------------------------------------------------- summary

test("the header counts what is running, not what is on the list", () => {
  const jobs = [job({ id: "a" }), job({ id: "b", state: "done" }), job({ id: "c", state: "done" })];
  expect(jobsSummary(jobs)).toBe("1 downloading");
});

test("mixed kinds fall back to the neutral verb", () => {
  const jobs = [job({ id: "a" }), job({ id: "b", kind: "task" })];
  expect(jobsSummary(jobs)).toBe("2 running");
});

test("with nothing running, failures are the news", () => {
  const jobs = [job({ id: "a", state: "done" }), job({ id: "b", state: "error" })];
  expect(jobsSummary(jobs)).toBe("1 failed");
});

test("with nothing running and nothing failed, it counts what finished", () => {
  expect(jobsSummary([job({ id: "a", state: "done" })])).toBe("1 finished");
});

// ---------------------------------------------------------- overall fraction

test("overall progress averages the jobs so a small one is not swallowed", () => {
  const jobs = [
    job({ id: "big", done: 1e9, total: 8e9 }), // 12.5%
    job({ id: "small", done: 3.5e7, total: 4e7 }), // 87.5%
  ];
  // A byte SUM would read ~12.9% and barely move while the small job finished.
  expect(overallFraction(jobs)).toBeCloseTo(0.5, 5);
});

test("one job with no numbers makes the overall bar indeterminate, not optimistic", () => {
  const jobs = [job({ id: "a", done: 5, total: 10 }), job({ id: "b" })];
  expect(overallFraction(jobs)).toBe(null);
});

test("finished jobs are not averaged into the running total", () => {
  const jobs = [job({ id: "a", done: 2, total: 10 }), job({ id: "b", state: "done" })];
  expect(overallFraction(jobs)).toBeCloseTo(0.2, 5);
});

// ---------------------------------------------------------------- poll pacing

test("the poll goes fast while anything runs and idles when nothing does", () => {
  expect(pollInterval([job()])).toBe(POLL_ACTIVE_MS);
  expect(pollInterval([job({ state: "done" })])).toBe(POLL_IDLE_MS);
  expect(pollInterval([])).toBe(POLL_IDLE_MS);
});
