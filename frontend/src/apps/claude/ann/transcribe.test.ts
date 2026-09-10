// The warm latch, the per-segment flattening, and the five error sentences
// (R:3310, 4437, 4463, 4489, 4490) — verbatim, because a caller shows them.
import { beforeEach, describe, expect, test } from "bun:test";
import type { Job, JobsSnapshot } from "@platform/lib/jobs";

import {
  asrCapability,
  flattenWords,
  isWarming,
  resetWarmLatchForTests,
  startTranscribe,
  transcribe,
  warmTranscriber,
  type TranscribeError,
  type TranscriptSegment,
} from "./transcribe";

function job(over: Partial<Job> = {}): Job {
  return {
    id: "j1",
    title: "Transcribing",
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
    owner: "server",
    cancellable: true,
    cancel_requested: false,
    started_at: 0,
    updated_at: 0,
    finished_at: null,
    stalled: false,
    waiting_for: "",
    tier: "trail",
    ...over,
  };
}

const snapshot = (jobs: Job[]): JobsSnapshot => ({ jobs }) as JobsSnapshot;

const started = {
  jobId: "j1",
  path: "/rec/a.m4a",
  output: "/t/a.json",
  outputText: "/t/a.txt",
  outputPartial: "/t/a.partial.jsonl",
  model: "whisper",
  provider: "local",
  task: "transcribe",
};

/** One tick per call, so a poll loop cannot hang a test. */
function polls(states: Array<Job[] | null>) {
  let i = 0;
  return () => {
    const next = states[Math.min(i, states.length - 1)];
    i += 1;
    return Promise.resolve(snapshot(next || []));
  };
}

const deps = (over: Record<string, unknown> = {}) => ({
  post: (() => Promise.resolve(started)) as never,
  jobs: polls([[job({ state: "done" })]]) as never,
  readJson: () =>
    Promise.resolve({
      text: "hello world",
      segments: [{ start: 0, end: 1, text: "hello world" }],
    }),
  cancel: () => Promise.resolve(undefined),
  sleep: () => Promise.resolve(),
  ...over,
});

describe("flattenWords (T:8093-8104)", () => {
  test("per word when every word of the segment is timed", () => {
    const segments: TranscriptSegment[] = [
      {
        text: " one two",
        startSecond: 0,
        words: [
          { word: " one", startSecond: 0.1 },
          { word: " two", startSecond: 0.9 },
        ],
      },
    ];
    expect(flattenWords(segments)).toEqual([
      { text: " one", start: 0.1, end: undefined },
      { text: " two", start: 0.9, end: undefined },
    ]);
  });

  test("a segment whose words are not ALL timed falls back WHOLE — no words lost", () => {
    const segments: TranscriptSegment[] = [
      {
        text: "one two",
        startSecond: 3,
        words: [
          { word: "one", startSecond: 3 },
          { word: "two", startSecond: undefined as unknown as number },
        ],
      },
    ];
    expect(flattenWords(segments)).toEqual([{ text: "one two", start: 3, end: undefined }]);
  });

  test("a MIXED reply is matched per segment, at whatever grain each arrived with", () => {
    const segments: TranscriptSegment[] = [
      { text: "a b", startSecond: 0, words: [{ word: "a", startSecond: 0 }, { word: "b", startSecond: 1 }] },
      { text: "c d", startSecond: 2 },
    ];
    expect(flattenWords(segments).map((u) => u.text)).toEqual(["a", "b", "c d"]);
  });
});

