// The video stage: prompt in, a short clip with audio out (SPEC §40).
//
// Shaped like the text and image stages (D431): heading with a cog,
// prompt, Generate — and every parameter behind the Config card, which is
// what replaced the settings rail this stage was first written with.
//
// `ImageStage`'s minimal sibling — no aspect chips, no live preview (this
// build has none), no guidance (H3 is CFG-distilled and takes no such
// parameter). What is genuinely different from an image render: the output
// is a `<video controls>` rather than an `<img>`, `frames` is a fourth
// numeric setting alongside width/height/steps, and a render can run for a
// long time — `onProgress`'s row survives a tab switch (it shows in
// Activity), same as the image stage, and only the WATCH stops on unmount.
// That length is also why this stage keeps the settled caption and the
// gallery strip the image stage dropped: a clip that cost an hour is worth
// naming the seed of, and worth being able to go back to.
import { useEffect, useRef, useState } from "react";
import { cancelJob, type Job } from "@platform/lib/jobs";
import { rawUrl, type AiCatalogCapability, type AiCatalogModel } from "@platform/lib/api";
import { startVideo, watchJob, type VideoStarted } from "./client";
import { Input } from "@platform/shadcn/ui/input";
import {
  ConfigPanel,
  RailField,
  RailSlider,
  ResultSlot,
  StageHeader,
  StarterCards,
  useAutoGrow,
  type Starter,
} from "./controls";
import { StarterIcons } from "./starterIcons";
import { numParam, readParam, writeParams } from "@apps/ai_models/lib/params";

// Small canvas by default — the first clip arriving quickly is the point,
// exactly like the image stage's 512² default. This is a UI choice
// independent of which engine is serving (H3's own valid canvas ceiling,
// `width * height <= 768 * 1344`, and the identical one LTX-2.3 shares —
// see `registry.py`'s own comment on why the pixel budget stays SHARED
// across every video runner — are both enforced server-side regardless).
const DEFAULTS = { width: 512, height: 512 };
const SIZE_RANGE = [256, 1344] as const;
// [2, 50]: the floor of 2 came from the dropped `h3-video` runner, which
// refused 1 step outright ("denoising steps must be in [2, 1000]"), and is
// kept as the app's own on that runner's removal (D468) — 1 step is not a
// meaningfully faster render on any engine. Both ends are shared across
// every engine, same as `SIZE_RANGE` — an app-chosen safety rail, not a
// fact about the engine's weights (`registry.py`'s own `MIN_VIDEO_FRAMES_N`
// comment makes the identical argument about the frame grid's own window).
const STEPS_RANGE = [2, 50] as const;

// **The fallback when the server sends no traits at all** — a capability row
// from a build old enough to predate this field, or one the caller built by
// hand for a test. **It must stay byte-for-byte the row
// `registry.video_traits_for` falls back to** (`ltx-video`'s, since D468
// dropped `h3-video` and with it the 5 + 17n / 864x480 / 20-step row both
// sides used to name): a client that guesses a different grid than the
// server snaps to draws a slider whose every value the render then moves,
// which is the exact "a control that lies about what will run" failure the
// `videoTraits` payload exists to close. If that server-side fallback moves
// again, this moves with it.
const FALLBACK_TRAITS: NonNullable<AiCatalogCapability["videoTraits"]> = {
  framesBase: 1,
  framesStep: 8,
  minFrames: 9,
  maxFrames: 169,
  defaultFrames: 97,
  defaultWidth: 704,
  defaultHeight: 480,
  defaultSteps: 8,
};

