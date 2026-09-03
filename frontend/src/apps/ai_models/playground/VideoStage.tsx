// The video stage: prompt in, a short clip with audio out (SPEC §40).
//
// Shaped like the text and image stages (D431): heading with a cog,
// prompt, Generate — and every parameter behind the settings panel.
//
// `ImageStage`'s minimal sibling — no aspect chips, no live preview (this
// build has none), no guidance (H3 is CFG-distilled and takes no such
// parameter). What is genuinely different from an image render: the output
// is a `<video controls>` rather than an `<img>`, `frames` is a fourth
// numeric setting alongside width/height/steps, and a render can run for a
// long time — the row survives a tab switch (it shows in Activity), same as
// the image stage, and only the WATCH stops on unmount. That length is also
// why this stage keeps the settled caption and the gallery strip the image
// stage dropped.
import { useEffect, useRef, useState } from "react";
import { cancelJob, type Job } from "@platform/lib/jobs";
import { pickFile, rawUrl, type AiCatalogCapability, type AiCatalogModel } from "@platform/lib/api";
import { startVideo, watchJob, type VideoStarted } from "./client";
import { Input } from "@platform/shadcn/ui/input";
import { Card } from "@platform/shadcn/ui/card";
import { Tiny } from "@platform/ui/flow/Typography";
import {
  AnswerBlock,
  AttachButton,
  AttachChip,
  ClearButton,
  ComposerCard,
  ComposerSide,
  ConfigPanel,
  Lightbox,
  ProgressBar,
  RailField,
  RailReset,
  RailSlider,
  ResultSlot,
  RunButton,
  StageHeader,
  StarterCards,
  StatusLine,
  StopButton,
  composerTextareaClass,
  useConfigOpen,
  type Starter,
} from "./controls";
import { canEdit, usableBase, type AttachedImage } from "./imageInput";
import { useAutoGrow } from "@platform/lib/autoGrow";
import { cn } from "@platform/lib/utils";
import { StarterIcons } from "./starterIcons";
import { numParam, readParam, writeParams } from "@apps/ai_models/lib/params";

// The three formats the server's own header reader understands (AI-9f) —
// identical list to the image stage's, restated rather than imported: a fact
// that happens to coincide today, not one they are required to share forever.
const ATTACH_EXTENSIONS = [".png", ".jpg", ".jpeg", ".webp"] as const;
const ATTACH_TYPES = ATTACH_EXTENSIONS.map((e) => e.slice(1));

// Small canvas by default — the first clip arriving quickly is the point.
// The pixel budget is enforced server-side regardless.
const DEFAULTS = { width: 512, height: 512 };
const SIZE_RANGE = [256, 1344] as const;
// [2, 50]: an app-chosen safety rail shared across every engine.
const STEPS_RANGE = [2, 50] as const;

// **The fallback when the server sends no traits at all.** It must stay
// byte-for-byte the row `registry.video_traits_for` falls back to
// (`ltx-video`'s): a client that guesses a different grid than the server
// snaps to draws a slider whose every value the render then moves.
const FALLBACK_TRAITS: NonNullable<AiCatalogCapability["videoTraits"]> = {
  framesBase: 1,
  framesStep: 8,
  minFrames: 9,
  maxFrames: 169,
  defaultFrames: 97,
  defaultWidth: 704,
  defaultHeight: 480,
  defaultSteps: 8,
  supportsImage: true,
};

