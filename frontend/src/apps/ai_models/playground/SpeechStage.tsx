import { useEffect, useRef, useState } from "react";
import { cancelJob, type Job } from "@platform/lib/jobs";
import { pickFile, rawUrl, type AiCatalogModel } from "@platform/lib/api";
import { startSpeech, startTranscribe, watchJob, type SpeechRequest, type SpeechStarted } from "./client";
import { Input } from "@platform/shadcn/ui/input";
import { Textarea } from "@platform/shadcn/ui/textarea";
import { Card } from "@platform/shadcn/ui/card";
import {
  ConfigPanel,
  useConfigOpen,
  RailField,
  RailSelect,
  StageHeader,
  StarterCards,
  type Starter,
} from "./controls";
import { useAutoGrow } from "@platform/lib/autoGrow";
import { StarterIcons } from "./starterIcons";
import { readParam, writeParams } from "@apps/ai_models/lib/params";

type VoiceMode = NonNullable<AiCatalogModel["voiceMode"]>;

const REF_TYPES = ["wav", "mp3", "flac", "m4a", "ogg"];

const AUTO = "auto";

interface SpeechSample extends Starter {
  voice: string;
}

const STARTERS: SpeechSample[] = [
  {
    name: "Welcome",
    icon: StarterIcons.sparkle,
    prompt: "Hello, and welcome. This voice was made on this computer. Nothing was sent to the internet.",
    voice: "A warm, friendly woman in her thirties. She speaks clearly and at a calm pace.",
  },
  {
    name: "News",
    icon: StarterIcons.globe,
    prompt:
      "Good evening. Rain will move in from the west tonight. Tomorrow will be cool, with sun in the afternoon.",
    voice: "A news reader with a deep, steady voice. He speaks with a clear and serious tone.",
  },
  {
    name: "Story",
    icon: StarterIcons.book,
    prompt:
      "Once upon a time, a small fox lived at the edge of a big forest. Every night, she looked up at the stars.",
    voice: "A gentle storyteller. She speaks softly and slowly, like a bedtime story.",
  },
  {
    name: "Recipe",
    icon: StarterIcons.bowl,
    prompt: "First, boil the water. Then add the pasta and stir. Cook it for nine minutes.",
    voice: "A cheerful chef with a light accent. He sounds happy and relaxed.",
  },
];

function nameOf(value: string): string {
  return value
    .split(/[_\s-]+/)
    .filter(Boolean)
    .map((w) => w[0].toUpperCase() + w.slice(1))
    .join(" ");
}

function missingFor(
  mode: VoiceMode,
  text: string,
  refAudio: string | null,
  refText: string,
): string | null {
  if (!text.trim()) return "Type the text to speak.";
  if (mode === "clone" && !refAudio) return "Add a voice clip to copy.";
  if (mode === "clone" && !refText.trim()) return "Type the words that the voice clip says.";
  return null;
}

interface Run {
  started: SpeechStarted;
  job: Job | null;
  done: boolean;
}

