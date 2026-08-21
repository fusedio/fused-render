// The chat stage: a conversation with the selected text model (SPEC AI-1a).
//
// The stream rides `playgroundClient.streamChat`; a model that is not resident
// answers 409 with the job id of the load this send just started (AI-5), and
// this component owns the dance — watch the job, say what is happening, retry
// ONCE. The transcript is session state, never URL state: the URL reproduces
// the setup (sampling, system prompt), not the conversation.
//
// Reasoning models wrap deliberation in <think>…</think>; half the curated
// text list does it, so the block collapses into a <details> instead of
// burying the answer (the same treatment showcase/local-chat gives it).
import { useEffect, useRef, useState } from "react";
import {
  cancelGeneration,
  ModelLoading,
  streamChat,
  watchJob,
  type ChatSettings,
  type ChatTurn,
  type ChatUsage,
} from "./playgroundClient";
import { readParam, writeParams } from "./AiModelsPlayground";

// The server's clamps (`_SAMPLING`, server/ai.py), restated on the controls so
// a slider cannot ask for a value the request would 400 on.
const DEFAULTS = { temperature: 0.7, top_p: 0.95, max_tokens: 1024 };

interface Message extends ChatTurn {
  /** Still being streamed into. */
  pending?: boolean;
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

export function PlaygroundChat({
  model,
  ready,
  downloaded,
}: {
  model: string;
  ready: boolean;
  downloaded: boolean;
}) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [streaming, setStreaming] = useState(false);
  const [speed, setSpeed] = useState<string | null>(null);
  const [advanced, setAdvanced] = useState(false);

  // Settings read from the URL once and written back debounced — only the
  // non-defaults, so a fresh playground is a clean URL and a shared link
  // carries exactly what its sender changed.
  const [temperature, setTemperature] = useState(() => Number(readParam("temp") ?? DEFAULTS.temperature));
  const [topP, setTopP] = useState(() => Number(readParam("topp") ?? DEFAULTS.top_p));
  const [maxTokens, setMaxTokens] = useState(() => Number(readParam("maxtok") ?? DEFAULTS.max_tokens));
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
  const listRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    // Follow the stream: the newest tokens are the whole point of the view.
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight });
  }, [messages]);

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

  const settings = (): ChatSettings => ({
    ...(temperature !== DEFAULTS.temperature ? { temperature } : {}),
    ...(topP !== DEFAULTS.top_p ? { top_p: topP } : {}),
    ...(maxTokens !== DEFAULTS.max_tokens ? { max_tokens: maxTokens } : {}),
    ...(system.trim() ? { system_prompt: system.trim() } : {}),
  });

  const send = async () => {
    const prompt = input.trim();
    if (!prompt || streaming) return;
    const history = messages.filter((m) => !m.pending).map(({ role, content }) => ({ role, content }));
    setInput("");
    setError(null);
    setSpeed(null);
    setStreaming(true);
    setMessages((m) => [...m, { role: "user", content: prompt }, { role: "assistant", content: "", pending: true }]);
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
        // A second failure is a real answer, not a retry loop.
        setStatus("Loading the model — the first message pays for it once…");
        if (e.jobId) {
          await watchJob(e.jobId, controller.signal, (job) =>
            setStatus(job.detail || "Loading the model…"),
          );
        }
        setStatus(null);
        result = await run();
      }
      const usage: ChatUsage | null = result.usage ?? null;
      if (usage?.output_tokens && usage.seconds) {
        setSpeed(`${(usage.output_tokens / usage.seconds).toFixed(1)} tok/s`);
      }
      setMessages((m) => m.map((msg) => ({ ...msg, pending: false })));
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

  return (
    <div className="pg-chat">
      <div className="pg-chat-log" ref={listRef}>
        {messages.length === 0 && (
          <p className="pg-chat-hint">
            {ready
              ? "The model is loaded — say something."
              : downloaded
                ? "The first message loads the model, then it answers from memory."
                : "The first message downloads and loads the model — the download is the slow part, once."}
          </p>
        )}
        {messages.map((message, index) => {
          if (message.role === "user") {
            return (
              <div key={index} className="pg-bubble user">
                {message.content}
              </div>
            );
          }
          const { think, answer, thinking } = splitThink(message.content);
          return (
            <div key={index} className="pg-bubble model">
              {think !== null && (
                <details className="pg-think">
                  <summary>{thinking ? "Thinking…" : "Thought process"}</summary>
                  <div className="pg-think-body">{think}</div>
                </details>
              )}
              {answer || (message.pending && !thinking ? "…" : "")}
            </div>
          );
        })}
        {status && <p className="pg-chat-status">{status}</p>}
      </div>
      {error && <p className="pg-error">{error}</p>}
      <div className="pg-chat-input">
        <textarea
          value={input}
          rows={2}
          placeholder="Message the model…"
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              void send();
            }
          }}
        />
        {streaming ? (
          <button type="button" className="btn btn-secondary" onClick={stop}>
            Stop
          </button>
        ) : (
          <button type="button" className="btn btn-secondary" disabled={!input.trim()} onClick={() => void send()}>
            Send
          </button>
        )}
      </div>
      <div className="pg-toolbar">
        <button type="button" className="pg-adv-toggle" onClick={() => setAdvanced((v) => !v)}>
          Advanced {advanced ? "▴" : "▾"}
        </button>
        {speed && <span className="pg-speed">{speed}</span>}
        {messages.length > 0 && (
          <button
            type="button"
            className="pg-adv-toggle"
            onClick={() => {
              setMessages([]);
              setError(null);
              setSpeed(null);
            }}
            disabled={streaming}
          >
            New chat
          </button>
        )}
      </div>
      {advanced && (
        <div className="pg-advanced">
          <label>
            Temperature <span className="pg-adv-val">{temperature}</span>
            <input
              type="range"
              min={0}
              max={2}
              step={0.05}
              value={temperature}
              onChange={(e) => setTemperature(Number(e.target.value))}
            />
          </label>
          <label>
            Top-p <span className="pg-adv-val">{topP}</span>
            <input
              type="range"
              min={0}
              max={1}
              step={0.01}
              value={topP}
              onChange={(e) => setTopP(Number(e.target.value))}
            />
          </label>
          <label>
            Max tokens <span className="pg-adv-val">{maxTokens}</span>
            <input
              type="range"
              min={1}
              max={32768}
              step={1}
              value={maxTokens}
              onChange={(e) => setMaxTokens(Number(e.target.value))}
            />
          </label>
          <label className="pg-adv-wide">
            System prompt
            <textarea
              rows={2}
              value={system}
              placeholder="Optional — how the model should behave"
              onChange={(e) => setSystem(e.target.value)}
            />
          </label>
        </div>
      )}
    </div>
  );
}
