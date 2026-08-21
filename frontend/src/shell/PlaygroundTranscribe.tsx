// The transcription stage: record or drop a recording, get the words (AI-10).
//
// The research on record-and-transcribe UX was unambiguous about three things,
// all here: the three states (idle → recording → transcribing) each get their
// own visible treatment; a LIVE LEVEL METER while recording, because the user
// must see "it hears me" before they will talk to it; and words that stream in
// rather than a spinner — the worker appends each segment to a partial JSONL
// beside the output (runners/partial.py) and this stage tails it.
//
// `POST /api/ai/transcribe` takes a PATH — the transcript is a file and the
// run outlives the page on purpose — so both inputs land bytes on disk first
// through `POST /api/fs/upload`, into ~/recordings. A browser file picker has
// no path to give; the upload is the door, not a workaround. Only the WATCH
// stops on unmount: the run itself is a job, visible in Activity.
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
import { RailSection } from "./PlaygroundControls";
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
  const [railOpen, setRailOpen] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [level, setLevel] = useState(0);
  const [dragging, setDragging] = useState(false);

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
  const meterRef = useRef<{ ctx: AudioContext; raf: number } | null>(null);

  const stopMeter = () => {
    if (meterRef.current) {
      cancelAnimationFrame(meterRef.current.raf);
      void meterRef.current.ctx.close().catch(() => {});
      meterRef.current = null;
    }
    setLevel(0);
  };

  useEffect(
    () => () => {
      abortRef.current?.abort();
      recorderRef.current?.stream.getTracks().forEach((t) => t.stop());
      stopMeter();
    },
    [],
  );

  // The elapsed counter while recording — the second half of "it hears me".
  useEffect(() => {
    if (phase.step !== "recording") return;
    setElapsed(0);
    const started = Date.now();
    const timer = window.setInterval(() => setElapsed((Date.now() - started) / 1000), 250);
    return () => window.clearInterval(timer);
  }, [phase.step]);

  // Tail the partial transcript while the run is live.
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
        setPhase((p) =>
          p.step === "running" && p.started.jobId === started.jobId ? { ...p, job } : p,
        ),
      );
    } catch (e) {
      if ((e as Error).name === "AbortError") return;
      setError((e as Error).message);
      setPhase({ step: "idle" });
      return;
    }
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
        stopMeter();
        recorderRef.current = null;
        const blob = new Blob(chunksRef.current, { type: recorder.mimeType || "audio/webm" });
        const ext = (recorder.mimeType || "audio/webm").includes("mp4") ? "m4a" : "webm";
        void land(blob, `mic.${ext}`);
      };
      // The level meter: an analyser on the same stream, RMS of one frame,
      // painted ~60/s. Cheap, and the one thing that proves the mic is live.
      const ctx = new AudioContext();
      const source = ctx.createMediaStreamSource(stream);
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 512;
      source.connect(analyser);
      const bytes = new Uint8Array(analyser.frequencyBinCount);
      const paint = () => {
        analyser.getByteTimeDomainData(bytes);
        let sum = 0;
        for (const b of bytes) {
          const centered = (b - 128) / 128;
          sum += centered * centered;
        }
        setLevel(Math.min(1, Math.sqrt(sum / bytes.length) * 3));
        if (meterRef.current) meterRef.current.raf = requestAnimationFrame(paint);
      };
      meterRef.current = { ctx, raf: requestAnimationFrame(paint) };
      recorder.start();
      setPhase({ step: "recording" });
    } catch (e) {
      setError(
        (e as Error).name === "NotAllowedError"
          ? "Microphone access was refused — allow it in the browser and try again."
          : (e as Error).message,
      );
    }
  };

  const busy = phase.step === "uploading" || phase.step === "running";
  const job = phase.step === "running" ? phase.job : null;
  const progress =
    job && job.unit === "s" && job.total
      ? `${clock(job.done ?? 0)} of ${clock(job.total)} transcribed`
      : job?.detail || (phase.step === "running" ? "Starting — a cold model loads first…" : null);
  const pct = job && job.unit === "s" && job.total ? Math.min(100, ((job.done ?? 0) / job.total) * 100) : null;
  const finalText = () =>
    (phase.step === "done" && phase.text.trim()) ||
    segments.map((s) => s.text ?? "").join(" ").trim();

  return (
    <div className={"pg-work" + (railOpen ? " rail-open" : "")}>
      <div className="pg-main pg-transcribe">
        {phase.step === "recording" ? (
          <div className="pg-recording">
            <button type="button" className="pg-rec-btn live" onClick={() => recorderRef.current?.stop()}>
              <span className="pg-rec-square" />
            </button>
            <div className="pg-rec-info">
              <span className="pg-rec-time">{clock(elapsed)}</span>
              <span className="pg-meter" aria-hidden="true">
                {Array.from({ length: 12 }, (_, i) => (
                  <span
                    key={i}
                    className={"pg-meter-bar" + (level * 12 > i ? " lit" : "")}
                  />
                ))}
              </span>
              <span className="pg-rec-hint">Recording — click to stop and transcribe</span>
            </div>
          </div>
        ) : (
          <div
            className={"pg-dropzone" + (dragging ? " dragging" : "") + (busy ? " busy" : "")}
            onDragOver={(e) => {
              e.preventDefault();
              if (!busy) setDragging(true);
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={(e) => {
              e.preventDefault();
              setDragging(false);
              const file = e.dataTransfer.files?.[0];
              if (file && !busy) void land(file, file.name);
            }}
          >
            <button type="button" className="pg-rec-btn" disabled={busy} onClick={() => void record()} title="Record from the microphone">
              <span className="pg-rec-dot" />
            </button>
            <div className="pg-drop-copy">
              <p className="pg-drop-title">
                {busy ? "Working…" : "Record, or drop an audio / video file"}
              </p>
              <p className="pg-drop-sub">
                {busy ? (
                  phase.step === "uploading" ? (
                    `Saving ${phase.name}…`
                  ) : (
                    progress
                  )
                ) : (
                  <>
                    …or{" "}
                    <label className="pg-browse">
                      browse for one
                      <input
                        type="file"
                        accept="audio/*,video/*"
                        onChange={(e) => {
                          const file = e.target.files?.[0];
                          e.target.value = "";
                          if (file) void land(file, file.name);
                        }}
                      />
                    </label>{" "}
                    — the words appear as they are decoded.
                  </>
                )}
              </p>
              {pct !== null && (
                <span className="pg-bar">
                  <span className="pg-bar-fill" style={{ width: `${pct}%` }} />
                </span>
              )}
            </div>
            {phase.step === "running" && (
              <button
                type="button"
                className="btn btn-secondary"
                onClick={() => void cancelJob(phase.started.jobId).catch(() => {})}
              >
                Stop
              </button>
            )}
          </div>
        )}
        {error && <p className="pg-error">{error}</p>}

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

        <div className="pg-under">
          <button type="button" className="pg-ghost-btn pg-rail-toggle" onClick={() => setRailOpen((v) => !v)}>
            {railOpen ? "Hide controls" : "Controls"}
          </button>
          {phase.step === "done" && (
            <button
              type="button"
              className="pg-ghost-btn"
              onClick={(e) => {
                void navigator.clipboard.writeText(finalText());
                const button = e.currentTarget;
                button.textContent = "Copied";
                window.setTimeout(() => {
                  button.textContent = "Copy transcript";
                }, 1200);
              }}
            >
              Copy transcript
            </button>
          )}
        </div>
      </div>

      <aside className="pg-rail" aria-label="Transcription settings">
        <RailSection title="Output">
          <label className="pg-ctl">
            <span className="pg-ctl-head">
              <span className="pg-ctl-label">Task</span>
            </span>
            <select
              className="pg-rail-input"
              value={task}
              onChange={(e) => setTask(e.target.value as "transcribe" | "translate")}
            >
              <option value="transcribe">Transcribe — same language</option>
              <option value="translate">Translate into English</option>
            </select>
          </label>
          <label className="pg-ctl">
            <span className="pg-ctl-head">
              <span className="pg-ctl-label">Language</span>
            </span>
            <input
              className="pg-rail-input"
              type="text"
              value={language}
              placeholder="Detected automatically"
              onChange={(e) => setLanguage(e.target.value)}
            />
            <span className="pg-ctl-hint">Set it only when detection gets it wrong.</span>
          </label>
        </RailSection>
        <RailSection title="Decoding">
          <label className="pg-ctl pg-ctl-row">
            <input type="checkbox" checked={vad} onChange={(e) => setVad(e.target.checked)} />
            <span>
              <span className="pg-ctl-label">Skip silence</span>
              <span className="pg-ctl-hint">Much faster on recordings with gaps. Turn off if it clips speech.</span>
            </span>
          </label>
          <label className="pg-ctl pg-ctl-row">
            <input type="checkbox" checked={words} onChange={(e) => setWords(e.target.checked)} />
            <span>
              <span className="pg-ctl-label">Word timestamps</span>
              <span className="pg-ctl-hint">Per-word timings in the saved transcript. Slower.</span>
            </span>
          </label>
        </RailSection>
      </aside>
    </div>
  );
}
