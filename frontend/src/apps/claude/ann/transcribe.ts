// THE WORDS — the shell's port of `fused.ai.transcribe({path, words: true})`
// (R:4232) and of `annWarmTranscriber` (T:7814).
//
// `POST /api/ai/transcribe` is JOB-BACKED, not streamed, and for the reason
// squared (`routers/ai_runtime.py api_ai_transcribe`): a 90-minute recording is
// minutes of decoding. The reply comes back immediately with a `jobId` to watch
// and with the OUTPUT PATHS already decided, so nothing needs a second lookup —
// and the transcript is a FILE, so a session that navigated away mid-run still
// finds it. There is no shell equivalent of this call: the download manager
// reads job rows, but nobody here has ever started one and read its artefact
// back, which is what this module is.
//
// The audio is never handed to Claude: the server reads it locally and only the
// WORDS leave this module, so it needs no `Read(...)` rule of its own
// (T:8245-8248).
import {
  getAiCatalog,
  loadAiModel,
  postJson,
  rawUrl,
  type AiCatalogCapability,
} from "@platform/lib/api";
import { fetchJobs, type Job, type JobState } from "@platform/lib/jobs";

/** The capability id the speech models sit under, server-side
 *  (`registry.SPEECH_TO_TEXT`). One spelling, used by both functions here. */
export const ASR_CAPABILITY = "automatic-speech-recognition";

// ── warming ────────────────────────────────────────────────────────────────
//
// Warm the transcriber the moment a recording STARTS, so the words are not
// waiting on a cold model when it stops — the load runs while the reader is
// still talking, which is exactly the dead time it fits in. Fire-and-forget and
// swallowed whole: the transcribe call loads a cold model inside its own job
// anyway, so a failed warm-up costs nothing but the latency it was trying to
// save (T:7804-7813).

/** `annAsrWarming` (T:7813): ONE warm-up in flight at a time. The mic and the
 *  walkthrough both ask, sometimes seconds apart. */
let warming = false;

/** Test-only reset — the latch is module state on purpose (it is a fact about
 *  this document, not about a component), so a suite needs a way to clear it. */
export function resetWarmLatchForTests(): void {
  warming = false;
}

export function isWarming(): boolean {
  return warming;
}

export interface WarmDeps {
  catalog?: typeof getAiCatalog;
  load?: typeof loadAiModel;
  warn?: (message: string, detail?: unknown) => void;
}

/** The speech row out of the catalog, or null when this machine has none
 *  (T:7818-7821). */
export function asrCapability(
  capabilities: AiCatalogCapability[] | undefined,
): AiCatalogCapability | null {
  const row = (capabilities || []).find((r) => r.capability === ASR_CAPABILITY);
  if (!row || !row.available) return null;
  return row;
}

/**
 * `annWarmTranscriber` (T:7814). Returns the promise so a test can await it;
 * every caller in the app fires and forgets.
 *
 * Skipped when a speech model is already resident (`loaded`), and when the
 * engine names no default — the transcribe call's own default (the same
 * first-curated entry) will resolve it then (T:7810-7812).
 */
export function warmTranscriber(deps: WarmDeps = {}): Promise<void> {
  if (warming) return Promise.resolve();
  warming = true;
  const catalog = deps.catalog || getAiCatalog;
  const load = deps.load || loadAiModel;
  const warn = deps.warn || ((m: string, d?: unknown) => console.warn(m, d));
  return (async () => {
    try {
      const cat = await catalog();
      const asr = asrCapability(cat && cat.capabilities);
      if (!asr) return;
      if ((asr.models || []).some((m) => m && m.loaded)) return;
      if (!asr.default) return;
      await load(asr.default, ASR_CAPABILITY);
    } catch (err) {
      warn("transcriber warm-up skipped:", errText(err));
    } finally {
      warming = false;
    }
  })();
}

// ── the transcript ─────────────────────────────────────────────────────────

/** One timed piece of speech — a WORD when the engine timed the words, else a
 *  whole segment. The unit `ann/rec.ts` matches clicks against.
 *
 *  `text` arrives with its leading space on a word (the engine's own shape),
 *  which the joins in `rec.ts` collapse like any other whitespace (T:8100). */
export interface TranscriptWord {
  text: string;
  start: number;
  end?: number;
}

/** A segment as the transcript FILE stores it, renamed the way `frameSegment`
 *  renames it (R:3288-3296): `{start, end}` on disk, `{startSecond, endSecond}`
 *  on the wire. Kept in that wire spelling so this module and the template
 *  describe the same object. */
