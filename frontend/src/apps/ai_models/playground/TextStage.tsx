// The text stage: one prompt in, one reply out (SPEC AI-1a, reshaped).
//
// This USED to be a chat (ChatStage) — bubbles, history, a bottom-anchored
// composer. The playground unification turned every stage into the same
// API-surface shape: a prompt, a Run, the rendered result of THAT run. A
// developer trying a local model wants exactly what one `fused.ai` call
// returns, and a transcript is state the one-call framing has no place for.
// Because the prompt is now setup rather than transcript, it rides the URL
// (`prompt`) the way the image stage's does — the chat rule ("the URL carries
// the setup, never the transcript") reclassified it.
//
// The stream rides `playgroundClient.streamChat`; a model that is not resident
// answers 409 with the job id of the load this send just started (AI-5), and
// this component owns the dance — watch the job, narrate it, retry ONCE.
import { useEffect, useRef, useState } from "react";
import {
  cancelGeneration,
  ModelLoading,
  streamChat,
  watchJob,
  type ChatSettings,
  type ChatUsage,
} from "./client";
import { renderMarkdown } from "./markdown";
import { ConfigPanel, CopyButton, RailSlider, StageHeader, StarterPrompts } from "./controls";
import { numParam, readParam, writeParams } from "@apps/ai_models/lib/params";

// The server's clamps (`_SAMPLING`, server/ai.py), restated on the controls so
// a slider cannot ask for a value the request would 400 on. 1024 max tokens is
// also the WORKER's own default — the slider states the truth of a bare call.
const DEFAULTS = { temperature: 0.7, top_p: 0.95, max_tokens: 1024 };
// The server's `_SAMPLING` bounds, restated because it REJECTS an out-of-range
// value rather than clamping it — so a hand-edited or stale link has to be
// clamped on the way IN, or every run on that link 400s. Keep in step with
// server/ai.py.
const LIMITS = {
  temperature: [0, 2],
  top_p: [0, 1],
  max_tokens: [1, 32768],
} as const;

// A standing system prompt by default, not a blank: small local models drift
// into rambling or page-long <think> blocks without one, and the person this
// tab exists for should meet the model at its best. Kept short and generic —
// it steers verbosity and reasoning length, never persona — and fully
// editable/clearable in the advanced panel (a cleared prompt round-trips as
// `system=`). It also asks for <thinking>, which splitThink lifts into the fold.
const DEFAULT_SYSTEM =
  "You are a helpful assistant answering a single one-off prompt. Answer directly " +
  "in Markdown and keep it short — expand only when asked for detail. If you need " +
  "to reason first, put that reasoning inside a <thinking>...</thinking> tag " +
  "before the answer, and keep it brief.";

const STARTERS = [
  "Explain how a language model predicts the next word, simply",
  "Write a haiku about running AI on a laptop",
  "Draft a short, polite email declining a meeting",
  "Give me three dinner ideas from rice, eggs and spinach",
];

/** Split one reply into the deliberation and the answer. A block still open
 *  (mid-stream) is all deliberation. Both spellings: <think> from
 *  reasoning-tuned models, <thinking> from the default system prompt — longer
 *  tag first, or "<thinking>" parses as "<think>" plus stray text. */
function splitThink(text: string): { think: string | null; answer: string; thinking: boolean } {
  for (const tag of ["thinking", "think"]) {
    const open = text.indexOf(`<${tag}>`);
    if (open < 0) continue;
    const close = text.indexOf(`</${tag}>`);
    if (close < 0) return { think: text.slice(open + tag.length + 2), answer: "", thinking: true };
    return {
      think: text.slice(open + tag.length + 2, close).trim(),
      answer: text.slice(close + tag.length + 3).replace(/^\s+/, ""),
      thinking: false,
    };
  }
  return { think: null, answer: text, thinking: false };
}

function replyStats(usage: ChatUsage | null | undefined): string | null {
  if (!usage?.output_tokens) return null;
  const rate = usage.seconds ? ` · ${(usage.output_tokens / usage.seconds).toFixed(1)} tok/s` : "";
  return `${usage.output_tokens} tokens${rate}`;
}

interface Reply {
  text: string;
  pending: boolean;
  usage?: ChatUsage | null;
}

