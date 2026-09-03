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
//
// A model whose checkpoint has a vision tower (AI-11j: `entry.acceptsImage`,
// grown from an IMAGE_GENERATION-only flag to cover this capability too once
// mlx_text switched to mlx-vlm) can also be handed a picture to ask about,
// on THIS turn only — `mlx_text/worker.py`'s own boundary, which costs this
// stage nothing extra since it already sends `history: []` (a single turn,
// not a chat). Kept deliberately smaller than `ImageStage`'s attachment row:
// no webcam here — a screenshot or a saved photo is the ordinary "what is
// this" ask, and the picker alone covers it without a second capture UI to
// keep in step with the image stage's own.
import { useEffect, useRef, useState } from "react";
import { pickFile, rawUrl, type AiCatalogModel } from "@platform/lib/api";
import {
  cancelGeneration,
  streamChat,
  withModelReady,
  type ChatSettings,
  type ChatUsage,
} from "./client";
import { canAttachImage, usableAttachment, type AttachedImage } from "./imageInput";
import { renderMarkdown } from "./markdown";
import { splitThink } from "./think";
import { Textarea } from "@platform/shadcn/ui/textarea";
import { Card } from "@platform/shadcn/ui/card";
import {
  ConfigPanel,
  useConfigOpen,
  CopyButton,
  RailField,
  RailReset,
  RailSlider,
  ResultSlot,
  StageHeader,
  StarterCards,
  type Starter,
} from "./controls";
import { useAutoGrow } from "@platform/lib/autoGrow";
import { StarterIcons } from "./starterIcons";
import { saveToCache, useWebcam, WebcamOverlay } from "./webcam";
import { numParam, readParam, writeParams } from "@apps/ai_models/lib/params";
import {
  AnswerBlock,
  AnswerLabel,
  AnswerCard,
  AttachButton,
  AttachChip,
  AttachDrop,
  AttachNote,
  AttachOpen,
  AttachRow,
  ClearButton,
  Composer,
  ComposerFoot,
  ComposerKbd,
  ComposerSide,
  composerStackTextareaClass,
  composerTextareaClass,
  Cursor,
  Lightbox,
  LightboxClose,
  lightboxImageClass,
  StageButton,
  StageError,
  StageStatus,
  ThinkBlock,
  ThinkBody,
  TurnFoot,
  stageWorkCardClass,
  workGridClass,
} from "@platform/ui/playground";

// The three formats `ImageStage`'s own picker restricts to (`ATTACH_EXTENSIONS`
// there) — kept identical here for one reason only: consistency with the
// picture-picking experience elsewhere in the Playground, not a size-parsing
// dependency (this stage never reads a picture's pixel dimensions, unlike the
// image stage's server-side header parse). A dialog that only ever offers
// three formats and then refuses a fourth picked around it is the one
// experience this app tries to give everywhere a picture is attached.
const ATTACH_EXTENSIONS = [".png", ".jpg", ".jpeg", ".webp"] as const;
const ATTACH_TYPES = ATTACH_EXTENSIONS.map((e) => e.slice(1));

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

// No system prompt by default. This stage's job is to show what THIS model does
// on a bare `fused.ai` call, and a standing prompt of ours — however short — is
// a second author in every reply: verbosity, formatting and reasoning length all
// come out steered, and nothing on screen says by whom. The panel's field is
// there for anyone who wants one, and `system=` still rides the URL when it is
// set. A model that rambles or thinks out loud without one is telling the reader
// something true about itself, which is what they came to find out.

// Eight authored examples — two pages of four (D465). Each is a real ask with
// its constraints spelled out, not a topic: what to write, how long, what to
// leave out. A one-line "write a haiku" tests that the model answers; these
// test what the reader actually came to find out, which is whether it follows
// the shape it was given.
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

function replyStats(usage: ChatUsage | null | undefined, seconds?: number | null): string | null {
  if (!usage?.outputTokens) return null;
  const rate = seconds ? ` · ${(usage.outputTokens / seconds).toFixed(1)} tok/s` : "";
  return `${usage.outputTokens} tokens${rate}`;
}

interface Reply {
  text: string;
  pending: boolean;
  usage?: ChatUsage | null;
  /** The local tier's wall-clock, from `providerMetadata.local.seconds` (D632). */
  seconds?: number | null;
}

