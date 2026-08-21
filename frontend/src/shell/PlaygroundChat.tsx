// The chat stage: a conversation with the selected text model (SPEC AI-1a).
//
// The anatomy follows what every serious chat surface converged on (LM Studio,
// Open WebUI, the hosted playgrounds): the conversation is a readable column —
// user turns as quiet right-aligned bubbles, model turns as full-width rendered
// markdown with a per-reply stats footer (tok/s · tokens, LM Studio's pattern)
// — a rounded composer at the bottom with ONE primary action, and the
// configuration (system prompt, sampling) in a right rail, because a system
// prompt is a setting, not a message. An empty chat offers starter prompts:
// a blank box demands ideas from the person the tab exists to convince.
//
// The stream rides `playgroundClient.streamChat`; a model that is not resident
// answers 409 with the job id of the load this send just started (AI-5), and
// this component owns the dance — watch the job, narrate it, retry ONCE.
// The transcript is session state, never URL state.
import { useEffect, useRef, useState } from "react";
import {
  cancelGeneration,
  ModelLoading,
  streamChat,
  watchJob,
  type ChatSettings,
  type ChatUsage,
} from "./playgroundClient";
import { renderMarkdown } from "./playgroundMarkdown";
import { RailSection, RailSlider, StarterPrompts } from "./PlaygroundControls";
import { numParam, readParam, writeParams } from "./AiModelsPlayground";

// The server's clamps (`_SAMPLING`, server/ai.py), restated on the controls so
// a slider cannot ask for a value the request would 400 on. 1024 max tokens is
// also the WORKER's own default — the slider states the truth of a bare call.
const DEFAULTS = { temperature: 0.7, top_p: 0.95, max_tokens: 1024 };

const STARTERS = [
  "Explain how a language model predicts the next word, simply",
  "Write a haiku about running AI on a laptop",
  "Draft a short, polite email declining a meeting",
  "Give me three dinner ideas from rice, eggs and spinach",
];

interface Message {
  role: "user" | "assistant";
  content: string;
  /** Still being streamed into. */
  pending?: boolean;
  /** The reply's own stats, from its terminal frame (AI-1b). */
  usage?: ChatUsage | null;
}

/** Split one reply into the deliberation and the answer. A <think> block still
 *  open (mid-stream) is all deliberation — the answer has not started. */
function splitThink(text: string): { think: string | null; answer: string; thinking: boolean } {
  const open = text.indexOf("<think>");
  if (open < 0) return { think: null, answer: text, thinking: false };
  const close = text.indexOf("</think>");
  if (close < 0) return { think: text.slice(open + 7), answer: "", thinking: true };
  return {
    think: text.slice(open + 7, close).trim(),
    answer: text.slice(close + 8).replace(/^\s+/, ""),
    thinking: false,
  };
}

function replyStats(usage: ChatUsage | null | undefined): string | null {
  if (!usage?.output_tokens) return null;
  const rate = usage.seconds ? ` · ${(usage.output_tokens / usage.seconds).toFixed(1)} tok/s` : "";
  return `${usage.output_tokens} tokens${rate}`;
}

