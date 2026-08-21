// The image stage: prompt in, picture out (SPEC AI-9).
//
// A render is job-shaped — `POST /api/ai/image` answers immediately with the
// job to watch and the SETTLED parameters (width snapped, steps clamped, seed
// invented), and everything shown afterwards echoes that reply, never the
// request: the render the user got is the one the caption must describe.
// While it denoises, the worker drops a preview beside the output path and
// this stage polls it, so minutes of diffusion are a picture sharpening rather
// than a bar crawling.
//
// The job deliberately OUTLIVES this stage (a render is fire-and-forget, like
// a download — it shows in Activity and survives a tab switch); Stop is the
// job's own cancel. Results are session state: the files land on disk through
// the server's own pipeline either way, this stage just does not curate them.
import { useEffect, useRef, useState } from "react";
import { cancelJob, type Job } from "@platform/lib/jobs";
import { rawUrl } from "@platform/lib/api";
import { startImage, watchJob, type ImageStarted } from "./playgroundClient";
import { readParam, writeParams } from "./AiModelsPlayground";

const DEFAULTS = { width: 1024, height: 1024, steps: 28, guidance: 4.0 };

interface Run {
  started: ImageStarted;
  job: Job | null;
  done: boolean;
}

export function PlaygroundImage({ model }: { model: string }) {
  const [prompt, setPrompt] = useState(() => readParam("prompt") ?? "");
  const [width, setWidth] = useState(() => Number(readParam("w") ?? DEFAULTS.width));
  const [height, setHeight] = useState(() => Number(readParam("h") ?? DEFAULTS.height));
  const [steps, setSteps] = useState(() => Number(readParam("steps") ?? DEFAULTS.steps));
  const [guidance, setGuidance] = useState(() => Number(readParam("guidance") ?? DEFAULTS.guidance));
  const [seed, setSeed] = useState<string>(() => readParam("seed") ?? "");
  const [advanced, setAdvanced] = useState(false);
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
        steps: steps !== DEFAULTS.steps ? String(steps) : null,
        guidance: guidance !== DEFAULTS.guidance ? String(guidance) : null,
        seed: seed ? seed : null,
      });
    }, 300);
    return () => window.clearTimeout(timer);
  }, [prompt, width, height, steps, guidance, seed]);

  const abortRef = useRef<AbortController | null>(null);
  // Only the WATCH stops on unmount — the render itself is a job the user can
  // still see (and cancel) in Activity, the same posture a download takes.
  useEffect(() => () => abortRef.current?.abort(), []);

  // The picture-in-progress: re-ask for the preview every 1.5s while the job
  // runs. A model with no fitted projection writes nothing there, which is the
  // ordinary case, not an error — the <img> just never gets to load.
  useEffect(() => {
    if (!run || run.done) return;
    const timer = window.setInterval(() => setPreviewTick((n) => n + 1), 1500);
    return () => window.clearInterval(timer);
  }, [run]);

  const generate = async () => {
    const asked = prompt.trim();
    if (!asked || (run && !run.done)) return;
    setError(null);
    setPreviewLive(false);
    try {
      const started = await startImage({
        prompt: asked,
        model,
        ...(width !== DEFAULTS.width ? { width } : {}),
        ...(height !== DEFAULTS.height ? { height } : {}),
        ...(steps !== DEFAULTS.steps ? { steps } : {}),
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
        setGallery((g) => [started, ...g].slice(0, 12));
      } catch (e) {
        if ((e as Error).name === "AbortError") return;
        setError((e as Error).message);
        setRun(null);
      }
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const stop = () => {
    if (run && !run.done) void cancelJob(run.started.jobId).catch(() => {});
  };

  const denoise =
    run && !run.done && run.job && run.job.total
      ? `Step ${run.job.done ?? 0} of ${run.job.total}`
      : run && !run.done
        ? run.job?.detail || "Starting the render…"
        : null;

  const settled = run?.started;

  return (
    <div className="pg-image">
      <div className="pg-image-input">
        <textarea
          rows={2}
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
        {run && !run.done ? (
          <button type="button" className="btn btn-secondary" onClick={stop}>
            Stop
          </button>
        ) : (
          <button
            type="button"
            className="btn btn-secondary"
            disabled={!prompt.trim()}
            onClick={() => void generate()}
          >
            Generate
          </button>
        )}
      </div>
      {error && <p className="pg-error">{error}</p>}
      {run && (
        <figure className="pg-image-result">
          {run.done ? (
            <img src={rawUrl(run.started.path) + "&t=" + run.started.jobId} alt={run.started.prompt} />
          ) : (
            <>
              <img
                src={rawUrl(run.started.previewPath) + "&t=" + previewTick}
                alt="Render in progress"
                style={previewLive ? undefined : { display: "none" }}
                onLoad={() => setPreviewLive(true)}
                onError={() => setPreviewLive(false)}
              />
              {!previewLive && <div className="pg-image-wait" aria-hidden="true" />}
            </>
          )}
          <figcaption className="pg-image-caption">
            {denoise ? (
              denoise
            ) : settled ? (
              // The SETTLED parameters — what actually ran (the server snaps
              // and clamps silently), with the seed one click from reuse.
              <>
                {settled.width}×{settled.height} · {settled.steps} steps · guidance{" "}
                {settled.guidance} ·{" "}
                <button
                  type="button"
                  className="pg-seed"
                  title="Reuse this seed — the same prompt renders the same picture"
                  onClick={() => setSeed(String(settled.seed))}
                >
                  seed {settled.seed}
                </button>
              </>
            ) : null}
          </figcaption>
        </figure>
      )}
      {gallery.length > 1 && (
        <div className="pg-image-strip">
          {gallery.slice(1).map((item) => (
            <img
              key={item.jobId}
              src={rawUrl(item.path) + "&t=" + item.jobId}
              alt={item.prompt}
              title={`${item.prompt} — seed ${item.seed}`}
              onClick={() => setRun({ started: item, job: null, done: true })}
            />
          ))}
        </div>
      )}
      <div className="pg-toolbar">
        <button type="button" className="pg-adv-toggle" onClick={() => setAdvanced((v) => !v)}>
          Advanced {advanced ? "▴" : "▾"}
        </button>
      </div>
      {advanced && (
        <div className="pg-advanced">
          <label>
            Width <span className="pg-adv-val">{width}</span>
            <input
              type="range"
              min={256}
              max={2048}
              step={16}
              value={width}
              onChange={(e) => setWidth(Number(e.target.value))}
            />
          </label>
          <label>
            Height <span className="pg-adv-val">{height}</span>
            <input
              type="range"
              min={256}
              max={2048}
              step={16}
              value={height}
              onChange={(e) => setHeight(Number(e.target.value))}
            />
          </label>
          <label>
            Steps <span className="pg-adv-val">{steps}</span>
            <input
              type="range"
              min={1}
              max={100}
              step={1}
              value={steps}
              onChange={(e) => setSteps(Number(e.target.value))}
            />
          </label>
          <label>
            Guidance <span className="pg-adv-val">{guidance}</span>
            <input
              type="range"
              min={0}
              max={20}
              step={0.5}
              value={guidance}
              onChange={(e) => setGuidance(Number(e.target.value))}
            />
          </label>
          <label className="pg-adv-wide">
            Seed
            <input
              type="text"
              inputMode="numeric"
              value={seed}
              placeholder="Blank — a fresh one each time"
              onChange={(e) => setSeed(e.target.value.replace(/[^0-9]/g, ""))}
            />
          </label>
        </div>
      )}
    </div>
  );
}