// Eight authored examples — two pages of four (D465). Every one names a
// subject AND a MOTION, because motion is the whole difference between this
// stage and the image one.
const STARTERS: Starter[] = [
  {
    name: "Paper boat",
    icon: StarterIcons.plane,
    prompt:
      "A paper boat drifting down a rain-soaked street gutter, cinematic, water rushing past " +
      "it, reflections of neon shopfronts rippling on the surface.",
  },
  {
    name: "Cloud time-lapse",
    icon: StarterIcons.landscape,
    prompt:
      "Time-lapse of clouds rolling fast over a mountain ridge at dawn, shadows sweeping " +
      "across the slopes, sky shifting from violet to gold.",
  },
  {
    name: "Cat and laser",
    icon: StarterIcons.sparkle,
    prompt:
      "A cat chasing a laser pointer across a sunlit kitchen floor, skidding on the tiles, " +
      "handheld camera following the dot.",
  },
  {
    name: "Night market",
    icon: StarterIcons.camera,
    prompt:
      "Slow dolly through a rainy night market, steam rising off the food stalls, neon signs " +
      "reflected in the puddles, crowd drifting past the lens.",
  },
  {
    name: "Coffee pour",
    icon: StarterIcons.bowl,
    prompt:
      "Macro shot of espresso pouring into a glass cup, crema swirling as it fills, warm " +
      "morning light raking across the counter.",
  },
  {
    name: "Ink in water",
    icon: StarterIcons.leaf,
    prompt:
      "Blue ink dropped into a tank of still water, blooming and curling into threads, backlit " +
      "against a white background, slow motion.",
  },
  {
    name: "City flyover",
    icon: StarterIcons.globe,
    prompt:
      "Aerial drone shot flying low over a dense city at blue hour, camera banking gently, " +
      "traffic streaming along the avenues below.",
  },
  {
    name: "Robot waking",
    icon: StarterIcons.robot,
    prompt:
      "A chrome robot sitting in a dim workshop opens its eyes and turns its head toward the " +
      "camera, dust drifting in a single shaft of light.",
  },
];

/** The `image`/`width`/`height` fields of one render request.
 *
 *  Deliberately NOT `imageInput.ts`'s own `imageFields`: that function's third
 *  case sends a CLIENT-computed size alongside `image`, and that arithmetic is
 *  the image route's own. `/api/ai/video` derives its default canvas off the
 *  reference itself (`_video_default_size`, D621), so with no size picked by
 *  hand `width`/`height` are left off entirely. */
function videoImageFields(
  base: AttachedImage | null,
  sizeFromImage: boolean,
  width: number,
  height: number,
): { image?: string; width?: number; height?: number } {
  if (!base) return { width, height };
  if (!sizeFromImage) return { image: base.path, width, height };
  return { image: base.path };
}

interface Run {
  started: VideoStarted;
  job: Job | null;
  done: boolean;
}

