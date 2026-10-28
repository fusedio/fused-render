/**
 * Type declarations for `window.fused`, the runtime fused-render injects into
 * every rendered HTML page (fused_render/static/runtime.js).
 *
 * A running fused-render serves this file at `/static/fused.d.ts`. Copy it
 * next to your sources, or add its path to your tsconfig `include`, and
 * `fused.*` is typed in any editor with a TypeScript language service — plain
 * JavaScript files included. Nothing here needs a build step: the runtime is
 * injected, never imported.
 *
 * The header of runtime.js is the documented contract; this file restates it
 * as types and declares no member that file does not export. Option bags are
 * typed as the runtime reads them. A tier that lacks an option drops it with a
 * `warnings[]` entry on the result rather than failing the call, so an
 * option being typed does not mean every provider honours it.
 *
 * Members marked LOCAL ONLY are absent or rejecting on a hosted/exported page
 * (`fused.env === "hosted"`); branch on `fused.env` before using them there.
 */

/** Where this page is running: the local app, or an exported bundle. */
type FusedEnv = "local" | "hosted";

/** Which kind of client the runtime is driving. Never sniff the user agent. */
type FusedDevice = "desktop" | "phone-web" | "ios-app";

/**
 * Errors the runtime rejects with. `type` is the field to branch on
 * (`"conflict"`, `"readonly"`, `"exists"`, `"cancelled"`, `"unavailable"`,
 * `"bad_request"`, `"model_loading"`, …); a Python failure also carries the
 * traceback and the script's stdout.
 */
interface FusedError extends Error {
  type?: string;
  traceback?: string;
  stdout?: string;
  /** HTTP status, on fused.tasks / fused.bots rejections. */
  status?: number;
  /** The server's own sentence, on fused.tasks / fused.bots rejections. */
  error?: string;
  /** The file's current mtime on a `"conflict"` (null when it was deleted). */
  mtime?: number | null;
  /** The load this call started, on `"model_loading"`. Watch it and retry. */
  jobId?: string;
  /** Why a bot refused a send: busy | headed | control. */
  reason?: string;
}

interface FusedTemplateRef {
  mode: string;
  path: string | null;
  icon: string | null;
}

/** The shape of `/api/fs/stat`, also what writeFile / uploadFile / mkdir resolve with. */
interface FusedStat {
  path: string;
  name: string;
  is_dir: boolean;
  size: number | null;
  mtime: number | null;
  writable: boolean;
  remote: boolean;
  templates: FusedTemplateRef[];
  template_error?: string;
}

interface FusedRunPythonOptions {
  /**
   * Stale-request channel. By default a new call for the same .py aborts the
   * prior in-flight one (its promise never settles). A string regroups the
   * channel; `null` opts out and runs fully concurrent.
   */
  key?: string | null;
  /** A caller-owned AbortSignal that composes with the channel's own. */
  signal?: AbortSignal;
}

interface FusedParamSetOptions {
  history?: "replace";
  /** Setting the key to this value removes it from the URL instead. */
  default?: string;
}

/** URL-synced view state. Values are strings; `set` with a non-string throws. */
interface FusedParams {
  get(key: string): string | undefined;
  getAll(): Record<string, string>;
  /** `value === null` removes the key. */
  set(key: string, value: string | null, opts?: FusedParamSetOptions): void;
  /** Fires on any change to the page's params, including the shell's own writes. Returns unsubscribe. */
  onChange(cb: () => void): () => void;
}

interface FusedWriteFileOptions {
  /** Optimistic lock: rejects `type: "conflict"` when the file changed or vanished. */
  expectedMtime?: number | null;
  /** Create only if absent; an existing path rejects `type: "exists"`. */
  create?: boolean;
}

interface FusedFileChange {
  path: string;
  /** The watcher's signature; null for a deletion. */
  mtime: number | null;
}

// ---------------------------------------------------------------- jobs

