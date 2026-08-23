// The image stage: prompt in, picture out (SPEC AI-9).
//
// Shaped like the text stage (D431): heading, prompt, Generate. Every
// parameter — chips included — lives behind the Config fold.
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
// parameters (width snapped, steps clamped, seed invented). While it denoises
// the worker drops a preview beside the output path and this stage polls it;
// the job survives a tab switch on purpose (it shows in Activity), so only the
// WATCH stops on unmount.
import { useEffect, useRef, useState } from "react";
import { cancelJob, type Job } from "@platform/lib/jobs";
import { rawUrl, type AiCatalogModel } from "@platform/lib/api";
import { startImage, watchJob, type ImageStarted } from "./client";
import { MenuIcons } from "@platform/ui/MenuIcons";
import { ConfigPanel, RailChips, RailSlider, StageHeader, StarterPrompts } from "./controls";
import { numParam, readParam, writeParams } from "@apps/ai_models/lib/params";

const SERVER_STEPS = 28;
// Small, fast AND wide on purpose: 480x272 renders in a fraction of 1024²'s
// time and is plenty to judge a prompt by — the first picture arriving quickly
// IS the playground's pitch — and 16:9 is the shape the result reads best at
// now that the frame spans the whole column. The Custom sliders still go to
// 2048 for anyone who wants a big one. 272, not the 275 the height was asked
// for: the route floors every side to a multiple of 16 (`side - side % 16`,
// ai_runtime.py), so 275 RENDERS as 272 and a default saying 275 would be a
// control lying about what runs. Guidance 1 because the shortlist's defaults
// are guidance-distilled (FLUX.2 klein bakes the prompt-following in — CFG on
// top only slows it down and overcooks the colours).
const DEFAULTS = { width: 480, height: 272, guidance: 1.0 };
// The rail's slider bounds, in one place so a URL value and a dragged value
// cannot disagree about what the control's scale is.
const SIZE_RANGE = [256, 2048] as const;
const STEPS_RANGE = [1, 100] as const;
const GUIDANCE_RANGE = [0, 20] as const;

