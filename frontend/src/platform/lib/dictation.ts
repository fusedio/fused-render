// DICTATION — a composer's microphone button, as one hook.
//
// Push-to-talk, not live: record the mic to a file (`captureAudio`), transcribe
// it with the local speech model (`POST /api/ai/transcribe`, a job), hand the
// text to the composer to drop at the caret for review. Nothing is sent on its
// own. The Bots composer wrote this flow first (apps/bots/components/Composer);
// this is the same flow, sentence for sentence, for a composer in any app —
// platform-layer because an app may only import platform + itself.
//
// A transcription is a job, and the first one on a machine spends minutes
// building the speech runner's venv and downloading the model before any audio
// is read. The server narrates that in the job's `detail` and points at the
// model job in `waiting_for`; a bare spinner over that stretch reads as a hang
// (seen 2026-10-04: "the transcriber is not working" was a 300 MB torch
// download). So the job is polled anyway and `jobNote` is shown instead of
// silence.
import { useCallback, useEffect, useRef, useState } from "react";

import { rawUrl } from "@platform/lib/api";
import {
  captureAudio,
  captureSources,
  type AudioRecording,
  type CaptureResult,
} from "@platform/lib/capture-audio";

/** Seconds; the recording stops itself here and is still transcribed. */
export const MIC_MAX_SECONDS = 120;
export const MIC_HINT =
  "Dictate: click to record, click again to stop. The text lands here for you to review before sending.";
export const MIC_STOP_HINT = "Stop recording (Esc discards)";

export interface JobNoteSource {
  detail?: string;
  waiting_for?: string;
}

/** The sentence for a transcription job row (or none yet): the server's own `detail` when it has one, a first-run
 *  install notice when the job is only waiting on a model, else the plain state. */
export function jobNote(rec: JobNoteSource | null | undefined): string {
  const detail = (rec?.detail || "").trim();
  if (detail) return detail;
  if ((rec?.waiting_for || "").startsWith("sys:ai-model:")) return "Preparing the speech model (first time only)…";
  return "Transcribing…";
}

