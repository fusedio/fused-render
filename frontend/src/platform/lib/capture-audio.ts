// THE MICROPHONE, AS A FILE — the shell's port of `fused.capture.audio()` and
// the audio half of `fused.capture.sources()` (R:5412 / R:5443, SPEC §45).
//
// Why not `getUserMedia` + a Blob: the point of going through the app is that
// the result is a FILE WHOSE PATH IS KNOWN BEFORE THE RECORDING STOPS
// (R:4963-4967), so `POST /api/ai/transcribe {path}` is the next line rather
// than a blob round-trip — and the recording is a job row, so it outlives the
// document that started it (R:4967). The voice walkthrough (`ann/rec.ts`) is
// the whole reason this exists in the shell.
//
// TWO BACKENDS, ONE CONTRACT. macOS records natively: `POST
// /api/capture/start` and the OS writes the file. Windows and Linux record
// through the BROWSER — `GET /api/capture` answers `sources.client: true` —
// where a MediaRecorder in this window streams timeslices up a WebSocket and
// the server writes them (R:5004-5014). A CALLER NEVER LEARNS WHICH IT GOT:
// both resolve with the same handle, and the two wire-only fields that steer
// it (`transport`, `streamToken`) never leave this module (CP-8, R:5012-5014).
//
// Platform-layer on purpose: no React, no app imports. `ann/rec.ts` injects
// this as a dependency and the tests inject a fake.
import { rawUrl } from "@platform/lib/api";

/** What `capture.sources()` says about one thing this machine could capture.
 *  `available` is about the OS and the build; `granted` is about TCC and moves
 *  without this process restarting — two booleans, not one
 *  (`fused_render/capture/__init__.py` `sources`). */
export interface CaptureSource {
  available: boolean;
  granted?: boolean;
  /** Why not — and on the browser-records platforms it NAMES A BROWSER THAT
   *  CAN (CP-11, R:5470-5472). Rendered verbatim; never appended to. */
  reason: string | null;
}

export interface CaptureMicrophone {
  id: string;
  name: string;
  default: boolean;
}

/** The `GET /api/capture` payload's `sources`, after the client merge below.
 *  `client` is deliberately absent from this type: it is deleted on the way
 *  out so a caller reads `{available, reason}` exactly as it does on macOS and
 *  has nothing to branch on (CP-8, R:5461). */
export interface CaptureSources {
  video?: CaptureSource;
  audio?: CaptureSource;
  systemAudio?: CaptureSource;
  screenshot?: CaptureSource;
  microphones?: CaptureMicrophone[];
}

/** What a stop or a cancel resolves with — `capture.stop()`'s own record
 *  (`fused_render/capture/__init__.py` `Session.public` + `_describe`).
 *
 *  `seconds` and `bytes` are the SHAPE `ann/rec.ts` gates on ("nothing to
 *  transcribe: a failed stop, an empty file, or a stop that landed on the
 *  start's own beat" — T:8199), so they keep the server's names rather than
 *  being re-spelled as `durationMs`: one name per fact, all the way from the
 *  route to the state machine.
 *
 *  `path`/`url` are null after a `cancel()` — the file is deleted, which is
 *  what cancelling means (CP-4). */
export interface CaptureResult {
  id: string;
  mode: string;
  state: string;
  path: string | null;
  url: string | null;
  seconds: number;
  bytes?: number;
  mime?: string;
  maxSeconds: number;
  jobId: string;
  /** A stop whose file failed to write reports the failure rather than handing
   *  back a path to something unplayable (R:5322-5329). */
  error?: string;
}

/** A recording in progress. A HANDLE, not a promise that resolves at the end:
 *  a recording is a session of unknown length under the user's control
 *  (R:5288-5292). `state` is a getter — a caller polling it must not read a
 *  copy that went stale. */
