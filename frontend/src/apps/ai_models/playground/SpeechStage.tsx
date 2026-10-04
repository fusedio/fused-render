import { useEffect, useRef, useState } from "react";
import { cancelJob, type Job } from "@platform/lib/jobs";
import { pickFile, rawUrl, type AiCatalogModel } from "@platform/lib/api";
import { startSpeech, watchJob, type SpeechRequest, type SpeechStarted } from "./client";
import { Input } from "@platform/shadcn/ui/input";
import { Textarea } from "@platform/shadcn/ui/textarea";
import { Card } from "@platform/shadcn/ui/card";
import {
  ConfigPanel,
  useConfigOpen,
  RailField,
  RailSelect,
  ResultSlot,
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
  instruct: string,
): string | null {
  if (!text.trim()) return "Type the text to speak.";
  if (mode === "clone" && !refAudio) return "Add a voice clip to copy.";
  if (mode === "clone" && !refText.trim()) return "Type the words that the voice clip says.";
  if (mode === "design" && !instruct.trim()) return "Describe the voice.";
  return null;
}

interface Run {
  started: SpeechStarted;
  job: Job | null;
  done: boolean;
}

export function SpeechStage({ model, entry }: { model: string; entry: AiCatalogModel }) {
  const mode: VoiceMode = entry.voiceMode ?? "preset";
  const voices = entry.voices ?? [];
  const languages = entry.languages ?? [];

  const [text, setText] = useState(() => readParam("prompt") ?? "");
  const [voice, setVoice] = useState(() => readParam("voice") ?? "");
  const [language, setLanguage] = useState(() => readParam("lang") ?? AUTO);
  const [instruct, setInstruct] = useState("");
  const [refAudio, setRefAudio] = useState<string | null>(() => readParam("ref"));
  const [refText, setRefText] = useState(() => readParam("reftext") ?? "");
  const { open: configOpen, toggle: toggleConfig, touched: configTouched } = useConfigOpen();
  const [run, setRun] = useState<Run | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [picking, setPicking] = useState(false);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      writeParams({
        prompt: text ? text : null,
        voice: mode === "preset" && voice ? voice : null,
        lang: language !== AUTO ? language : null,
        ref: mode === "clone" && refAudio ? refAudio : null,
        reftext: mode === "clone" && refText.trim() ? refText : null,
      });
    }, 300);
    return () => window.clearTimeout(timer);
  }, [text, voice, language, refAudio, refText, mode]);

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
      if (path !== null && aliveRef.current) setRefAudio(path);
    } catch (e) {
      if (aliveRef.current) setError((e as Error).message);
    } finally {
      if (aliveRef.current) setPicking(false);
    }
  };

  const generate = async (sample?: SpeechSample) => {
    if (run && !run.done) return;
    const wanted = (sample?.prompt ?? text).trim();
    const style = mode === "design" && sample && !instruct.trim() ? sample.voice : instruct;
    if (sample) setText(sample.prompt);
    if (style !== instruct) setInstruct(style);
    if (missingFor(mode, wanted, refAudio, refText, style)) return;
    setError(null);
    const request: SpeechRequest = {
      text: wanted,
      model,
      ...(language !== AUTO ? { language } : {}),
      ...(mode === "preset" && voice ? { voice } : {}),
      ...(mode !== "clone" && style.trim() ? { instruct: style.trim() } : {}),
      ...(mode === "clone" && refAudio ? { refAudio, refText: refText.trim() } : {}),
    };
    try {
      const controller = new AbortController();
      abortRef.current = controller;
      const started = await startSpeech(request);
      setRun({ started, job: null, done: false });
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
      setError((e as Error).message);
    }
  };

  const clear = () => {
    setText("");
    setRun(null);
    setError(null);
    boxRef.current?.focus();
  };

  const busy = !!run && !run.done;
  const job = busy ? run.job : null;
  const pct = job && job.total ? Math.min(100, ((job.done ?? 0) / job.total) * 100) : null;
  const settled = run?.started;
  const missing = missingFor(mode, text, refAudio, refText, instruct);
  const refName = refAudio ? refAudio.split("/").pop() || refAudio : null;

  const voiceLine = settled?.voice
    ? nameOf(settled.voice)
    : { preset: "Default voice", clone: "Copied voice", design: "Designed voice" }[mode];

  return (
    <div className={"pg-work" + (configOpen ? " has-config" : "")}>
      <Card className="pg-work-card flex-none gap-3 px-(--card-spacing) [--card-spacing:--spacing(6)]">
        <StageHeader title="Type text to speak" configOpen={configOpen} onToggleConfig={toggleConfig} />

        <div className="pg-composer">
          <textarea
            ref={boxRef}
            rows={3}
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
            {busy ? (
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

        <div className="pg-speech-voice">
          {mode === "preset" && (
            <>
              <RailField
                label="Voice"
                hint={voices.length === 0 ? "The voice list shows after the download." : undefined}
              >
                <RailSelect value={voice} onChange={(e) => setVoice(e.target.value)}>
                  <option value="">Default</option>
                  {voices.map((v) => (
                    <option key={v} value={v}>
                      {nameOf(v)}
                    </option>
                  ))}
                  {voice && !voices.includes(voice) && <option value={voice}>{nameOf(voice)}</option>}
                </RailSelect>
              </RailField>
              <RailField label="Style" hint="Optional. Tell the voice how to speak.">
                <Input
                  type="text"
                  value={instruct}
                  placeholder="For example: speak slowly and calmly"
                  onChange={(e) => setInstruct(e.target.value)}
                />
              </RailField>
            </>
          )}
          {mode === "clone" && (
            <>
              <RailField
                label="Voice clip"
                hint="A clear clip of one person, 10 to 30 seconds long."
              >
                {refAudio ? (
                  <div className="pg-audio-row">
                    <div className="pg-audio-meta">
                      <span className="pg-audio-label">Voice to copy</span>
                      <span className="pg-audio-name" title={refAudio}>
                        {refName}
                      </span>
                    </div>
                    <audio className="pg-audio" controls preload="metadata" src={rawUrl(refAudio)} />
                    <button
                      type="button"
                      className="pg-ghost-btn"
                      disabled={picking}
                      onClick={() => void chooseClip()}
                    >
                      Replace
                    </button>
                    <button
                      type="button"
                      className="pg-ghost-btn"
                      title="Remove this clip"
                      onClick={() => setRefAudio(null)}
                    >
                      Remove
                    </button>
                  </div>
                ) : (
                  <div className="pg-attach-row">
                    <button
                      type="button"
                      className="pg-attach-btn"
                      title="Point at an audio file on this disk. Nothing is copied."
                      disabled={picking}
                      onClick={() => void chooseClip()}
                    >
                      {StarterIcons.music}
                      <span>Add a voice clip</span>
                    </button>
                    {picking && <span className="pg-attach-note">Working…</span>}
                  </div>
                )}
              </RailField>
              <RailField label="Clip words" hint="Type the exact words that the clip says.">
                <Textarea
                  rows={2}
                  value={refText}
                  placeholder="The words in the voice clip"
                  onChange={(e) => setRefText(e.target.value)}
                />
              </RailField>
            </>
          )}
          {mode === "design" && (
            <RailField
              label="Voice description"
              hint="Say who speaks and how: age, tone, pace, accent."
            >
              <Textarea
                rows={2}
                value={instruct}
                placeholder="For example: a calm older man with a deep, warm voice"
                onChange={(e) => setInstruct(e.target.value)}
              />
            </RailField>
          )}
        </div>

        <ConfigPanel open={configOpen} animated={configTouched.current}>
          <RailField label="Language" hint="Auto finds the language from the text.">
            <RailSelect value={language} onChange={(e) => setLanguage(e.target.value)}>
              <option value={AUTO}>Auto</option>
              {languages.map((l) => (
                <option key={l} value={l}>
                  {nameOf(l)}
                </option>
              ))}
              {language !== AUTO && !languages.includes(language) && (
                <option value={language}>{nameOf(language)}</option>
              )}
            </RailSelect>
          </RailField>
        </ConfigPanel>

        {!run && <StarterCards samples={STARTERS} onPick={(s) => void generate(s)} />}

        {error && <p className="pg-error">{error}</p>}

        {!run ? (
          <ResultSlot
            label="Result"
            capability="text-to-speech"
            note="Your audio shows here. Type some text above, then Speak."
          />
        ) : (
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
