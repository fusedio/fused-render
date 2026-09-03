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
// through `POST /api/fs/upload`, into this stage's own scratch dir under the
// app's cache. Only the WATCH stops on unmount: the run itself is a job,
// visible in Activity.
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
import { Button } from "@platform/shadcn/ui/button";
import { Input } from "@platform/shadcn/ui/input";
import { Card } from "@platform/shadcn/ui/card";
import { Tiny } from "@platform/ui/flow/Typography";
import { bucketBorder, bucketFill } from "@platform/ui/status-colors";
import { cn } from "@platform/lib/utils";
import {
  AnswerBlock,
  AnswerBox,
  ClearButton,
  ConfigPanel,
  CopyButton,
  ProgressBar,
  RailCheck,
  RailField,
  RailSelect,
  ResultSlot,
  StageHeader,
  StatusLine,
  useConfigOpen,
} from "./controls";
import { readParam, writeParams } from "@apps/ai_models/lib/params";

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

const TASKS = [
  { value: "transcribe" as const, label: "Transcribe — same language" },
  { value: "translate" as const, label: "Translate into English" },
];

/** The record button: idle = ring + red dot; live = red ring + square + pulse.
 *  The state IS the treatment — research's "three explicit states" rule. */
function RecordButton({
  live,
  disabled,
  onClick,
}: {
  live?: boolean;
  disabled?: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      className={cn(
        "inline-flex size-13 flex-none cursor-pointer items-center justify-center rounded-full border-2 border-border bg-card hover:border-ring disabled:cursor-default disabled:opacity-50",
        live && cn(bucketBorder.red, "motion-safe:animate-pulse"),
      )}
      disabled={disabled}
      title={live ? undefined : "Record from the microphone"}
      onClick={onClick}
    >
      <span
        className={cn(bucketFill.red, live ? "size-4 rounded-sm" : "size-[18px] rounded-full")}
      />
    </button>
  );
}