/** The take's clock: m:ss. */
export const micClock = (seconds: number): string =>
  `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;

/** Put `text` at the caret (or over the selection) of `value`, with a space on
 *  either side when the neighbours need one. Pure: the caller owns the box. */
export function insertAt(
  value: string,
  start: number,
  end: number,
  text: string,
): { value: string; caret: number } {
  const a = Math.max(0, Math.min(start, value.length));
  const b = Math.max(a, Math.min(end, value.length));
  const before = value.slice(0, a);
  const after = value.slice(b);
  const pad = before && !/\s$/.test(before) ? " " : "";
  const tail = after && !/^\s/.test(after) ? " " : "";
  const head = before + pad + text;
  return { value: head + tail + after, caret: head.length };
}

// ---- the plumbing (capture → transcribe job → transcript file) -------------

interface JobRow {
  id: string;
  state: string;
  message?: string;
  detail?: string;
  waiting_for?: string;
}
type TypedError = Error & { type?: string; jobId?: string };
const typed = (msg: string, type: string, jobId?: string): TypedError =>
  Object.assign(new Error(msg), { type, jobId });
const errText = (e: unknown): string => {
  const err = e as { message?: string } | null;
  return (err && err.message) || String(e);
};

export interface DictationDeps {
  fetch?: typeof fetch;
  capture?: typeof captureAudio;
  sleep?: (ms: number) => Promise<void>;
}

const sleepFor = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

/** Poll /api/jobs (every 700 ms) until the job leaves running/waiting; null
 *  when it vanished (5 misses after a sighting) or `stopped()` says so. `tick`
 *  sees every sighting while it runs, so a long first-run phase is narrated. */
export async function watchJob(
  id: string,
  stopped: () => boolean = () => false,
  tick?: (rec: JobRow) => void,
  deps: DictationDeps = {},
): Promise<JobRow | null> {
  const doFetch = deps.fetch || fetch;
  const sleep = deps.sleep || sleepFor;
  let seen = false;
  let missing = 0;
  for (;;) {
    if (stopped()) return null;
    const rec = await doFetch("/api/jobs", { cache: "no-store" })
      .then((r) => (r.ok ? (r.json() as Promise<{ jobs?: JobRow[] }>) : null))
      .then((d) => (d?.jobs || []).find((j) => j.id === id) || null)
      .catch(() => null);
    if (rec) {
      seen = true;
      missing = 0;
      if (rec.state !== "running" && rec.state !== "waiting") return rec;
      tick?.(rec);
    } else if (seen && ++missing >= 5) {
      return null;
    }
    await sleep(700);
  }
}

/** POST /api/ai/transcribe, reading the error body itself: a `model_loading`
 *  reply ({error: {type, message}, jobId}) names the download job to wait on.
 *  Transcribe's usual 409 is the plain {error: "…"} (no model or no runner)
 *  and stays an error. */
async function transcribeStart(
  path: string,
  requester: string,
  deps: DictationDeps,
): Promise<{ jobId: string; output: string }> {
  const r = await (deps.fetch || fetch)("/api/ai/transcribe", {
    method: "POST",
    headers: {
      "X-Fused": "1",
      // Who is asking (SPEC-model-slot-conflict.md §1): the composer's own mic
      // is a shell surface with a fixed identity, not the user settling a
      // conflict with their own hands.
      "X-Fused-Requester": requester,
      "X-Fused-Requester-Label": requester,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ path }),
    cache: "no-store",
  });
  const data = (await r.json().catch(() => ({}))) as {
    jobId?: string;
    output?: string;
    job_id?: string;
    error?: string | { type?: string; message?: string; jobId?: string };
  };
  if (!r.ok) {
    const e = data.error;
    if (e && typeof e === "object") {
      throw typed(e.message || r.statusText, e.type || "ai_error", e.jobId || data.jobId || data.job_id);
    }
    throw typed(String(e || r.statusText), r.status === 409 ? "unavailable" : "bad_request");
  }
  if (!data.jobId || !data.output) throw typed("/api/ai/transcribe replied with no jobId", "ai_error");
  return { jobId: data.jobId, output: data.output };
}

async function transcribeOnce(
  path: string,
  requester: string,
  note: (text: string) => void,
  deps: DictationDeps,
): Promise<string> {
  const started = await transcribeStart(path, requester, deps);
  const rec = await watchJob(started.jobId, undefined, (j) => note(jobNote(j)), deps);
  if (rec && rec.state !== "done") {
    throw typed(
      rec.state === "cancelled" ? "the transcription was cancelled" : rec.message || "the transcription failed",
      rec.state === "cancelled" ? "cancelled" : "ai_error",
      started.jobId,
    );
  }
  const written = await (deps.fetch || fetch)(rawUrl(started.output), { cache: "no-store" })
    .then((r) => {
      if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
      return r.json() as Promise<{ text?: string }>;
    })
    .catch((cause) => {
      throw typed("the transcript could not be read: " + errText(cause), "ai_error", started.jobId);
    });
  return written.text || "";
}

/** One recording's words. A cold model answers `model_loading` with the
 *  download's job: say so once, wait for it, ask again. */
export async function transcribeTake(
  path: string,
  requester: string,
  note: (text: string) => void,
  say: (message: string, tone: DictationTone) => void,
  deps: DictationDeps = {},
): Promise<string> {
  try {
    return await transcribeOnce(path, requester, note, deps);
  } catch (e) {
    const t = e as TypedError;
    if (t?.type !== "model_loading" || !t.jobId) throw e;
    say("Downloading the speech model (first time only). Your recording is kept; the text follows.", "info");
    await watchJob(t.jobId, undefined, (j) => note(jobNote(j)), deps);
    return transcribeOnce(path, requester, note, deps);
  }
}

// ---- can this machine record at all -----------------------------------------

let canRecord: Promise<boolean> | null = null;
const CAN_DICTATE_RETRIES = 3;
const CAN_DICTATE_RETRY_MS = 4000;

/** Test-only: forget the probe's answer. */
export function resetCanDictateForTests(): void {
  canRecord = null;
}

/**
 * Whether to draw a mic at all: true only once the machine has said it can
 * record (`captureSources`, which never prompts for permission). The Bots
 * composer's rule — the button exists only when the machine can record — so a
 * probe that fails, or has not answered yet, draws nothing. Asked once per
 * page: `available` is a fact about the OS and the build.
 *
 * `enabled` false asks nothing (a surface that has no mic seat).
 */
export function useCanDictate(enabled = true): boolean {
  const [can, setCan] = useState(false);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    if (!enabled) return;
    let live = true;
    let retry: ReturnType<typeof setTimeout> | undefined;
    // Only an ANSWER is kept: a probe that failed (the server still starting)
    // is asked again — a few times here, a few seconds apart, and by the next
    // mount after that — rather than hiding the mic for the life of the page.
    const asked = (canRecord ??= captureSources().then((sources) => sources.audio?.available === true));
    asked.then(
      (ok) => {
        if (live) setCan(ok);
      },
      () => {
        if (canRecord === asked) canRecord = null;
        if (live && attempt < CAN_DICTATE_RETRIES) {
          retry = setTimeout(() => setAttempt((n) => n + 1), CAN_DICTATE_RETRY_MS);
        }
      },
    );
    return () => {
      live = false;
      clearTimeout(retry);
    };
  }, [enabled, attempt]);
  return enabled && can;
}

// ---- the hook ---------------------------------------------------------------

/** "info": something to know (no speech heard, the first-run download).
 *  "error": the take was lost or never started. */
export type DictationTone = "info" | "error";

export interface DictationOptions {
  /** `X-Fused-Requester` for the transcribe call, e.g. "shell:chat-composer". */
  requester: string;
  /** The words, whitespace collapsed, never empty. */
  onText(text: string): void;
  /** A sentence for the reader: a refusal, a failure, the first-run download. */
  onMessage(message: string, tone: DictationTone): void;
  /** After a take was transcribed (or failed to be): the composer takes the
   *  caret back. Not called for a discarded take. */
  onSettled?(): void;
  /** The document Esc is heard on (a framed composer's own); default `document`. */
  doc?: () => Document | null | undefined;
  deps?: DictationDeps;
}

export interface Dictation {
  /** A take is being recorded. */
  rec: boolean;
  /** A take is being transcribed. */
  busy: boolean;
  /** The take's clock while recording ("0:07"), else "". */
  time: string;
  /** What the transcription is doing right now (`jobNote`), while busy. */
  note: string;
  /** The button's press: start a take, or stop the one running. */
  toggle(): void;
  /** Throw away whatever is in flight — a take being recorded, or one still
   *  being transcribed — without a word or a message. For a composer whose box
   *  has just become another conversation's. */
  cancel(): void;
}

export function useDictation(opts: DictationOptions): Dictation {
  const o = useRef(opts);
  o.current = opts;
  const [ui, setUi] = useState({ rec: false, busy: false, time: "", note: "" });
  const take = useRef<AudioRecording | null>(null);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);
  const busy = useRef(false);
  const starting = useRef(false);
  const live = useRef(true);
  /** Which take the box is waiting on; `cancel()` moves it, and a transcription
   *  that comes back to an older number has nowhere to land. */
  const gen = useRef(0);

  const sync = () =>
    setUi((m) => ({
      rec: !!take.current,
      busy: busy.current,
      time: take.current ? m.time : "",
      note: busy.current ? m.note : "",
    }));
  const note = (text: string) => setUi((m) => (m.note === text ? m : { ...m, note: text }));

  const stop = async (discard?: boolean): Promise<void> => {
    const rec = take.current;
    if (!rec) return;
    take.current = null;
    if (timer.current) clearInterval(timer.current);
    timer.current = null;
    if (discard) {
      if (live.current) sync();
      try {
        await rec.cancel();
      } catch {
        /* gone */
      }
      return;
    }
    const mine = gen.current;
    const current = () => live.current && gen.current === mine;
    busy.current = true;
    sync();
    note(jobNote(null));
    try {
      const took: CaptureResult = await rec.stop().catch((e) => {
        throw typed(errText(e), "capture_error");
      });
      if (!took?.path) throw typed("the recording was not saved", "capture_error");
      if ((took.seconds ?? 1) < 0.5) return; // a stray click, nothing worth transcribing
      const said = await transcribeTake(
        took.path,
        o.current.requester,
        (text) => {
          if (current()) note(text);
        },
        (message, tone) => {
          if (current()) o.current.onMessage(message, tone);
        },
        o.current.deps,
      );
      if (!current()) return;
      const text = said.replace(/\s+/g, " ").trim();
      if (text) o.current.onText(text);
      else o.current.onMessage("Didn't catch any speech in that recording.", "info");
    } catch (e) {
      const t = e as TypedError;
      if (t?.type === "cancelled" || !current()) return;
      o.current.onMessage(
        t?.type === "capture_error"
          ? "The recording failed to save; please try again."
          : `Transcription failed: ${errText(e)}`,
        "error",
      );
    } finally {
      // A cancelled take's state is no longer this call's to settle: a newer
      // take may already be recording.
      if (gen.current === mine) {
        busy.current = false;
        if (live.current) {
          sync();
          o.current.onSettled?.();
        }
      }
    }
  };
  const stopRef = useRef(stop);
  stopRef.current = stop;

  const start = async (): Promise<void> => {
    if (busy.current || take.current || starting.current) return;
    starting.current = true;
    let rec: AudioRecording;
    try {
      rec = await (o.current.deps?.capture || captureAudio)({
        source: "mic",
        maxSeconds: MIC_MAX_SECONDS,
        title: "Dictation",
      });
    } catch (e) {
      const refused = (e as { type?: string } | null)?.type === "unavailable";
      if (live.current) {
        o.current.onMessage(refused ? errText(e) : `Could not start the microphone: ${errText(e)}`, "error");
      }
      return;
    } finally {
      starting.current = false;
    }
    // The composer left while the microphone was opening: nobody is there to
    // stop this take, so it is not kept.
    if (!live.current) {
      void rec.cancel().catch(() => undefined);
      return;
    }
    take.current = rec;
    const t0 = Date.now();
    setUi({ rec: true, busy: false, time: micClock(0), note: "" });
    timer.current = setInterval(
      () => setUi((m) => ({ ...m, time: micClock(Math.floor((Date.now() - t0) / 1000)) })),
      500,
    );
    // Hitting maxSeconds (or ✕ on the job row) ends the take without a click:
    // follow the job so the button catches up.
    void watchJob(rec.jobId, () => take.current !== rec, undefined, o.current.deps).then((j) => {
      if (take.current === rec && j?.state) void stopRef.current(j.state === "cancelled");
    });
  };
  const startRef = useRef(start);
  startRef.current = start;

  // Esc discards the take, wherever the caret is — heard only while one runs.
  // IT IS THE TAKE'S Esc AND NOBODY ELSE'S: heard first (the window's capture
  // phase) and stopped there, so the same key does not also close the panel,
  // card or popover the composer happens to sit in.
  const recording = ui.rec;
  useEffect(() => {
    if (!recording) return;
    const doc = o.current.doc?.() ?? (typeof document === "undefined" ? null : document);
    const target: Window | Document | null = doc?.defaultView ?? doc ?? null;
    if (!target) return;
    const onKey = (e: Event) => {
      if ((e as KeyboardEvent).key !== "Escape" || !take.current) return;
      e.preventDefault();
      e.stopImmediatePropagation();
      void stopRef.current(true);
    };
    target.addEventListener("keydown", onKey, true);
    return () => target.removeEventListener("keydown", onKey, true);
  }, [recording]);

  // The composer unmounting ends a take nobody could stop any more.
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
      void stopRef.current(true);
    };
  }, []);

  const toggle = useCallback(() => {
    void (take.current ? stopRef.current() : startRef.current());
  }, []);

  const cancel = useCallback(() => {
    gen.current += 1;
    busy.current = false;
    if (take.current) void stopRef.current(true);
    else if (live.current) setUi({ rec: false, busy: false, time: "", note: "" });
  }, []);

  return { rec: ui.rec, busy: ui.busy, time: ui.time, note: ui.note, toggle, cancel };
}
