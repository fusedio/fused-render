// The image stage: prompt in, picture out (SPEC AI-9).
//
// The controls follow what the image playgrounds converged on (fal, Midjourney
// web, Ideogram, Leonardo): ASPECT RATIO chips, not raw width×height sliders —
// people think in shapes, developer forms think in pixels — with the exact
// size still available under Custom, because the chips are a view over the
// same `w`/`h` URL params, never a new vocabulary. Speed is a chip row too,
// and its numbers come from the CATALOG's per-model hint: FLUX.2 klein is
// step-distilled and was benchmarked at 4 steps (D310) — the generic 28-step
// default turned a ~30-second first image into minutes, which is the
// difference between a playground and a demo that appears broken. A model
// with no hint keeps the server's 28.
//
// A render is job-shaped — the reply carries the job to watch and the SETTLED
// parameters (width snapped, steps clamped, seed invented), and the caption
// echoes that reply, never the request. While it denoises the worker drops a
// preview beside the output path and this stage polls it; the job survives a
// tab switch on purpose (it shows in Activity), so only the WATCH stops on
// unmount.
import { useEffect, useRef, useState } from "react";
import { cancelJob, type Job } from "@platform/lib/jobs";
import { rawUrl, type AiCatalogModel } from "@platform/lib/api";
import { startImage, watchJob, type ImageStarted } from "./playgroundClient";
import { RailChips, RailSection, RailSlider, StarterPrompts } from "./PlaygroundControls";
import { numParam, readParam, writeParams } from "./AiModelsPlayground";

const SERVER_STEPS = 28;
const DEFAULTS = { width: 1024, height: 1024, guidance: 4.0 };

// Multiple-of-16 sizes near one megapixel — the grid FLUX-class models are
// happiest on. The chip writes this pair into the same `w`/`h` params the
// custom sliders edit; a pair matching no chip lights none of them.
const ASPECTS = [
  { value: "1:1", label: "1:1", title: "Square — 1024×1024", width: 1024, height: 1024 },
  { value: "3:4", label: "3:4", title: "Portrait — 864×1152", width: 864, height: 1152 },
  { value: "4:3", label: "4:3", title: "Landscape — 1152×864", width: 1152, height: 864 },
  { value: "16:9", label: "16:9", title: "Wide — 1280×720", width: 1280, height: 720 },
  { value: "9:16", label: "9:16", title: "Tall — 720×1280", width: 720, height: 1280 },
] as const;

const STARTERS = [
  "A lighthouse on a cliff at golden hour, oil painting",
  "Isometric cutaway of a cozy coffee shop, warm light, detailed",
  "Studio photo of a chrome robot holding a daisy, soft shadows",
];

interface Run {
  started: ImageStarted;
  job: Job | null;
  done: boolean;
}

