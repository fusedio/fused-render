// The voice chat stage (Moshi, `voice-to-voice`, SPEC AI-33): one button to
// start talking, one to hang up, and the conversation as it happens.
//
// Full duplex, so there is no turn to take: the mic is open for the whole
// call and Moshi answers while you are still speaking. The stage shows the
// two facts a caller needs — that it hears you (a mic meter) and that it is
// speaking (a speaker meter) — plus the words Moshi says, which the model
// emits as text a beat before it says them aloud. The clock runs against
// the session cap, after which the worker ends the call on its own.
import { useEffect, useRef, useState } from "react";
import { Card } from "@platform/shadcn/ui/card";
import type { AiCatalogModel } from "@platform/lib/api";
import { withModelReady } from "./client";
import { ConfigPanel, useConfigOpen, RailField, RailSelect, StageHeader } from "./controls";
import { readParam, writeParams } from "@apps/ai_models/lib/params";
import { startVoice, VoiceCall, type VoiceResult, type VoiceStarted } from "./voice";

const CAPS = [
  { value: "120", label: "2 minutes" },
  { value: "300", label: "5 minutes" },
  { value: "600", label: "10 minutes" },
];

function clock(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function Meter({ level, label }: { level: number; label: string }) {
  return (
    <span className="pg-voice-meter" role="img" aria-label={`${label} level`}>
      <span className="pg-meter" aria-hidden="true">
        {Array.from({ length: 12 }, (_, i) => (
          <span key={i} className={"pg-meter-bar" + (level * 12 > i ? " lit" : "")} />
        ))}
      </span>
      <span className="pg-voice-meter-label">{label}</span>
    </span>
  );
}

type Phase =
  | { step: "idle" }
  | { step: "starting" }
  | { step: "live"; started: VoiceStarted }
  | { step: "ended"; result: VoiceResult | null; error?: string };

export function VoiceStage({ model, entry }: { model: string; entry: AiCatalogModel }) {
  const [cap, setCap] = useState(() => readParam("cap") ?? "300");
  const { open: configOpen, toggle: toggleConfig, touched: configTouched } = useConfigOpen();
  const [phase, setPhase] = useState<Phase>({ step: "idle" });
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [words, setWords] = useState("");
  const [levels, setLevels] = useState({ mic: 0, speaker: 0 });
  const [elapsed, setElapsed] = useState(0);

  const callRef = useRef<VoiceCall | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const aliveRef = useRef(true);
  const wordsRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
      abortRef.current?.abort();
      callRef.current?.hangUp();
      callRef.current = null;
    };
  }, []);

  useEffect(() => {
    writeParams({ cap: cap !== "300" ? cap : null });
  }, [cap]);

  useEffect(() => {
    if (phase.step !== "live") return;
    setElapsed(0);
    const started = Date.now();
    const timer = window.setInterval(() => setElapsed((Date.now() - started) / 1000), 250);
    return () => window.clearInterval(timer);
  }, [phase.step]);

  useEffect(() => {
    const box = wordsRef.current;
    if (box) box.scrollTop = box.scrollHeight;
  }, [words]);

  const start = async () => {
    if (phase.step === "starting" || phase.step === "live") return;
    setError(null);
    setWords("");
    setLevels({ mic: 0, speaker: 0 });
    setPhase({ step: "starting" });
    const controller = new AbortController();
    abortRef.current = controller;
    try {
      const started = await withModelReady(
        () => startVoice({ model, maxSeconds: Number(cap) }),
        { signal: controller.signal, downloaded: entry.downloaded, onStatus: setStatus },
      );
      if (!aliveRef.current || controller.signal.aborted) return;
      setStatus("Opening the microphone…");
      const call = await VoiceCall.open(started, {
        onText: (piece) => {
          if (aliveRef.current) setWords((w) => w + piece);
        },
        onLevel: (mic, speaker) => {
          if (aliveRef.current) setLevels({ mic, speaker });
        },
        onEnd: (result, why) => {
          callRef.current = null;
          if (!aliveRef.current) return;
          setLevels({ mic: 0, speaker: 0 });
          setPhase({ step: "ended", result, error: why });
          if (why) setError(why);
        },
      });
      if (!aliveRef.current) {
        call.hangUp();
        return;
      }
      callRef.current = call;
      setPhase({ step: "live", started });
    } catch (e) {
      if (!aliveRef.current) return;
      if ((e as Error).name === "AbortError") {
        setPhase({ step: "idle" });
        return;
      }
      const err = e as Error;
      setError(
        err.name === "NotAllowedError"
          ? "The microphone was refused. Allow it for this app and try again."
          : err.message,
      );
      setPhase({ step: "idle" });
    } finally {
      if (aliveRef.current) setStatus(null);
      abortRef.current = null;
    }
  };

  const hangUp = () => {
    abortRef.current?.abort();
    callRef.current?.hangUp();
  };

  const live = phase.step === "live";
  const capSeconds = Number(cap);
  const outcome = phase.step === "ended" ? phase : null;

  return (
    <div className={"pg-work" + (configOpen ? " has-config" : "")}>
      <Card className="pg-work-card flex-none gap-3 px-(--card-spacing) [--card-spacing:--spacing(6)]">
        <StageHeader title="Talk with it" configOpen={configOpen} onToggleConfig={toggleConfig} />

        <div className={"pg-voice-call" + (live ? " live" : "")}>
          {live ? (
            <button type="button" className="pg-rec-btn live" title="Hang up" onClick={hangUp}>
              <span className="pg-rec-square" />
            </button>
          ) : (
            <button
              type="button"
              className="pg-rec-btn"
              title="Start talking"
              disabled={phase.step === "starting"}
              onClick={() => void start()}
            >
              <span className="pg-rec-dot" />
            </button>
          )}
          <div className="pg-rec-info">
            {live ? (
              <>
                <span className="pg-rec-time">{clock(elapsed)} / {clock(capSeconds)}</span>
                <Meter level={levels.mic} label="You" />
                <Meter level={levels.speaker} label="Moshi" />
                <span className="pg-rec-hint">Just talk — it listens while it speaks. Click to hang up.</span>
              </>
            ) : phase.step === "starting" ? (
              <span className="pg-rec-hint">{status || "Starting…"}</span>
            ) : (
              <span className="pg-rec-hint">
                Click to start a conversation. Headphones keep it from hearing itself.
              </span>
            )}
          </div>
        </div>

        <ConfigPanel open={configOpen} animated={configTouched.current}>
          <RailField label="Call length" hint="The call ends on its own at this length.">
            <RailSelect aria-label="Call length" value={cap} onValueChange={setCap} options={CAPS} />
          </RailField>
        </ConfigPanel>

        {error && <p className="pg-error">{error}</p>}

        {(live || outcome || words) && (
          <div className="pg-answer-block">
            <p className="pg-answer-label">
              What it said
              {outcome?.result && (
                <span className="pg-voice-outcome">
                  {clock(outcome.result.seconds)} ·{" "}
                  {outcome.result.finishReason === "length" ? "reached the call length" : "ended"}
                </span>
              )}
            </p>
            <div ref={wordsRef} className="pg-voice-words" aria-live="polite">
              {words || (live ? "Listening…" : "Nothing yet.")}
            </div>
          </div>
        )}
      </Card>
    </div>
  );
}
