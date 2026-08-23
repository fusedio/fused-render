// The "Run all" queue: sequencing, stop semantics, and the success/failure
// tally for benchmarking every model in a capability one after another
// (SPEC AI-14 follow-up). Pure state here, no network calls — `BenchmarkTab.tsx`
// drives the actual `runAiBenchmark` request per model and feeds each outcome
// back in through `advanceQueue`, exactly the way `runButtonState`
// (benchmark.ts) stays a pure decision function beside the component that
// acts on it.
//
// **Sequential by construction, not by a lock.** There is no "is something
// running" flag to check before starting the next model — `advanceQueue` is
// only ever called with the PREVIOUS model's settled result, so the caller
// physically cannot ask for the next model before the current one finishes.
// This also happens to be exactly what the server's own one-resident-model-
// per-capability rule requires (a second concurrent run on the same
// capability is a 409) — the queue satisfies that constraint by construction
// rather than by racing it.
//
// **A failure does not stop the queue.** `advanceQueue` records whatever
// result it is given — `ok: true` or `ok: false` — and moves on regardless;
// only `requestQueueStop` (a deliberate user action) or reaching the end of
// `models` stops it. A model failing is a real fact about this machine
// (`ai/benchmark.py` already records it as a normal, non-cancelled run) and
// stopping a six-model queue because the second one OOM'd would throw away
// five perfectly good measurements over one bad one.
//
// **Stop only prevents the NEXT model from starting — it does not reach
// into an in-flight request.** `requestQueueStop` just sets `stopped`; the
// caller is responsible for ALSO interrupting the current run through
// whatever already stops a single benchmark (`unloadAiModel` — the same
// mechanism `stoppedNote` in benchmark.ts documents: unloading the model a
// benchmark is using is what makes its held-open request resolve with
// `cancelled: true`). This module deliberately does not invent a second
// cancellation idiom; it only has to make sure that once the interrupted (or
// naturally finished) run settles, `advanceQueue` sees `stopped` and goes no
// further.
import type { LeaderboardRow } from "@apps/ai_models/lib/benchmark";

/** One model's settled outcome within a queue — `ok` mirrors the run's own
 *  `AiBenchmarkRun.ok` for a real measurement, and is `false` for a run that
 *  came back cancelled (nothing recorded) too, since either way THIS queue
 *  slot did not produce a comparable number. */
export interface QueueRunResult {
  model: string;
  ok: boolean;
}

export interface BenchmarkQueue {
  capability: string;
  /** The fixed run order, decided once at `startQueue` and never reordered —
   *  a model finishing early must not jump another one further back in line,
   *  or "3 of 6" would name a different model each time it was read. */
  models: string[];
  /** How many of `models` have been STARTED so far (settled or still in
   *  flight) — `current`'s own 1-based position within `models`, or
   *  `models.length` once nothing is left to start. This is what "3 of 6"
   *  progress reads from, not `results.length` (which lags by one while a
   *  run is in flight). */
  started: number;
  /** The model currently in flight, or null between the last settle and
   *  nothing left to run — the caller reuses the ordinary per-capability
   *  `inFlight` state for the actual busy-row spinner, but needs this to
   *  know WHICH model to start the request for next. */
  current: string | null;
  /** One entry per model that has SETTLED, in the order they settled — never
   *  includes `current`. */
  results: QueueRunResult[];
  /** A stop was requested. Once true, `advanceQueue` will not start another
   *  model once `current` settles, no matter how many `models` remain. */
  stopped: boolean;
}

/** Every model this queue should attempt — every one `ranked` lists that is
 *  NOT `gone` (weights no longer on disk, no Run button to press). Order
 *  follows `ranked`'s own best-first order, which is already computed and
 *  already the order a reader scans the leaderboard in.
 *
 *  **Includes models already benchmarked, and ones that failed before** —
 *  deliberately: a re-run is the whole point of a trend, and a failure may
 *  have been transient. Nothing here filters on `row`/history at all, only
 *  on whether the model can physically be run right now.
 */