export function PlaygroundImage({ model, entry }: { model: string; entry: AiCatalogModel }) {
  // The model's own benchmarked step count, when the curation measured one.
  const modelSteps = entry.defaults?.steps ?? SERVER_STEPS;
  const speedChips =
    entry.defaults?.steps != null
      ? [
          { value: "quick", label: `Quick · ${modelSteps}`, title: `${modelSteps} steps — what this model was benchmarked at`, steps: modelSteps },
          { value: "balanced", label: `Finer · ${Math.min(modelSteps * 3, SERVER_STEPS)}`, title: "More denoising steps — slower, sometimes cleaner", steps: Math.min(modelSteps * 3, SERVER_STEPS) },
          { value: "fine", label: `Max · ${SERVER_STEPS}`, title: `${SERVER_STEPS} steps — the server's generic default`, steps: SERVER_STEPS },
        ]
      : null;

  const [prompt, setPrompt] = useState(() => readParam("prompt") ?? "");
  const [width, setWidth] = useState(() => numParam("w", DEFAULTS.width));
  const [height, setHeight] = useState(() => numParam("h", DEFAULTS.height));
  const [steps, setSteps] = useState(() => numParam("steps", modelSteps));
  const [guidance, setGuidance] = useState(() => numParam("guidance", DEFAULTS.guidance));
  const [seed, setSeed] = useState<string>(() => readParam("seed") ?? "");
  const [railOpen, setRailOpen] = useState(false);
  const [run, setRun] = useState<Run | null>(null);
  const [gallery, setGallery] = useState<ImageStarted[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [previewTick, setPreviewTick] = useState(0);
  const [previewLive, setPreviewLive] = useState(false);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      writeParams({
        prompt: prompt ? prompt : null,
        w: width !== DEFAULTS.width ? String(width) : null,
        h: height !== DEFAULTS.height ? String(height) : null,
        steps: steps !== modelSteps ? String(steps) : null,
        guidance: guidance !== DEFAULTS.guidance ? String(guidance) : null,
        seed: seed ? seed : null,
      });
    }, 300);
    return () => window.clearTimeout(timer);
  }, [prompt, width, height, steps, guidance, seed, modelSteps]);

  const abortRef = useRef<AbortController | null>(null);
  useEffect(() => () => abortRef.current?.abort(), []);

  useEffect(() => {
    if (!run || run.done) return;
    const timer = window.setInterval(() => setPreviewTick((n) => n + 1), 1500);
    return () => window.clearInterval(timer);
  }, [run]);

  const aspect = ASPECTS.find((a) => a.width === width && a.height === height)?.value ?? null;
  const speed = speedChips?.find((c) => c.steps === steps)?.value ?? null;

  const generate = async (asked?: string) => {
    const wanted = (asked ?? prompt).trim();
    if (!wanted || (run && !run.done)) return;
    if (asked) setPrompt(asked);
    setError(null);
    setPreviewLive(false);
    try {
      const started = await startImage({
        prompt: wanted,
        model,
        ...(width !== DEFAULTS.width ? { width } : {}),
        ...(height !== DEFAULTS.height ? { height } : {}),
        // Always sent: the stage's default is the MODEL's (4 for klein), and
        // leaving it off would hand the server its generic 28.
        steps,
        ...(guidance !== DEFAULTS.guidance ? { guidance } : {}),
        ...(seed.trim() !== "" ? { seed: Number(seed) } : {}),
      });
      setRun({ started, job: null, done: false });
      const controller = new AbortController();
      abortRef.current = controller;
      try {
        await watchJob(started.jobId, controller.signal, (job) =>
          setRun((r) => (r && r.started.jobId === started.jobId ? { ...r, job } : r)),
        );
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
      <div className="pg-main pg-image">
        <div className="pg-composer">
          <textarea
            rows={1}
            value={prompt}
            placeholder="Describe the picture…"
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
            <p className="pg-empty-title">Make a picture with {entry.nickname || entry.label}</p>
            <p className="pg-empty-sub">
              {entry.defaults?.steps != null
                ? "Runs entirely on this machine — the Quick setting is what this model was benchmarked at."
                : "Runs entirely on this machine."}
            </p>
            <StarterPrompts title="Try one:" prompts={STARTERS} onPick={(p) => void generate(p)} />
          </div>
        )}

        {run && (
          <figure className="pg-image-result">
            {run.done ? (
              <img
                src={rawUrl(run.started.path) + "&t=" + run.started.jobId}
                alt={run.started.prompt}
                style={{ aspectRatio: `${run.started.width} / ${run.started.height}` }}
              />
            ) : (
              <>
                <img
                  src={rawUrl(run.started.previewPath) + "&t=" + previewTick}
                  alt="Render in progress"
                  style={
                    previewLive
                      ? { aspectRatio: `${run.started.width} / ${run.started.height}` }
                      : { display: "none" }
                  }
                  onLoad={() => setPreviewLive(true)}
                  onError={() => setPreviewLive(false)}
                />
                {!previewLive && (
                  <div
                    className="pg-image-wait"
                    style={{ aspectRatio: `${run.started.width} / ${run.started.height}` }}
                    aria-hidden="true"
                  />
                )}
              </>
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
                // The SETTLED parameters — what actually ran (the server
                // snaps and clamps silently), with the seed one click away.
                <>
                  {settled.width}×{settled.height} · {settled.steps} steps · guidance{" "}
                  {settled.guidance} ·{" "}
                  <button
                    type="button"
                    className="pg-seed"
                    title="Reuse this seed — the same prompt and settings render the same picture"
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
              <img
                key={item.jobId}
                src={rawUrl(item.path) + "&t=" + item.jobId}
                alt={item.prompt}
                className={run?.started.jobId === item.jobId ? "active" : undefined}
                title={`${item.prompt} — seed ${item.seed}`}
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

      <aside className="pg-rail" aria-label="Image settings">
        <RailSection title="Shape">
          <RailChips
            options={ASPECTS.map(({ value, label, title }) => ({ value, label, title }))}
            active={aspect}
            onPick={(value) => {
              const pick = ASPECTS.find((a) => a.value === value);
              if (pick) {
                setWidth(pick.width);
                setHeight(pick.height);
              }
            }}
          />
          <details className="pg-custom">
            <summary>Custom size{aspect === null ? ` — ${width}×${height}` : ""}</summary>
            <RailSlider
              label="Width"
              hint="Snapped to a multiple of 16 by the server."
              min={256}
              max={2048}
              step={16}
              value={width}
              fallback={DEFAULTS.width}
              onChange={setWidth}
            />
            <RailSlider
              label="Height"
              hint="Bigger is slower and needs more memory."
              min={256}
              max={2048}
              step={16}
              value={height}
              fallback={DEFAULTS.height}
              onChange={setHeight}
            />
          </details>
        </RailSection>
        <RailSection title="Quality">
          {speedChips && (
            <RailChips
              options={speedChips.map(({ value, label, title }) => ({ value, label, title }))}
              active={speed}
              onPick={(value) => {
                const pick = speedChips.find((c) => c.value === value);
                if (pick) setSteps(pick.steps);
              }}
            />
          )}
          <RailSlider
            label="Steps"
            hint={
              entry.defaults?.steps != null
                ? "This model is distilled for few steps — more is slower, rarely better."
                : "Denoising passes — more is slower and usually cleaner."
            }
            min={1}
            max={100}
            step={1}
            value={steps}
            fallback={modelSteps}
            onChange={setSteps}
          />
          <RailSlider
            label="Guidance"
            hint="How literally the prompt is followed. Very high looks overcooked."
            min={0}
            max={20}
            step={0.5}
            value={guidance}
            fallback={DEFAULTS.guidance}
            onChange={setGuidance}
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
              Same seed + same prompt + same settings = the same picture.
            </span>
          </label>
        </RailSection>
      </aside>
    </div>
  );
}