export function TextStage({
  model,
  modelLabel,
  downloaded,
}: {
  model: string;
  modelLabel: string;
  downloaded: boolean;
}) {
  const [prompt, setPrompt] = useState(() => readParam("prompt") ?? "");
  const [reply, setReply] = useState<Reply | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [streaming, setStreaming] = useState(false);
  const [configOpen, setConfigOpen] = useState(false);

  const [temperature, setTemperature] = useState(() =>
    numParam("temp", DEFAULTS.temperature, ...LIMITS.temperature),
  );
  const [topP, setTopP] = useState(() => numParam("topp", DEFAULTS.top_p, ...LIMITS.top_p));
  const [maxTokens, setMaxTokens] = useState(() =>
    numParam("maxtok", DEFAULTS.max_tokens, ...LIMITS.max_tokens),
  );
  // `??`, not `||`: an EMPTY `system=` param is the user having cleared the
  // default on purpose, and must stay cleared on reload.
  const [system, setSystem] = useState(() => readParam("system") ?? DEFAULT_SYSTEM);
  useEffect(() => {
    const timer = window.setTimeout(() => {
      writeParams({
        prompt: prompt ? prompt : null,
        temp: temperature !== DEFAULTS.temperature ? String(temperature) : null,
        topp: topP !== DEFAULTS.top_p ? String(topP) : null,
        maxtok: maxTokens !== DEFAULTS.max_tokens ? String(maxTokens) : null,
        system: system !== DEFAULT_SYSTEM ? system : null,
      });
    }, 300);
    return () => window.clearTimeout(timer);
  }, [prompt, temperature, topP, maxTokens, system]);

  const abortRef = useRef<AbortController | null>(null);
  const boxRef = useRef<HTMLTextAreaElement | null>(null);

  // Leaving the stage must not orphan a generation burning battery behind a
  // tab the user left: abort the fetch AND tell the worker (an abort alone
  // closes the relay; the model keeps generating).
  useEffect(
    () => () => {
      if (abortRef.current) {
        abortRef.current.abort();
        void cancelGeneration();
      }
    },
    [],
  );

  const grow = () => {
    const box = boxRef.current;
    if (!box) return;
    box.style.height = "auto";
    box.style.height = Math.min(box.scrollHeight, 180) + "px";
  };

  const settings = (): ChatSettings => ({
    ...(temperature !== DEFAULTS.temperature ? { temperature } : {}),
    ...(topP !== DEFAULTS.top_p ? { top_p: topP } : {}),
    ...(maxTokens !== DEFAULTS.max_tokens ? { max_tokens: maxTokens } : {}),
    ...(system.trim() ? { system_prompt: system.trim() } : {}),
  });

  const send = async (asked?: string) => {
    const wanted = (asked ?? prompt).trim();
    if (!wanted || streaming) return;
    if (asked) setPrompt(asked);
    setError(null);
    setStreaming(true);
    setReply({ text: "", pending: true });
    const controller = new AbortController();
    abortRef.current = controller;

    const run = () =>
      streamChat({
        model,
        prompt: wanted,
        history: [],
        settings: settings(),
        signal: controller.signal,
        onChunk: (text) => setReply((r) => (r ? { ...r, text: r.text + text } : r)),
      });

    try {
      let result;
      try {
        result = await run();
      } catch (e) {
        if (!(e instanceof ModelLoading)) throw e;
        // The run STARTED the load (AI-5); watch it, then ask again — once.
        setStatus(
          downloaded
            ? "Loading the model into memory — the first run pays for this once…"
            : "Downloading the model — the first run pays for this once…",
        );
        if (e.jobId) {
          const outcome = await watchJob(e.jobId, controller.signal, (job) =>
            setStatus(job.detail || "Loading the model…"),
          );
          // Someone stopped the load from the Activity panel. Retrying would
          // just earn a second 409 and surface it as a stream error, so say
          // what actually happened.
          if (outcome.state === "cancelled") throw new Error("the model load was cancelled");
        }
        setStatus(null);
        result = await run();
      }
      setReply((r) => (r ? { ...r, pending: false, usage: result.usage ?? null } : r));
    } catch (e) {
      if ((e as Error).name !== "AbortError") setError((e as Error).message);
      // Keep what streamed before the stop — those tokens were real.
      setReply((r) => (r && r.text ? { ...r, pending: false } : null));
    } finally {
      setStatus(null);
      setStreaming(false);
      abortRef.current = null;
    }
  };

  // Back to empty. Settings stay put — this clears the prompt and its reply.
  const clear = () => {
    setPrompt("");
    setReply(null);
    setError(null);
    const box = boxRef.current;
    if (box) {
      box.style.height = "auto";
      box.focus();
    }
  };

  const stop = () => {
    // The worker owns the generation; the abort only closes the relay. Cancel
    // reaches the model, and false from it (nothing to stop) is not an error.
    abortRef.current?.abort();
    void cancelGeneration();
  };

  const shown = reply ? splitThink(reply.text) : null;
  const stats = replyStats(reply?.usage);

  return (
    <div className={"pg-work" + (configOpen ? " has-config" : "")}>
      {/* The action, and the way to the settings. The hero card above names
          the model and its state. */}
      <StageHeader
        title="Try a prompt"
        configOpen={configOpen}
        onToggleConfig={() => setConfigOpen((open) => !open)}
      />

      <div className="pg-composer">
        <textarea
          ref={boxRef}
          value={prompt}
          rows={2}
          placeholder={`Ask ${modelLabel} something…`}
          onChange={(e) => {
            setPrompt(e.target.value);
            grow();
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              void send();
            }
          }}
        />
        {!streaming && reply && (
          <button
            type="button"
            className="pg-ghost-btn pg-clear"
            title="Clear the prompt and reply"
            onClick={clear}
          >
            Clear
          </button>
        )}
        {streaming ? (
          <button type="button" className="btn btn-secondary pg-send" onClick={stop}>
            Stop
          </button>
        ) : (
          <button
            type="button"
            className="btn btn-primary pg-send"
            disabled={!prompt.trim()}
            title="Enter to run · Shift+Enter for a new line"
            onClick={() => void send()}
          >
            Run <kbd className="pg-kbd">⏎</kbd>
          </button>
        )}
      </div>

      {/* Examples first, under the box they fill; hidden once there is a
          reply to read, which is what that space is then for. */}
      {!reply && !status && <StarterPrompts prompts={STARTERS} onPick={(p) => void send(p)} />}

      {/* Every knob is behind the cog; the surface above is prompt and Run. */}
      <ConfigPanel open={configOpen}>
        <RailSlider
          label="Temperature"
          hint="Lower is focused and repeatable; higher is varied and creative."
          min={LIMITS.temperature[0]}
          max={LIMITS.temperature[1]}
          step={0.05}
          value={temperature}
          fallback={DEFAULTS.temperature}
          onChange={setTemperature}
        />
        <RailSlider
          label="Max tokens"
          hint="The longest reply allowed. One token is roughly ¾ of a word."
          min={LIMITS.max_tokens[0]}
          max={LIMITS.max_tokens[1]}
          step={1}
          value={maxTokens}
          fallback={DEFAULTS.max_tokens}
          onChange={setMaxTokens}
        />
        <label className="pg-ctl">
          <span className="pg-ctl-head">
            <span className="pg-ctl-label">System prompt</span>
            {system !== DEFAULT_SYSTEM && (
              <button type="button" className="pg-ctl-reset" onClick={() => setSystem(DEFAULT_SYSTEM)}>
                reset
              </button>
            )}
          </span>
          <textarea
            className="pg-rail-textarea"
            rows={4}
            value={system}
            placeholder="Who the model should be"
            onChange={(e) => setSystem(e.target.value)}
          />
          <span className="pg-ctl-hint">Standing instructions, applied to every run.</span>
        </label>
        <RailSlider
          label="Top-p"
          hint="How much of the probability mass the model may sample from."
          min={LIMITS.top_p[0]}
          max={LIMITS.top_p[1]}
          step={0.01}
          value={topP}
          fallback={DEFAULTS.top_p}
          onChange={setTopP}
        />
      </ConfigPanel>

      {status && <p className="pg-status">{status}</p>}
      {error && <p className="pg-error">{error}</p>}

      {reply && shown && (
        <div className="pg-answer-block">
          <p className="pg-answer-label">Response</p>
          <div className="pg-answer">
          {!reply.pending && reply.text && (
            <CopyButton text={shown.answer || reply.text} label="Copy the reply" />
          )}
          {shown.think !== null && (
            <details className="pg-think">
              <summary>{shown.thinking ? "Thinking…" : "Thought process"}</summary>
              <div className="pg-think-body">{shown.think}</div>
            </details>
          )}
          {shown.answer ? (
            renderMarkdown(shown.answer)
          ) : reply.pending && !shown.thinking ? (
            <span className="pg-cursor" aria-label="Generating" />
          ) : null}
          {!reply.pending && stats && (
            <div className="pg-turn-foot">
              <span>{stats}</span>
            </div>
          )}
          </div>
        </div>
      )}
    </div>
  );
}