export interface AudioRecording {
  readonly id: string;
  readonly mode: string;
  /** Named before the first sample exists (CP-2) — this is the field that makes
   *  `transcribe({path})` the next line. */
  readonly path: string;
  readonly jobId: string;
  readonly maxSeconds: number;
  readonly state: string;
  readonly url: string;
  /** Keeps the file. The ending a caller asks for (CP-4). */
  stop(): Promise<CaptureResult>;
  /** Stops AND DELETES — the same meaning the download manager's ✕ has,
   *  spelled out here so nobody has to discover it from a row (R:5347). */
  cancel(): Promise<CaptureResult>;
}

export interface AudioOptions {
  /** "mic". System audio is a property of a SCREEN recording, and asking for
   *  it here is refused by the server with the sentence that says where to ask
   *  instead (R:5404-5411). */
  source?: string;
  /** Forwarded, not dropped, so the refusal is the SERVER's one good sentence
   *  rather than two (R:5408-5411). */
  device?: string;
  path?: string;
  maxSeconds?: number;
  /** The download-manager row's title. */
  title?: string;
}

/** A capture error, typed the way `/api/ai/*` types its own so one `catch` can
 *  branch: "unavailable" is "this machine cannot" (409), "bad_request" is "you
 *  asked wrong" (400), "capture_error" is a file that failed to write
 *  (R:4986-4990, R:5325). */
export interface CaptureError extends Error {
  type?: "unavailable" | "bad_request" | "capture_error";
}

function captureError(
  message: string,
  type: CaptureError["type"],
): CaptureError {
  const err = new Error(message) as CaptureError;
  err.type = type;
  return err;
}

// ── the browser-records realm ───────────────────────────────────────────────
//
// R:5038-5042 IS THE REMARK THIS FILE IS SHAPED BY: the recorder, its stream
// and its socket live in the TOPMOST SAME-ORIGIN WINDOW, not in the frame that
// asked. A MediaRecorder belongs to the realm that made it, so one created in
// a frame ends when that frame navigates — and a recording that stops because
// the user clicked another file is not a recording. The shell is usually the
// top window already; it is not when the chat is framed (ChatMount's hosted
// layouts), which is exactly the case the walk covers.

/** The members of a window this module actually uses. A structural type, not
 *  `Window`, so a test can hand over an object with four fake constructors on
 *  it. */
export interface CaptureHost {
  MediaRecorder?: typeof MediaRecorder;
  MediaStream?: typeof MediaStream;
  WebSocket?: typeof WebSocket;
  AudioContext?: typeof AudioContext;
  navigator: { mediaDevices?: Partial<MediaDevices> };
  location: { protocol: string; host: string };
  parent?: unknown;
}

/** The topmost same-origin ancestor (R:5043-5055). Param boundaries (D72) are
 *  deliberately NOT honoured here: this is about object lifetime, not params. */
export function captureHost(from?: CaptureHost): CaptureHost {
  if (from) return from;
  if (typeof window === "undefined") {
    throw captureError("no window to record in", "unavailable");
  }
  let host: Window = window;
  try {
    while (host.parent && host.parent !== host) {
      void host.parent.location.href;
      host = host.parent;
    }
  } catch {
    /* reached a cross-origin ancestor; host is the topmost same-origin one */
  }
  return host as unknown as CaptureHost;
}

const CAPTURE_SLICE_MS = 1000;

/** mp4 before webm: a fragmented mp4 is what everything else on the machine
 *  reads without a remux. Both are playable AS WRITTEN, which is why a
 *  recording whose page dies still leaves a valid, shorter file rather than a
 *  movie with no index in it (R:5018-5035). The AUDIO ladder only — screen
 *  recording is not the shell's to start. */
const AUDIO_TYPES: ReadonlyArray<readonly [string, string]> = [
  ["mp4", 'audio/mp4;codecs="mp4a.40.2"'],
  ["mp4", "audio/mp4"],
  ["webm", "audio/webm;codecs=opus"],
  ["webm", "audio/webm"],
];

/** The first container this browser will actually encode, or null (R:5056). */
export function audioType(host: CaptureHost): { container: string; mimeType: string } | null {
  const Recorder = host.MediaRecorder;
  if (!Recorder || !Recorder.isTypeSupported) return null;
  for (const [container, mimeType] of AUDIO_TYPES) {
    if (Recorder.isTypeSupported(mimeType)) return { container, mimeType };
  }
  return null;
}