/** One row of the download manager (`/api/jobs`). Fields beyond `id` are the reporter's. */
interface FusedJob {
  id: string;
  title?: string;
  state?: string;
  done?: number;
  total?: number;
  cancelRequested?: boolean;
  [key: string]: unknown;
}

interface FusedTrackJobSpec {
  id?: string;
  title?: string;
  done?: number;
  total?: number;
  [key: string]: unknown;
}

/** Write side of the download manager, for work THIS PAGE runs. Nothing here ever rejects. */
interface FusedTrackJobHandle {
  update(fields: Partial<FusedJob>): Promise<FusedJob | null>;
  finish(detail?: Partial<FusedJob> | string): Promise<FusedJob | null>;
  fail(message?: string | Partial<FusedJob>): Promise<FusedJob | null>;
  /** Report that this page honoured a cancel request. */
  cancelled(): Promise<FusedJob | null>;
  /** True once the user pressed ✕ on the row; the page is expected to stop. */
  readonly cancelRequested: boolean;
}

/** Read side of the download manager, for work the SERVER runs (model loads, generations). */
interface FusedWatchJobHandle {
  get(): Promise<FusedJob | null>;
  watch(cb: (job: FusedJob | null) => void, everyMs?: number): () => void;
  stop(): void;
  /** A real cancel: the server owns the process and stops it. */
  cancel(): Promise<boolean>;
}

// ---------------------------------------------------------------- fused.ai

type FusedAiProvider = "local" | "apple" | "claude" | "copilot" | "codex" | "gemini";

interface FusedAiWarning {
  type: string;
  setting?: string;
  message?: string;
  [key: string]: unknown;
}

/** The one frame every fused.ai verb resolves with, plus the verb's own payload. */
interface FusedAiFrame {
  /** The tier that answered. */
  provider: string;
  finishReason: "stop" | "length" | "cancelled" | "content-filter" | string | null;
  warnings: FusedAiWarning[];
  usage: Record<string, number> | null;
  response: { id: string | null; modelId: string | null; timestamp: string | null };
  /** What the server snapped, clamped or invented, under the provider's key. */
  providerMetadata: Record<string, Record<string, unknown>>;
}

interface FusedAiMessage {
  role: "user" | "assistant";
  content: string;
}

interface FusedAiCommonOptions {
  /** A Hugging Face repo id or .gguf filename is weights on this machine; anything else is a Claude alias. */
  model?: string;
  provider?: FusedAiProvider;
  abortSignal?: AbortSignal;
}

interface FusedAiTextOptions extends FusedAiCommonOptions {
  prompt: string;
  systemPrompt?: string;
  /** Prior turns, for a caller holding a conversation. Local models only. */
  history?: FusedAiMessage[];
  /** Send the prompt verbatim with no chat template. Local models only. */
  raw?: boolean;
  /** Paths to pictures for THIS turn (page-relative or absolute). */
  images?: string[];
  temperature?: number;
  maxTokens?: number;
  topP?: number;
  /** Local text models only; default on. */
  thinking?: boolean;
  effort?: "low" | "medium" | "high" | "xhigh";
  /** Streaming: each chunk of text as it decodes (the whole text once, on tiers that do not stream). */
  onChunk?: (text: string) => void;
}

interface FusedAiTextResult extends FusedAiFrame {
  text: string;
}

interface FusedAiMediaFile {
  path: string;
  /** A ready-made /api/fs/raw URL for an <img>, <video> or <audio> src. */
  url: string;
  mediaType: string;
}

interface FusedAiImageOptions extends FusedAiCommonOptions {
  prompt: string;
  width?: number;
  height?: number;
  steps?: number;
  guidance?: number;
  /** Omitted: invented server-side and returned, so a render is repeatable. */
  seed?: number;
  /** One reference image (page-relative or absolute) to edit from. */
  image?: string;
  /** Per denoising step, with the download-manager record (`previewUrl` while it lasts). */
  onProgress?: (job: FusedJob & { previewUrl?: string | null }) => void;
}