export function SpeechStage({ model, entry, transcribeModel }: {
  model: string; entry: AiCatalogModel; transcribeModel?: string;
}) {
  const mode: VoiceMode = entry.voiceMode ?? "preset";
  const voices = entry.voices ?? [];
  const languages = entry.languages ?? [];

  const [text, setText] = useState(() => readParam("prompt") ?? STARTERS[0].prompt);
  const [voice, setVoice] = useState(() => readParam("voice") ?? "");
  const [language, setLanguage] = useState(() => readParam("lang") ?? AUTO);
  const [instruct, setInstruct] = useState(() => readParam("instruct") ?? (mode === "design"
    ? STARTERS[0].voice : "Speak clearly, warmly, and at a natural pace."));
  const [refAudio, setRefAudio] = useState<string | null>(() => readParam("ref"));
  const [refText, setRefText] = useState(() => readParam("reftext") ?? "");
  const { open: configOpen, toggle: toggleConfig, touched: configTouched } = useConfigOpen();
  const [run, setRun] = useState<Run | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [picking, setPicking] = useState(false);
  const [starting, setStarting] = useState(false);
  const [transcribing, setTranscribing] = useState(false);
  const [clipNote, setClipNote] = useState<string | null>(null);
  const [showClipWords, setShowClipWords] = useState(false);

  useEffect(() => {
    setTranscribing(false);
    if (mode !== "clone" || !refAudio || refText.trim()) return;
    if (!transcribeModel) {
      setClipNote("Add the clip’s words below, or download a Speech to text model to fill them automatically.");
      setShowClipWords(true);
      return;
    }
    let active = true;
    let jobId: string | null = null;
    const controller = new AbortController();
    setTranscribing(true);
    setClipNote("Reading the clip’s words locally…");
    void (async () => {
      try {
        const started = await startTranscribe({ path: refAudio, model: transcribeModel });
        jobId = started.jobId;
        if (!active) { void cancelJob(jobId).catch(() => {}); return; }
        const outcome = await watchJob(jobId, controller.signal, () => {});
        if (outcome.state === "cancelled") throw new Error("Transcription was stopped.");
        const response = await fetch(rawUrl(started.output), { signal: controller.signal });
        if (!response.ok) throw new Error("The clip’s words could not be read.");
        const record = await response.json() as { text?: string };
        if (!record.text?.trim()) throw new Error("No words were found in this clip.");
        if (active) {
          setRefText(record.text.trim());
          setClipNote(null);
        }
      } catch (e) {
        if (active) {
          setClipNote(`${(e as Error).message} You can enter the words below.`);
          setShowClipWords(true);
        }
      } finally {
        if (active) setTranscribing(false);
      }
    })();
    return () => {
      active = false;
      controller.abort();
      if (jobId) void cancelJob(jobId).catch(() => {});
    };
    // Only a new clip starts transcription; editing its words must not restart it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode, refAudio, transcribeModel]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      writeParams({
        prompt: text ? text : null,
        voice: mode === "preset" && voice ? voice : null,
        lang: language !== AUTO ? language : null,
        ref: mode === "clone" && refAudio ? refAudio : null,
        reftext: mode === "clone" && refText.trim() ? refText : null,
        instruct: mode !== "clone" && instruct.trim() ? instruct : null,
      });
    }, 300);
    return () => window.clearTimeout(timer);
  }, [text, voice, language, refAudio, refText, instruct, mode]);

  const { ref: boxRef } = useAutoGrow(text);

  const abortRef = useRef<AbortController | null>(null);
  const aliveRef = useRef(true);
  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
      abortRef.current?.abort();
    };
  }, []);

  const chooseClip = async () => {
    setError(null);
    setPicking(true);
    try {
      const path = await pickFile({ title: "Choose a voice clip", types: REF_TYPES });
      if (path !== null && path !== refAudio && aliveRef.current) {
        setRefText("");
        setClipNote(null);
        setShowClipWords(false);
        setRefAudio(path);
      }
    } catch (e) {
      if (aliveRef.current) setError((e as Error).message);
    } finally {
      if (aliveRef.current) setPicking(false);
    }
  };

  const generate = async () => {
    if (starting || (run && !run.done) || picking || transcribing) return;
    const wanted = text.trim();
    const style = mode === "design" ? instruct.trim() || STARTERS[0].voice : instruct;
    if (style !== instruct) setInstruct(style);
    const why = missingFor(mode, wanted, refAudio, refText);
    setError(why);
    if (why) return;
    const request: SpeechRequest = {
      text: wanted,
      model,
      ...(language !== AUTO ? { language } : {}),
      ...(mode === "preset" && voice ? { voice } : {}),
      ...(mode !== "clone" && style.trim() ? { instruct: style.trim() } : {}),
      ...(mode === "clone" && refAudio ? { refAudio, refText: refText.trim() } : {}),
    };
    setStarting(true);
    try {
      const controller = new AbortController();
      abortRef.current = controller;
      const started = await startSpeech(request);
      if (!aliveRef.current) return;
      setRun({ started, job: null, done: false });
      setStarting(false);
      try {
        const outcome = await watchJob(started.jobId, controller.signal, (job) =>
          setRun((r) => (r && r.started.jobId === started.jobId ? { ...r, job } : r)),
        );
        if (outcome.state === "cancelled") {
          setRun(null);
          return;
        }
        setRun((r) => (r && r.started.jobId === started.jobId ? { ...r, done: true } : r));
      } catch (e) {
        if ((e as Error).name === "AbortError") return;
        setError((e as Error).message);
        setRun(null);
      }
    } catch (e) {
      if (aliveRef.current) setError((e as Error).message);
    } finally {
      if (aliveRef.current) setStarting(false);
    }
  };

  const clear = () => {
    setText("");
    setRun(null);
    setError(null);
    boxRef.current?.focus();
  };

  const busy = starting || (!!run && !run.done);
  const job = busy ? run?.job : null;
  const pct = job && job.total ? Math.min(100, ((job.done ?? 0) / job.total) * 100) : null;
  const settled = run?.started;
  const missing = picking ? "Choose a voice clip first." : transcribing ? "Reading the clip’s words…" : missingFor(mode, text, refAudio, refText);
  const refName = refAudio ? refAudio.split("/").pop() || refAudio : null;

  const voiceLine = settled?.voice
    ? nameOf(settled.voice)
    : { preset: "Default voice", clone: "Copied voice", design: "Designed voice" }[mode];

  return (
    <div className={"pg-work" + (configOpen ? " has-config" : "")}>
      <Card className="pg-work-card flex-none gap-3 px-(--card-spacing) [--card-spacing:--spacing(6)]">
        <StageHeader title="Make it speak" configOpen={configOpen} onToggleConfig={toggleConfig} />

        <div className="pg-composer">
          <textarea
            ref={boxRef}
            rows={3}
            aria-label="Text to speak"
            value={text}
            placeholder="Type the text to read aloud…"
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                void generate();
              }
            }}
          />
          <div className="pg-composer-side">
            {!busy && run && (
              <button
                type="button"
                className="pg-ghost-btn pg-clear"
                title="Clear the text and the audio"
                onClick={clear}
              >
                Clear
              </button>
            )}
            {starting ? (
              <button type="button" className="btn btn-primary pg-send" disabled>Starting…</button>
            ) : busy && run ? (
              <button
                type="button"
                className="btn btn-secondary pg-send"
                onClick={() => void cancelJob(run.started.jobId).catch(() => {})}
              >
                Stop
              </button>
            ) : (
              <button
                type="button"
                className="btn btn-primary pg-send"
                disabled={missing !== null}
                title={missing ?? "Enter to run · Shift+Enter for a new line"}
                onClick={() => void generate()}
              >
                Speak <kbd className="pg-kbd">⏎</kbd>
              </button>
            )}
          </div>
        </div>

        {mode === "clone" && (
          <div className="pg-speech-voice">
            <div className="pg-speech-choice">
              <div>
                <strong>{refAudio ? "Voice to copy" : "Choose the voice to copy"}</strong>
                <p className="text-muted-foreground text-sm">
                  {refAudio ? refName : "Add a clear recording of one person. We’ll fill in the words for you."}
                </p>
              </div>
              <button type="button" className="btn btn-secondary" disabled={picking || busy}
                onClick={() => void chooseClip()}>
                {picking ? "Choosing…" : refAudio ? "Change clip" : "Choose a clip"}
              </button>
            </div>
            {refAudio && <audio className="pg-speech-audio" controls preload="metadata" src={rawUrl(refAudio)} />}
            {clipNote && <p role="status" className="text-muted-foreground text-sm">{clipNote}</p>}
            {refAudio && (
              <details open={showClipWords} onToggle={(e) => setShowClipWords(e.currentTarget.open)}>
                <summary className="pg-speech-detail">{refText.trim() ? "Review clip words" : "Add clip words"}</summary>
                <Textarea aria-label="Clip words" rows={2} value={refText}
                  disabled={transcribing}
                  placeholder="The words in your recording"
                  onChange={(e) => setRefText(e.target.value)} />
              </details>
            )}
          </div>
        )}
        {mode !== "clone" && (
          <div className="pg-speech-choice">
            <div>
              <strong>{mode === "design" ? "Designed voice" : voice ? nameOf(voice) : voices.length ? nameOf(voices[0]) : "Default voice"}</strong>
              <p className="text-muted-foreground text-sm">
                {mode === "design" ? instruct.trim() || STARTERS[0].voice : instruct.trim() || "Natural delivery"}
              </p>
            </div>
            <button type="button" className="pg-ghost-btn" aria-expanded={configOpen} onClick={toggleConfig}>
              Change voice
            </button>
          </div>
        )}

        <ConfigPanel open={configOpen} animated={configTouched.current}>
          {mode === "preset" && (
            <>
              <RailField label="Voice">
                <RailSelect aria-label="Voice" value={voice} onValueChange={setVoice} options={[
                  { value: "", label: voices.length ? `Default (${nameOf(voices[0])})` : "Default" },
                  ...voices.map((v) => ({ value: v, label: nameOf(v) })),
                  ...(voice && !voices.includes(voice) ? [{ value: voice, label: nameOf(voice) }] : []),
                ]} />
              </RailField>
              <RailField label="Delivery">
                <Input value={instruct} placeholder="Natural delivery" onChange={(e) => setInstruct(e.target.value)} />
              </RailField>
            </>
          )}
          {mode === "design" && (
            <RailField label="Voice description" hint="Already filled in. Make it your own, or leave it for a warm, friendly voice.">
              <Textarea aria-label="Voice description" rows={3} value={instruct}
                placeholder={STARTERS[0].voice} onChange={(e) => setInstruct(e.target.value)} />
            </RailField>
          )}
          <RailField label="Language" hint="Auto finds the language from the text.">
            <RailSelect aria-label="Language" value={language} onValueChange={setLanguage} options={[
              { value: AUTO, label: "Auto" },
              ...languages.map((l) => ({ value: l, label: nameOf(l) })),
              ...(language !== AUTO && !languages.includes(language) ? [{ value: language, label: nameOf(language) }] : []),
            ]} />
          </RailField>
        </ConfigPanel>

        {!busy && <StarterCards samples={STARTERS} onPick={(sample) => {
          setText(sample.prompt);
          setRun(null);
          setError(null);
        }} />}

        {error && <p className="pg-error">{error}</p>}

        {run && (
          <div className="pg-answer-block">
            <p className="pg-answer-label">Result</p>
            <div className="pg-speech-result">
              {run.done ? (
                <audio
                  key={run.started.jobId}
                  className="pg-speech-audio"
                  src={rawUrl(run.started.path) + "&t=" + run.started.jobId}
                  controls
                  autoPlay
                />
              ) : (
                <div className="pg-image-wait pg-speech-wait" aria-hidden="true" />
              )}
              <div className="pg-image-caption">
                {busy ? (
                  <>
                    <span>{job?.detail || "Starting. A cold model loads first…"}</span>
                    {pct !== null && (
                      <span className="pg-bar">
                        <span className="pg-bar-fill" style={{ width: `${pct}%` }} />
                      </span>
                    )}
                  </>
                ) : settled ? (
                  <span>
                    {voiceLine} · {settled.language && settled.language !== AUTO ? nameOf(settled.language) : "Auto"}
                  </span>
                ) : null}
              </div>
            </div>
          </div>
        )}
      </Card>
    </div>
  );
}