/** R:5065. `getDisplayMedia` is deliberately NOT required: audio-only never
 *  opens a share picker, and a browser without screen capture can still record
 *  a microphone. */
function mediaOk(host: CaptureHost): boolean {
  const devices = host.navigator && host.navigator.mediaDevices;
  return !!(host.MediaRecorder && devices && devices.getUserMedia);
}

/** R:5071. Every "already ended" swallowed: stopping a stopped track is the
 *  ordinary case on the teardown paths, not an error. */
function tracksOff(streams: Array<MediaStream | null>): void {
  for (const stream of streams) {
    if (!stream) continue;
    for (const track of stream.getTracks()) {
      try {
        track.stop();
      } catch {
        /* already ended */
      }
    }
  }
}

/** The wire-only half of a `/api/capture/start` reply. Never handed out. */
interface StartedRecord {
  id: string;
  mode: string;
  path: string;
  state: string;
  seconds: number;
  maxSeconds: number;
  jobId: string;
  transport?: string;
  streamToken?: string;
}

export interface CaptureDeps {
  /** Injected so a test can answer the four routes without a server. */
  fetch?: typeof fetch;
  /** Injected so a test can hand over a fake MediaRecorder realm; defaults to
   *  the topmost same-origin window (R:5043). */
  host?: CaptureHost;
  /** The calling page's own path, for the page-relative `path` rule (RH-1,
   *  R:4997-5002). The shell has no `?path=` of its own, so this is normally
   *  absent and every `path` is absolute or the server's own default. */
  base?: string | null;
}

function captureFetch<T>(
  deps: CaptureDeps,
  path: string,
  body?: unknown,
  method?: "GET" | "POST",
): Promise<T> {
  const doFetch = deps.fetch || fetch;
  return doFetch(path, {
    method: method || "POST",
    headers:
      body === undefined
        ? { "X-Fused": "1" }
        : { "Content-Type": "application/json", "X-Fused": "1" },
    body: body === undefined ? undefined : JSON.stringify(body || {}),
  }).then(async (res) => {
    const data = (await res.json().catch(() => ({}))) as { error?: string } | null;
    if (!res.ok) {
      // 409 is "this machine cannot", 400 is "you asked wrong" — the same split
      // /api/ai/* makes, so one `catch` can branch on `.type` (R:4984-4990).
      throw captureError(
        (data && data.error) || res.statusText,
        res.status === 409 ? "unavailable" : "bad_request",
      );
    }
    return data as T;
  });
}

/** R:4997 — a RELATIVE `path` lands beside the calling page, the rule
 *  readFile/rawUrl/ai.transcribe already follow (RH-1). */
function withBase<T extends object>(deps: CaptureDeps, body: T): T & { base?: string } {
  return deps.base ? { ...body, base: deps.base } : body;
}

// ── the streamed recorder (browser-records platforms) ───────────────────────

/** The socket. Opened BEFORE the recorder starts, because a chunk produced
 *  before it is open is a chunk missing from the middle of the file
 *  (R:5147-5151). */
function openSocket(host: CaptureHost, started: StartedRecord): Promise<WebSocket> {
  const Sock = host.WebSocket;
  if (!Sock) {
    return Promise.reject(captureError("could not open the capture stream", "bad_request"));
  }
  const where = host.location;
  const scheme = where.protocol === "https:" ? "wss:" : "ws:";
  const url =
    scheme +
    "//" +
    where.host +
    "/api/capture/" +
    encodeURIComponent(started.id) +
    "/stream?token=" +
    encodeURIComponent(started.streamToken || "");
  const ws = new Sock(url);
  ws.binaryType = "arraybuffer";
  return new Promise<WebSocket>((resolve, reject) => {
    ws.onopen = () => resolve(ws);
    // The server closes with 1008 and a reason when it refuses — a token that
    // does not match, a recording already streaming. That sentence is the only
    // thing a caller would ever see, so it becomes the error (R:5163-5167).
    ws.onclose = (event: CloseEvent) => {
      reject(
        captureError(
          (event && event.reason) || "the capture stream closed before it opened",
          "bad_request",
        ),
      );
    };
    ws.onerror = () => {
      reject(captureError("could not open the capture stream", "bad_request"));
    };
  });
}

