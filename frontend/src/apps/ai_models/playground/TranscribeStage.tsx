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
} from "./client";
import { ConfigPanel, CopyButton, StageHeader, StarterCards, type Starter } from "./controls";
import { StarterIcons } from "./starterIcons";
import { readParam, writeParams } from "@apps/ai_models/lib/params";

// The examples, for a stage whose input is a microphone (D451). A prompt here
// is a SCRIPT: the words to say into it. Picking one only puts the line on
// screen — it never starts recording, because a click that opens the mic
// permission prompt is a click that asked for something else.
//
// Chosen for the things transcription actually gets wrong: proper nouns,
// digits and dates, library names, unfamiliar first names, and one pangram for
// plain coverage. A demo that reads "hello world" tells the reader nothing.
const STARTERS: Starter[] = [
  {
    name: "The pangram",
    icon: StarterIcons.pen,
    prompt:
      "The quick brown fox jumps over the lazy dog — then it stops, turns around, and does it " +
      "again just to be sure.",
  },
  {
    name: "Dates and numbers",
    icon: StarterIcons.chart,
    prompt:
      "Ship it on March the third at nine forty-five in the morning, invoice number four eight " +
      "two seven, total two thousand and sixteen dollars.",
  },
  {
    name: "An address",
    icon: StarterIcons.map,
    prompt:
      "The office moved to twelve Bishopsgate, third floor, London EC2N 4AJ — the entrance is " +
      "round the back, past the bike racks.",
  },
  {
    name: "Tongue twister",
    icon: StarterIcons.music,
    prompt:
      "She sells sea shells by the sea shore, and the shells she sells are surely sea shells, " +
      "so if she sells shells on the sea shore, the shells are sea shore shells.",
  },
  {
    name: "Library names",
    icon: StarterIcons.code,
    prompt:
      "Kubernetes, PostgreSQL, WebAssembly, quantisation and tokeniser — five words a " +
      "transcript almost always gets wrong the first time.",
  },
  {
    name: "A meeting note",
    icon: StarterIcons.mail,
    prompt:
      "We agreed to cut the third milestone, move the review to Tuesday, and let Priya own the " +
      "migration until the end of the quarter.",
  },
  {
    name: "A grocery list",
    icon: StarterIcons.bowl,
    prompt:
      "Two litres of milk, a loaf of rye, four tomatoes, olive oil, a jar of harissa, and " +
      "whatever apples look least sad.",
  },
  {
    name: "Names, five ways",
    icon: StarterIcons.globe,
    prompt:
      "Siobhan, Xiaoming, Kwame, Aleksandr and Yuki all joined the call from four different " +
      "time zones this morning.",
  },
];

type Phase =
  | { step: "idle" }
  | { step: "recording" }
  | { step: "uploading"; name: string }
  | { step: "running"; started: TranscribeStarted; job: Job | null }
  | { step: "done"; started: TranscribeStarted; text: string; readFailed?: boolean };

