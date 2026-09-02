// The Playground tab's wire layer: the slice of what `runtime.js` does for
// pages, rewritten for the shell (SPEC §40 — the wire shapes are AI-1/AI-1b's).
//
// It exists because the shell cannot ride `runtime.js` — that file is served to
// pages and carries page concerns (params bridge, export rules) the shell must
// not import — and `postJson` cannot stream. So this module holds the three
// things a playground needs that no shell code had: an NDJSON reader over
// `POST /api/ai`, the cold-start dance (a non-resident text model answers 409
// with the job id of the load this call just started — watch it, retry once),
// and the job poll that image and transcription runs are built on.
//
// The wire names are the PAGE's, camelCase (`systemPrompt`, `topP`,
// `maxTokens`) since D633 — the same names `runtime.js` exposes, so nothing
// is its business, not a second contract to copy here.
import { postJson, rawUrl } from "@platform/lib/api";
import { fetchJobs, type Job } from "@platform/lib/jobs";

export interface ChatTurn {
  role: "user" | "assistant";
  content: string;
}

/** Sampling settings on the server's own names and clamps (`_SAMPLING`,
 *  server/ai.py). Undefined means "leave it to the model's default" and the
 *  key is not sent at all. */
export interface ChatSettings {
  temperature?: number;
  topP?: number;
  maxTokens?: number;
  systemPrompt?: string;
}

/** The AI SDK's token counts (D632) — `totalTokens` is the sum the server
 *  already did. */
export interface ChatUsage {
  inputTokens: number | null;
  outputTokens: number | null;
  totalTokens: number | null;
}

/** `/api/ai`'s result: the one frame every fused.ai verb resolves with
 *  (RH-11, D632) plus `text`. `response.modelId` is the id that ran; the
 *  local tier's wall-clock lands under `providerMetadata.local.seconds`. */
export interface ChatResult {
  text: string;
  provider: "local" | "claude";
  finishReason: "stop" | "length" | "cancelled";
  warnings: { type: string; setting?: string; message: string }[];
  usage: ChatUsage | null;
  response: { id: string | null; modelId: string; timestamp: string };
  providerMetadata: Record<string, { seconds?: number | null } & Record<string, unknown>>;
}

/** The 409 a text generation answers when its model is not resident (AI-5):
 *  not a failure — the load has STARTED, and `jobId` is the row to watch. */
export class ModelLoading extends Error {
  jobId: string | null;
  constructor(message: string, jobId: string | null) {
    super(message);
    this.jobId = jobId;
  }
}

function frameError(error: unknown): Error {
  const e = error as { message?: string; type?: string } | null;
  return new Error(e?.message || (typeof error === "string" ? error : "generation failed"));
}

/** One streamed completion from `POST /api/ai`. Resolves with the terminal
 *  frame's result — the chunks are a view of the completion, never the only
 *  copy of it (AI-1b). Throws `ModelLoading` on the cold-start 409; the caller
 *  owns the watch-and-retry, because only it knows what to show meanwhile. */
export async function streamChat(opts: {
  model: string;
  prompt: string;
  history: ChatTurn[];
  settings: ChatSettings;
  signal: AbortSignal;
  onChunk: (text: string) => void;
  /** Absolute paths to base images for a vision-language model, on THIS turn
   *  only (AI-11j) — optional, so a caller that never attaches anything (the
   *  ordinary case) changes nothing about its own request. Omitted from the
   *  body entirely when absent/empty rather than sent as `images: []`,
   *  matching the worker's own "absent = today's text path, unchanged"
   *  contract (mlx_text/worker.py). A LIST, like the wire shape it forwards
   *  to — server/ai.py's own comment says why a list rather than one path. */
  images?: string[];
}): Promise<ChatResult> {
  const body: Record<string, unknown> = {
    prompt: opts.prompt,
    model: opts.model,
    stream: true,
    ...(opts.history.length ? { history: opts.history } : {}),
    ...(opts.images && opts.images.length ? { images: opts.images } : {}),
  };
  for (const [key, value] of Object.entries(opts.settings)) {
    if (value !== undefined && value !== null && value !== "") body[key] = value;
  }
  const res = await fetch("/api/ai", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Fused": "1" },
    body: JSON.stringify(body),
    signal: opts.signal,
  });
  if (!res.ok) {
    // Validation and the cold start both answer BEFORE any stream exists, so
    // the body here is one JSON object, not NDJSON.
    const data = (await res.json().catch(() => null)) as {
      error?: { type?: string; message?: string; jobId?: string };
    } | null;
    const error = data?.error;
    if (res.status === 409 && error?.type === "model_loading") {
      throw new ModelLoading(error.message || "model is loading", error.jobId ?? null);
    }
    throw new Error(error?.message || `generation failed (${res.status})`);
  }
  if (!res.body) throw new Error("no response body");
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let result: ChatResult | null = null;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let newline;
    while ((newline = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, newline).trim();
      buffer = buffer.slice(newline + 1);
      if (!line) continue;
      const frame = JSON.parse(line) as {
        type: string;
        text?: string;
        ok?: boolean;
        result?: ChatResult;
        error?: unknown;
      };
      if (frame.type === "chunk") opts.onChunk(frame.text || "");
      else if (frame.type === "done") {
        // Errors after the first byte are demoted to an ok:false done frame on
        // a 200 (AI-1b) — this is the one place they surface.
        if (!frame.ok) throw frameError(frame.error);
        result = frame.result ?? null;
      }
    }
  }
  if (!result) throw new Error("the stream ended without a result frame");
  return result;
}