export function VideoStage({
  model,
  entry,
  traits,
}: {
  model: string;
  entry: AiCatalogModel;
  /** `selected.row.videoTraits` — the RESOLVED engine's own request shape.
   *  `null` on a machine where nothing serves video generation at all, or from
   *  a build old enough to predate this field — see `FALLBACK_TRAITS`. */
  traits: AiCatalogCapability["videoTraits"];
}) {
  const engineTraits = traits ?? FALLBACK_TRAITS;
  const framesRange = [engineTraits.minFrames, engineTraits.maxFrames] as const;
  const modelSteps = entry.defaults?.steps ?? engineTraits.defaultSteps;

  const [prompt, setPrompt] = useState(() => readParam("prompt") ?? "");
  const [width, setWidth] = useState(() => numParam("w", DEFAULTS.width, ...SIZE_RANGE));
  const [height, setHeight] = useState(() => numParam("h", DEFAULTS.height, ...SIZE_RANGE));
  const [frames, setFrames] = useState(() =>
    numParam("frames", engineTraits.defaultFrames, ...framesRange),
  );
  const [steps, setSteps] = useState(() => numParam("steps", modelSteps, ...STEPS_RANGE));
  const [seed, setSeed] = useState<string>(() => readParam("seed") ?? "");
  const { open: configOpen, toggle: toggleConfig, touched: configTouched } = useConfigOpen();
  const [run, setRun] = useState<Run | null>(null);
  const [gallery, setGallery] = useState<VideoStarted[]>([]);
  const [error, setError] = useState<string | null>(null);

  // A reference image (SPEC AI-15) — the same gate and attachment shape
  // `ImageStage.tsx` uses for its own base image (AI-9f), reused through
  // `imageInput.ts`.
  const editable = canEdit(engineTraits.supportsImage);
  const [attachment, setAttachment] = useState<AttachedImage | null>(() => {
    const path = readParam("img");
    return path ? { path, name: path.split("/").pop() ?? path } : null;
  });
  const [sizeFromImage, setSizeFromImage] = useState(true);
  const base = usableBase(engineTraits.supportsImage, attachment);
  const [attaching, setAttaching] = useState(false);
  const [showBase, setShowBase] = useState(false);
  // Is the size the REFERENCE's? Only with one attached, and only until
  // somebody picks a size themselves.
  const sizeIsTheImages = base !== null && sizeFromImage;

  useEffect(() => {
    const timer = window.setTimeout(() => {
      writeParams({
        prompt: prompt ? prompt : null,
        w: width !== DEFAULTS.width ? String(width) : null,
        h: height !== DEFAULTS.height ? String(height) : null,
        frames: frames !== engineTraits.defaultFrames ? String(frames) : null,
        steps: steps !== modelSteps ? String(steps) : null,
        seed: seed ? seed : null,
        // Omitted rather than nulled on an engine that cannot condition on
        // one — same rule `ImageStage.tsx` follows for `img`.
        ...(editable ? { img: attachment ? attachment.path : null } : {}),
      });
    }, 300);
    return () => window.clearTimeout(timer);
  }, [
    prompt, width, height, frames, steps, seed, modelSteps,
    engineTraits.defaultFrames, editable, attachment,
  ]);

  const { ref: boxRef } = useAutoGrow(prompt);

  const abortRef = useRef<AbortController | null>(null);
  // Set on the way in as well as cleared on the way out: the OS file dialog
  // `choose()` awaits can stay open arbitrarily long.
  const aliveRef = useRef(true);
  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
      abortRef.current?.abort();
    };
  }, []);

  const attach = (picked: AttachedImage) => {
    setAttachment(picked);
    // A fresh reference is a fresh size question.
    setSizeFromImage(true);
  };

  const choose = async () => {
    setError(null);
    setAttaching(true);
    try {
      const path = await pickFile({
        title: "Choose a reference image",
        types: ATTACH_TYPES,
      });
      // A cancel is an answer: nothing changes and nothing is said about it.
      if (path === null || !aliveRef.current) return;
      const name = path.split("/").pop() || path;
      // Still checked, filter or no filter — see ATTACH_EXTENSIONS.
      if (!ATTACH_EXTENSIONS.some((ext) => name.toLowerCase().endsWith(ext))) {
        setError(
          `${name} is not a PNG, JPEG or WebP — those are the three the renderer ` +
            "can read the size of.",
        );
        return;
      }
      attach({ path, name });
    } catch (e) {
      if (aliveRef.current) setError((e as Error).message);
    } finally {
      if (aliveRef.current) setAttaching(false);
    }
  };

  const generate = async (asked?: string) => {
    const wanted = (asked ?? prompt).trim();
    if (!wanted || (run && !run.done)) return;
    if (asked) setPrompt(asked);
    setError(null);
    try {
      const controller = new AbortController();
      abortRef.current = controller;
      const started = await startVideo({
        prompt: wanted,
        model,
        frames,
        steps,
        ...videoImageFields(base, sizeFromImage, width, height),
        ...(seed.trim() !== "" ? { seed: Number(seed) } : {}),
      });
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
        setGallery((g) => [started, ...g.filter((i) => i.jobId !== started.jobId)].slice(0, 12));
      } catch (e) {
        if ((e as Error).name === "AbortError") return;
        setError((e as Error).message);
        setRun(null);
      }
    } catch (e) {
      setError((e as Error).message);
    }
  };

  // Back to empty: the prompt, the result, and the attached reference.
  // Settings stay put, and so does the gallery strip.
  const clear = () => {
    setPrompt("");
    setRun(null);
    setError(null);
    setAttachment(null);
    boxRef.current?.focus();
  };

  const busy = !!run && !run.done;
  const job = busy ? run.job : null;
  const pct = job && job.total ? Math.min(100, ((job.done ?? 0) / job.total) * 100) : null;
  const settled = run?.started;

  return (
    <Card className="w-full flex-none gap-3 px-(--card-spacing) [--card-spacing:--spacing(6)]">
      <StageHeader title="Describe a video" configOpen={configOpen} onToggleConfig={toggleConfig} />

      <ComposerCard>
        <textarea
          ref={boxRef}
          rows={3}
          value={prompt}
          className={composerTextareaClass}
          placeholder="Describe the video…"
          onChange={(e) => setPrompt(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              void generate();
            }
          }}
        />
        {/* Clear at the top of this column, Generate at the bottom — the
            column's width is set by Generate, so nothing moves when Clear
            comes and goes. */}
        <ComposerSide>
          {!busy && run && <ClearButton title="Clear the prompt and the clip" onClick={clear} />}
          {busy ? (
            <StopButton onClick={() => void cancelJob(run.started.jobId).catch(() => {})}>
              Stop
            </StopButton>
          ) : (
            <RunButton
              disabled={!prompt.trim()}
              title="Enter to run · Shift+Enter for a new line"
              onClick={() => void generate()}
            >
              Generate
            </RunButton>
          )}
        </ComposerSide>
      </ComposerCard>

      {/* The reference image, on its own line below the composer — drawn only
          when the resolved engine can honour one at all (`editable`). */}
      {(base || editable) && (
        <div className="flex items-end justify-end gap-2">
          {base && (
            <AttachChip
              src={rawUrl(base.path)}
              onOpen={() => setShowBase(true)}
              onRemove={() => setAttachment(null)}
            />
          )}
          {editable && (
            <div className="flex flex-wrap items-center gap-2 px-1">
              <AttachButton
                title="Point at a picture already on this disk — nothing is copied"
                disabled={attaching}
                onClick={() => void choose()}
              >
                {StarterIcons.landscape}
                <span>{base ? "Replace" : "Add a reference image"}</span>
              </AttachButton>
              {attaching && <Tiny>Working…</Tiny>}
            </div>
          )}
        </div>
      )}

      <ConfigPanel open={configOpen} animated={configTouched.current}>
        {/* Hidden, not disabled, while the attached reference decides the
            size. */}
        {sizeIsTheImages ? (
          <RailField
            label="Size"
            action={<RailReset onClick={() => setSizeFromImage(false)}>Set a size</RailReset>}
            hint="Derived from the reference's own shape, on the engine's own canvas grid — read the reply for the exact size that rendered."
          >
            <span className="text-xs">From the attached reference</span>
          </RailField>
        ) : (
          <>
            <RailSlider
              label="Width"
              hint="Snapped to a multiple of 32, and shrunk if width×height is too large."
              min={SIZE_RANGE[0]}
              max={SIZE_RANGE[1]}
              step={32}
              value={width}
              fallback={DEFAULTS.width}
              onChange={setWidth}
            />
            <RailSlider
              label="Height"
              hint="Bigger is slower and needs more memory."
              min={SIZE_RANGE[0]}
              max={SIZE_RANGE[1]}
              step={32}
              value={height}
              fallback={DEFAULTS.height}
              onChange={setHeight}
            />
          </>
        )}
        <RailSlider
          label="Frames"
          hint="Rounded to the video engine's own valid grid — the number that runs may differ slightly."
          min={framesRange[0]}
          max={framesRange[1]}
          step={engineTraits.framesStep}
          value={frames}
          fallback={engineTraits.defaultFrames}
          onChange={setFrames}
        />
        <RailSlider
          label="Steps"
          hint="Denoising passes — more is slower and usually cleaner."
          min={STEPS_RANGE[0]}
          max={STEPS_RANGE[1]}
          step={1}
          value={steps}
          fallback={modelSteps}
          onChange={setSteps}
        />
        <RailField label="Seed" hint="Same seed + same prompt + same settings = the same video.">
          <Input
            type="text"
            inputMode="numeric"
            value={seed}
            placeholder="Random each time"
            onChange={(e) => setSeed(e.target.value.replace(/[^0-9]/g, ""))}
          />
        </RailField>
      </ConfigPanel>

      {base && (
        <Lightbox
          open={showBase}
          onClose={() => setShowBase(false)}
          label="The attached reference image"
        >
          <img
            src={rawUrl(base.path)}
            alt=""
            className="max-h-[calc(100vh-6rem)] max-w-full rounded-md object-contain"
          />
        </Lightbox>
      )}

      {/* Examples first, under the box they fill; hidden once a clip is on
          screen. */}
      {!run && <StarterCards samples={STARTERS} onPick={(s) => void generate(s.prompt)} />}

      {error && <StatusLine status="error">{error}</StatusLine>}

      {!run ? (
        <ResultSlot
          label="Result"
          capability="text-to-video"
          note="Your clip appears here. Describe one above, then Generate."
        />
      ) : (
        <AnswerBlock label="Result" status={busy ? "running" : null}>
          <figure className="m-0 flex flex-col gap-2">
            <div
              className="relative max-w-full self-start leading-none"
              style={{
                aspectRatio: `${run.started.width} / ${run.started.height}`,
                width: "100%",
              }}
            >
              {run.done ? (
                <video
                  key={run.started.jobId}
                  className="block size-full rounded-lg border border-border bg-muted/30 object-contain"
                  src={rawUrl(run.started.path) + "&t=" + run.started.jobId}
                  controls
                />
              ) : (
                <div
                  className="size-full rounded-lg border border-border bg-muted motion-safe:animate-pulse"
                  aria-hidden="true"
                />
              )}
            </div>
            {/* Unlike the image stage, the settled line STAYS after a render:
                `frames` is rounded to the engine's own grid, and a clip that
                took an hour is worth being able to reproduce. */}
            <figcaption className="flex flex-col gap-1.5 text-xs text-muted-foreground tabular-nums">
              {busy ? (
                <>
                  <span>{job?.detail || "Starting — a cold model loads first…"}</span>
                  {pct !== null && <ProgressBar pct={pct} />}
                </>
              ) : settled ? (
                <span>
                  {settled.width}×{settled.height} · {settled.frames} frames · {settled.steps} steps
                  ·{" "}
                  <button
                    type="button"
                    className="cursor-pointer border-0 bg-transparent p-0 font-[inherit] text-muted-foreground underline decoration-dotted hover:text-foreground"
                    title="Reuse this seed — the same prompt and settings render the same video"
                    onClick={() => setSeed(String(settled.seed))}
                  >
                    seed {settled.seed}
                  </button>
                </span>
              ) : null}
            </figcaption>
          </figure>
        </AnswerBlock>
      )}

      {/* Past clips, kept because a video render is the one call here that can
          cost an hour. */}
      {gallery.length > 0 && (
        <div className="flex gap-2 overflow-x-auto pb-0.5">
          {gallery.map((item) => (
            <video
              key={item.jobId}
              src={rawUrl(item.path) + "&t=" + item.jobId}
              className={cn(
                "h-[84px] cursor-pointer rounded-lg border border-border opacity-80 hover:opacity-100",
                run?.started.jobId === item.jobId && "border-ring opacity-100",
                // Disabled, not wired to a no-op click: swapping `run`
                // mid-render would silently drop the in-flight Stop button.
                busy && "pointer-events-none cursor-not-allowed opacity-40",
              )}
              title={
                busy
                  ? "Finish or stop the current render to view another clip"
                  : `${item.prompt} — seed ${item.seed}`
              }
              muted
              onClick={busy ? undefined : () => setRun({ started: item, job: null, done: true })}
            />
          ))}
        </div>
      )}
    </Card>
  );
}