interface FusedAiImageResult extends FusedAiFrame {
  images: FusedAiMediaFile[];
}

interface FusedAiVideoOptions extends FusedAiCommonOptions {
  prompt: string;
  width?: number;
  height?: number;
  /** Snapped server-side to the engine's valid grid. */
  frames?: number;
  steps?: number;
  seed?: number;
  /** One reference image to condition frame 0 on. */
  image?: string;
  onProgress?: (job: FusedJob) => void;
}

interface FusedAiVideoResult extends FusedAiFrame {
  videos: FusedAiMediaFile[];
}

interface FusedAiWord {
  start: number;
  end: number;
  /** Keeps its leading space; a segment's text is its words concatenated. */
  word: string;
}

interface FusedAiSegment {
  text: string;
  startSecond: number;
  endSecond: number;
  /** "Speaker 1", … when diarizing; null where nobody could be heard. */
  speaker?: string | null;
  /** Only with `words: true`, and only on engines that time words. Write `segment.words || []`. */
  words?: FusedAiWord[];
}

interface FusedAiTranscribeOptions extends FusedAiCommonOptions {
  /** An audio or video file on THIS machine (page-relative or absolute). Nothing is uploaded. */
  path: string;
  /** Omitted: auto-detect (the system locale on the apple tier). */
  language?: string;
  task?: "transcribe" | "translate";
  /** Label who said each segment. */
  diarize?: boolean;
  /** How many people are in the recording; estimated when omitted. */
  speakers?: number;
  /** Time each word inside a segment. */
  words?: boolean;
  /** Silero voice-activity detection; default true. */
  vad?: boolean;
  initialPrompt?: string;
  /** `done`/`total` are seconds of audio. */
  onProgress?: (job: FusedJob) => void;
  /** Each segment as it decodes, in order, exactly once. */
  onChunk?: (segment: FusedAiSegment) => void;
}

interface FusedAiTranscribeResult extends FusedAiFrame {
  text: string;
  segments: FusedAiSegment[];
  language: string | null;
  durationInSeconds: number | null;
  /** The speaker labels that landed on a segment, when diarizing. */
  speakers?: string[];
  estimatedSpeakers?: number;
}

interface FusedAiEmbedOptions extends FusedAiCommonOptions {
  /** Exactly one of `texts` or `paths`, up to 64 items. */
  texts?: string[];
  /** Image files on this machine (page-relative or absolute). */
  paths?: string[];
}

interface FusedAiEmbedResult extends FusedAiFrame {
  /** Unit-normalized, so cosine similarity is a plain dot product. */
  embeddings: number[][];
  values?: unknown[];
}

type FusedAiDecideQuestion =
  | { type: "choice"; instructions?: string; criteria: string[] | Record<string, string> }
  | { type: "score"; instructions?: string; criteria: string[] }
  | { type: "noul"; instructions?: string };

interface FusedAiDecideAnswer {
  type: "choice" | "score" | "noul";
  confidence: number;
  action: { actProbability: number };
  choice?: string;
  score?: number;
  legend?: string[];
  probabilities?: number[] | Record<string, number>;
  noul?: number;
}

interface FusedAiDecideOptions extends FusedAiCommonOptions {
  state: string | Record<string, unknown> | FusedAiMessage[];
  questions: Record<string, FusedAiDecideQuestion>;
}

interface FusedAiDecideResult extends FusedAiFrame {
  answers: Record<string, FusedAiDecideAnswer>;
}

interface FusedAiVisionOptions extends FusedAiCommonOptions {
  /** One image (page-relative or absolute). */
  image: string;
  task: "caption" | "query" | "detect" | "point";
  prompt?: string;
  length?: "short" | "normal" | "long" | string;
  reasoning?: boolean;
  maxObjects?: number;
}