/** Stop the generation in flight without unloading (AI-1a). False from the
 *  server means there was nothing to stop, which is not an error. */
export function cancelGeneration(capability?: string): Promise<{ cancelled: boolean }> {
  return postJson<{ cancelled: boolean }>("/api/ai/cancel", capability ? { capability } : {});
}

/** How many polls in a row may fail before the watch gives up. A blip while
 *  the server is busy is ordinary and the next tick asks again; ten seconds of
 *  silence is an outage, and a watch that polls a dead server forever is a
 *  spinner nobody can dismiss. The throw lands in each caller's own catch. */
const MAX_POLL_FAILURES = 10;

/** How many CONSECUTIVE polls may fail to find the row before the watch calls
 *  it `gone`, rather than firing on the very first miss. `FINISHED_TTL_S`
 *  (`fused_render/jobs.py`) is a few seconds against this watch's 1s poll, so
 *  a single slow tick, a re-render stall, or a background-tab timer throttle
 *  can miss the row's whole window with margin to spare — and `gone` is read
 *  as success by every caller (`ImageStage`, `TranscribeStage`, `TextStage`,
 *  `EmbedStage` all treat it as "done, no artefact to distrust"), so firing on
 *  the first miss risks rendering a path nothing was ever written to. Matches
 *  `runtime.js`'s own `watchJob`, which has tolerated 5 for the same reason
 *  since before this one existed. */
const GONE_MISS_TOLERANCE = 5;

/** Why a watch ended. Three outcomes, not two, because the callers genuinely
 *  need to tell them apart and a `Job | null` cannot say it:
 *
 *  - `done`      — the row reached a terminal non-error state. The artefact,
 *                  if the job makes one, is on disk.
 *  - `cancelled` — somebody stopped it. NO artefact was written, so a caller
 *                  must not render an output path or claim a saved file.
 *  - `gone`      — the row was missing from `GONE_MISS_TOLERANCE` consecutive
 *                  polls in a row, not just one. `FINISHED_TTL_S` is a few
 *                  seconds against a 1s poll, so a single missed poll is not
 *                  enough evidence the manager retired the row rather than us
 *                  just being slow to read it once; treat `gone` as `done`
 *                  unless the caller can check the artefact itself.
 *
 *  A FAILED poll is none of these and never ends the watch — that conflation
 *  is what made a single flaky `/api/jobs` read resolve as success. Rejects on
 *  an error state (with the row's own message), on abort, and after
 *  `MAX_POLL_FAILURES` consecutive failures. */
export type WatchOutcome =
  | { state: "done"; job: Job }
  | { state: "cancelled"; job: Job }
  | { state: "gone" };