describe("warmTranscriber (T:7814)", () => {
  beforeEach(resetWarmLatchForTests);

  const cap = (over: Record<string, unknown> = {}) =>
    ({
      capability: "automatic-speech-recognition",
      available: true,
      default: "whisper-tiny",
      models: [{ id: "whisper-tiny", loaded: false }],
      ...over,
    }) as never;

  test("loads the capability's default", async () => {
    const loads: Array<[string, string | undefined]> = [];
    await warmTranscriber({
      catalog: (() => Promise.resolve({ capabilities: [cap()] })) as never,
      load: ((m: string, c?: string) => {
        loads.push([m, c]);
        return Promise.resolve({ jobId: "l", model: m, state: "running" });
      }) as never,
    });
    expect(loads).toEqual([["whisper-tiny", "automatic-speech-recognition"]]);
  });

  test("skipped when a speech model is already resident (T:7810)", async () => {
    let loaded = 0;
    await warmTranscriber({
      catalog: (() =>
        Promise.resolve({ capabilities: [cap({ models: [{ id: "x", loaded: true }] })] })) as never,
      load: (() => {
        loaded += 1;
        return Promise.resolve({ jobId: "l", model: "x", state: "running" });
      }) as never,
    });
    expect(loaded).toBe(0);
  });

  test("skipped when the engine names no default (T:7811)", async () => {
    let loaded = 0;
    await warmTranscriber({
      catalog: (() => Promise.resolve({ capabilities: [cap({ default: null })] })) as never,
      load: (() => {
        loaded += 1;
        return Promise.resolve({ jobId: "l", model: "x", state: "running" });
      }) as never,
    });
    expect(loaded).toBe(0);
  });

  test("an unavailable capability is no capability (T:7820)", () => {
    expect(asrCapability([cap({ available: false })])).toBeNull();
    expect(asrCapability(undefined)).toBeNull();
  });

  test("ONE warm-up in flight: the second ask is a no-op while the first runs", async () => {
    let asked = 0;
    let release = () => {};
    const first = warmTranscriber({
      catalog: (() =>
        new Promise((res) => {
          asked += 1;
          release = () => res({ capabilities: [] });
        })) as never,
    });
    expect(isWarming()).toBe(true);
    await warmTranscriber({ catalog: (() => Promise.resolve({ capabilities: [] })) as never });
    expect(asked).toBe(1);
    release();
    await first;
    expect(isWarming()).toBe(false);
  });

  test("a failed warm-up is SWALLOWED with its own sentence (T:7828)", async () => {
    const said: string[] = [];
    await warmTranscriber({
      catalog: (() => Promise.reject(new Error("no engine"))) as never,
      warn: (m, d) => said.push(m + " " + String(d)),
    });
    expect(said).toEqual(["transcriber warm-up skipped: no engine"]);
    expect(isWarming()).toBe(false);
  });
});

describe("startTranscribe", () => {
  test("the happy path reads the transcript file and flattens it", async () => {
    const out = await transcribe({ path: "/rec/a.m4a", words: true }, deps());
    expect(out.text).toBe("hello world");
    expect(out.words).toEqual([{ text: "hello world", start: 0, end: 1 }]);
  });

  test("`words: true` and nothing else reaches the route", async () => {
    const bodies: unknown[] = [];
    await transcribe(
      { path: "/rec/a.m4a", words: true },
      deps({
        post: ((_url: string, body: unknown) => {
          bodies.push(body);
          return Promise.resolve(started);
        }) as never,
      }),
    );
    expect(bodies).toEqual([{ path: "/rec/a.m4a", words: true }]);
  });

  test("a 200 with no jobId is an error, not a watch on undefined (R:3310)", async () => {
    const err = (await transcribe(
      { path: "/a" },
      deps({ post: (() => Promise.resolve({})) as never }),
    ).catch((e: TranscribeError) => e)) as TranscribeError;
    expect(err.message).toBe("/api/ai/transcribe replied with no jobId");
    expect(err.type).toBe("ai_error");
  });

  test("an error row rejects with the row's own message (R:4490)", async () => {
    const err = (await transcribe(
      { path: "/a" },
      deps({ jobs: polls([[job({ state: "error", message: "the model fell over" })]]) as never }),
    ).catch((e: TranscribeError) => e)) as TranscribeError;
    expect(err.message).toBe("the model fell over");
    expect(err.type).toBe("ai_error");
    expect(err.jobId).toBe("j1");
    expect(err.outputPartial).toBe("/t/a.partial.jsonl");
  });

  test("a messageless error row gets the fallback sentence (R:4490)", async () => {
    const err = (await transcribe(
      { path: "/a" },
      deps({ jobs: polls([[job({ state: "error" })]]) as never }),
    ).catch((e: TranscribeError) => e)) as TranscribeError;
    expect(err.message).toBe("the transcription failed");
  });

  test("a cancelled row is `cancelled`, not a failure (R:4489)", async () => {
    const err = (await transcribe(
      { path: "/a" },
      deps({ jobs: polls([[job({ state: "cancelled" })]]) as never }),
    ).catch((e: TranscribeError) => e)) as TranscribeError;
    expect(err.message).toBe("the transcription was cancelled");
    expect(err.type).toBe("cancelled");
  });

  test("an unreadable transcript is TYPED, not a bare SyntaxError (R:4437)", async () => {
    const err = (await transcribe(
      { path: "/a" },
      deps({ readJson: () => Promise.reject(new Error("HTTP 404")) }),
    ).catch((e: TranscribeError) => e)) as TranscribeError;
    expect(err.message).toBe("the transcript could not be read: HTTP 404");
    expect(err.type).toBe("ai_error");
  });

  test("a row that AGED OUT is answered from the transcript when it landed (R:4448)", async () => {
    const out = await transcribe(
      { path: "/a" },
      deps({ jobs: polls([[job()], [], [], [], [], []]) as never }),
    );
    expect(out.text).toBe("hello world");
  });

  test("…and rejects with the reporting sentence when it did not (R:4463)", async () => {
    const err = (await transcribe(
      { path: "/a" },
      deps({
        jobs: polls([[job()], [], [], [], [], []]) as never,
        readJson: () => Promise.reject(new Error("gone")),
      }),
    ).catch((e: TranscribeError) => e)) as TranscribeError;
    expect(err.message).toBe("the transcription job is no longer being reported");
    expect(err.output).toBe("/t/a.json");
  });

  test("progress is reported per tick while the row lives (R:4204)", async () => {
    const seen: string[] = [];
    await transcribe(
      { path: "/a" },
      deps({
        jobs: polls([[job()], [job()], [job({ state: "done" })]]) as never,
        onProgress: (j: Job) => seen.push(j.state),
      }),
    );
    expect(seen).toEqual(["running", "running", "done"]);
  });

  test("cancel stops the watch and rejects `cancelled`", async () => {
    let cancelled = "";
    const run = await startTranscribe(
      { path: "/a" },
      deps({
        jobs: polls([[job()]]) as never,
        cancel: (id: string) => {
          cancelled = id;
          return Promise.resolve(undefined);
        },
      }),
    );
    expect(run.jobId).toBe("j1");
    await run.cancel();
    const err = (await run.done.catch((e: TranscribeError) => e)) as TranscribeError;
    expect(cancelled).toBe("j1");
    expect(err.type).toBe("cancelled");
    expect(err.message).toBe("the transcription was cancelled");
  });
});

