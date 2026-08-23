import { describe, expect, it } from "bun:test";
import {
  advanceQueue,
  queueableModels,
  queueStatus,
  queueTally,
  requestQueueStop,
  startQueue,
} from "@apps/ai_models/lib/benchmarkQueue";

describe("queueableModels", () => {
  it("is every model, in the ranked order", () => {
    const ranked = [{ model: "a" }, { model: "b" }, { model: "c" }];
    expect(queueableModels(ranked, new Set())).toEqual(["a", "b", "c"]);
  });

  it("excludes a model that is gone (no weights, no Run button)", () => {
    const ranked = [{ model: "a" }, { model: "b" }, { model: "c" }];
    expect(queueableModels(ranked, new Set(["b"]))).toEqual(["a", "c"]);
  });

  it("includes already-benchmarked and previously-failed models — a re-run is the point", () => {
    // queueableModels doesn't even look at history — this test pins that
    // absence: nothing here filters on prior runs, only on whether the
    // model can physically be run right now.
    const ranked = [{ model: "benchmarked-already" }, { model: "failed-before" }];
    expect(queueableModels(ranked, new Set())).toEqual(["benchmarked-already", "failed-before"]);
  });

  it("is empty when everything is gone", () => {
    const ranked = [{ model: "a" }];
    expect(queueableModels(ranked, new Set(["a"]))).toEqual([]);
  });
});

describe("startQueue", () => {
  it("starts on the first model, with nothing settled yet", () => {
    const q = startQueue("automatic-speech-recognition", ["a", "b", "c"]);
    expect(q.current).toBe("a");
    expect(q.started).toBe(1);
    expect(q.results).toEqual([]);
    expect(q.stopped).toBe(false);
  });

  it("is immediately finished over an empty list — nothing to run", () => {
    const q = startQueue("automatic-speech-recognition", []);
    expect(q.current).toBeNull();
    expect(queueStatus(q)).toBe("done");
  });
});

describe("advanceQueue", () => {
  it("moves to the next model on a successful result", () => {
    const q = advanceQueue(startQueue("cap", ["a", "b", "c"]), { model: "a", ok: true });
    expect(q.current).toBe("b");
    expect(q.started).toBe(2);
    expect(q.results).toEqual([{ model: "a", ok: true }]);
  });

  // The requirement this pins: one bad model must not end the whole run.
  it("moves to the next model on a FAILED result too — a failure does not stop the queue", () => {
    const q = advanceQueue(startQueue("cap", ["a", "b", "c"]), { model: "a", ok: false });
    expect(q.current).toBe("b");
    expect(q.results).toEqual([{ model: "a", ok: false }]);
  });

  it("finishes once the last model settles", () => {
    let q = startQueue("cap", ["a", "b"]);
    q = advanceQueue(q, { model: "a", ok: true });
    expect(q.current).toBe("b");
    q = advanceQueue(q, { model: "b", ok: true });
    expect(q.current).toBeNull();
    expect(queueStatus(q)).toBe("done");
    expect(q.results).toEqual([
      { model: "a", ok: true },
      { model: "b", ok: true },
    ]);
  });

  it("stops advancing once requestQueueStop was called, even mid-run", () => {
    let q = startQueue("cap", ["a", "b", "c"]);
    q = requestQueueStop(q); // stop requested while "a" is still in flight
    expect(q.current).toBe("a"); // the in-flight model is untouched by the request itself
    q = advanceQueue(q, { model: "a", ok: true }); // "a" finishes on its own
    expect(q.current).toBeNull(); // but "b" never starts
    expect(q.results).toEqual([{ model: "a", ok: true }]);
    expect(queueStatus(q)).toBe("stopped");
  });

  it("ignores a result for a model that is no longer current — a stale callback cannot misfile", () => {
    const q = startQueue("cap", ["a", "b"]);
    const unchanged = advanceQueue(q, { model: "b", ok: true }); // "b" isn't current yet
    expect(unchanged).toBe(q);
  });

  it("is a no-op once the queue has already finished", () => {
    let q = startQueue("cap", ["a"]);
    q = advanceQueue(q, { model: "a", ok: true });
    const after = advanceQueue(q, { model: "a", ok: true });
    expect(after).toBe(q);
  });
});

describe("queueStatus", () => {
  it("is 'running' while a model is in flight", () => {
    expect(queueStatus(startQueue("cap", ["a"]))).toBe("running");
  });

  it("is 'stopped' when a stop ended the queue before every model ran", () => {
    let q = startQueue("cap", ["a", "b"]);
    q = requestQueueStop(q);
    q = advanceQueue(q, { model: "a", ok: true });
    expect(queueStatus(q)).toBe("stopped");
  });

  it("is 'done' when every model was attempted, no stop needed", () => {
    let q = startQueue("cap", ["a"]);
    q = advanceQueue(q, { model: "a", ok: true });
    expect(queueStatus(q)).toBe("done");
  });

  it("is 'done', not 'stopped', when a stop request arrives too late to change anything", () => {
    // Stopping AFTER the last model already settled must not retroactively
    // read as an early stop — nothing was actually left in the queue.
    let q = startQueue("cap", ["a"]);
    q = advanceQueue(q, { model: "a", ok: true });
    q = requestQueueStop(q);
    expect(queueStatus(q)).toBe("done");
  });
});

describe("queueTally", () => {
  it("counts successes and failures, with nothing remaining once done", () => {
    let q = startQueue("cap", ["a", "b", "c"]);
    q = advanceQueue(q, { model: "a", ok: true });
    q = advanceQueue(q, { model: "b", ok: false });
    q = advanceQueue(q, { model: "c", ok: true });
    expect(queueTally(q)).toEqual({ succeeded: 2, failed: 1, remaining: 0 });
  });

  it("counts a cancelled-by-stop run as failed, not a separate category", () => {
    // The run that was in flight when Stop was pressed settles as `ok:
    // false` (a cancelled request produced no comparable number either) —
    // the tally does not need a fourth bucket for it.
    let q = startQueue("cap", ["a", "b", "c"]);
    q = requestQueueStop(q);
    q = advanceQueue(q, { model: "a", ok: false });
    expect(queueTally(q)).toEqual({ succeeded: 0, failed: 1, remaining: 2 });
  });

  it("reports the un-started models as remaining after a stop", () => {
    let q = startQueue("cap", ["a", "b", "c", "d"]);
    q = advanceQueue(q, { model: "a", ok: true });
    q = requestQueueStop(q);
    q = advanceQueue(q, { model: "b", ok: true });
    expect(queueTally(q)).toEqual({ succeeded: 2, failed: 0, remaining: 2 });
  });

  it("counts the in-flight model as neither settled nor remaining", () => {
    const q = startQueue("cap", ["a", "b", "c"]); // "a" in flight, nothing settled
    expect(queueTally(q)).toEqual({ succeeded: 0, failed: 0, remaining: 2 });
  });
});