interface FusedAiVisionResult extends FusedAiFrame {
  task: "caption" | "query" | "detect" | "point";
  imageSize: { width: number; height: number } | [number, number];
  text?: string;
  reasoning?: string;
  /** Coordinates 0–1. */
  objects?: { xMin: number; yMin: number; xMax: number; yMax: number; label: string; score: number }[];
  points?: { x: number; y: number; label: string }[];
}

interface FusedAiSpeechOptions extends FusedAiCommonOptions {
  text: string;
  voice?: string;
  instruct?: string;
  /** A reference recording to clone the voice from, and its transcript. */
  refAudio?: string;
  refText?: string;
  language?: string;
  onProgress?: (job: FusedJob) => void;
}

interface FusedAiSpeechResult extends FusedAiFrame {
  audio: FusedAiMediaFile[];
}

type FusedAiCapability =
  | "text-generation"
  | "text-to-image"
  | "automatic-speech-recognition"
  | "text-to-video"
  | string;

interface FusedAiModelLoadOptions {
  /** Which runner gets the repo. Pass it whenever the page knows. */
  capability?: FusedAiCapability;
  [key: string]: unknown;
}

/** Local inference: what this machine holds in memory, what it could, and the jobs that move weights. */
interface FusedAiModels {
  /** The resident models and what they cost (`/api/ai/runtime`). */
  list(): Promise<Record<string, unknown>>;
  /** The suggested models per capability (`/api/ai/catalog`). */
  catalog(): Promise<Record<string, unknown>>;
  /** Starts a load (and the download it needs); watch the returned job with fused.watchJob. */
  load(model: string, opts?: FusedAiModelLoadOptions): Promise<{ jobId: string } & Record<string, unknown>>;
  download(model: string, opts?: FusedAiModelLoadOptions): Promise<{ jobId: string } & Record<string, unknown>>;
  /** A model id, or `{capability}` to release whatever is resident for that capability. */
  unload(model?: string | null | { capability: FusedAiCapability }): Promise<Record<string, unknown>>;
}

/** Ask a model. Local models, the OS's on-device model, or an agent CLI on the user's own plan. */
interface FusedAi {
  text(opts: FusedAiTextOptions): Promise<FusedAiTextResult>;
  image(opts: FusedAiImageOptions): Promise<FusedAiImageResult>;
  /** Apple Silicon only. */
  video(opts: FusedAiVideoOptions): Promise<FusedAiVideoResult>;
  transcribe(opts: FusedAiTranscribeOptions): Promise<FusedAiTranscribeResult>;
  embed(opts: FusedAiEmbedOptions): Promise<FusedAiEmbedResult>;
  /** Typed decisions from a small local model. Apple Silicon only. */
  decide(opts: FusedAiDecideOptions): Promise<FusedAiDecideResult>;
  vision(opts: FusedAiVisionOptions): Promise<FusedAiVisionResult>;
  /** Text to speech. Apple Silicon only. */
  speech(opts: FusedAiSpeechOptions): Promise<FusedAiSpeechResult>;
  models: FusedAiModels;
  /** Stop the generation in flight on a local model, keeping it loaded. False when there was nothing to stop. */
  cancel(capability?: FusedAiCapability): Promise<boolean>;
}

// ---------------------------------------------------------------- fused.capture

interface FusedCaptureFile {
  path: string;
  url: string;
  mime: string;
  seconds?: number;
  bytes?: number;
  warning?: string;
}

/** A recording in progress. Resolves when the recording is RUNNING; `stop()` gives the file. */
interface FusedCaptureHandle {
  id: string;
  path: string;
  url: string;
  mode: string;
  jobId: string;
  state: string;
  stop(): Promise<FusedCaptureFile>;
  /** Stops AND deletes the file. */
  cancel(): Promise<unknown>;
}

interface FusedCaptureScreenOptions {
  display?: number | string;
  rect?: { x: number; y: number; width: number; height: number } | number[];
  audio?: false | "mic" | "system" | "both";
  device?: string;
  cursor?: boolean;
  path?: string;
  /** Default 30 minutes; hitting it is a stop, not a cancel. */
  maxSeconds?: number;
  title?: string;
}

