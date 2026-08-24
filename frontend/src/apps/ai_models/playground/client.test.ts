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
const { ModelLoading, streamChat, watchJob, withModelReady } = await import("./client");
import type { Job } from "@platform/lib/jobs";

const JOB: Job = {
  id: "j1",
  title: "Rendering",
  detail: "",
  model: "",
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

// -- withModelReady: the cold-start dance, bounded ---------------------------
//
// The bug these encode: the dance used to retry EXACTLY once, so a load that
// finished and then lost the capability slot to another model — a second tab,
// the Models page, an app calling fused.ai — came back to the reader as
// "<model> is still loading (loading)". Nothing was broken; the answer was to
// ask again. The image stage never showed this because its wait happens on the
// server, inside the render job.

/** Drive one dance. `script` is what each attempt does: "ok" resolves, "409"
 *  throws ModelLoading with a job id, "409-nojob" throws one without. Every
 *  job poll answers `done`, and both sleeps are stubbed to zero. */
async function dance(script: string[], jobState = "done") {
  const realFetch = globalThis.fetch;
  const realSetTimeout = globalThis.setTimeout;
  const status: (string | null)[] = [];
  let attempts = 0;
  (globalThis as { fetch: unknown }).fetch = async () => ({
    ok: true,
    json: async () => ({ jobs: [{ ...JOB, state: jobState }], now: 0 }),
  });
  (globalThis as { setTimeout: unknown }).setTimeout = (fn: () => void) => realSetTimeout(fn, 0);
  const attempt = async () => {
    const step = script[Math.min(attempts++, script.length - 1)];
    if (step === "409") throw new ModelLoading("tiny-model is still loading (loading)", "j1");
    if (step === "409-nojob") throw new ModelLoading("tiny-model is loading now", null);
    if (step === "boom") throw new Error("the model process did not answer");
    return "the answer";
  };
  try {
    return {
      result: await withModelReady(attempt, {
        signal: new AbortController().signal,
        downloaded: true,
        onStatus: (text) => status.push(text),
      }),
      attempts,
      status,
    };
  } finally {
    globalThis.fetch = realFetch;
    globalThis.setTimeout = realSetTimeout;
  }
}

test("a resident model runs on the first attempt and narrates nothing", async () => {
  const { result, attempts, status } = await dance(["ok"]);
  expect(result).toBe("the answer");
  expect(attempts).toBe(1);
  expect(status).toEqual([]);
});

test("a cold start is watched and asked again", async () => {
  const { result, attempts, status } = await dance(["409", "ok"]);
  expect(result).toBe("the answer");
  expect(attempts).toBe(2);
  expect(status[0]).toMatch(/first run pays/);
  // Handed null once there is nothing left to say.
  expect(status[status.length - 1]).toBeNull();
});

test("a model evicted between the load and the retry is waited for again", async () => {
  // THE REGRESSION. Two 409s in a row is not a failure: the first load
  // finished, something else took the slot, and the second load is ours again.
  const { result, attempts, status } = await dance(["409", "409", "409", "ok"]);
  expect(result).toBe("the answer");
  expect(attempts).toBe(4);
  expect(status.some((text) => text?.includes("took its place"))).toBe(true);
});

test("a model that never keeps its place says so, rather than spinning", async () => {
  await expect(dance(["409"])).rejects.toThrow(/keeps losing its place/);
});

test("the failing message carries what the server said", async () => {
  await expect(dance(["409"])).rejects.toThrow(/still loading \(loading\)/);
});

test("a 409 with no job to watch is paced, not hammered", async () => {
  const { result, attempts } = await dance(["409-nojob", "ok"]);
  expect(result).toBe("the answer");
  expect(attempts).toBe(2);
});

test("a load stopped from the Activity panel ends the call", async () => {
  await expect(dance(["409", "ok"], "cancelled")).rejects.toThrow(/load was cancelled/);
});

test("any other failure is the caller's, untouched", async () => {
  await expect(dance(["boom"])).rejects.toThrow("the model process did not answer");
});

// -- streamChat's images: AI-11j's text-stage half ----------------------------

/** A minimal `Response.body`-shaped fake: one `read()` per NDJSON line, then
 *  done — enough for `streamChat`'s reader loop, without a real ReadableStream. */
function fakeBody(lines: string[]) {
  let at = 0;
  const encoder = new TextEncoder();
  return {
    getReader() {
      return {
        read: async () => {
          if (at >= lines.length) return { done: true, value: undefined };
          return { done: false, value: encoder.encode(lines[at++]) };
        },
      };
    },
  };
}

test("streamChat sends images in the request body", async () => {
  let sentBody: Record<string, unknown> | null = null;
  const realFetch = globalThis.fetch;
  (globalThis as { fetch: unknown }).fetch = async (_url: string, init: RequestInit) => {
    sentBody = JSON.parse(init.body as string);
    return {
      ok: true,
      body: fakeBody([
        '{"type":"chunk","text":"a cat"}\n',
        '{"type":"done","ok":true,"result":{"text":"a cat","model":"m","usage":null}}\n',
      ]),
    };
  };
  try {
    const result = await streamChat({
      model: "m",
      prompt: "what is this?",
      history: [],
      settings: {},
      images: ["/Users/x/photo.png"],
      signal: new AbortController().signal,
      onChunk: () => {},
    });
    expect(result.text).toBe("a cat");
  } finally {
    globalThis.fetch = realFetch;
  }
  expect(sentBody).not.toBeNull();
  expect(sentBody!.images).toEqual(["/Users/x/photo.png"]);
});

test("streamChat omits images entirely when none are attached", async () => {
  // Optional, and left off the body rather than sent empty — matching the
  // worker's own "absent/empty is today's text path, unchanged" contract
  // (mlx_text/worker.py).
  let sentBody: Record<string, unknown> | null = null;
  const realFetch = globalThis.fetch;
  (globalThis as { fetch: unknown }).fetch = async (_url: string, init: RequestInit) => {
    sentBody = JSON.parse(init.body as string);
    return {
      ok: true,
      body: fakeBody([
        '{"type":"done","ok":true,"result":{"text":"","model":"m","usage":null}}\n',
      ]),
    };
  };
  try {
    await streamChat({
      model: "m",
      prompt: "hi",
      history: [],
      settings: {},
      signal: new AbortController().signal,
      onChunk: () => {},
    });
  } finally {
    globalThis.fetch = realFetch;
  }
  expect(sentBody).not.toBeNull();
  expect("images" in sentBody!).toBe(false);
});