interface Streamer {
  begin(): Promise<void>;
  flush(): Promise<void>;
  dispose(): void;
}

/** R:5180. ONE chain, not one promise per chunk: `Blob.arrayBuffer()` is
 *  async, so two chunks read in parallel can be sent out of order — and two
 *  swapped clusters are a corrupt container, not a glitch. */
function makeStreamer(
  host: CaptureHost,
  started: StartedRecord,
  stream: MediaStream,
  mimeType: string,
  sources: Array<MediaStream | null>,
): Streamer {
  const Recorder = host.MediaRecorder as typeof MediaRecorder;
  const recorder = new Recorder(stream, { mimeType });
  let closed = false;
  let ws: WebSocket | null = null;
  let queue: Promise<void> = Promise.resolve();

  recorder.ondataavailable = (event: BlobEvent) => {
    if (!event.data || !event.data.size) return;
    queue = queue
      .then(() => event.data.arrayBuffer())
      .then((bytes) => {
        if (ws && ws.readyState === 1) ws.send(bytes);
      })
      .catch(() => {
        /* a closed socket is an ending, not a chunk error (R:5197) */
      });
  };

  return {
    async begin() {
      ws = await openSocket(host, started);
      ws.onclose = () => {
        // The server ended it: the cap, the manager's ✕, or a write that could
        // not continue. Stop producing bytes nothing will read (R:5203-5209).
        closed = true;
        try {
          recorder.stop();
        } catch {
          /* already stopped */
        }
        tracksOff(sources);
      };
      recorder.start(CAPTURE_SLICE_MS);
    },
    // Everything the server must have BEFORE the stop request lands. The `eos`
    // round-trip is the only thing that actually proves it: frames are ordered
    // on the socket, so a reply to `eos` means every chunk before it was
    // already appended — whereas the stop request travels on a different
    // connection and could otherwise close the file first and lose the tail
    // (R:5214-5219).
    async flush() {
      if (recorder.state !== "inactive") {
        await new Promise<void>((done) => {
          recorder.onstop = () => done();
          try {
            recorder.stop();
          } catch {
            done();
          }
        });
      }
      await queue;
      if (closed || !ws || ws.readyState !== 1) return;
      const sock = ws;
      await new Promise<void>((done) => {
        const timer = setTimeout(done, 5000);
        sock.onmessage = (event: MessageEvent) => {
          if (event.data === "flushed") {
            clearTimeout(timer);
            done();
          }
        };
        try {
          sock.send("eos");
        } catch {
          clearTimeout(timer);
          done();
        }
      });
    },
    dispose() {
      tracksOff(sources);
      if (ws) {
        ws.onclose = null;
        try {
          ws.close();
        } catch {
          /* already closed */
        }
      }
    },
  };
}

/** Ask for the MEDIA before the server allocates anything: a denied microphone
 *  must leave no job row and no empty file behind (R:5079-5083). Audio-only
 *  needs one `getUserMedia` and no picker — which is also why the WebAudio
 *  mixer at R:5129-5143 has no port here: it exists for `audio: "both"` on a
 *  SCREEN recording, where system audio and the microphone arrive as two
 *  tracks and MediaRecorder takes one. A microphone is always one. */
async function openMic(
  host: CaptureHost,
  opts: AudioOptions,
): Promise<{ stream: MediaStream; sources: Array<MediaStream | null> }> {
  const devices = host.navigator.mediaDevices as MediaDevices;
  let mic: MediaStream;
  try {
    mic = await devices.getUserMedia({
      audio: opts.device ? { deviceId: { exact: opts.device } } : true,
    });
  } catch (err) {
    const e = err as { name?: string; message?: string };
    throw captureError(
      e && e.name === "NotAllowedError"
        ? "the capture was not allowed — the share dialog was dismissed or " +
          "the permission was denied"
        : (e && e.message) || "could not open the capture stream",
      "bad_request",
    );
  }
  const sound = mic.getAudioTracks();
  if (!sound.length) {
    tracksOff([mic]);
    throw captureError("no microphone track was produced", "bad_request");
  }
  return { stream: mic, sources: [mic] };
}