interface FusedCaptureAudioOptions {
  source?: "mic" | "system" | "both";
  device?: string;
  path?: string;
  maxSeconds?: number;
  title?: string;
}

interface FusedCaptureScreenshotOptions {
  display?: number | string;
  rect?: { x: number; y: number; width: number; height: number } | number[];
  cursor?: boolean;
  /** The extension picks png or jpeg. */
  path?: string;
}

interface FusedCaptureAvailability {
  available: boolean;
  granted: boolean;
  reason?: string | null;
}

interface FusedCaptureSources {
  video: FusedCaptureAvailability;
  audio: FusedCaptureAvailability;
  systemAudio: FusedCaptureAvailability;
  screenshot: FusedCaptureAvailability;
  displays: unknown[];
  microphones: unknown[];
}

/** Native screen / microphone / still capture. LOCAL ONLY. */
interface FusedCapture {
  screen(opts?: FusedCaptureScreenOptions): Promise<FusedCaptureHandle>;
  audio(opts?: FusedCaptureAudioOptions): Promise<FusedCaptureHandle>;
  screenshot(opts?: FusedCaptureScreenshotOptions): Promise<FusedCaptureFile & { width: number; height: number }>;
  /** What this machine can capture. Never prompts. */
  sources(): Promise<FusedCaptureSources>;
  /** Live recordings on this machine, including another page's. */
  list(): Promise<Record<string, unknown>[]>;
  attach(id: string): Promise<FusedCaptureHandle>;
}

// ---------------------------------------------------------------- fused.terminal / fileIndex / daemon

/** The shell's terminal drawer. Rejects where there is no shell (standalone, embedded, hosted). */
interface FusedTerminal {
  open(opts?: { cwd?: string }): Promise<void>;
  run(command: string, opts?: { cwd?: string }): Promise<void>;
}

interface FusedIndexReady {
  indexed: boolean;
  scanning: boolean | null;
  stale: boolean;
  reason: "no-index" | "outdated" | "not-covered" | null;
}

/** The machine-wide file index. LOCAL ONLY. */
interface FusedFileIndex {
  /** One folder's indexed corpus, the per-keystroke path. */
  search(opts: { root?: string; q: string; limit?: number }): Promise<{ ready: FusedIndexReady | null } & Record<string, unknown>>;
  /** One read-only SQL statement over the `files` and `dirs` views. */
  query(opts: { sql: string; limit?: number }): Promise<{ ready: FusedIndexReady | null } & Record<string, unknown>>;
}

interface FusedDaemonStatus {
  running: boolean;
  autostart: boolean;
  pid: number | null;
  version: string | null;
  engine_id: string | null;
  /** "main" or "daemon" per the folder's manifest; null without a valid one. */
  protocol: "main" | "daemon" | null;
}

/** A folder's declared background worker (`[tool.fused-render.app]` in its pyproject.toml). LOCAL ONLY. */
interface FusedDaemon {
  status(): Promise<FusedDaemonStatus>;
  start(): Promise<FusedDaemonStatus>;
  stop(): Promise<FusedDaemonStatus>;
  restart(): Promise<FusedDaemonStatus>;
  /** The only call that persists "bring this back at every launch". */
  setAutostart(on: boolean): Promise<FusedDaemonStatus>;
  /** The `main =` convenience: one call, the runPython envelope unwrapped. Warms the daemon as needed. */
  run<T = unknown>(params?: Record<string, unknown>): Promise<T>;
  /** Reach a `daemon =` folder's own routes; the body comes back raw. */
  call<T = unknown>(path: string, body?: unknown): Promise<T>;
  /** Learn that the state changed without this page causing it. Returns unsubscribe. */
  watch(cb: (status: FusedDaemonStatus) => void): () => void;
}

// ---------------------------------------------------------------- fused.tasks

type FusedTaskStatus =
  | "pending"
  | "queued"
  | "in_progress"
  | "needs_attention"
  | "blocked"
  | "done"
  | "archived"
  | string;

