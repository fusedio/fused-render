// The video stage: prompt in, a short clip with audio out (SPEC §40).
//
// `ImageStage`'s minimal sibling — no aspect chips, no live preview (this
// build has none), no guidance (H3 is CFG-distilled and takes no such
// parameter). What is genuinely different from an image render: the output
// is a `<video controls>` rather than an `<img>`, `frames` is a fourth
// numeric setting alongside width/height/steps, and a render can run for a
// long time — `onProgress`'s row survives a tab switch (it shows in
// Activity), same as the image stage, and only the WATCH stops on unmount.
import { useEffect, useRef, useState } from "react";
import { cancelJob, type Job } from "@platform/lib/jobs";
import { rawUrl, type AiCatalogModel } from "@platform/lib/api";
import { startVideo, watchJob, type VideoStarted } from "./client";
import { RailSection, RailSlider, StarterPrompts } from "./controls";
import { numParam, readParam, writeParams } from "@apps/ai_models/lib/params";

// Small canvas and few frames by default — the first clip arriving quickly is
// the point, exactly like the image stage's 512² default. H3's own valid
// canvas ceiling is `width * height <= 768 * 1344`, enforced server-side.
const DEFAULTS = { width: 512, height: 512, frames: 90, steps: 20 };
const SIZE_RANGE = [256, 1344] as const;
// h3's own frame grid is `5 + 17n`; the slider steps by 17 so every value it
// can land on is one the server will not have to move.
const FRAMES_RANGE = [5, 345] as const;
const STEPS_RANGE = [1, 50] as const;

const STARTERS = [
  "A paper boat drifting down a rain-soaked street, cinematic",
  "Time-lapse of clouds rolling over a mountain ridge at dawn",
  "A cat chasing a laser pointer across a sunlit kitchen floor",
];

interface Run {
  started: VideoStarted;
  job: Job | null;
  done: boolean;
}

export function VideoStage({ model, entry }: { model: string; entry: AiCatalogModel }) {
  const modelSteps = entry.defaults?.steps ?? DEFAULTS.steps;

  const [prompt, setPrompt] = useState(() => readParam("prompt") ?? "");
  const [width, setWidth] = useState(() => numParam("w", DEFAULTS.width, ...SIZE_RANGE));
  const [height, setHeight] = useState(() => numParam("h", DEFAULTS.height, ...SIZE_RANGE));
  const [frames, setFrames] = useState(() => numParam("frames", DEFAULTS.frames, ...FRAMES_RANGE));
  const [steps, setSteps] = useState(() => numParam("steps", modelSteps, ...STEPS_RANGE));
  const [seed, setSeed] = useState<string>(() => readParam("seed") ?? "");
  const [railOpen, setRailOpen] = useState(false);
  const [run, setRun] = useState<Run | null>(null);
  const [gallery, setGallery] = useState<VideoStarted[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      writeParams({
        prompt: prompt ? prompt : null,
        w: width !== DEFAULTS.width ? String(width) : null,
        h: height !== DEFAULTS.height ? String(height) : null,
        frames: frames !== DEFAULTS.frames ? String(frames) : null,
        steps: steps !== modelSteps ? String(steps) : null,
        seed: seed ? seed : null,
      });
    }, 300);
    return () => window.clearTimeout(timer);
  }, [prompt, width, height, frames, steps, seed, modelSteps]);

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

  const busy = !!run && !run.done;
  const job = busy ? run.job : null;
  const pct = job && job.total ? Math.min(100, ((job.done ?? 0) / job.total) * 100) : null;
  const settled = run?.started;

  return (
    <div className={"pg-work" + (railOpen ? " rail-open" : "")}>
      <div className="pg-main pg-video">
        <div className="pg-composer">
          <textarea
            rows={1}
            value={prompt}
            placeholder="Describe the video…"
            onChange={(e) => setPrompt(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                void generate();
              }
            }}
          />
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
              onClick={() => void generate()}
            >
              Generate
            </button>
          )}
        </div>
        {error && <p className="pg-error">{error}</p>}

        {!run && (
          <div className="pg-empty-stage">
            <p className="pg-empty-title">Make a video with {entry.nickname || entry.label}</p>
            <p className="pg-empty-sub">
              Runs entirely on this machine — a render can take a long time, so watch it in
              Activity if you switch tabs.
            </p>
            <StarterPrompts title="Try one:" prompts={STARTERS} onPick={(p) => void generate(p)} />
          </div>
        )}

        {run && (
          <figure className="pg-video-result">
            {run.done ? (
              <video
                key={run.started.jobId}
                src={rawUrl(run.started.path) + "&t=" + run.started.jobId}
                controls
                style={{ aspectRatio: `${run.started.width} / ${run.started.height}` }}
              />
            ) : (
              <div
                className="pg-image-wait"
                style={{ aspectRatio: `${run.started.width} / ${run.started.height}` }}
                aria-hidden="true"
              />
            )}
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
        )}

        {gallery.length > 0 && (
          <div className="pg-image-strip">
            {gallery.map((item) => (
              <video
                key={item.jobId}
                src={rawUrl(item.path) + "&t=" + item.jobId}
                className={run?.started.jobId === item.jobId ? "active" : undefined}
                title={`${item.prompt} — seed ${item.seed}`}
                muted
                onClick={() => setRun({ started: item, job: null, done: true })}
              />
            ))}
          </div>
        )}

        <div className="pg-under">
          <button type="button" className="pg-ghost-btn pg-rail-toggle" onClick={() => setRailOpen((v) => !v)}>
            {railOpen ? "Hide controls" : "Controls"}
          </button>
        </div>
      </div>

      <aside className="pg-rail" aria-label="Video settings">
        <RailSection title="Canvas">
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
        </RailSection>
        <RailSection title="Length">
          <RailSlider
            label="Frames"
            hint="Rounded to h3's own valid grid — the number that runs may differ slightly."
            min={FRAMES_RANGE[0]}
            max={FRAMES_RANGE[1]}
            step={17}
            value={frames}
            fallback={DEFAULTS.frames}
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
        </RailSection>
        <RailSection title="Seed">
          <label className="pg-ctl">
            <input
              className="pg-rail-input"
              type="text"
              inputMode="numeric"
              value={seed}
              placeholder="Random each time"
              onChange={(e) => setSeed(e.target.value.replace(/[^0-9]/g, ""))}
            />
            <span className="pg-ctl-hint">
              Same seed + same prompt + same settings = the same video.
            </span>
          </label>
        </RailSection>
      </aside>
    </div>
  );
}
