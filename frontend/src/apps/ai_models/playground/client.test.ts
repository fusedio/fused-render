// watchJob's contract, which is three-way and was being read as two.
//
// The bug these encode: a FAILED poll left `row` undefined and fell into the
// "the row vanished" return, so one flaky `/api/jobs` read resolved the watch
// as an ordinary finish — the image stage rendered a file the worker had not
// written yet, the transcribe stage read back a transcript that was not there,
// and the chat stage retried into a second 409. None of that is visible in a
// manual smoke, because the happy path never drops a poll.
import { expect, test } from "bun:test";

import { installDomShim } from "@platform/lib/testDomShim";

// client.ts reaches api.ts/router.ts, which read `location` at module scope;
// bun has no DOM. See testDomShim.ts for why this is the one shared stub
// every suite in the run installs, rather than a stub hand-rolled per file.
installDomShim();

// Dynamic, so the shim above is in place before the module graph evaluates.
const { ModelLoading, startImage, startVideo, streamChat, watchJob, withModelReady } =
  await import("./client");
import type { Job } from "@platform/lib/jobs";
const { currentPresencePage } = await import("@platform/lib/presence");

const JOB: Job = {
  id: "j1",
  title: "Rendering",
  detail: "",
  model: "",
  kind: "task",
  state: "running",
  done: null,
  total: null,
  total_scope: "phase",
  total_estimated: false,
  unit: "",
  message: "",
  page: "",
  source: "",
  origin: "",
  owner: "server",
  cancellable: true,
  cancel_requested: false,
  started_at: 0,
  updated_at: 0,
  finished_at: null,
  stalled: false,
  waiting_for: "",
  tier: "trail",
  group: "j1",
};

// Bug 1 (SPEC-quiet-notifications.md): the Playground is the one producer
// that must send a DISTINCT `X-Fused-Source` — a render's own `page` field
// is deliberately left to fall back to its output path (so a click opens
// the file), which makes `page` useless for "is the user still on the page
// that asked for this". Without this header, `Job.source` on the server
// falls back to whatever `page` resolves to (the same value), and a
// server-backed render can never be suppressed while the Playground is
// open. This pins that the client actually sends it, not just that the
// server can read it if sent.
async function capturedRequest(call: () => Promise<unknown>): Promise<{
  url: string;
  headers: Record<string, string>;
  body: unknown;
}> {
  const realFetch = globalThis.fetch;
  let captured: { url: string; headers: Record<string, string>; body: unknown } | null = null;
  (globalThis as { fetch: unknown }).fetch = async (url: string, init?: RequestInit) => {
    captured = {
      url,
      headers: { ...(init?.headers as Record<string, string> | undefined) },
      body: init?.body ? JSON.parse(init.body as string) : undefined,
    };
    return { ok: true, json: async () => ({}) };
  };
  try {
    await call();
  } finally {
    globalThis.fetch = realFetch;
  }
  if (!captured) throw new Error("fetch was never called");
  return captured;
}

test("startImage sends X-Fused-Source, the raising route, without touching X-Fused-Page", async () => {
  const req = await capturedRequest(() =>
    startImage({ prompt: "a red fox", model: "org/m" }),
  );
  expect(req.url).toBe("/api/ai/image");
  // Percent-encoded, like `X-Fused-Page`'s own header (api.ts's `runHeaders`)
  // and unquoted the same way server-side (`unquote(x_fused_source)`,
  // ai_runtime.py) — SPEC-quiet-notifications.md bug 2 routed this header
  // through api.ts's shared `ambientSourceHeaders`, which now encodes it to
  // match that convention rather than sending the raw path.
  expect(req.headers["X-Fused-Source"]).toBe(encodeURIComponent(currentPresencePage()));
  // Deliberately absent: sending X-Fused-Page here would make the render's
  // OWN `page` (the render's output-path click destination) permanently
  // inherit the Playground's route instead, breaking "click opens the
  // file" — see `fused_render/ai/supervisor.py` `_start_render`'s `page`
  // fallback, which this header must never interfere with.
  expect(req.headers["X-Fused-Page"]).toBeUndefined();
});

test("startVideo sends X-Fused-Source the same way", async () => {
  const req = await capturedRequest(() => startVideo({ prompt: "a red fox running" }));
  expect(req.url).toBe("/api/ai/video");
  expect(req.headers["X-Fused-Source"]).toBe(encodeURIComponent(currentPresencePage()));
  expect(req.headers["X-Fused-Page"]).toBeUndefined();
});

