// The transcription stage: record or pick a recording, get the words (AI-10).
//
// `POST /api/ai/transcribe` takes a PATH — the transcript is a file and the
// run outlives the page on purpose — so both inputs land bytes on disk first
// through `POST /api/fs/upload`, into ~/recordings (the folder the capture
// work already established as where recordings live). A browser file picker
// has no path to give; the upload is not a workaround, it is the door.
//
// Progress is SECONDS OF AUDIO (the job row's unit), and the words arrive
// before the run ends: the worker appends each segment to a partial JSONL
// beside the output (runners/partial.py), and this stage tails it — a
// navigation away and back would find the same file still filling.
import { useEffect, useRef, useState } from "react";
import { getConfig, mkdir, rawUrl } from "@platform/lib/api";
import { cancelJob, type Job } from "@platform/lib/jobs";
import {
  readPartialTranscript,
  startTranscribe,
  uploadFile,
  watchJob,
  type TranscriptSegment,
  type TranscribeStarted,
} from "./playgroundClient";
import { readParam, writeParams } from "./AiModelsPlayground";

type Phase =
  | { step: "idle" }
  | { step: "recording" }
  | { step: "uploading"; name: string }
  | { step: "running"; started: TranscribeStarted; job: Job | null }
  | { step: "done"; started: TranscribeStarted; text: string };