export async function watchJob(
  jobId: string,
  signal: AbortSignal,
  onTick?: (job: Job) => void,
): Promise<WatchOutcome> {
  let failures = 0;
  let missingPolls = 0;
  for (;;) {
    if (signal.aborted) throw new DOMException("aborted", "AbortError");
    let row: Job | undefined;
    try {
      const snapshot = await fetchJobs(signal);
      failures = 0;
      row = snapshot.jobs.find((j) => j.id === jobId);
      if (row && onTick) onTick(row);
      if (!row) {
        // One miss is not evidence the row is gone — see GONE_MISS_TOLERANCE.
        if (++missingPolls >= GONE_MISS_TOLERANCE) return { state: "gone" };
      } else {
        missingPolls = 0;
        if (row.state !== "running") {
          if (row.state === "error") throw new Error(row.message || "the job failed");
          return { state: row.state === "cancelled" ? "cancelled" : "done", job: row };
        }
      }
    } catch (e) {
      if ((e as Error).name === "AbortError") throw e;
      // A terminal row throws its own message out of the try — that is the
      // job failing, not the poll, and it must not be retried.
      if (row) throw e;
      if (++failures >= MAX_POLL_FAILURES) {
        throw new Error("lost contact with the job list while waiting for this to finish");
      }
      // One failed poll is not news; the next tick asks again.
    }
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
}

/** How many cold-start 409s one call may wait out before giving up. More than
 *  one, because a load finishing is NOT the same as this call's model being
 *  resident when it asks again: ONE model per capability is resident at a time
 *  (AI-13), so any other surface asking for a different one — a second tab, the
 *  Models page, an app calling `fused.ai` — evicts ours between the job row
 *  going `done` and the retry landing, and the retry earns a fresh 409. The
 *  single retry this replaces surfaced that as "<model> is still loading
 *  (loading)", which reads as a broken run when nothing was broken: the answer
 *  was to ask again. Bounded because two surfaces asking for two models can
 *  trade the slot forever, and a spinner that never resolves is worse than a
 *  sentence saying what is happening. */
const MAX_LOAD_WAITS = 4;

/** Sleep, but abortable — a Stop pressed during the wait must land on the same
 *  AbortError every other step of the dance throws. */
function pause(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) return reject(new DOMException("aborted", "AbortError"));
    const onAbort = () => {
      clearTimeout(timer);
      reject(new DOMException("aborted", "AbortError"));
    };
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

/** Run `attempt`, waiting out AI-5's cold start: the first call STARTS the load
 *  and 409s with the job id, so this watches that job and asks again.
 *
 *  Lives here rather than in a stage because both stages that generate against
 *  a resident model do the identical dance, and the copy in each of them drifted
 *  the moment one of them learned something (this bound is that something). The
 *  IMAGE path needs none of it: `generate_image` waits for the model inside the
 *  render job server-side (`_wait_ready`), which is why the image stage never
 *  saw the failure this fixes — a text box may not hang for a multi-GB load, so
 *  the text and embedding paths fail fast and the waiting happens HERE.
 *
 *  `onStatus` is told what to narrate and handed null once there is nothing to
 *  say. A load stopped from the Activity panel ends the call rather than being
 *  retried into a second 409. */
export async function withModelReady<T>(
  attempt: () => Promise<T>,
  opts: { signal: AbortSignal; downloaded: boolean; onStatus: (text: string | null) => void },
): Promise<T> {
  for (let waited = 0; ; waited++) {
    try {
      return await attempt();
    } catch (e) {
      if (!(e instanceof ModelLoading)) throw e;
      if (waited >= MAX_LOAD_WAITS) {
        throw new Error(
          `${e.message} — and it keeps losing its place before a run can start. ` +
            `One model at a time is resident, so another page asking for a ` +
            `different one takes the slot. Try again.`,
        );
      }
      opts.onStatus(
        waited > 0
          ? "Loading the model again — something else took its place…"
          : opts.downloaded
            ? "Loading the model into memory — the first run pays for this once…"
            : "Downloading the model — the first run pays for this once…",
      );
      if (e.jobId) {
        const outcome = await watchJob(e.jobId, opts.signal, (job) =>
          opts.onStatus(job.detail || "Loading the model…"),
        );
        // Someone stopped the load from the Activity panel. Asking again would
        // just earn a second 409 and read as a stream error, so say what
        // actually happened.
        if (outcome.state === "cancelled") throw new Error("the model load was cancelled");
        // No row to watch: retired, or never seen. Nothing to poll, so pace the
        // retry rather than hammering the route with it.
        if (outcome.state === "gone") await pause(1000, opts.signal);
      } else {
        await pause(1000, opts.signal);
      }
      opts.onStatus(null);
    }
  }
}

// -- Images (POST /api/ai/image, AI-9) ----------------------------------------

/** What the route accepts — `_reject_unknown` refuses any other key, so this
 *  interface is closed on purpose. */
export interface ImageRequest {
  prompt: string;
  model?: string;
  /** An absolute path to a base image to EDIT instead of rendering from the
   *  prompt alone (AI-9f). Only the mflux engine honours it, and only for a
   *  model with an edit variant — `AiCatalogModel.acceptsImage` is the server's
   *  own answer to whether this model is one of them, and sending it for one
   *  that is not is a 400. Absolute, so no `base` is needed: the shell is not a
   *  page and has no `?path=` to resolve against. */
  image?: string;
  width?: number;
  height?: number;
  steps?: number;
  guidance?: number;
  seed?: number;
}

/** The reply echoes the SETTLED request — width snapped, steps clamped, seed
 *  invented — which is what a caller must render, never what it asked for. */
export interface ImageStarted {
  jobId: string;
  path: string;
  previewPath: string;
  model: string;
  prompt: string;
  width: number;
  height: number;
  steps: number;
  guidance: number;
  seed: number;
}

export function startImage(request: ImageRequest): Promise<ImageStarted> {
  return postJson<ImageStarted>("/api/ai/image", request);
}

// -- Video (POST /api/ai/video, SPEC §40) --------------------------------------

/** `ImageRequest`'s twin, minus `guidance` (the engine is CFG-distilled and
 *  takes no such parameter) and plus `frames`. Closed the same way, for the
 *  same reason: `_reject_unknown` refuses any other key. `image`: one
 *  reference image, conditioning at frame 0 with strength 1.0 — the same
 *  single-image scope `ImageRequest.image` already uses for an edit
 *  (SPEC AI-15, restating AI-9f). */
export interface VideoRequest {
  prompt: string;
  model?: string;
  width?: number;
  height?: number;
  frames?: number;
  steps?: number;
  seed?: number;
  /** An absolute path to a reference image to condition on (frame 0,
   *  strength 1.0 — AI-15). `AiCatalogCapability.videoTraits.supportsImage`
   *  is the server's own answer to whether the resolved engine honours it.
   *  Absolute, same reason `ImageRequest.image`'s own comment gives: the
   *  shell is not a page and has no `?path=` to resolve a relative one
   *  against. */
  image?: string;
}

/** The reply echoes the SETTLED request — width/height snapped and shrunk to
 *  fit the canvas ceiling (derived from `image`'s own aspect ratio when one
 *  was sent), `frames` rounded to the engine's own grid, `steps` clamped,
 *  `seed` invented — never what was asked. No `previewPath`: there is no
 *  live preview in this build. `image` is present only when the request
 *  carried one, echoing the resolved absolute path. */
export interface VideoStarted {
  jobId: string;
  path: string;
  model: string;
  prompt: string;
  width: number;
  height: number;
  frames: number;
  steps: number;
  seed: number;
  image?: string;
}

export function startVideo(request: VideoRequest): Promise<VideoStarted> {
  return postJson<VideoStarted>("/api/ai/video", request);
}

// -- Embeddings (POST /api/ai/embed, SPEC §40) ---------------------------------

/** `/api/ai/embed`'s result — the same frame as every other verb (D632)
 *  with `embeddings` as the payload and the inputs echoed as `values`;
 *  `dim` sits under `providerMetadata.local`. */
export interface EmbedResult {
  embeddings: number[][];
  values: string[];
  provider: "local" | "claude";
  finishReason: string;
  warnings: { type: string; setting?: string; message: string }[];
  usage: null;
  response: { id: string | null; modelId: string; timestamp: string };
  providerMetadata: Record<string, { dim?: number; kind?: string }>;
}

/** One batch of texts into the model's vector space. Vectors come back
 *  unit-length (embed_common.unit_normalize, applied by both workers), so a
 *  dot product between two of them IS their cosine similarity. The reply is
 *  wrapped (`{ok, result}`) unlike image/transcribe, and a cold model answers
 *  the same model_loading 409 the chat route does — thrown as `ModelLoading`
 *  for the caller's own watch-and-retry.
 *
 *  `kind` picks which half of a retrieval model's prompt pair goes in front of
 *  these texts — `"query"` for the thing being searched WITH, `"document"` for
 *  the things being searched THROUGH. **Sent only when the caller has one**, and
 *  that is not tidiness: the route 400s a `kind` on a model with no retrieval
 *  convention (every dual encoder), because a parameter that changed nothing
 *  would be worse than one that is refused. `AiCatalogModel.promptScheme` is the
 *  server's own answer to whether this model has one. */
export async function embedTexts(
  model: string,
  texts: string[],
  kind?: "query" | "document",
): Promise<EmbedResult> {
  const res = await fetch("/api/ai/embed", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Fused": "1" },
    body: JSON.stringify(kind ? { model, texts, kind } : { model, texts }),
  });
  return readEmbedReply(res);
}