function clock(seconds: number | undefined): string {
  if (seconds === undefined || !isFinite(seconds)) return "";
  const s = Math.max(0, Math.floor(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

export function TranscribeStage({ model }: { model: string }) {
  const [phase, setPhase] = useState<Phase>({ step: "idle" });
  // The audio the run heard, as its server path. In the URL (`src`) on
  // purpose: this stage is keyed by model id (AiModelsPlayground), so picking
  // another model REMOUNTS it — and "same recording, different model" is
  // exactly the comparison a playground should make effortless. The param
  // survives the remount; the player and the Transcribe button come back.
  const [source, setSource] = useState<{ path: string; name: string } | null>(() => {
    const path = readParam("src");
    return path ? { path, name: path.split("/").pop() ?? path } : null;
  });
  const [segments, setSegments] = useState<TranscriptSegment[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [elapsed, setElapsed] = useState(0);
  const [level, setLevel] = useState(0);
  const [dragging, setDragging] = useState(false);
  const [configOpen, setConfigOpen] = useState(false);
  // The picked script, shown above the record button so it can be read off the
  // screen while recording. Session state, never in the URL: it is what the
  // reader is about to say, not part of the run's setup.
  const [script, setScript] = useState<string | null>(null);

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
        src: source ? source.path : null,
      });
    }, 300);
    return () => window.clearTimeout(timer);
  }, [task, language, vad, words, source]);

  const abortRef = useRef<AbortController | null>(null);
  // `land()` awaits the config, the mkdir and the upload before it starts a
  // job at all, so an unmount inside that window runs the cleanup below while
  // the continuation is still queued — it would then start a watch against a
  // controller the cleanup has already come and gone for, leaking a 1/s poll
  // from a dead component. The flag is what the continuation checks.
  const aliveRef = useRef(true);
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

  useEffect(() => {
    // Set on the way IN as well as cleared on the way out. The app does not
    // mount under StrictMode today, but its dev double-mount reuses the same
    // instance and its refs — a flag only ever cleared would latch false on
    // the simulated unmount and kill transcription for the rest of the
    // session. Two other modules here already guard that double invocation.
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
      abortRef.current?.abort();
      recorderRef.current?.stream.getTracks().forEach((t) => t.stop());
      stopMeter();
    };
  }, []);

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
    if (!aliveRef.current) return;
    // Published BEFORE the first await, for the same reason ImageStage does
    // it: an unmount during this POST used to leave the ref null, so nothing
    // aborted the watch that the continuation went on to start.
    const controller = new AbortController();
    abortRef.current = controller;
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
    try {
      const outcome = await watchJob(started.jobId, controller.signal, (job) =>
        setPhase((p) =>
          p.step === "running" && p.started.jobId === started.jobId ? { ...p, job } : p,
        ),
      );
      // Stop was pressed. Falling through would read back two artefacts that
      // were never written and then report "the transcript could not be read
      // back — it is saved in the transcripts folder", which is a lie about a
      // run the user themselves killed.
      if (outcome.state === "cancelled") {
        setPhase({ step: "idle" });
        return;
      }
    } catch (e) {
      if ((e as Error).name === "AbortError") return;
      setError((e as Error).message);
      setPhase({ step: "idle" });
      return;
    }
    // The final words come from the final `.json` — NOT another read of the
    // partial file: the Sink DELETES the partial on a clean exit (a finished
    // run's partial is duplicate bytes, its docstring says so), so on a short
    // clip that finishes before the first tail tick the partial never renders
    // and a re-read here finds nothing. The settled record has the same
    // segments, plus the joined text.
    try {
      const res = await fetch(rawUrl(started.output) + "&t=" + Date.now());
      if (res.ok) {
        const record = (await res.json()) as { segments?: TranscriptSegment[]; text?: string };
        if (Array.isArray(record.segments) && record.segments.length) {
          setSegments(record.segments);
        }
        setPhase({
          step: "done",
          started,
          text: typeof record.text === "string" ? record.text : "",
        });
        return;
      }
    } catch {
      // Fall through to the .txt below.
    }
    try {
      const res = await fetch(rawUrl(started.outputText) + "&t=" + Date.now());
      if (res.ok) {
        setPhase({ step: "done", started, text: await res.text() });
        return;
      }
    } catch {
      // Both artefacts unreadable — say THAT, below, never "no speech":
      // a failed read is a fact about the read, not about the audio.
    }
    setPhase({ step: "done", started, text: "", readFailed: true });
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
      setSource({ path, name: safe });
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

  const clear = () => {
    setSource(null);
    setSegments([]);
    setError(null);
    setScript(null);
    setPhase({ step: "idle" });
  };

  return (
    <div className={"pg-work" + (configOpen ? " has-config" : "")}>
        {/* The action, and the way to the settings. The hero card above names
            the model and its state. */}
        <StageHeader
          title="Transcribe a recording"
          configOpen={configOpen}
          onToggleConfig={() => setConfigOpen((open) => !open)}
        />

        {/* The picked script, above the record button rather than below it: it
            has to be readable while the mic is live, and the recording panel
            replaces the dropzone in place. Kept through the run, dropped once
            there is a transcript to compare it against — at that point the
            words are on screen twice and only one of them is the result. */}
        {script && phase.step !== "done" && (
          <p className="pg-script">
            <span className="pg-script-label">Read this aloud</span>
            {script}
          </p>
        )}

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

        {/* Something to say, for a reader who came to hear the model and has no
            recording to hand. Idle only: mid-run the row would offer a second
            script while the first is being transcribed. */}
        {phase.step === "idle" && !segments.length && (
          <StarterCards samples={STARTERS} onPick={(sample) => setScript(sample.prompt)} />
        )}

        <ConfigPanel open={configOpen}>
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
        </ConfigPanel>

        {error && <p className="pg-error">{error}</p>}

        {source && phase.step !== "recording" && (
          // The recording itself, playable — hearing what the model heard is
          // how a surprising transcript stops being a mystery. And because the
          // path rides the URL, this row is also the compare loop: pick
          // another model in the sidebar and the same recording is one click
          // from a fresh run.
          <div className="pg-audio-row">
            <div className="pg-audio-meta">
              <span className="pg-audio-label">What the model hears</span>
              <span className="pg-audio-name">{source.name}</span>
            </div>
            <audio className="pg-audio" controls preload="metadata" src={rawUrl(source.path)} />
            {!busy && (
              <button
                type="button"
                className="btn btn-secondary"
                onClick={() => {
                  setError(null);
                  void transcribePath(source.path).catch((e: Error) => {
                    setError(e.message);
                    setPhase({ step: "idle" });
                  });
                }}
              >
                {phase.step === "done" ? "Transcribe again" : "Transcribe this recording"}
              </button>
            )}
            {!busy && (
              <button
                type="button"
                className="pg-ghost-btn pg-clear"
                title="Drop this recording and start over"
                onClick={clear}
              >
                Clear
              </button>
            )}
          </div>
        )}

        {(segments.length > 0 || phase.step === "done") && (
          <div className="pg-answer-block">
            <p className="pg-answer-label">Transcript</p>
            <div className="pg-segments">
              {phase.step === "done" && finalText() && (
                <CopyButton text={finalText()} label="Copy the transcript" />
              )}
              {segments.length > 0 ? (
                segments.map((segment, index) => (
                  <div key={index} className="pg-segment">
                    <span className="pg-segment-time">{clock(segment.start)}</span>
                    <span className="pg-segment-text">
                      {segment.speaker ? <strong>{segment.speaker}: </strong> : null}
                      {segment.text}
                    </span>
                  </div>
                ))
              ) : phase.step === "done" && phase.text.trim() ? (
                <p className="pg-transcript-text">{phase.text.trim()}</p>
              ) : phase.step === "done" && phase.readFailed ? (
                // The read failed, not the recording — the file is still saved.
                <p className="pg-transcript-text pg-transcript-empty">
                  The run finished, but the transcript could not be read back — it is saved in
                  the transcripts folder.
                </p>
              ) : (
                <p className="pg-transcript-text pg-transcript-empty">
                  No speech was detected in this recording.
                </p>
              )}
            </div>
          </div>
        )}
    </div>
  );
}