export interface TranscriptSegment {
  text: string;
  startSecond: number;
  endSecond?: number;
  speaker?: string;
  /** `startSecond` is OPTIONAL because the engine's answer is: a model without
   *  word timings leaves it off, and `flattenWords` narrows on exactly that
   *  before it trusts one (a cast said the opposite and hid the narrowing). */
  words?: Array<{ word: string; startSecond?: number; endSecond?: number }>;
}

export interface Transcript {
  /** The whole thing, as the engine wrote it. */
  text: string;
  /** The flattened timeline — per WORD wherever the engine timed the words,
   *  per segment where it did not. THE FLATTENING IS T's, per segment
   *  (T:8093-8104), so a mixed reply (some segments worded, some not) is
   *  matched at whatever grain each segment arrived with, and a segment whose
   *  words are not all timed falls back WHOLE rather than dropping the untimed
   *  ones: no words are lost either way. */
  words: TranscriptWord[];
  /** The raw segments, for a caller that wants the sentence grain back. */
  segments: TranscriptSegment[];
  language?: string;
  durationInSeconds?: number;
}

/** A typed rejection, the shape every AI call on this bridge produces
 *  (R:4443-4455). `.jobId` is carried so a caller can point at the row. */
export interface TranscribeError extends Error {
  type?: "bad_request" | "ai_unavailable" | "ai_error" | "cancelled" | "timeout";
  jobId?: string;
  /** Where the salvage is on a failed run: the `.json` that was never written,
   *  and the `.partial.jsonl` the worker deliberately LEAVES behind
   *  (R:4468-4478). Carried, not read — see the deferral note at the bottom. */
  output?: string;
  outputPartial?: string;
}

function fail(
  message: string,
  type: TranscribeError["type"],
  extra: Partial<TranscribeError> = {},
): TranscribeError {
  const err = new Error(message) as TranscribeError;
  err.type = type;
  return Object.assign(err, extra);
}

function errText(err: unknown): string {
  const e = err as { message?: string; name?: string } | null;
  return (e && (e.message || e.name)) || String(err);
}

export interface TranscribeOptions {
  /** Absolute, or relative to `base`. The walkthrough always has an absolute
   *  one — the recorder named the file (CP-2). */
  path: string;
  /** Per-word timings inside each segment (D392), which is what lets a click
   *  that landed MID-sentence take only the words after it. Best-effort by
   *  contract, never refused: an engine without word timings just leaves the
   *  `words` key off its segments, so the matcher degrades to whole-segment
   *  matching on its own (T:8250-8260). */
  words?: boolean;
  model?: string;
  language?: string;
  task?: "transcribe" | "translate";
  /** The calling page's own path, for the page-relative `path` rule (RH-1). */
  base?: string;
}

/** The `/api/ai/transcribe` reply: where the words WILL land. */
interface TranscribeStarted {
  jobId: string;
  path: string;
  output: string;
  outputText: string;
  outputPartial: string;
  model: string;
  provider: string;
  task: string;
}

/** A run in flight. The three things R:4232's wrapper gives a caller — the id,
 *  the progress, the cancel — plus the promise. */
export interface TranscribeRun {
  jobId: string;
  /** The transcript, or a typed rejection. */
  done: Promise<Transcript>;
  /** A real stop: the server owns the worker and can genuinely kill it
   *  (R:4750-4756). */
  cancel(): Promise<void>;
}

export interface TranscribeDeps {
  post?: typeof postJson;
  jobs?: typeof fetchJobs;
  /** Reads the finished transcript. Injected so a test needs no `/api/fs/raw`. */
  readJson?: (path: string) => Promise<unknown>;
  /** `POST /api/jobs/{id}/cancel`. */
  cancel?: (jobId: string) => Promise<unknown>;
  /** Per-tick, while the row lives (R:4204's `onProgress`). */
  onProgress?: (job: Job) => void;
  /** The poll interval — `watchJob`'s own floor and default (R:4732). */
  intervalMs?: number;
  sleep?: (ms: number) => Promise<void>;
}

function defaultReadJson(path: string): Promise<unknown> {
  return fetch(rawUrl(path)).then((res) => {
    if (!res.ok) throw new Error("failed to read " + path + ": HTTP " + res.status);
    return res.json();
  });
}