// Multiple-of-16 pairs, all small by default. The chip writes its pair into the
// same `w`/`h` params the custom sliders edit; a pair matching no chip lights
// none of them — including a saved link from before 16:9 was re-footed onto the
// default size, which now lights nothing rather than lying.
const ASPECTS = [
  { value: "1:1", label: "1:1", title: "Square — 512×512", width: 512, height: 512 },
  { value: "3:4", label: "3:4", title: "Portrait — 480×640", width: 480, height: 640 },
  { value: "4:3", label: "4:3", title: "Landscape — 640×480", width: 640, height: 480 },
  // The default pair, so a fresh stage lights a chip rather than none. 480/272
  // is 1.76 rather than 1.778 — the nearest multiple-of-16 pair to the size
  // asked for, and the same rounding SDXL's own "16:9" bucket carries.
  { value: "16:9", label: "16:9", title: "Wide — 480×272", width: 480, height: 272 },
  { value: "9:16", label: "9:16", title: "Tall — 432×768", width: 432, height: 768 },
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

export function ImageStage({ model, entry }: { model: string; entry: AiCatalogModel }) {
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
  // Clamped to the rail's own ranges. The image route SETTLES what it is sent
  // rather than refusing it, so a wild number here is not the 400 the chat
  // stage risks — but a slider pinned off its own scale by a URL is still a
  // control that lies about what will run.
  const [width, setWidth] = useState(() => numParam("w", DEFAULTS.width, ...SIZE_RANGE));
  const [height, setHeight] = useState(() => numParam("h", DEFAULTS.height, ...SIZE_RANGE));
  const [steps, setSteps] = useState(() => numParam("steps", modelSteps, ...STEPS_RANGE));
  const [guidance, setGuidance] = useState(() =>
    numParam("guidance", DEFAULTS.guidance, ...GUIDANCE_RANGE),
  );
  const [seed, setSeed] = useState<string>(() => readParam("seed") ?? "");
  const [run, setRun] = useState<Run | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [previewTick, setPreviewTick] = useState(0);
  const [previewLive, setPreviewLive] = useState(false);
  const [configOpen, setConfigOpen] = useState(false);

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
  const boxRef = useRef<HTMLTextAreaElement | null>(null);
  useEffect(() => () => abortRef.current?.abort(), []);

  // Grows with the prompt so a Shift+Enter newline is visible.
  const grow = () => {
    const box = boxRef.current;
    if (!box) return;
    box.style.height = "auto";
    box.style.height = Math.min(box.scrollHeight, 180) + "px";
  };

  // Keyed on the STARTED reply, not on `run`: the watch's onTick rewrites
  // `run` every poll (a fresh `{...r, job}`), so an effect keyed on the whole
  // object was torn down and rebuilt each second and the 1500ms timer never
  // lived long enough to fire once — the live preview never advanced. Same
  // shape TranscribeStage uses for its partial-transcript tail.
  const rendering = run && !run.done ? run.started : null;
  useEffect(() => {
    if (!rendering) return;
    const timer = window.setInterval(() => setPreviewTick((n) => n + 1), 1500);
    return () => window.clearInterval(timer);
  }, [rendering]);

  const aspect = ASPECTS.find((a) => a.width === width && a.height === height)?.value ?? null;
  const speed = speedChips?.find((c) => c.steps === steps)?.value ?? null;

  const generate = async (asked?: string) => {
    const wanted = (asked ?? prompt).trim();
    if (!wanted || (run && !run.done)) return;
    if (asked) setPrompt(asked);
    setError(null);
    setPreviewLive(false);
    try {
      // Published BEFORE the first await: unmounting while this POST is in
      // flight used to leave the ref null, so the cleanup aborted nothing and
      // the watch below polled /api/jobs once a second from a dead component
      // for the whole render. watchJob checks the signal on entry, so an abort
      // that lands during the POST throws AbortError straight out.
      const controller = new AbortController();
      abortRef.current = controller;
      const started = await startImage({
        prompt: wanted,
        model,
        // Always sent, all four: the stage's defaults are its own (480×272,
        // the model's steps, guidance 1), and leaving any off would hand the
        // server its generic 1024² / 28 / 4.0.
        width,
        height,
        steps,
        guidance,
        ...(seed.trim() !== "" ? { seed: Number(seed) } : {}),
      });
      setRun({ started, job: null, done: false });
      try {
        const outcome = await watchJob(started.jobId, controller.signal, (job) =>
          setRun((r) => (r && r.started.jobId === started.jobId ? { ...r, job } : r)),
        );
        // Stop was pressed. The worker died before writing the output, so
        // marking this done would render an <img> at a path that holds
        // nothing and file the dead render in the gallery strip.
        if (outcome.state === "cancelled") {
          setRun(null);
          return;
        }
        setRun((r) => (r && r.started.jobId === started.jobId ? { ...r, done: true } : r));
      } catch (e) {
        if ((e as Error).name === "AbortError") return;
        setError((e as Error).message);
        setRun(null);
      }
    } catch (e) {
      setError((e as Error).message);
    }
  };

  // Back to empty. Settings stay put — this clears the prompt and its result,
  // not the setup.
  const clear = () => {
    setPrompt("");
    setRun(null);
    setError(null);
    setPreviewLive(false);
    const box = boxRef.current;
    if (box) {
      box.style.height = "auto";
      box.focus();
    }
  };

  const busy = !!run && !run.done;
  const job = busy ? run.job : null;
  const pct = job && job.total ? Math.min(100, ((job.done ?? 0) / job.total) * 100) : null;
  // One box for shimmer, preview and final picture, so the column cannot
  // resize mid-render — the worker's preview is a thumbnail, and sizing by its
  // own pixels is what made the layout jump. Full parent width by request: the
  // run's own ratio gives the height, so the render sizes to the page rather
  // than to its pixel count — a 480×272 default fills the column instead of
  // sitting at 80% of it. A portrait ratio is therefore TALL (a 9:16 render is
  // ~1.8 column widths high); the old 80%/50vh caps are what that trades away.
  const shot = run
    ? {
        aspectRatio: `${run.started.width} / ${run.started.height}`,
        width: "100%",
      }
    : undefined;

  return (
    <div className={"pg-work" + (configOpen ? " has-config" : "")}>
      {/* The action, and the way to the settings. The hero card above names
          the model and its state. */}
      <StageHeader
        title="Describe a picture"
        configOpen={configOpen}
        onToggleConfig={() => setConfigOpen((open) => !open)}
      />

      <div className="pg-composer">
          <textarea
            ref={boxRef}
            rows={2}
            value={prompt}
            placeholder="Describe the picture…"
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
          {!busy && run && (
            <button
              type="button"
              className="pg-ghost-btn pg-clear"
              title="Clear the prompt and the picture"
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

      {/* Examples first, under the box they fill; hidden once a picture is on
          screen, which is what that space is then for. */}
      {!run && <StarterPrompts prompts={STARTERS} onPick={(p) => void generate(p)} />}

      {/* Chips lead the panel; sliders and the seed follow. */}
      <ConfigPanel open={configOpen}>
        <div className="pg-config-chips">
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
        </div>
        <RailSlider
          label="Width"
          hint="Snapped to a multiple of 16 by the server."
          min={SIZE_RANGE[0]}
          max={SIZE_RANGE[1]}
          step={16}
          value={width}
          fallback={DEFAULTS.width}
          onChange={setWidth}
        />
        <RailSlider
          label="Height"
          hint="Bigger is slower and needs more memory."
          min={SIZE_RANGE[0]}
          max={SIZE_RANGE[1]}
          step={16}
          value={height}
          fallback={DEFAULTS.height}
          onChange={setHeight}
        />
        <RailSlider
          label="Steps"
          hint={
            entry.defaults?.steps != null
              ? "This model is distilled for few steps — more is slower, rarely better."
              : "Denoising passes — more is slower and usually cleaner."
          }
          min={STEPS_RANGE[0]}
          max={STEPS_RANGE[1]}
          step={1}
          value={steps}
          fallback={modelSteps}
          onChange={setSteps}
        />
        <RailSlider
          label="Guidance"
          hint="How literally the prompt is followed. Distilled models want 1; raise it only for classic models. Very high looks overcooked."
          min={GUIDANCE_RANGE[0]}
          max={GUIDANCE_RANGE[1]}
          step={0.5}
          value={guidance}
          fallback={DEFAULTS.guidance}
          onChange={setGuidance}
        />
        <label className="pg-ctl">
          <span className="pg-ctl-head">
            <span className="pg-ctl-label">Seed</span>
          </span>
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
      </ConfigPanel>

      {error && <p className="pg-error">{error}</p>}

        {run && (
          <div className="pg-answer-block">
          <p className="pg-answer-label">Result</p>
          <figure className="pg-image-result">
            {run.done ? (
              <div className="pg-image-frame" style={shot}>
                <img src={rawUrl(run.started.path) + "&t=" + run.started.jobId} alt={run.started.prompt} />
                {/* A download link, not a clipboard write: ClipboardItem takes
                    image/png only and the render's format is unknown here. */}
                <a
                  className="pg-copy-btn pg-image-save"
                  href={rawUrl(run.started.path)}
                  download={run.started.path.split("/").pop() || "picture.png"}
                  title="Save this picture"
                  aria-label="Save this picture"
                >
                  {MenuIcons.download}
                </a>
              </div>
            ) : (
              <div className="pg-image-frame" style={shot}>
                <img
                  src={rawUrl(run.started.previewPath) + "&t=" + previewTick}
                  alt="Render in progress"
                  style={previewLive ? undefined : { display: "none" }}
                  onLoad={() => setPreviewLive(true)}
                  onError={() => setPreviewLive(false)}
                />
                {!previewLive && <div className="pg-image-wait" aria-hidden="true" />}
              </div>
            )}
            {/* Progress only. The settled parameters were dropped by request,
                and D429's seed-reuse button with them: an invented seed is now
                surfaced nowhere, so a random render cannot be reproduced. */}
            {busy && (
              <figcaption className="pg-image-caption">
                <span>{job?.detail || "Starting — a cold model loads first…"}</span>
                {pct !== null && (
                  <span className="pg-bar">
                    <span className="pg-bar-fill" style={{ width: `${pct}%` }} />
                  </span>
                )}
              </figcaption>
            )}
          </figure>
          </div>
        )}
    </div>
  );
}