// ── the watch always ends ──────────────────────────────────────────────────
//
// `end()` paints "Transcribing…" and disables the Comment seat before it awaits
// this promise, so an exit the loop can never reach is a status bar stuck for
// the life of the page (Bugbot, PR #1074). Two ways a row is never terminal —
// never LISTED, and never READ — and both are bounded.
describe("a job the listing never carries", () => {
  test("stops polling instead of watching forever (Bugbot, PR #1074)", async () => {
    let asked = 0;
    const err = (await transcribe(
      { path: "/a" },
      deps({
        // The row is absent from the very first poll: nothing was ever seen, so
        // the old `seen &&` gate never armed the miss counter.
        jobs: (() => {
          asked += 1;
          return Promise.resolve(snapshot([]));
        }) as never,
        readJson: () => Promise.reject(new Error("gone")),
      }),
    ).catch((e: TranscribeError) => e)) as TranscribeError;
    // Five misses is the whole budget — the loop is not still running.
    expect(asked).toBe(5);
    expect(err.message).toBe("the transcription job is no longer being reported");
    expect(err.type).toBe("ai_error");
    // The salvage paths ride along, same as the aged-out row's rejection.
    expect(err.jobId).toBe("j1");
    expect(err.outputPartial).toBe("/t/a.partial.jsonl");
  });

  test("…and is still answered from the transcript when the words did land", async () => {
    // Unseen is not failed: the worker may have finished and the row retired
    // before the first poll. The FILE is the witness (R:4448).
    const out = await transcribe(
      { path: "/a" },
      deps({ jobs: (() => Promise.resolve(snapshot([]))) as never }),
    );
    expect(out.text).toBe("hello world");
  });

  test("a listing that keeps failing gives up too, and never spends a miss", async () => {
    let asked = 0;
    const err = (await transcribe(
      { path: "/a" },
      deps({
        jobs: (() => {
          asked += 1;
          return Promise.reject(new Error("offline"));
        }) as never,
        readJson: () => Promise.reject(new Error("gone")),
      }),
    ).catch((e: TranscribeError) => e)) as TranscribeError;
    // A failed READ says nothing about the row, so it costs a failure and not a
    // miss: ten tries, not five.
    expect(asked).toBe(10);
    expect(err.message).toBe("the transcription job is no longer being reported");
  });

  test("a blip in the listing does NOT count against the row", async () => {
    // Four failures, then the row, then done. Nothing is spent permanently: a
    // transient `/api/jobs` outage must not retire a job that is still running.
    const script: Array<() => Promise<JobsSnapshot>> = [
      () => Promise.reject(new Error("blip")),
      () => Promise.reject(new Error("blip")),
      () => Promise.reject(new Error("blip")),
      () => Promise.reject(new Error("blip")),
      () => Promise.resolve(snapshot([job()])),
      () => Promise.reject(new Error("blip")),
      () => Promise.resolve(snapshot([job({ state: "done" })])),
    ];
    let i = 0;
    const out = await transcribe(
      { path: "/a" },
      deps({ jobs: (() => script[Math.min(i++, script.length - 1)]()) as never }),
    );
    expect(out.text).toBe("hello world");
  });
});