/** R:5288 — the handle. The PROMISE is memoized, not the settled value: a
 *  double-clicked stop fired the request twice and the second one 404s (the
 *  registry entry is already gone) as an unhandled rejection. Both callers
 *  await the same in-flight request; it is cleared on failure so a stop that
 *  really failed can be retried (R:5295-5300).
 *
 *  That memoization is also what makes the walkthrough's discard safe: a stop
 *  click landing inside `cancel()`'s await gets this cancel's promise back
 *  rather than opening a second request (T:8368-8371). */
function makeHandle(
  deps: CaptureDeps,
  started: StartedRecord,
  streamer: Streamer | null,
): AudioRecording {
  let state = "recording";
  let result: CaptureResult | null = null;
  let ending: Promise<CaptureResult> | null = null;

  function end(action: "stop" | "cancel"): Promise<CaptureResult> {
    if (ending) return ending;
    // A streamed recording has to be FLUSHED first: the encoder is in this
    // browser, and the last timeslice is still in flight when the button is
    // clicked. Native recordings have no streamer and go straight to the
    // request (R:5302-5310).
    ending = (streamer ? streamer.flush() : Promise.resolve())
      .catch(() => {})
      .then(() =>
        captureFetch<CaptureResult>(
          deps,
          "/api/capture/" + encodeURIComponent(started.id) + "/" + action,
        ),
      )
      .then((done) => {
        // AFTER the request, not before: disposing closes the socket, and a
        // socket closing is itself an ending (`_sink.detach`) — one that KEEPS
        // the file. Racing it ahead of a `cancel` would answer from the
        // finished-record cache and leave the file the caller asked to delete
        // (R:5316-5320).
        if (streamer) streamer.dispose();
        state = done.state || (action === "cancel" ? "cancelled" : "stopped");
        if (done.error) throw captureError(done.error, "capture_error");
        result = done;
        return done;
      })
      .catch((err) => {
        if (streamer) streamer.dispose();
        ending = null;
        throw err;
      });
    return ending;
  }

  return {
    id: started.id,
    mode: started.mode,
    path: started.path,
    jobId: started.jobId,
    maxSeconds: started.maxSeconds,
    get state() {
      return state;
    },
    get url() {
      return result && result.url ? result.url : rawUrl(started.path);
    },
    stop: () => end("stop"),
    cancel: () => end("cancel"),
  };
}

/** The streamed half. Media first, then the row, then the socket, then the
 *  encoder — each step undoing the ones before it if it fails, so a failure
 *  never leaves a recording nobody can stop (R:5247-5251). */
async function startStreamed(
  deps: CaptureDeps,
  host: CaptureHost,
  opts: AudioOptions,
): Promise<AudioRecording> {
  const type = audioType(host);
  if (!mediaOk(host) || !type) {
    throw captureError(
      "this browser cannot record: it has no MediaRecorder with a container " +
        "this app can store. Chrome, Edge or a recent Firefox can",
      "unavailable",
    );
  }
  const media = await openMic(host, opts);
  let started: StartedRecord;
  try {
    started = await captureFetch<StartedRecord>(
      deps,
      "/api/capture/start",
      withBase(deps, { ...bodyOf(opts), container: type.container }),
    );
  } catch (err) {
    tracksOff(media.sources);
    throw err;
  }
  const streamer = makeStreamer(host, started, media.stream, type.mimeType, media.sources);
  try {
    await streamer.begin();
  } catch (err) {
    streamer.dispose();
    // The row exists and the file is open, so this cancels rather than leaking
    // a recording the caller has no handle for (R:5273-5277).
    captureFetch(deps, "/api/capture/" + encodeURIComponent(started.id) + "/cancel").catch(
      () => {},
    );
    throw err;
  }
  return makeHandle(deps, started, streamer);
}