/** `/api/ai/embed`'s reply, for both halves of the call.
 *
 *  One reader rather than a copy per entry point: the 409-means-LOADING fork is
 *  the part that matters (a cold model is not a failed request, and dropping the
 *  job id loses the download the caller would have shown), and two copies of it
 *  would be two places for a `texts` call to keep watching and a `paths` call to
 *  stop. */
async function readEmbedReply(res: Response): Promise<EmbedResult> {
  const data = (await res.json().catch(() => null)) as {
    ok?: boolean;
    result?: EmbedResult;
    error?: { type?: string; message?: string; jobId?: string };
  } | null;
  if (!res.ok || !data?.ok) {
    const error = data?.error;
    if (res.status === 409 && error?.type === "model_loading") {
      throw new ModelLoading(error.message || "model is loading", error.jobId ?? null);
    }
    throw new Error(error?.message || `embedding failed (${res.status})`);
  }
  if (!data.result) throw new Error("the reply carried no result");
  return data.result;
}

/** One batch of image PATHS into the same vector space — the other half of a
 *  dual encoder, and the reason a typed phrase can rank photographs at all.
 *
 *  **Absolute paths, and no `base`.** The route resolves a relative path against
 *  the calling PAGE's own file (RH-1); the shell is not a page and has no
 *  `?path=` to resolve against, so it sends what the OS file dialog handed back.
 *
 *  Refused with a 400 naming the model when that model has no vision tower —
 *  `AiCatalogModel.acceptsPaths` is the server's own answer to whether it does,
 *  and a caller drawing an affordance off anything else is drawing one whose
 *  request then fails. Shares `embedTexts`' reply handling, including the
 *  `ModelLoading` fork, by calling through the same reader below. */