function defaultCancel(jobId: string): Promise<unknown> {
  return fetch("/api/jobs/" + encodeURIComponent(jobId) + "/cancel", {
    method: "POST",
    headers: { "X-Fused": "1" },
  }).catch(() => undefined);
}

/** The transcript file's own segment shape → the wire's (R:3288). */
function frameSegment(raw: unknown): TranscriptSegment {
  const s = (raw || {}) as {
    text?: string;
    start?: number;
    end?: number;
    speaker?: string;
    words?: Array<{ word?: string; start?: number; end?: number }>;
  };
  const out: TranscriptSegment = {
    text: s.text || "",
    startSecond: typeof s.start === "number" ? s.start : 0,
  };
  if (typeof s.end === "number") out.endSecond = s.end;
  if (s.speaker) out.speaker = s.speaker;
  if (Array.isArray(s.words)) {
    out.words = s.words.map((w) => ({
      word: w.word || "",
      ...(typeof w.start === "number" ? { startSecond: w.start } : {}),
      endSecond: w.end,
    }));
  }
  return out;
}

/** T:8093-8104, verbatim in behaviour: per WORD when the engine timed every
 *  word of a segment, per SEGMENT otherwise, decided one segment at a time. */
export function flattenWords(segments: TranscriptSegment[]): TranscriptWord[] {
  const units: TranscriptWord[] = [];
  for (const s of segments) {
    const ws = Array.isArray(s.words) ? s.words : [];
    // The narrowing IS the decision: a predicate signature rather than an
    // `every` plus a cast, so "every word of this segment is timed" is proved to
    // the type checker by the very filter the loop below reads.
    const timed = ws.filter(
      (w): w is { word: string; startSecond: number; endSecond?: number } =>
        !!w && typeof w.startSecond === "number",
    );
    if (ws.length && timed.length === ws.length) {
      // `word`, not `text` — and it arrives with its leading space, which the
      // joins downstream collapse like any other whitespace (T:8098-8100).
      for (const w of timed) units.push({ text: w.word || "", start: w.startSecond, end: w.endSecond });
    } else {
      units.push({ text: s.text, start: s.startSecond, end: s.endSecond });
    }
  }
  return units;
}

/** `startJob` (R:3305). A 200 with no jobId is an error, not a watch on
 *  `undefined` that never settles. The start POST is not aborted for the same
 *  reason it is not there: an abort that interrupted it could leave a job
 *  running whose id nobody received (R:3297-3304). */
async function startJob(
  post: typeof postJson,
  path: string,
  body: Record<string, unknown>,
): Promise<TranscribeStarted> {
  const started = await post<TranscribeStarted>(path, body);
  if (!started || typeof started.jobId !== "string" || !started.jobId) {
    throw fail(path + " replied with no jobId", "ai_error");
  }
  return started;
}

/** `watchJob(id).watch()` (R:4711-4746): poll `/api/jobs` until the row leaves
 *  "running", calling back on the way. Resolves NULL when the row is GONE — a
 *  finished record is dropped after its retention window (SPEC BG-6), which a
 *  backgrounded tab can sleep straight through, and polling forever for a row
 *  that is never coming back is a promise that never settles. */
async function watchJob(
  deps: TranscribeDeps,
  id: string,
  stopped: () => boolean,
): Promise<Job | null> {
  const list = deps.jobs || fetchJobs;
  const sleep = deps.sleep || ((ms: number) => new Promise<void>((r) => setTimeout(r, ms)));
  const every = Math.max(200, deps.intervalMs || 700);
  let seen = false;
  let missing = 0;
  for (;;) {
    if (stopped()) return null;
    const snapshot = await list().catch(() => null);
    const record = snapshot ? snapshot.jobs.find((j) => j.id === id) || null : null;
    if (record) {
      seen = true;
      missing = 0;
      if (deps.onProgress) deps.onProgress(record);
      if (record.state !== ("running" as JobState)) return record;
    } else if (seen && ++missing >= 5) {
      return null;
    }
    await sleep(every);
  }
}

/**
 * Start one transcription and watch it to the end.
 *
 * The ERROR TEXT is R's, verbatim, because a caller switching on `.type` and
 * showing `.message` is the whole contract:
 *   - `"the transcription was cancelled"`  (R:4489, type "cancelled")
 *   - `record.message || "the transcription failed"`  (R:4490, type "ai_error")
 *   - `"the transcription job is no longer being reported"`  (R:4463)
 *   - `"the transcript could not be read: " + cause`  (R:4437)
 *   - `"<path> replied with no jobId"`  (R:3310)
 */
