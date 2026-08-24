// The text stage: one prompt in, one reply out (SPEC AI-1a, reshaped).
//
// Every stage is the same API-surface shape: a prompt, a Run, the rendered
// result of THAT run. The prompt is setup rather than transcript, so it rides
// the URL (`prompt`) the way the image stage's does.
//
// The stream rides `playgroundClient.streamChat`; a model that is not resident
// answers 409 with the job id of the load this send just started (AI-5), and
// this component owns the dance — watch the job, narrate it, retry ONCE.
import { useEffect, useRef, useState } from "react";
import { ChevronRightIcon, CornerDownLeftIcon } from "lucide-react";
import {
  cancelGeneration,
  streamChat,
  withModelReady,
  type ChatSettings,
  type ChatUsage,
} from "./client";
import { renderMarkdown } from "./markdown";
import { splitThink } from "./think";
import {
  AnswerBlock,
  CopyButton,
  Field,
  FieldContent,
  FieldDescription,
  FieldTitle,
  RailSlider,
  ResultSlot,
  StageShell,
  StarterCards,
  useAutoGrow,
  type Starter,
} from "./controls";
import { StarterIcons } from "./starterIcons";
import { Button } from "@apps/ai_models/ui/button";
import { Alert, AlertDescription } from "@apps/ai_models/ui/alert";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@apps/ai_models/ui/collapsible";
import { InputGroup, InputGroupAddon, InputGroupTextarea } from "@apps/ai_models/ui/input-group";
import { Kbd } from "@apps/ai_models/ui/kbd";
import { Spinner } from "@apps/ai_models/ui/spinner";
import { Textarea } from "@apps/ai_models/ui/textarea";
import { numParam, readParam, writeParams } from "@apps/ai_models/lib/params";

// The server's clamps (`_SAMPLING`, server/ai.py), restated on the controls so
// a slider cannot ask for a value the request would 400 on. 1024 max tokens is
// also the WORKER's own default — the slider states the truth of a bare call.
const DEFAULTS = { temperature: 0.7, top_p: 0.95, max_tokens: 1024 };
// The server's `_SAMPLING` bounds, restated because it REJECTS an out-of-range
// value rather than clamping it. Keep in step with server/ai.py.
const LIMITS = {
  temperature: [0, 2],
  top_p: [0, 1],
  max_tokens: [1, 32768],
} as const;

// No system prompt by default: this stage's job is to show what THIS model
// does on a bare `fused.ai` call, and a standing prompt of ours is a second
// author in every reply. The panel's field is there for anyone who wants one.