/** The request body: `mode: "audio"` plus only the keys the caller set — an
 *  `undefined` forwarded as null would be a value the server has to re-read as
 *  absence (R:5412-5420). */
function bodyOf(opts: AudioOptions): Record<string, unknown> {
  const body: Record<string, unknown> = { mode: "audio" };
  for (const key of ["source", "device", "path", "maxSeconds", "title"] as const) {
    if (opts[key] !== undefined) body[key] = opts[key];
  }
  return body;
}

/**
 * Record the microphone to a file. The handle resolves when the recording is
 * ALREADY RUNNING (CP-1), which is what lets the walkthrough's clock and the
 * transcript's own timestamps differ only by the start reply's trip home
 * (T:7899-7901).
 *
 * WHERE THE TWO PATHS FORK, and the only place they do (R:5378-5382):
 * `sources.client` — the server's own answer, never a user-agent sniff — says
 * the recording is the browser's to make on this platform.
 *
 * Rejects `.type "unavailable"` with the MACHINE'S OWN SENTENCE where nothing
 * can record, rather than opening a picker that cannot lead anywhere
 * (R:5391-5396). `ann/rec.ts` shows that sentence unappended (T:7893).
 */
export async function captureAudio(
  opts: AudioOptions = {},
  deps: CaptureDeps = {},
): Promise<AudioRecording> {
  const data = await captureFetch<{ sources?: CaptureSources & { client?: boolean } }>(
    deps,
    "/api/capture",
    undefined,
    "GET",
  );
  const sources = data.sources || {};
  if (!sources.client) {
    const started = await captureFetch<StartedRecord>(
      deps,
      "/api/capture/start",
      withBase(deps, bodyOf(opts)),
    );
    return makeHandle(deps, started, null);
  }
  const gate = sources.audio;
  if (gate && gate.available === false) {
    throw captureError(gate.reason || "capture is unavailable here", "unavailable");
  }
  return startStreamed(deps, captureHost(deps.host), opts);
}

/**
 * What can be captured, and what it is waiting for — permission included, and
 * WITHOUT PROMPTING for it (the prompt rides the first real capture, CP-7).
 * This is what a record button is drawn off: a machine that cannot record at
 * all gets no mic rather than one that could only ever alert (T:7834-7838).
 *
 * On the browser-records platforms the three recording keys are replaced HERE,
 * from what this window can actually do — a server route cannot know which
 * browser is asking — and `client` is deleted on the way out (R:5449-5461).
 */
export async function captureSources(deps: CaptureDeps = {}): Promise<CaptureSources> {
  const data = await captureFetch<{ sources?: CaptureSources & { client?: boolean } }>(
    deps,
    "/api/capture",
    undefined,
    "GET",
  );
  return mergeSources(data.sources, deps.host);
}

/** R:5455. Exported for the test, which is the only caller that has a fake
 *  window to merge against. */
export async function mergeSources(
  sources: (CaptureSources & { client?: boolean }) | undefined,
  hostArg?: CaptureHost,
): Promise<CaptureSources> {
  if (!sources) return {};
  if (!sources.client) return sources;
  const merged: CaptureSources & { client?: boolean } = { ...sources };
  delete merged.client;
  const host = captureHost(hostArg);
  const type = audioType(host);
  const ok = mediaOk(host) && !!type;
  const why = ok
    ? null
    : "this browser cannot record — it has no MediaRecorder with a container " +
      "this app can store. Chrome, Edge or a recent Firefox can";
  merged.audio = { available: ok, granted: ok, reason: why };
  if (ok) {
    try {
      const devices = host.navigator.mediaDevices as MediaDevices;
      // `label` is EMPTY until the microphone permission has been granted once
      // — a browser rule, not a bug here. A caller shows what it gets and the
      // names appear after the first recording (R:5480-5483).
      const list = await devices.enumerateDevices();
      merged.microphones = list
        .filter((device) => device.kind === "audioinput")
        .map((device, index) => ({
          id: device.deviceId,
          name: device.label || "Microphone " + (index + 1),
          default: device.deviceId === "default" || index === 0,
        }));
    } catch {
      merged.microphones = [];
    }
  }
  return merged;
}