export function TextStage({
  model,
  modelLabel,
  downloaded,
  entry,
}: {
  model: string;
  modelLabel: string;
  downloaded: boolean;
  entry: AiCatalogModel;
}) {
  const [prompt, setPrompt] = useState(() => readParam("prompt") ?? "");
  const [reply, setReply] = useState<Reply | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [streaming, setStreaming] = useState(false);
  const { open: configOpen, toggle: toggleConfig, touched: configTouched } = useConfigOpen();

  // Can THIS model be asked about a picture at all — the server's own answer
  // (AI-11j), read through `imageInput.ts` so the row drawn here and the
  // `images` field a send actually carries cannot come to disagree, exactly
  // as `ImageStage` already keeps `editable`/`base` in step.
  const attachable = canAttachImage(entry.acceptsImage);
  // Deliberately NOT persisted to the URL, unlike `ImageStage`'s `img` param:
  // an image here rides the CURRENT turn only (`mlx_text/worker.py`'s own
  // boundary) and this stage already sends `history: []` on every send — a
  // picture surviving a reload would model a permanence the request itself
  // never had, and a stale path pointing at a picture the user has since
  // moved would silently 400 the next send.
  const [attachment, setAttachment] = useState<AttachedImage | null>(null);
  const attachedImage = usableAttachment(entry.acceptsImage, attachment);
  const [attaching, setAttaching] = useState(false);
  // Is the attached picture open at full size? A thumbnail 28px on a side is a
  // reminder of WHICH picture, not a look at it — the same rule ImageStage's
  // own lightbox exists for.
  const [showAttachment, setShowAttachment] = useState(false);

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
  const { ref: boxRef } = useAutoGrow(prompt);
  // Set on the way in as well as cleared on the way out, the same shape
  // `ImageStage`'s own flag has: a pick awaits the dialog, and a continuation
  // that lands after an unmount must not write state from a dead component.
  const aliveRef = useRef(true);
  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
    };
  }, []);

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

  // Escape closes the preview — the one keystroke somebody reaches for before
  // the ✕, and the same answer every overlay in this app gives it.
  useEffect(() => {
    if (!showAttachment) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setShowAttachment(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [showAttachment]);

  /** Point the composer at a file that is ALREADY on this disk — no copy, no
   *  upload, the user's own path, exactly as `ImageStage.choose` does and for
   *  the identical reason: a browser's `<input type=file>` strips the path,
   *  so the OS dialog raised in the server process is the only way to one. */
  const choose = async () => {
    setError(null);
    setAttaching(true);
    try {
      const path = await pickFile({
        title: "Choose a picture to ask about",
        types: ATTACH_TYPES,
      });
      // A cancel is an answer: nothing changes and nothing is said about it.
      if (path === null || !aliveRef.current) return;
      const name = path.split("/").pop() || path;
      if (!ATTACH_EXTENSIONS.some((ext) => name.toLowerCase().endsWith(ext))) {
        setError(`${name} is not a PNG, JPEG or WebP.`);
        return;
      }
      setAttachment({ path, name });
    } catch (e) {
      if (aliveRef.current) setError((e as Error).message);
    } finally {
      if (aliveRef.current) setAttaching(false);
    }
  };

  // The camera, shared with the image stage (webcam.tsx). The only difference
  // between the two is this stage's answer to the frame: there it is a picture
  // to EDIT, here one to ask a question about — everything up to the blob is
  // the same code.
  const webcam = useWebcam({ onError: setError });

  const openCamera = async () => {
    setError(null);
    await webcam.start();
  };

  /** The frame, written to the app's cache dir and attached. A capture is the
   *  one attachment with no path of its own — it does not exist anywhere until
   *  it is saved — which is why this is the only attach route that writes. */
  const capture = () =>
    webcam.capture((blob) => {
      void (async () => {
        setError(null);
        setAttaching(true);
        try {
          const landed = await saveToCache(blob, "webcam.png");
          if (aliveRef.current) setAttachment(landed);
        } catch (e) {
          if (aliveRef.current) setError((e as Error).message);
        } finally {
          if (aliveRef.current) setAttaching(false);
        }
      })();
    });

  const settings = (): ChatSettings => ({
    ...(temperature !== DEFAULTS.temperature ? { temperature } : {}),
    ...(topP !== DEFAULTS.top_p ? { topP } : {}),
    ...(maxTokens !== DEFAULTS.max_tokens ? { maxTokens } : {}),
    ...(system.trim() ? { systemPrompt: system.trim() } : {}),
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
        // The attached picture, if this model can actually be sent one right
        // now — `attachedImage` already applies `usableAttachment`, so an
        // attachment kept from a model that could ask about it, switched to
        // one that cannot, is never sent (the same rule `ImageStage`'s `base`
        // applies to its own render request).
        ...(attachedImage ? { images: [attachedImage.path] } : {}),
      });

    try {
      // AI-5's dance, and the wait is bounded rather than one retry — see
      // `withModelReady`, which owns it for this stage and the embedding one.
      const result = await withModelReady(run, {
        signal: controller.signal,
        downloaded,
        onStatus: setStatus,
      });
      setReply((r) => (r ? {
        ...r, pending: false, usage: result.usage ?? null,
        seconds: result.providerMetadata?.local?.seconds ?? null,
      } : r));
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

  // Back to empty. Settings stay put — this clears the prompt, its reply, AND
  // the attached picture, which is part of the request rather than of the
  // setup (the same rule `ImageStage.clear` follows for its own attachment).
  const clear = () => {
    setPrompt("");
    setReply(null);
    setError(null);
    setAttachment(null);
    // The height follows the emptied prompt on its own (useAutoGrow).
    boxRef.current?.focus();
  };

  const stop = () => {
    // The worker owns the generation; the abort only closes the relay. Cancel
    // reaches the model, and false from it (nothing to stop) is not an error.
    abortRef.current?.abort();
    void cancelGeneration();
  };

  const shown = reply ? splitThink(reply.text) : null;
  const stats = replyStats(reply?.usage, reply?.seconds);

  // Hoisted because the composer has two shapes below and this button is the
  // one thing both put in the same place — the bottom-right corner. Written
  // twice it would be two buttons to keep in step.
  const runButton = streaming ? (
    <StageButton type="button" variant="secondary" onClick={stop}>
      Stop
    </StageButton>
  ) : (
    <StageButton
      type="button"
      variant="primary"
      disabled={!prompt.trim()}
      title="Enter to run · Shift+Enter for a new line"
      onClick={() => void send()}
    >
      Run <ComposerKbd>⏎</ComposerKbd>
    </StageButton>
  );

  return (
    <div className={workGridClass(configOpen)}>
      <Card className={stageWorkCardClass + " flex-none gap-3 px-(--card-spacing) [--card-spacing:--spacing(6)]"}>
      {/* The action, and the way to the settings. The hero card above names
          the model and its state. */}
      <StageHeader
        title="Try a prompt"
        configOpen={configOpen}
        onToggleConfig={toggleConfig}
      />

      {/* Two shapes, one composer. Without an attachment this stage is the
          plain row every other text-in stage is: [prompt | Clear-over-Run].
          The moment the model can be asked about a picture (AI-11j) it becomes
          `ImageStage`'s STACKED composer instead — prompt across the whole box,
          a floor holding the attach pill beside Run, Clear floating in the
          corner. The attach row used to be a third child of the ROW flex,
          which laid it out BESIDE the prompt: the pill took the left of the
          box, the placeholder was squeezed into the middle, and neither read as
          belonging to the other. Same classes as the image stage throughout, so
          the two are one feature wearing one layout. */}
      <Composer layout={attachable ? "stacked" : "row"}>
        <textarea
          ref={boxRef}
          className={attachable ? composerStackTextareaClass : composerTextareaClass}
          value={prompt}
          rows={3}
          placeholder={
            attachedImage ? "Ask about the attached picture…" : `Ask ${modelLabel} something…`
          }
          onChange={(e) => setPrompt(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              void send();
            }
          }}
        />
        {/* Stacked, Clear floats in the box's top-right corner rather than
            taking a slot above Run: on this shape the two are not sharing a row
            with the prompt, so a slot of its own would cost the box a permanent
            40px of height for a button that only exists once there is a reply.
            The row shape keeps the stack — see the column below. */}
        {attachable && !streaming && reply && (
          <ClearButton
            type="button"
            placement="corner"
            title="Clear the prompt and reply"
            onClick={clear}
          >
            Clear
          </ClearButton>
        )}
        {attachable ? (
          /* The composer's floor: the way to attach a picture, the picture
             itself once there is one, then Run — one cluster in the
             bottom-right corner, exactly as the image stage arranges its own. */
          <ComposerFoot>
            {attachedImage && (
              <AttachChip>
                <AttachOpen
                  type="button"
                  title="See this picture"
                  aria-label="See this picture"
                  onClick={() => setShowAttachment(true)}
                >
                  <img src={rawUrl(attachedImage.path)} alt="" />
                </AttachOpen>
                <AttachDrop
                  type="button"
                  title="Remove this image"
                  aria-label="Remove this image"
                  onClick={() => setAttachment(null)}
                >
                  ✕
                </AttachDrop>
              </AttachChip>
            )}
            <AttachRow>
              <AttachButton
                type="button"
                title="Point at a picture already on this disk — nothing is copied"
                disabled={attaching}
                onClick={() => void choose()}
              >
                {StarterIcons.landscape}
                <span>{attachedImage ? "Replace" : "Add an image"}</span>
              </AttachButton>
              <AttachButton
                type="button"
                active={webcam.open}
                title="Take one with the webcam"
                disabled={attaching}
                onClick={() => (webcam.open ? webcam.stop() : void openCamera())}
              >
                {StarterIcons.camera}
                <span>Webcam</span>
              </AttachButton>
              {attaching && <AttachNote>Working…</AttachNote>}
            </AttachRow>
            <ComposerSide flat>{runButton}</ComposerSide>
          </ComposerFoot>
        ) : (
          /* Clear at the top of this column, Run at the bottom — not inline with
             the prompt. Inline, Clear appeared and disappeared BESIDE the text,
             narrowing the box by its own width and rewrapping the prompt taller
             than the height the grow already wrote. The column's width is set by
             Run, the wider of the two, so nothing moves when Clear comes and
             goes. */
          <ComposerSide>
            {!streaming && reply && (
              <ClearButton type="button" title="Clear the prompt and reply" onClick={clear}>
                Clear
              </ClearButton>
            )}
            {runButton}
          </ComposerSide>
        )}
      </Composer>

      {/* The attached picture at full size — the whole modal, no title bar, no
          filename, no actions: the ✕ above already removes it, and this only
          exists because a 28px thumbnail cannot be looked at. Click the
          backdrop or press Escape to close, exactly as `ImageStage`'s own
          lightbox answers to both. */}
      {webcam.open && (
        <WebcamOverlay videoRef={webcam.videoRef} onCapture={capture} onClose={webcam.stop} />
      )}

      {attachedImage && showAttachment && (
        <Lightbox
          open
          onClose={() => setShowAttachment(false)}
          label="The attached picture"
        >
          <img
            className={lightboxImageClass}
            src={rawUrl(attachedImage.path)}
            alt=""
            onClick={(e) => e.stopPropagation()}
          />
          <LightboxClose
            type="button"
            title="Close"
            aria-label="Close"
            onClick={() => setShowAttachment(false)}
          >
            ✕
          </LightboxClose>
        </Lightbox>
      )}

      {/* Every knob is behind the cog; the surface above is prompt and Run. */}
      <ConfigPanel open={configOpen} animated={configTouched.current}>
        <RailSlider
          label="Temperature"
          hint="Lower is focused and repeatable; higher is varied and creative."
          min={LIMITS.temperature[0]}
          max={LIMITS.temperature[1]}
          step={0.1}
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
        {/* "clear", not the other controls' "reset": resetting this one IS
            emptying it, and a button that says reset beside a prompt the
            user wrote reads like it would restore one of ours. */}
        <RailField
          label="System prompt"
          action={system !== "" && <RailReset onClick={() => setSystem("")}>clear</RailReset>}
          hint="Standing instructions, applied to every run. Empty by default — the reply is whatever this model does on its own."
        >
          <Textarea
            className="min-h-0 resize-y text-xs leading-normal"
            rows={4}
            value={system}
            placeholder="Who the model should be"
            onChange={(e) => setSystem(e.target.value)}
          />
        </RailField>
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

      {/* Examples first, under the box they fill; hidden once there is a
          reply to read, which is what that space is then for. */}
      {!reply && !status && (
        <StarterCards samples={STARTERS} onPick={(s) => void send(s.prompt)} />
      )}

      {status && <StageStatus>{status}</StageStatus>}
      {error && <StageError>{error}</StageError>}

      {!(reply && shown) ? (
        <ResultSlot
          label="Response"
          capability="text-generation"
          note="The reply appears here. Ask something above, then Run."
        />
      ) : (
        <AnswerBlock>
          <AnswerLabel>Response</AnswerLabel>
          <AnswerCard>
          {!reply.pending && reply.text && (
            <CopyButton text={shown.answer || reply.text} label="Copy the reply" />
          )}
          {shown.think !== null && (
            <ThinkBlock>
              <summary>{shown.thinking ? "Thinking…" : "Thought process"}</summary>
              <ThinkBody>{shown.think}</ThinkBody>
            </ThinkBlock>
          )}
          {shown.answer ? (
            renderMarkdown(shown.answer)
          ) : reply.pending && !shown.thinking ? (
            <Cursor aria-label="Generating" />
          ) : null}
          {!reply.pending && stats && (
            <TurnFoot>
              <span>{stats}</span>
            </TurnFoot>
          )}
          </AnswerCard>
        </AnswerBlock>
      )}
      </Card>
    </div>
  );
}