// Eight authored examples — two pages of four (D465). Each is a real ask with
// its constraints spelled out, not a topic.
const STARTERS: Starter[] = [
  {
    name: "How it guesses",
    icon: StarterIcons.bulb,
    prompt:
      "Explain how a language model picks the next word to someone who has never written " +
      "code. Use one everyday analogy, stay under 150 words, and end with the thing people " +
      "most often get wrong about it.",
  },
  {
    name: "Decline a meeting",
    icon: StarterIcons.mail,
    prompt:
      "Write a short, warm email declining Thursday's design review because I am shipping a " +
      "release that day. Offer to read the notes and send comments, keep it to four " +
      "sentences, and do not apologise twice.",
  },
  {
    name: "Dinner from this",
    icon: StarterIcons.bowl,
    prompt:
      "I have rice, two eggs, spinach and a lemon. Give me three dinners I can cook in under " +
      "20 minutes — a title and three steps each, ordered from least to most effort.",
  },
  {
    name: "Explain an error",
    icon: StarterIcons.code,
    prompt:
      "Explain what a Python KeyError means, the three most common ways it happens in real " +
      "code, and how to fix each one. One short snippet per fix, no preamble.",
  },
  {
    name: "Regex, in parts",
    icon: StarterIcons.list,
    prompt:
      "Write a regular expression that matches an ISO date (YYYY-MM-DD) and nothing else, " +
      "then explain it token by token as a bullet list, including why each anchor is there.",
  },
  {
    name: "One day in Lisbon",
    icon: StarterIcons.plane,
    prompt:
      "Plan one day in Lisbon for someone who would rather walk and drink coffee than queue " +
      "for museums. Morning, afternoon, evening — one line each, plus the walk between them.",
  },
  {
    name: "Three haiku",
    icon: StarterIcons.pen,
    prompt:
      "Write three haiku about running a large AI model on a laptop that gets hot. Give each " +
      "a different mood: proud, tired, funny. Nothing about clouds.",
  },
  {
    name: "Argue both sides",
    icon: StarterIcons.chart,
    prompt:
      "I am choosing between a laptop with 16GB of memory and one with 32GB for running AI " +
      "models locally. Argue both sides in a short table, then commit to one recommendation " +
      "and say what would change your mind.",
  },
];

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
  const [system, setSystem] = useState(() => readParam("system") ?? "");
  useEffect(() => {
    const timer = window.setTimeout(() => {
      writeParams({
        prompt: prompt ? prompt : null,
        temp: temperature !== DEFAULTS.temperature ? String(temperature) : null,
        topp: topP !== DEFAULTS.top_p ? String(topP) : null,
        maxtok: maxTokens !== DEFAULTS.max_tokens ? String(maxTokens) : null,
        // Empty is the default now, so the param appears only when there IS one.
        system: system.trim() ? system : null,
      });
    }, 300);
    return () => window.clearTimeout(timer);
  }, [prompt, temperature, topP, maxTokens, system]);

  const abortRef = useRef<AbortController | null>(null);
  const { ref: boxRef, grow } = useAutoGrow();

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
      // AI-5's dance, and the wait is bounded rather than one retry — see
      // `withModelReady`, which owns it for this stage and the embedding one.
      const result = await withModelReady(run, {
        signal: controller.signal,
        downloaded,
        onStatus: setStatus,
      });
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

  const config = (
    <>
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
      <Field>
        <FieldContent>
          <div className="flex items-center gap-2">
            <FieldTitle className="flex-1">System prompt</FieldTitle>
            {/* "clear", not the other controls' "reset": resetting this one IS
                emptying it, and a button that says reset beside a prompt the
                user wrote reads like it would restore one of ours. */}
            {system !== "" && (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="h-6 px-1.5 text-xs text-muted-foreground"
                onClick={() => setSystem("")}
              >
                clear
              </Button>
            )}
          </div>
          <Textarea
            rows={4}
            value={system}
            placeholder="Who the model should be"
            onChange={(e) => setSystem(e.target.value)}
          />
          <FieldDescription>
            Standing instructions, applied to every run. Empty by default — the
            reply is whatever this model does on its own.
          </FieldDescription>
        </FieldContent>
      </Field>
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
    </>
  );

  return (
    <StageShell
      title="Try a prompt"
      configOpen={configOpen}
      onToggleConfig={() => setConfigOpen((open) => !open)}
      config={config}
    >
      <InputGroup>
        <InputGroupTextarea
          ref={boxRef}
          value={prompt}
          rows={3}
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
        <InputGroupAddon align="block-end" className="justify-end">
          {!streaming && reply && (
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="text-muted-foreground"
              title="Clear the prompt and reply"
              onClick={clear}
            >
              Clear
            </Button>
          )}
          {streaming ? (
            <Button type="button" variant="secondary" size="sm" className="ml-auto" onClick={stop}>
              <Spinner data-icon="inline-start" />
              Stop
            </Button>
          ) : (
            <Button
              type="button"
              size="sm"
              className="ml-auto"
              disabled={!prompt.trim()}
              title="Enter to run · Shift+Enter for a new line"
              onClick={() => void send()}
            >
              Run <Kbd className="bg-transparent text-inherit">⏎</Kbd>
            </Button>
          )}
        </InputGroupAddon>
      </InputGroup>

      {/* Examples first, under the box they fill; hidden once there is a
          reply to read, which is what that space is then for. */}
      {!reply && !status && (
        <StarterCards samples={STARTERS} onPick={(s) => void send(s.prompt)} />
      )}

      {status && (
        <p className="flex items-center gap-2 text-sm text-muted-foreground">
          <Spinner className="size-3.5" />
          {status}
        </p>
      )}
      {error && (
        <Alert variant="destructive">
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      {!(reply && shown) ? (
        <ResultSlot
          label="Response"
          capability="text-generation"
          note="The reply appears here. Ask something above, then Run."
        />
      ) : (
        <AnswerBlock
          label="Response"
          className="rounded-lg border bg-card px-4 py-3 text-sm leading-relaxed"
        >
          {!reply.pending && reply.text && (
            <CopyButton text={shown.answer || reply.text} label="Copy the reply" />
          )}
          {shown.think !== null && (
            <Collapsible className="mb-2">
              <CollapsibleTrigger className="group flex items-center gap-1 text-xs text-muted-foreground">
                <ChevronRightIcon className="size-3 transition-transform group-data-[state=open]:rotate-90" />
                {shown.thinking ? "Thinking…" : "Thought process"}
              </CollapsibleTrigger>
              <CollapsibleContent className="mt-1 border-l-2 pl-3 text-xs whitespace-pre-wrap text-muted-foreground">
                {shown.think}
              </CollapsibleContent>
            </Collapsible>
          )}
          {shown.answer ? (
            renderMarkdown(shown.answer)
          ) : reply.pending && !shown.thinking ? (
            <Spinner className="size-4" aria-label="Generating" />
          ) : null}
          {!reply.pending && stats && (
            <div className="mt-2 flex items-center gap-2 border-t pt-2 text-xs text-muted-foreground">
              <CornerDownLeftIcon className="size-3" aria-hidden="true" />
              <span>{stats}</span>
            </div>
          )}
        </AnswerBlock>
      )}
    </StageShell>
  );
}
