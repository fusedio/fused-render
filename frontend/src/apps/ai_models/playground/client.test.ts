// watchJob's contract, which is three-way and was being read as two.
//
// The bug these encode: a FAILED poll left `row` undefined and fell into the
// "the row vanished" return, so one flaky `/api/jobs` read resolved the watch
// as an ordinary finish — the image stage rendered a file the worker had not
// written yet, the transcribe stage read back a transcript that was not there,
// and the chat stage retried into a second 409. None of that is visible in a
// manual smoke, because the happy path never drops a poll.
import { expect, test } from "bun:test";

// client.ts reaches api.ts/router.ts, which read `location` at module scope;
// bun has no DOM. Same shim as router.test.ts.
(globalThis as { location?: unknown }).location ??= {
  pathname: "/ai-models/playground",
  search: "",
  href: "http://localhost/ai-models/playground",
  origin: "http://localhost",
};
(globalThis as { history?: unknown }).history ??= {
  state: null,
  pushState() {},
  replaceState() {},
};
(globalThis as { window?: unknown }).window ??= {
  dispatchEvent() {},
  addEventListener() {},
  removeEventListener() {},
};

// Dynamic, so the shim above is in place before the module graph evaluates.
const { watchJob } = await import("./client");
import type { Job } from "@platform/lib/jobs";

const JOB: Job = {
  id: "j1",
  title: "Rendering",
  detail: "",
  kind: "task",
  state: "running",
  done: null,
  total: null,
  unit: "",
  message: "",
  page: "",
  owner: "server",
  cancellable: true,
  cancel_requested: false,
  started_at: 0,
  updated_at: 0,
  finished_at: null,
  stalled: false,
};

/** Drive one watch over a scripted sequence of polls. A string entry is a
 *  state for job j1, `"absent"` is a snapshot without the row, and an Error is
 *  a transport failure. The 1s sleep is stubbed out so a multi-tick scenario
 *  costs nothing. */
async function watch(script: (string | Error)[], signal = new AbortController().signal) {
  const realFetch = globalThis.fetch;
  const realSetTimeout = globalThis.setTimeout;
  let at = 0;
  const seen: string[] = [];
  (globalThis as { fetch: unknown }).fetch = async () => {
    const step = script[Math.min(at++, script.length - 1)];
    if (step instanceof Error) throw step;
    return {
      ok: true,
      json: async () => ({
        jobs: step === "absent" ? [] : [{ ...JOB, state: step }],
        now: 0,
      }),
    };
  };
  (globalThis as { setTimeout: unknown }).setTimeout = (fn: () => void) => realSetTimeout(fn, 0);
  try {
    return { outcome: await watchJob("j1", signal, (job) => seen.push(job.state)), seen, at };
  } finally {
    globalThis.fetch = realFetch;
    globalThis.setTimeout = realSetTimeout;
  }
}

test("a terminal row resolves done, carrying the row", async () => {
  const { outcome, seen } = await watch(["running", "done"]);
  expect(outcome).toEqual({ state: "done", job: { ...JOB, state: "done" } });
  // onTick fires for every poll that read a row, the running one included —
  // that is what draws the progress bar.
  expect(seen).toEqual(["running", "done"]);
});

test("a cancelled row is NOT a finished one", async () => {
  // The distinction the callers need: no artefact was written, so the image
  // stage must not render the output path and the transcribe stage must not
  // claim a saved transcript.
  const { outcome } = await watch(["running", "cancelled"]);
  expect(outcome.state).toBe("cancelled");
});

test("a vanished row resolves gone after enough consecutive misses", async () => {
  // FINISHED_TTL_S is a few seconds against a 1s poll, so the manager
  // retiring a row we took too long to read is still the ordinary case here
  // — it just takes a run of misses, not one, to conclude that is what
  // happened (see GONE_MISS_TOLERANCE).
  const { outcome } = await watch(["absent"]);
  expect(outcome).toEqual({ state: "gone" });
});

test("a single missed poll is not read as gone", async () => {
  // The regression this tolerance exists to close: one slow tick used to
  // resolve `gone`, which every caller (ImageStage, TranscribeStage,
  // TextStage, EmbedStage) reads as "done, no artefact to distrust" — so a
  // render that was still in flight got filed as a finished one with a path
  // that held nothing.
  const { outcome, seen } = await watch(["running", "absent", "done"]);
  expect(outcome).toEqual({ state: "done", job: { ...JOB, state: "done" } });
  expect(seen).toEqual(["running", "done"]);
});

test("misses only count while consecutive — a sighting resets the count", async () => {
  const script: (string | Error)[] = ["running"];
  for (let i = 0; i < 8; i++) {
    script.push("absent");
    script.push("running"); // resets the streak before it reaches tolerance
  }
  script.push("done");
  const { outcome } = await watch(script);
  expect(outcome).toEqual({ state: "done", job: { ...JOB, state: "done" } });
});

test("an error row throws its own message", async () => {
  const realFetch = globalThis.fetch;
  (globalThis as { fetch: unknown }).fetch = async () => ({
    ok: true,
    json: async () => ({ jobs: [{ ...JOB, state: "error", message: "out of memory" }], now: 0 }),
  });
  try {
    await expect(watchJob("j1", new AbortController().signal)).rejects.toThrow("out of memory");
  } finally {
    globalThis.fetch = realFetch;
  }
});

test("a failed poll is retried, NOT read as a vanished row", async () => {
  // The regression. Before the fix this resolved `null` on the first throw and
  // every caller treated that as success.
  const { outcome, at } = await watch([
    new Error("network"),
    "running",
    new Error("network"),
    "done",
  ]);
  expect(outcome.state).toBe("done");
  expect(at).toBe(4);
});

test("a run of failed polls eventually gives up instead of hanging", async () => {
  // Without a cap, a dead server is a spinner that never resolves and never
  // errors — the failure the retry loop would otherwise trade the old bug for.
  const script = Array.from({ length: 40 }, () => new Error("network"));
  await expect(watch(script)).rejects.toThrow(/lost contact/);
});

test("a poll run that recovers resets the failure count", async () => {
  const script: (string | Error)[] = [];
  for (let round = 0; round < 5; round++) {
    for (let i = 0; i < 9; i++) script.push(new Error("network"));
    script.push("running");
  }
  script.push("done");
  const { outcome } = await watch(script);
  expect(outcome.state).toBe("done");
});

test("an already-aborted signal throws before the first poll", async () => {
  const controller = new AbortController();
  controller.abort();
  await expect(watch(["done"], controller.signal)).rejects.toThrow(/abort/i);
});