/** One row of the shell's Tasks page. */
interface FusedTask {
  key: string;
  status: FusedTaskStatus;
  title?: string;
  backend?: string;
  model?: string;
  effort?: string;
  [key: string]: unknown;
}

interface FusedTaskMessage {
  id?: string;
  role?: string;
  text?: string;
  [key: string]: unknown;
}

interface FusedTaskListOptions {
  /** "app" (default): tasks under this page's app folder; "all": every task. */
  scope?: "app" | "all";
  status?: FusedTaskStatus[];
  archived?: boolean;
}

interface FusedTaskCreateSpec {
  prompt: string;
  /** A folder to run in; default this app's. */
  target?: string;
  title?: string;
  model?: string;
  effort?: string;
  backend?: "claude" | "copilot" | "codex" | "gemini" | string;
  permissionMode?: string;
  /** ISO string or Date; omit to start now. */
  due?: string | Date;
}

interface FusedTaskChange {
  full: boolean;
  rows: FusedTask[];
  gone: string[];
}

interface FusedTaskHandle {
  /** `pending:<entryId>` until the session's row appears, then the session id. */
  key: string;
  entryId: string;
  get(): Promise<FusedTask | null>;
  send(text: string, opts?: { model?: string; effort?: string }): Promise<{ queued: boolean; key: string }>;
  cancel(): Promise<void>;
  archive(): Promise<void>;
  watch(cb: (row: FusedTask | null, change: FusedTaskChange) => void): () => void;
  /** Resolves with the row once it reaches done or archived. Never rejects. */
  done: Promise<FusedTask | null>;
}

/** Claude tasks: the rows the shell's Tasks page draws. LOCAL ONLY. */
interface FusedTasks {
  list(opts?: FusedTaskListOptions): Promise<FusedTask[]>;
  get(key: string, opts?: { scope?: "app" | "all" }): Promise<FusedTask | null>;
  create(spec: FusedTaskCreateSpec): Promise<FusedTaskHandle>;
  /** Whether "one task at a time per folder" is on. */
  queue(): Promise<boolean>;
  setQueue(on: boolean): Promise<boolean>;
  /** The shell's Tasks UI as a same-origin URL for an <iframe>. */
  ui(opts?: { view?: "list" | "board" | "cards" | "calendar"; task?: string; scope?: "app" | "all" }): Promise<string>;
  send(key: string, text: string, opts?: { model?: string; effort?: string }): Promise<{ queued: boolean; key: string }>;
  cancel(key: string): Promise<void>;
  archive(key: string): Promise<void>;
  unarchive(key: string): Promise<void>;
  delete(key: string): Promise<void>;
  markRead(key: string, messageId?: string): Promise<void>;
  /** The full thread, newest first. */
  messages(key: string): Promise<FusedTaskMessage[]>;
  transcript(key: string, opts?: { native?: boolean; file?: boolean; scope?: "app" | "all" }): Promise<unknown[]>;
  export(key: string, opts?: { format?: "md" | "json"; scope?: "app" | "all" }): Promise<unknown>;
  settings(key: string, patch: { model?: string; effort?: string }, opts?: { scope?: "app" | "all" }): Promise<void>;
  watch(cb: (rows: FusedTask[], change: FusedTaskChange) => void, opts?: FusedTaskListOptions): () => void;
}

// ---------------------------------------------------------------- fused.bots

interface FusedBot {
  id: string;
  name: string;
  kind: "bot" | "super";
  preset?: string;
  description?: string;
  status: "idle" | "running" | "waiting" | "paused" | "error" | string;
  busy: boolean;
  task?: string | null;
  note?: string | null;
  step?: string | null;
  waitingOn: "question" | "approval" | "login" | null;
  model?: string;
  effort?: string;
  face?: { shape: string; color: string; icon: string };
  hidden?: boolean;
  pinned?: boolean;
  updated?: string | number;
}