function clock(seconds: number | undefined): string {
  if (seconds === undefined || !isFinite(seconds)) return "";
  const s = Math.max(0, Math.floor(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

export function PlaygroundTranscribe({ model }: { model: string }) {
  const [phase, setPhase] = useState<Phase>({ step: "idle" });
  const [segments, setSegments] = useState<TranscriptSegment[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [advanced, setAdvanced] = useState(false);
  const [task, setTask] = useState<"transcribe" | "translate">(() =>
    readParam("task") === "translate" ? "translate" : "transcribe",
  );
  const [language, setLanguage] = useState(() => readParam("lang") ?? "");
  const [vad, setVad] = useState(() => readParam("vad") !== "0");
  const [words, setWords] = useState(() => readParam("words") === "1");

  useEffect(() => {
    const timer = window.setTimeout(() => {
      writeParams({
        task: task !== "transcribe" ? task : null,
        lang: language ? language : null,
        vad: vad ? null : "0",
        words: words ? "1" : null,
      });
    }, 300);
    return () => window.clearTimeout(timer);
  }, [task, language, vad, words]);

  const abortRef = useRef<AbortController | null>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  useEffect(
    () => () => {
      // Stop watching and let the mic go; the RUN itself is a job that
      // deliberately survives a navigation (it shows in Activity).
      abortRef.current?.abort();
      recorderRef.current?.stream.getTracks().forEach((t) => t.stop());
    },
    [],
  );

  // Tail the partial transcript while the run is live: whole JSON lines,
  // flushed per segment, re-read whole each tick (the file is words, cheap).
  const running = phase.step === "running" ? phase.started : null;
  useEffect(() => {
    if (!running) return;
    let alive = true;
    const tick = () =>
      readPartialTranscript(running.outputPartial).then(
        (rows) => alive && rows.length && setSegments(rows),
        () => {},
      );
    void tick();
    const timer = window.setInterval(tick, 1000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [running]);

  const transcribePath = async (path: string) => {
    const started = await startTranscribe({
      path,
      model,
      ...(task !== "transcribe" ? { task } : {}),
      ...(language.trim() ? { language: language.trim() } : {}),
      ...(vad ? {} : { vad: false }),
      ...(words ? { words: true } : {}),
    });
    setSegments([]);
    setPhase({ step: "running", started, job: null });
    const controller = new AbortController();
    abortRef.current = controller;
    try {
      await watchJob(started.jobId, controller.signal, (job) =>
        setPhase((p) => (p.step === "running" && p.started.jobId === started.jobId ? { ...p, job } : p)),
      );
    } catch (e) {
      if ((e as Error).name === "AbortError") return;
      setError((e as Error).message);
      setPhase({ step: "idle" });
      return;
    }
    // The final words: the partial has them too, but the .txt is the settled
    // artefact the run wrote — read it once rather than trusting the last tail.
    try {
      const res = await fetch(rawUrl(started.outputText) + "&t=" + Date.now());
      const text = res.ok ? await res.text() : "";
      const rows = await readPartialTranscript(started.outputPartial);
      if (rows.length) setSegments(rows);
      setPhase({ step: "done", started, text });
    } catch {
      setPhase({ step: "done", started, text: "" });
    }
  };

  const land = async (data: Blob, name: string) => {
    setError(null);
    setPhase({ step: "uploading", name });
    try {
      const config = await getConfig();
      const dir = `${config.home}/recordings`;
      // No mkdir -p on the server, and no "already there" probe worth a round
      // trip: create it and let "exists" be the cheap refusal it is.
      await mkdir(dir).catch(() => {});
      const stamp = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
      const safe = name.replace(/[^A-Za-z0-9._-]/g, "_").slice(0, 60) || "recording";
      const path = `${dir}/playground-${stamp}-${safe}`;
      await uploadFile(path, data, safe);
      await transcribePath(path);
    } catch (e) {
      setError((e as Error).message);
      setPhase({ step: "idle" });
    }
  };

  const record = async () => {
    setError(null);
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const recorder = new MediaRecorder(stream);
      recorderRef.current = recorder;
      chunksRef.current = [];
      recorder.ondataavailable = (e) => e.data.size && chunksRef.current.push(e.data);
      recorder.onstop = () => {
        stream.getTracks().forEach((t) => t.stop());
        recorderRef.current = null;
        const blob = new Blob(chunksRef.current, { type: recorder.mimeType || "audio/webm" });
        const ext = (recorder.mimeType || "audio/webm").includes("mp4") ? "m4a" : "webm";
        void land(blob, `mic.${ext}`);
      };
      recorder.start();
      setPhase({ step: "recording" });
    } catch (e) {
      setError(
        (e as Error).name === "NotAllowedError"
          ? "Microphone access was refused — allow it and try again."
          : (e as Error).message,
      );
    }
  };

  const stopRecording = () => recorderRef.current?.stop();

  const stopRun = () => {
    if (phase.step === "running") void cancelJob(phase.started.jobId).catch(() => {});
  };

  const busy = phase.step === "uploading" || phase.step === "running";
  const job = phase.step === "running" ? phase.job : null;
  const progress =
    job && job.unit === "s" && job.total
      ? `${clock(job.done ?? 0)} of ${clock(job.total)} transcribed`
      : job?.detail || (phase.step === "running" ? "Starting — a cold model loads first…" : null);

  return (
    <div className="pg-transcribe">
      <div className="pg-transcribe-sources">
        {phase.step === "recording" ? (
          <button type="button" className="btn btn-danger" onClick={stopRecording}>
            ◼ Stop recording
          </button>
        ) : (
          <button type="button" className="btn btn-secondary" disabled={busy} onClick={() => void record()}>
            ● Record
          </button>
        )}
        <label className={"btn btn-secondary pg-file" + (busy || phase.step === "recording" ? " disabled" : "")}>
          Pick a file…
          <input
            type="file"
            accept="audio/*,video/*"
            disabled={busy || phase.step === "recording"}
            onChange={(e) => {
              const file = e.target.files?.[0];
              e.target.value = "";
              if (file) void land(file, file.name);
            }}
          />
        </label>
        {phase.step === "running" && (
          <button type="button" className="btn btn-secondary" onClick={stopRun}>
            Stop
          </button>
        )}
      </div>
      {error && <p className="pg-error">{error}</p>}
      {phase.step === "uploading" && <p className="pg-chat-status">Saving {phase.name}…</p>}
      {progress && <p className="pg-chat-status">{progress}</p>}
      {segments.length > 0 && (
        <div className="pg-segments">
          {segments.map((segment, index) => (
            <div key={index} className="pg-segment">
              <span className="pg-segment-time">{clock(segment.start)}</span>
              <span className="pg-segment-text">
                {segment.speaker ? <strong>{segment.speaker}: </strong> : null}
                {segment.text}
              </span>
            </div>
          ))}
        </div>
      )}
      {phase.step === "done" && (
        <div className="pg-toolbar">
          <button
            type="button"
            className="pg-adv-toggle"
            onClick={() => {
              const text =
                phase.text.trim() || segments.map((s) => s.text ?? "").join(" ").trim();
              void navigator.clipboard.writeText(text);
            }}
          >
            Copy transcript
          </button>
        </div>
      )}
      {phase.step === "idle" && segments.length === 0 && (
        <p className="pg-chat-hint">
          Record a few seconds, or pick an audio/video file — the words appear as they are
          decoded.
        </p>
      )}
      <div className="pg-toolbar">
        <button type="button" className="pg-adv-toggle" onClick={() => setAdvanced((v) => !v)}>
          Advanced {advanced ? "▴" : "▾"}
        </button>
      </div>
      {advanced && (
        <div className="pg-advanced">
          <label>
            Task
            <select value={task} onChange={(e) => setTask(e.target.value as "transcribe" | "translate")}>
              <option value="transcribe">Transcribe (same language)</option>
              <option value="translate">Translate into English</option>
            </select>
          </label>
          <label>
            Language
            <input
              type="text"
              value={language}
              placeholder="Blank — detect it"
              onChange={(e) => setLanguage(e.target.value)}
            />
          </label>
          <label className="pg-adv-check">
            <input type="checkbox" checked={vad} onChange={(e) => setVad(e.target.checked)} />
            Skip silence (VAD)
          </label>
          <label className="pg-adv-check">
            <input type="checkbox" checked={words} onChange={(e) => setWords(e.target.checked)} />
            Word timestamps
          </label>
        </div>
      )}
    </div>
  );
}