export async function startTranscribe(
  opts: TranscribeOptions,
  deps: TranscribeDeps = {},
): Promise<TranscribeRun> {
  const post = deps.post || postJson;
  const readJson = deps.readJson || defaultReadJson;
  const cancelJob = deps.cancel || defaultCancel;

  const body: Record<string, unknown> = { path: opts.path };
  if (opts.words !== undefined) body.words = opts.words;
  if (opts.model !== undefined) body.model = opts.model;
  if (opts.language !== undefined) body.language = opts.language;
  if (opts.task !== undefined) body.task = opts.task;
  // The page's own path, so a RELATIVE `path` resolves beside it — the same
  // rule readFile/stat follow (RH-1, R:4225-4231).
  if (opts.base) body.base = opts.base;

  const started = await startJob(post, "/api/ai/transcribe", body);
  let stopped = false;

  // The transcript file is the RESULT; the row only said when to read it. Typed
  // on failure like every other rejection here — the reads can fail on their
  // own (a transcript deleted between the row going done and this fetch, a
  // truncated file that fails JSON.parse), and without this a caller switching
  // on `.type` got a bare SyntaxError (R:4386-4398).
  const readResult = async (): Promise<Transcript> => {
    try {
      const written = (await readJson(started.output)) as {
        text?: string;
        segments?: unknown[];
        language?: string;
        duration?: number;
      };
      const segments = (written.segments || []).map(frameSegment);
      return {
        text: written.text || "",
        segments,
        words: flattenWords(segments),
        language: written.language,
        durationInSeconds: written.duration,
      };
    } catch (cause) {
      throw fail("the transcript could not be read: " + errText(cause), "ai_error", {
        jobId: started.jobId,
      });
    }
  };

  const done = (async (): Promise<Transcript> => {
    const record = await watchJob(deps, started.jobId, () => stopped);
    if (stopped) {
      throw fail("the transcription was cancelled", "cancelled", { jobId: started.jobId });
    }
    if (!record) {
      // The row is gone. The TRANSCRIPT is the other witness and the one that
      // matters, so reading it is both the answer and the check: if it is
      // there, the work landed (R:4448-4460).
      try {
        return await readResult();
      } catch {
        throw fail("the transcription job is no longer being reported", "ai_error", {
          jobId: started.jobId,
          output: started.output,
          outputPartial: started.outputPartial,
        });
      }
    }
    if (record.state === "done") return readResult();
    throw fail(
      record.state === "cancelled"
        ? "the transcription was cancelled"
        : record.message || "the transcription failed",
      record.state === "cancelled" ? "cancelled" : "ai_error",
      {
        jobId: started.jobId,
        // Where the salvage is: a run that dies at minute 80 of 90 writes no
        // `.json` at all and its `.partial.jsonl` is left behind (R:4468-4478).
        output: started.output,
        outputPartial: started.outputPartial,
      },
    );
  })();

  return {
    jobId: started.jobId,
    done,
    cancel: async () => {
      stopped = true;
      await cancelJob(started.jobId);
    },
  };
}

/** The one-call form, which is what `ann/rec.ts` uses: the walkthrough has no
 *  ✕ of its own — Esc leaves the mode and the transcription goes on in the
 *  background (T:8291-8300, states table row `settling/transcribing | Esc`). */
export async function transcribe(
  opts: TranscribeOptions,
  deps: TranscribeDeps = {},
): Promise<Transcript> {
  const run = await startTranscribe(opts, deps);
  return run.done;
}

// ── deferred from R, deliberately ──────────────────────────────────────────
//
// * THE PROGRESSIVE TRANSCRIPT (R:4238-4470): `outputPartial` is a
//   `.partial.jsonl` the worker appends a segment at a time, tailed by BYTE
//   RANGE through `/api/fs/raw` so a caller can render words before the run
//   finishes. Roughly 200 lines of offset bookkeeping, and it exists for the
//   long-recording case (`onChunk`). A walkthrough is seconds of speech and
//   the template never asks for it, so `outputPartial` is CARRIED on the
//   rejection (a caller can still salvage the file) and never read. Port it
//   here, not into rec.ts, if a "words so far" caption ever lands.
// * `diarize` / `speakers` / `initialPrompt` / `vad` (R:4144's key list): the
//   route accepts them; a one-voice walkthrough has no use for any.
// * The APPLE tier (`_apple_transcribe`, ai_runtime.py:2254) needs nothing
//   here — the route picks it, and the reply shape is the same.