interface FusedBotEvent {
  role: string;
  text?: string;
  [key: string]: unknown;
}

interface FusedBotResult {
  state: "working" | "blocked" | "done" | "failed" | "cancelled";
  text?: string;
  summary?: string;
  seq?: number;
  artifacts?: unknown[];
  events: FusedBotEvent[];
}

type FusedBotFile = File | Blob | { name: string; blob: Blob };

interface FusedBotTurn {
  bot: FusedBot;
  addr: string;
  /** Whether the bot's engine can see the images you attach. */
  vision: boolean;
  state: FusedBotResult["state"];
  /** Set while a question or approval waits for the human at the Mac. */
  blocked: { kind: string; text: string } | null;
  events: FusedBotEvent[];
  result: FusedBotResult | null;
  /** Resolves with the result; never rejects. */
  done: Promise<FusedBotResult>;
  watch(cb: (event: FusedBotEvent, turn: FusedBotTurn) => void): () => void;
  /** A follow-up instruction, or the answer to the bot's question. */
  send(text: string, opts?: { files?: FusedBotFile[] }): Promise<unknown>;
  stop(): Promise<unknown>;
}

/** The Bots on this Mac. LOCAL ONLY; 403 while Bots is off in Preferences. */
interface FusedBots {
  list(): Promise<FusedBot[]>;
  /** By id, else exact name (case-insensitive). */
  get(idOrName: string): Promise<FusedBot | null>;
  /** Starts a task on an idle bot, stamped as from this app. A busy bot rejects 409 with `.reason`. */
  send(idOrName: string, text: string, opts?: { files?: FusedBotFile[] }): Promise<FusedBotTurn>;
  watch(cb: (bots: FusedBot[]) => void): () => void;
}

// ---------------------------------------------------------------- fused

interface FusedSnapshot {
  sha: string;
  dir: string;
  app_dir: string;
}

/** `window.fused`: the bridge every rendered page gets. */
interface Fused {
  env: FusedEnv;
  device: FusedDevice;

  /**
   * Run `main(**params)` in the .py beside this page (or an absolute path).
   * Resolves with main's return value; rejects with a FusedError carrying the
   * Python traceback. A new call for the same .py aborts the one in flight.
   */
  runPython<T = unknown>(pyPath: string, params?: Record<string, unknown>, opts?: FusedRunPythonOptions): Promise<T>;

  params: FusedParams;

  /** A synchronous URL that serves a file's bytes — <img>, <video>, download links, dynamic import(). */
  rawUrl(path: string): string;
  stat(path: string): Promise<FusedStat>;
  /** UTF-8 text. */
  readFile(path: string): Promise<string>;
  /** Atomic write of UTF-8 text; resolves with the fresh stat (keep its mtime for the next lock). */
  writeFile(path: string, content: string, opts?: FusedWriteFileOptions): Promise<FusedStat>;
  /** Bytes: a pasted image, a recording. */
  uploadFile(path: string, blob: Blob): Promise<FusedStat>;
  /** One directory; parents are not created. An existing path rejects `type: "exists"`. */
  mkdir(path: string): Promise<FusedStat>;

  ai: FusedAi;
  capture: FusedCapture;
  terminal: FusedTerminal;
  fileIndex: FusedFileIndex;
  tasks: FusedTasks;
  bots: FusedBots;
  daemon: FusedDaemon;

  trackJob(spec?: FusedTrackJobSpec): FusedTrackJobHandle;
  watchJob(id: string): FusedWatchJobHandle;

  /** Reload-on-file-change is on by default; an in-page editor turns it off. */
  autoReload(enabled: boolean): void;
  /** A watched file changed on disk (for a page that must not reload). Returns unsubscribe. */
  onFileChanged(cb: (change: FusedFileChange) => void): () => void;
  /** Whether this frame shows a git snapshot rather than the live folder. */
  snapshot(): Promise<FusedSnapshot | null>;
}

declare const fused: Fused;

interface Window {
  fused: Fused;
}