/** A scripted events client whose `jobs` subscription pushes one snapshot
 *  per entry: a string is a state for job j1, `"absent"` a snapshot without
 *  the row, and an Error an error frame (a transport failure the bus will
 *  say again about). Installed through `setEventsClientForTests`, since bun
 *  has no module mocks that stay inside one file. */
function fakeJobsClient(script: (string | Error)[]) {
  let at = 0;
  return {
    subscribe: ((_t: string, _p: unknown, cb: (s: unknown, d: unknown, m: Record<string, unknown>) => void) => {
      let open = true;
      // Frames land on microtasks, one per entry, so a watch that settles on
      // an early frame never sees the later ones — exactly as a closed
      // subscription hears nothing more.
      const next = () => {
        if (!open || at >= script.length) return;
        const step = script[at++];
        if (step instanceof Error) cb(null, null, { error: step.message, status: 500 });
        else cb({ jobs: step === "absent" ? [] : [{ ...JOB, state: step }], now: 0 }, null, { gen: null });
        queueMicrotask(next);
      };
      queueMicrotask(next);
      return () => {
        open = false;
      };
    }) as never,
    at: () => at,
  };
}

/** Drive one watch over a scripted sequence of frames. */
async function watch(script: (string | Error)[], signal = new AbortController().signal) {
  const { setEventsClientForTests } = await import("@platform/lib/events");
  const { GONE_GRACE_MS } = await import("@platform/lib/jobs");
  const realSetTimeout = globalThis.setTimeout;
  const seen: string[] = [];
  const client = fakeJobsClient(script);
  setEventsClientForTests(client);
  // The gone grace is a real timer; collapse it so an `absent` scenario
  // costs nothing.
  (globalThis as { setTimeout: unknown }).setTimeout = (fn: () => void, ms?: number) =>
    realSetTimeout(fn, ms === GONE_GRACE_MS ? 0 : ms);
  try {
    return { outcome: await watchJob("j1", signal, (job) => seen.push(job.state)), seen, at: client.at() };
  } finally {
    setEventsClientForTests(null);
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

test("a vanished row resolves gone once the grace has passed", async () => {
  // A snapshot without the row is not yet evidence it never existed — the
  // reporter's first tick may still be landing — so `gone` takes the grace
  // (`watchJobRow`), not one frame.
  const { outcome } = await watch(["absent"]);
  expect(outcome).toEqual({ state: "gone" });
});

test("a row that comes back inside the grace is not read as gone", async () => {
  // The regression this grace exists to close: one missing frame used to
  // resolve `gone`, which every caller (ImageStage, TranscribeStage,
  // TextStage, EmbedStage) reads as "done, no artefact to distrust" — so a
  // render that was still in flight got filed as a finished one with a path
  // that held nothing.
  const { outcome, seen } = await watch(["running", "absent", "done"]);
  expect(outcome).toEqual({ state: "done", job: { ...JOB, state: "done" } });
  expect(seen).toEqual(["running", "done"]);
});

test("an error row throws its own message", async () => {
  const { setEventsClientForTests } = await import("@platform/lib/events");
  try {
    const realJob = { ...JOB, state: "error", message: "out of memory" };
    setEventsClientForTests({
      subscribe: ((_t: string, _p: unknown, cb: (s: unknown, d: unknown, m: Record<string, unknown>) => void) => {
        queueMicrotask(() => cb({ jobs: [realJob], now: 0 }, null, { gen: null }));
        return () => {};
      }) as never,
    });
    await expect(watchJob("j1", new AbortController().signal)).rejects.toThrow("out of memory");
  } finally {
    setEventsClientForTests(null);
  }
});

test("an error frame is waited out, NOT read as a vanished row", async () => {
  // The regression. Before the fix a transport failure resolved `null` and
  // every caller treated that as success. The bus says again when it is back.
  const { outcome, at } = await watch([
    new Error("network"),
    "running",
    new Error("network"),
    "done",
  ]);
  expect(outcome.state).toBe("done");
  expect(at).toBe(4);
});

test("an already-aborted signal throws before the first frame", async () => {
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
  const { setEventsClientForTests } = await import("@platform/lib/events");
  const realSetTimeout = globalThis.setTimeout;
  const status: (string | null)[] = [];
  let attempts = 0;
  // Every job watch sees one snapshot with the row in `jobState`.
  setEventsClientForTests({
    subscribe: ((_t: string, _p: unknown, cb: (s: unknown, d: unknown, m: Record<string, unknown>) => void) => {
      queueMicrotask(() => cb({ jobs: [{ ...JOB, state: jobState }], now: 0 }, null, { gen: null }));
      return () => {};
    }) as never,
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
    setEventsClientForTests(null);
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