export function TranscribeStage({ model }: { model: string }) {
  const [phase, setPhase] = useState<Phase>({ step: "idle" });
  // The audio the run heard, as its server path. In the URL (`src`) on
  // purpose: this stage is keyed by model id, so picking another model
  // REMOUNTS it — and "same recording, different model" is exactly the
  // comparison a playground should make effortless.
  const [source, setSource] = useState<{ path: string; name: string } | null>(() => {
    const path = readParam("src");
    return path ? { path, name: path.split("/").pop() ?? path } : null;
  });
  const [segments, setSegments] = useState<TranscriptSegment[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [elapsed, setElapsed] = useState(0);
  const [level, setLevel] = useState(0);
  const [dragging, setDragging] = useState(false);
  const { open: configOpen, toggle: toggleConfig, touched: configTouched } = useConfigOpen();

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
  // job at all, so an unmount inside that window must stop the continuation
  // from starting a watch from a dead component.
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
    // Set on the way IN as well as cleared on the way out (dev double-mount).
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
    // Published BEFORE the first await, for the same reason ImageStage does it.
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
      // Stop was pressed: the artefacts were never written.
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
    // partial file: the Sink DELETES the partial on a clean exit.
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
      // Both artefacts unreadable — say THAT, below, never "no speech".
    }
    setPhase({ step: "done", started, text: "", readFailed: true });
  };

  // Bytes this stage invented — a mic take, or a dropped file the server has no
  // path for — land in the app's own scratch dir, `<cache>/transcribe-playground`.
  const land = async (data: Blob, name: string) => {
    setError(null);
    setPhase({ step: "uploading", name });
    try {
      const config = await getConfig();
      await mkdir(config.cache_dir).catch(() => {});
      const dir = `${config.cache_dir}/transcribe-playground`;
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
    setPhase({ step: "idle" });
  };

  return (
    <Card className="w-full flex-none gap-3 px-(--card-spacing) [--card-spacing:--spacing(6)]">
      <StageHeader
        title="Transcribe a recording"
        configOpen={configOpen}
        onToggleConfig={toggleConfig}
      />

      {phase.step === "recording" ? (
        <div className={cn("flex items-center gap-4 rounded-lg border-[1.5px] bg-card p-4", bucketBorder.red)}>
          <RecordButton live onClick={() => recorderRef.current?.stop()} />
          <div className="flex flex-wrap items-center gap-3.5">
            <span className="text-base font-semibold tabular-nums">{clock(elapsed)}</span>
            {/* The level meter: 12 bars, lit from the left by the live RMS. */}
            <span className="inline-flex h-5 items-center gap-[3px]" aria-hidden="true">
              {Array.from({ length: 12 }, (_, i) => (
                <span
                  key={i}
                  className={cn(
                    "w-1 rounded-full",
                    level * 12 > i ? cn("h-4", bucketFill.green) : "h-2 bg-muted",
                  )}
                />
              ))}
            </span>
            <Tiny>Recording — click to stop and transcribe</Tiny>
          </div>
        </div>
      ) : (
        <div
          className={cn(
            "pg-composer flex items-center gap-4 rounded-lg border-[1.5px] border-dashed border-border bg-card p-4",
            dragging && "border-ring bg-accent/30",
            busy && "border-solid",
          )}
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
          <RecordButton disabled={busy} onClick={() => void record()} />
          <div className="flex min-w-0 flex-1 flex-col gap-1">
            <p className="m-0 text-sm font-semibold">
              {busy ? "Working…" : "Record, or drop an audio / video file"}
            </p>
            <Tiny className="block">
              {busy ? (
                phase.step === "uploading" ? (
                  `Saving ${phase.name}…`
                ) : (
                  progress
                )
              ) : (
                <>
                  …or{" "}
                  <label className="relative cursor-pointer text-foreground underline decoration-dotted">
                    browse for one
                    <input
                      type="file"
                      className="absolute inset-0 w-full cursor-pointer opacity-0"
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
            </Tiny>
            {pct !== null && <ProgressBar pct={pct} />}
          </div>
          {phase.step === "running" && (
            <Button
              type="button"
              variant="secondary"
              onClick={() => void cancelJob(phase.started.jobId).catch(() => {})}
            >
              Stop
            </Button>
          )}
        </div>
      )}

      <ConfigPanel open={configOpen} animated={configTouched.current}>
        <RailField label="Task">
          <RailSelect label="Task" value={task} options={TASKS} onChange={setTask} />
        </RailField>
        <RailField label="Language" hint="Set it only when detection gets it wrong.">
          <Input
            type="text"
            value={language}
            placeholder="Detected automatically"
            onChange={(e) => setLanguage(e.target.value)}
          />
        </RailField>
        <RailCheck
          label="Skip silence"
          hint="Much faster on recordings with gaps. Turn off if it clips speech."
          checked={vad}
          onChange={setVad}
        />
        <RailCheck
          label="Word timestamps"
          hint="Per-word timings in the saved transcript. Slower."
          checked={words}
          onChange={setWords}
        />
      </ConfigPanel>

      {error && <StatusLine status="error">{error}</StatusLine>}

      {source && phase.step !== "recording" && (
        // The recording itself, playable — hearing what the model heard is how
        // a surprising transcript stops being a mystery. Because the path
        // rides the URL, this row is also the compare loop.
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2.5 rounded-lg border border-border bg-card px-3.5 py-2.5">
          <div className="flex min-w-0 flex-col gap-0.5">
            <Tiny className="font-semibold uppercase tracking-wide">What the model hears</Tiny>
            <Tiny className="max-w-[220px] truncate">{source.name}</Tiny>
          </div>
          <audio
            className="h-9 min-w-[200px] flex-[1_1_240px]"
            controls
            preload="metadata"
            src={rawUrl(source.path)}
          />
          {!busy && (
            <Button
              type="button"
              variant="secondary"
              onClick={() => {
                setError(null);
                void transcribePath(source.path).catch((e: Error) => {
                  setError(e.message);
                  setPhase({ step: "idle" });
                });
              }}
            >
              {phase.step === "done" ? "Transcribe again" : "Transcribe this recording"}
            </Button>
          )}
          {!busy && <ClearButton title="Drop this recording and start over" onClick={clear} />}
        </div>
      )}

      {segments.length === 0 && phase.step !== "done" ? (
        <ResultSlot
          label="Transcript"
          capability="automatic-speech-recognition"
          note="The words come back here, timed — record something above, or pick a file."
        />
      ) : (
        <AnswerBlock label="Transcript" status={phase.step === "running" ? "running" : null}>
          <AnswerBox className="flex min-h-[140px] flex-col gap-1.5 overflow-y-auto py-3">
            {phase.step === "done" && finalText() && (
              <CopyButton text={finalText()} label="Copy the transcript" />
            )}
            {segments.length > 0 ? (
              segments.map((segment, index) => (
                <div key={index} className="flex gap-3 text-sm leading-relaxed">
                  <Tiny className="w-[42px] flex-none pt-0.5 tabular-nums">{clock(segment.start)}</Tiny>
                  <span className="min-w-0">
                    {segment.speaker ? <strong>{segment.speaker}: </strong> : null}
                    {segment.text}
                  </span>
                </div>
              ))
            ) : phase.step === "done" && phase.text.trim() ? (
              <p className="m-0 text-sm leading-relaxed whitespace-pre-wrap">{phase.text.trim()}</p>
            ) : phase.step === "done" && phase.readFailed ? (
              // The read failed, not the recording — the file is still saved.
              <p className="m-0 text-sm leading-relaxed text-muted-foreground">
                The run finished, but the transcript could not be read back — it is saved in
                the transcripts folder.
              </p>
            ) : (
              <p className="m-0 text-sm leading-relaxed text-muted-foreground">
                No speech was detected in this recording.
              </p>
            )}
          </AnswerBox>
        </AnswerBlock>
      )}
    </Card>
  );
}