export function queueableModels(ranked: Pick<LeaderboardRow, "model">[], gone: Set<string>): string[] {
  return ranked.filter((r) => !gone.has(r.model)).map((r) => r.model);
}

/** Start a fresh queue over `models`, in the order given. An empty list
 *  produces an already-finished queue (`current: null`, nothing to tally) —
 *  the caller (the Run All button) is expected not to offer this in the
 *  first place, but a function that degrades honestly over its own empty
 *  input is cheaper than trusting every call site to check first. */
export function startQueue(capability: string, models: string[]): BenchmarkQueue {
  return {
    capability,
    models,
    started: models.length > 0 ? 1 : 0,
    current: models[0] ?? null,
    results: [],
    stopped: false,
  };
}

/** Record `current`'s settled outcome and start the next model — unless a
 *  stop was requested, or `models` is exhausted, in which case `current`
 *  becomes null and the queue is finished (see `queueStatus`).
 *
 *  Takes the result RATHER than reading `queue.current` back out, so a
 *  caller cannot accidentally record a result for the wrong model if the
 *  queue has moved on underneath it (defensive: `model` is asserted against
 *  `queue.current` in the one case that would otherwise silently misfile a
 *  result — a stale async callback resolving after the queue already moved
 *  past its own model).
 */
export function advanceQueue(queue: BenchmarkQueue, result: QueueRunResult): BenchmarkQueue {
  if (queue.current === null || result.model !== queue.current) return queue;
  const results = [...queue.results, result];
  const nextIndex = queue.started; // `started` already counts `current` as attempt #`started`
  if (queue.stopped || nextIndex >= queue.models.length) {
    return { ...queue, current: null, results };
  }
  return { ...queue, current: queue.models[nextIndex]!, started: nextIndex + 1, results };
}

/** Mark a queue stopped — the in-flight model (if any) is left to settle on
 *  its own; nothing beyond it will start. See the file header for why
 *  actually INTERRUPTING the in-flight request is the caller's job, through
 *  the same mechanism a single run's own cancellation already uses. */
export function requestQueueStop(queue: BenchmarkQueue): BenchmarkQueue {
  return { ...queue, stopped: true };
}

export type QueueStatus = "running" | "stopped" | "done";

/** Which of three shapes a queue is in, for the UI to read off directly
 *  rather than re-deriving from `current`/`stopped`/`results` at each call
 *  site.
 *
 *  `"running"` — a model is in flight right now.
 *  `"stopped"` — nothing in flight, a stop was requested, and `models`
 *  was not exhausted — the honest "you ended this early" state, distinct
 *  from finishing the whole list.
 *  `"done"` — nothing in flight and every model was attempted, stop or not.
 */
export function queueStatus(queue: BenchmarkQueue): QueueStatus {
  if (queue.current !== null) return "running";
  return queue.results.length < queue.models.length ? "stopped" : "done";
}

export interface QueueTally {
  succeeded: number;
  failed: number;
  /** Models never even started — only nonzero for a `"stopped"` queue. */
  remaining: number;
}

/** The end-of-run summary — "N succeeded, M failed" — plus how many were
 *  never attempted at all (only possible after a stop). `failed` counts
 *  every settled result that was NOT `ok`, which includes a run cancelled by
 *  the stop button interrupting it — that slot produced no comparable
 *  number either way, and the reader's own stop click is not something this
 *  tally needs to explain away as a separate category.
 */
export function queueTally(queue: BenchmarkQueue): QueueTally {
  let succeeded = 0;
  for (const r of queue.results) if (r.ok) succeeded++;
  const failed = queue.results.length - succeeded;
  const remaining = queue.models.length - queue.results.length - (queue.current !== null ? 1 : 0);
  return { succeeded, failed, remaining: Math.max(0, remaining) };
}