export function PlaygroundChat({
  model,
  modelLabel,
  ready,
  downloaded,
}: {
  model: string;
  modelLabel: string;
  ready: boolean;
  downloaded: boolean;
}) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [streaming, setStreaming] = useState(false);
  const [railOpen, setRailOpen] = useState(false);

  const [temperature, setTemperature] = useState(() => numParam("temp", DEFAULTS.temperature));
  const [topP, setTopP] = useState(() => numParam("topp", DEFAULTS.top_p));
  const [maxTokens, setMaxTokens] = useState(() => numParam("maxtok", DEFAULTS.max_tokens));
  const [system, setSystem] = useState(() => readParam("system") ?? "");
  useEffect(() => {
    const timer = window.setTimeout(() => {
      writeParams({
        temp: temperature !== DEFAULTS.temperature ? String(temperature) : null,
        topp: topP !== DEFAULTS.top_p ? String(topP) : null,
        maxtok: maxTokens !== DEFAULTS.max_tokens ? String(maxTokens) : null,
        system: system ? system : null,
      });
    }, 300);
    return () => window.clearTimeout(timer);
  }, [temperature, topP, maxTokens, system]);

  const abortRef = useRef<AbortController | null>(null);
  const logRef = useRef<HTMLDivElement | null>(null);
  const boxRef = useRef<HTMLTextAreaElement | null>(null);
  useEffect(() => {
    // Follow the stream: the newest tokens are the whole point of the view.
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight });
  }, [messages, status]);

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
    const prompt = (asked ?? input).trim();
    if (!prompt || streaming) return;
    const history = messages
      .filter((m) => !m.pending)
      .map(({ role, content }) => ({ role, content }));
    setInput("");
    if (boxRef.current) boxRef.current.style.height = "auto";
    setError(null);
    setStreaming(true);
    setMessages((m) => [
      ...m,
      { role: "user", content: prompt },
      { role: "assistant", content: "", pending: true },
    ]);
    const controller = new AbortController();
    abortRef.current = controller;

    const run = () =>
      streamChat({
        model,
        prompt,
        history,
        settings: settings(),
        signal: controller.signal,
        onChunk: (text) =>
          setMessages((m) => {
            const next = m.slice();
            const last = next[next.length - 1];
            next[next.length - 1] = { ...last, content: last.content + text };
            return next;
          }),
      });

    try {
      let result;
      try {
        result = await run();
      } catch (e) {
        if (!(e instanceof ModelLoading)) throw e;
        // The send STARTED the load (AI-5); watch it, then ask again — once.
        setStatus(
          downloaded
            ? "Loading the model into memory — the first message pays for this once…"
            : "Downloading the model — the first message pays for this once…",
        );
        if (e.jobId) {
          await watchJob(e.jobId, controller.signal, (job) =>
            setStatus(job.detail || "Loading the model…"),
          );
        }
        setStatus(null);
        result = await run();
      }
      const usage = result.usage ?? null;
      setMessages((m) =>
        m.map((msg, at) => (at === m.length - 1 ? { ...msg, pending: false, usage } : msg)),
      );
    } catch (e) {
      if ((e as Error).name !== "AbortError") setError((e as Error).message);
      // Keep what streamed before the stop — those tokens were real.
      setMessages((m) =>
        m
          .map((msg) => ({ ...msg, pending: false }))
          .filter((msg) => msg.role !== "assistant" || msg.content !== ""),
      );
    } finally {
      setStatus(null);
      setStreaming(false);
      abortRef.current = null;
    }
  };

  const stop = () => {
    // The worker owns the generation; the abort only closes the relay. Cancel
    // reaches the model, and false from it (nothing to stop) is not an error.
    abortRef.current?.abort();
    void cancelGeneration();
  };

  const empty = messages.length === 0 && !status;

  return (
    <div className={"pg-work" + (railOpen ? " rail-open" : "")}>
      <div className="pg-main pg-chat">
        <div className="pg-chat-log" ref={logRef}>
          {empty ? (
            <div className="pg-empty-stage">
              <p className="pg-empty-title">Chat with {modelLabel}</p>
              <p className="pg-empty-sub">
                {ready
                  ? "Loaded and ready — everything runs on this machine."
                  : downloaded
                    ? "The first message loads it into memory, then replies are instant to start."
                    : "The first message downloads it — that is the slow part, and it happens once."}
              </p>
              <StarterPrompts title="Try one:" prompts={STARTERS} onPick={(p) => void send(p)} />
            </div>
          ) : (
            messages.map((message, index) => {
              if (message.role === "user") {
                return (
                  <div key={index} className="pg-turn-user">
                    {message.content}
                  </div>
                );
              }
              const { think, answer, thinking } = splitThink(message.content);
              const stats = replyStats(message.usage);
              return (
                <div key={index} className="pg-turn-model">
                  {think !== null && (
                    <details className="pg-think">
                      <summary>{thinking ? "Thinking…" : "Thought process"}</summary>
                      <div className="pg-think-body">{think}</div>
                    </details>
                  )}
                  {answer ? (
                    renderMarkdown(answer)
                  ) : message.pending && !thinking ? (
                    <span className="pg-cursor" aria-label="Generating" />
                  ) : null}
                  {!message.pending && (message.content || stats) && (
                    <div className="pg-turn-foot">
                      <button
                        type="button"
                        className="pg-ghost-btn"
                        onClick={(e) => {
                          void navigator.clipboard.writeText(answer || message.content);
                          const button = e.currentTarget;
                          button.textContent = "Copied";
                          window.setTimeout(() => {
                            button.textContent = "Copy";
                          }, 1200);
                        }}
                      >
                        Copy
                      </button>
                      {stats && <span>{stats}</span>}
                    </div>
                  )}
                </div>
              );
            })
          )}
          {status && <p className="pg-status">{status}</p>}
        </div>
        {error && <p className="pg-error">{error}</p>}
        <div className="pg-composer">
          <textarea
            ref={boxRef}
            value={input}
            rows={1}
            placeholder={`Message ${modelLabel}…`}
            onChange={(e) => {
              setInput(e.target.value);
              grow();
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                void send();
              }
            }}
          />
          {streaming ? (
            <button type="button" className="btn btn-secondary pg-send" onClick={stop}>
              Stop
            </button>
          ) : (
            <button
              type="button"
              className="btn btn-primary pg-send"
              disabled={!input.trim()}
              title="Enter to send · Shift+Enter for a new line"
              onClick={() => void send()}
            >
              Send
            </button>
          )}
        </div>
        <div className="pg-under">
          <button type="button" className="pg-ghost-btn pg-rail-toggle" onClick={() => setRailOpen((v) => !v)}>
            {railOpen ? "Hide controls" : "Controls"}
          </button>
          {messages.length > 0 && (
            <button
              type="button"
              className="pg-ghost-btn"
              disabled={streaming}
              onClick={() => {
                setMessages([]);
                setError(null);
              }}
            >
              New chat
            </button>
          )}
        </div>
      </div>

      <aside className="pg-rail" aria-label="Chat settings">
        <RailSection title="Behaviour">
          <label className="pg-ctl">
            <span className="pg-ctl-head">
              <span className="pg-ctl-label">System prompt</span>
            </span>
            <textarea
              className="pg-rail-textarea"
              rows={3}
              value={system}
              placeholder="Optional — who the model should be"
              onChange={(e) => setSystem(e.target.value)}
            />
            <span className="pg-ctl-hint">Standing instructions, applied to every reply.</span>
          </label>
        </RailSection>
        <RailSection title="Sampling">
          <RailSlider
            label="Temperature"
            hint="Lower is focused and repeatable; higher is varied and creative."
            min={0}
            max={2}
            step={0.05}
            value={temperature}
            fallback={DEFAULTS.temperature}
            onChange={setTemperature}
          />
          <RailSlider
            label="Top-p"
            hint="How much of the probability mass the model may sample from."
            min={0}
            max={1}
            step={0.01}
            value={topP}
            fallback={DEFAULTS.top_p}
            onChange={setTopP}
          />
          <RailSlider
            label="Max tokens"
            hint="The longest reply allowed. One token is roughly ¾ of a word."
            min={1}
            max={32768}
            step={1}
            value={maxTokens}
            fallback={DEFAULTS.max_tokens}
            onChange={setMaxTokens}
          />
        </RailSection>
      </aside>
    </div>
  );
}