export async function embedPaths(model: string, paths: string[]): Promise<EmbedResult> {
  const res = await fetch("/api/ai/embed", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Fused": "1" },
    body: JSON.stringify({ model, paths }),
  });
  return readEmbedReply(res);
}

// -- Transcription (POST /api/ai/transcribe, AI-10) ---------------------------

export interface TranscribeRequest {
  path: string;
  model?: string;
  task?: "transcribe" | "translate";
  language?: string;
  vad?: boolean;
  words?: boolean;
}

export interface TranscribeStarted {
  jobId: string;
  path: string;
  output: string;
  outputText: string;
  outputPartial: string;
  model: string;
  task: string;
}

export function startTranscribe(request: TranscribeRequest): Promise<TranscribeStarted> {
  return postJson<TranscribeStarted>("/api/ai/transcribe", request);
}

export interface TranscriptSegment {
  start?: number;
  end?: number;
  text?: string;
  speaker?: string;
  words?: { word: string; start: number; end: number }[];
}

/** The progressive transcript: whole JSON lines, appended and flushed per
 *  segment (runners/partial.py). Re-fetched whole on each poll — the file is
 *  segments of text, not audio, and a Range dance would buy nothing. A file
 *  that is not there yet (the worker has not decoded a segment) is an empty
 *  list, not an error. */
export async function readPartialTranscript(path: string): Promise<TranscriptSegment[]> {
  const res = await fetch(rawUrl(path) + "&t=" + Date.now());
  if (!res.ok) return [];
  const text = await res.text();
  const segments: TranscriptSegment[] = [];
  for (const line of text.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    try {
      segments.push(JSON.parse(trimmed) as TranscriptSegment);
    } catch {
      // A torn final line mid-append; the next poll sees it whole.
    }
  }
  return segments;
}

/** Land one recorded/picked file on disk so `/api/ai/transcribe` — which takes
 *  a PATH, not an upload — has something to point at. Multipart, `X-Fused`
 *  like every other write. The caller mkdir'd the parent (the upload route
 *  does not, by the same rule `_fs_write` follows). */
export async function uploadFile(path: string, data: Blob, name: string): Promise<void> {
  const form = new FormData();
  form.append("file", data, name);
  form.append("path", path);
  const res = await fetch("/api/fs/upload", {
    method: "POST",
    headers: { "X-Fused": "1" },
    body: form,
  });
  const payload = (await res.json().catch(() => null)) as { error?: string } | null;
  if (!res.ok) throw new Error(payload?.error || `upload failed (${res.status})`);
}