// Eight authored examples — two pages of four (D465). Every one names a
// subject AND a MOTION, because motion is the whole difference between this
// stage and the image one: a prompt that describes only a scene gets a clip
// that barely moves, and a newcomer has no way to know that is their prompt's
// fault rather than the model's.
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
  /** `selected.row.videoTraits` — the RESOLVED engine's own request shape,
   *  never a fact about `entry` (one model, not one engine). `null` on a
   *  machine where nothing serves video generation at all, or from a build
   *  old enough to predate this field — see `FALLBACK_TRAITS`. */
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
  const [configOpen, setConfigOpen] = useState(true);
  const [run, setRun] = useState<Run | null>(null);
  const [gallery, setGallery] = useState<VideoStarted[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      writeParams({
        prompt: prompt ? prompt : null,
        w: width !== DEFAULTS.width ? String(width) : null,
        h: height !== DEFAULTS.height ? String(height) : null,
        frames: frames !== engineTraits.defaultFrames ? String(frames) : null,
        steps: steps !== modelSteps ? String(steps) : null,
        seed: seed ? seed : null,
      });
    }, 300);
    return () => window.clearTimeout(timer);
  }, [prompt, width, height, frames, steps, seed, modelSteps, engineTraits.defaultFrames]);

  const { ref: boxRef, grow } = useAutoGrow();

  const abortRef = useRef<AbortController | null>(null);
  useEffect(() => () => abortRef.current?.abort(), []);

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
        width,
        height,
        frames,
        steps,
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

  // Back to empty: the prompt and the result. Settings stay put, and so does
  // the gallery strip — a clip already rendered is not what Clear is about.
  const clear = () => {
    setPrompt("");
    setRun(null);
    setError(null);
    const box = boxRef.current;
    if (box) {
      box.style.height = "auto";
      box.focus();
    }
  };

  const busy = !!run && !run.done;
  const job = busy ? run.job : null;
  const pct = job && job.total ? Math.min(100, ((job.done ?? 0) / job.total) * 100) : null;
  const settled = run?.started;

  return (
    <div className={"pg-work" + (configOpen ? " has-config" : "")}>
      {/* The action, and the way to the settings. The hero card above names
          the model and its state. */}
      <StageHeader
        title="Describe a video"
        configOpen={configOpen}
        onToggleConfig={() => setConfigOpen((open) => !open)}
      />

      <div className="pg-composer">
        <textarea
          ref={boxRef}
          rows={3}
          value={prompt}
          placeholder="Describe the video…"
          onChange={(e) => {
            setPrompt(e.target.value);
            grow();
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              void generate();
            }
          }}
        />
        {/* Clear at the top of this column, Generate at the bottom —
            the column's width is set by Generate, the wider of the two, so
            nothing moves when Clear comes and goes. */}
        <div className="pg-composer-side">
          {!busy && run && (
            <button
              type="button"
              className="pg-ghost-btn pg-clear"
              title="Clear the prompt and the clip"
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
              disabled={!prompt.trim()}
              title="Enter to run · Shift+Enter for a new line"
              onClick={() => void generate()}
            >
              Generate <kbd className="pg-kbd">⏎</kbd>
            </button>
          )}
        </div>
      </div>

      {/* Examples first, under the box they fill; hidden once a clip is on
          screen, which is what that space is then for. */}
      {!run && <StarterCards samples={STARTERS} onPick={(s) => void generate(s.prompt)} />}

      {/* Every knob is behind the cog; the surface above is prompt and
          Generate. The four sliders in the order a render is thought about:
          how big, how long, how carefully — then the seed. */}
      <ConfigPanel open={configOpen}>
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
        <RailField
          label="Seed"
          hint="Same seed + same prompt + same settings = the same video."
        >
          <Input
            type="text"
            inputMode="numeric"
            value={seed}
            placeholder="Random each time"
            onChange={(e) => setSeed(e.target.value.replace(/[^0-9]/g, ""))}
          />
        </RailField>
      </ConfigPanel>

      {error && <p className="pg-error">{error}</p>}

      {!run ? (
        <ResultSlot
          label="Result"
          capability="text-to-video"
          note="Your clip appears here. Describe one above, then Generate."
        />
      ) : (
        <div className="pg-answer-block">
          <p className="pg-answer-label">Result</p>
          <figure className="pg-image-result">
            <div
              className="pg-image-frame"
              style={{
                aspectRatio: `${run.started.width} / ${run.started.height}`,
                width: "100%",
              }}
            >
              {run.done ? (
                <video
                  key={run.started.jobId}
                  src={rawUrl(run.started.path) + "&t=" + run.started.jobId}
                  controls
                />
              ) : (
                <div className="pg-image-wait" aria-hidden="true" />
              )}
            </div>
            {/* Unlike the image stage, the settled line STAYS after a render:
                `frames` is rounded to the engine's own grid, so what ran is
                genuinely not what was asked for — and a clip that took an hour
                is worth being able to reproduce, which is what the seed button
                is for. */}
            <figcaption className="pg-image-caption">
              {busy ? (
                <>
                  <span>{job?.detail || "Starting — a cold model loads first…"}</span>
                  {pct !== null && (
                    <span className="pg-bar">
                      <span className="pg-bar-fill" style={{ width: `${pct}%` }} />
                    </span>
                  )}
                </>
              ) : settled ? (
                <>
                  {settled.width}×{settled.height} · {settled.frames} frames · {settled.steps} steps
                  ·{" "}
                  <button
                    type="button"
                    className="pg-seed"
                    title="Reuse this seed — the same prompt and settings render the same video"
                    onClick={() => setSeed(String(settled.seed))}
                  >
                    seed {settled.seed}
                  </button>
                </>
              ) : null}
            </figcaption>
          </figure>
        </div>
      )}

      {/* Past clips, kept because a video render is the one call here that can
          cost an hour — losing it to the next Generate is not a fair trade. */}
      {gallery.length > 0 && (
        <div className="pg-image-strip">
          {gallery.map((item) => (
            <video
              key={item.jobId}
              src={rawUrl(item.path) + "&t=" + item.jobId}
              className={
                (run?.started.jobId === item.jobId ? "active" : "") + (busy ? " disabled" : "")
              }
              // Disabled, not wired to a no-op click: a render here can run
              // for HOURS (unlike the image stage's seconds), so swapping
              // `run` mid-render would silently drop the in-flight Stop
              // button and its progress -- Generate would even re-enable
              // while the render kept going underneath. Picking a past
              // clip is safe once there is nothing left to lose.
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
    </div>
  );
}
