// The image stage: prompt in, picture out (SPEC AI-9).
//
// Shaped like the text stage (D431): heading, prompt, Generate. Every
// parameter — chips included — lives behind the settings cog.
//
// The controls follow what the image playgrounds converged on (fal, Midjourney
// web, Ideogram, Leonardo): ASPECT RATIO chips, not raw width×height sliders —
// people think in shapes, developer forms think in pixels — with the exact
// size still available under Custom, because the chips are a view over the
// same `w`/`h` URL params, never a new vocabulary. Speed is a chip row too,
// and its numbers come from the CATALOG's per-model hint: FLUX.2 klein is
// step-distilled and was benchmarked at 4 steps (D310). A model with no
// CATALOG hint falls back to `FALLBACK_STEPS`, not to the server's 28.
//
// A model that can be handed a BASE IMAGE (AI-9f) also gets an attachment row
// on the composer's floor: pick a file, or take one with the webcam. Which
// models those are is the SERVER's answer — `entry.acceptsImage`. The picked
// bytes land on disk first because the route takes a PATH.
//
// A render is job-shaped — the reply carries the job to watch and the SETTLED
// parameters (width snapped, steps clamped, seed invented). While it denoises
// the worker drops a preview beside the output path and this stage polls it;
// the job survives a tab switch on purpose (it shows in Activity), so only the
// WATCH stops on unmount.
import { useEffect, useRef, useState } from "react";
import { cancelJob, type Job } from "@platform/lib/jobs";
import { pickFile, rawUrl, type AiCatalogModel } from "@platform/lib/api";
import { startImage, watchJob, type ImageStarted } from "./client";
import { Download } from "lucide-react";
import { Button } from "@platform/shadcn/ui/button";
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
  RailChips,
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
import { useAutoGrow } from "@platform/lib/autoGrow";
import { cn } from "@platform/lib/utils";
import { StarterIcons } from "./starterIcons";
import { saveToCache, useWebcam, WebcamOverlay } from "./webcam";
import {
  canEdit,
  fitToImage,
  imageFields,
  usableBase,
  EDIT_LONGEST_SIDE,
  type AttachedImage,
  type Size,
} from "./imageInput";
import { numParam, readParam, writeParams } from "@apps/ai_models/lib/params";
import { SERVER_STEPS, middleSteps } from "./speedChips";

// What an UNCATALOGUED image model starts at. Not the server's 28, which is a
// generic diffusion default and wrong for everything this app actually ships:
// the shortlist is step-distilled (FLUX.2 klein was benchmarked at 4, D310).
// 4 makes the unknown-model case start fast and be dragged up, instead of
// starting slow and having to be diagnosed.
const FALLBACK_STEPS = 4;
// Small, fast AND wide on purpose: 480x272 renders in a fraction of 1024²'s
// time and is plenty to judge a prompt by. 272, not 275: the route floors
// every side to a multiple of 16. Guidance 1 is the fallback for a model that
// declares no guidance of its own (right for guidance-distilled models).
const DEFAULTS = { width: 480, height: 272, guidance: 1.0 };
const SIZE_RANGE = [256, 2048] as const;
// Steps top out at the server's own 28: nothing in the shortlist gets better
// past it.
const STEPS_RANGE = [1, 28] as const;
const GUIDANCE_RANGE = [0, 20] as const;

// Multiple-of-16 pairs, all small by default. The chip writes its pair into the
// same `w`/`h` params the sliders edit, and then LOCKS the shape: moving width
// re-derives height (and the other way round) until Custom is picked.
const ASPECTS = [
  { value: "1:1", label: "1:1", title: "Square — 512×512", width: 512, height: 512, rw: 1, rh: 1 },
  { value: "3:4", label: "3:4", title: "Portrait — 480×640", width: 480, height: 640, rw: 3, rh: 4 },
  { value: "4:3", label: "4:3", title: "Landscape — 640×480", width: 640, height: 480, rw: 4, rh: 3 },
  // The default pair, so a fresh stage lights a chip rather than none.
  { value: "16:9", label: "16:9", title: "Wide — 480×272", width: 480, height: 272, rw: 16, rh: 9 },
  { value: "9:16", label: "9:16", title: "Tall — 432×768", width: 432, height: 768, rw: 9, rh: 16 },
] as const;
type Aspect = (typeof ASPECTS)[number];
const CUSTOM = "custom";
const ASPECT_CHIPS = [
  ...ASPECTS.map(({ value, label, title }) => ({ value, label, title })),
  { value: CUSTOM, label: "Custom", title: "Width and height move on their own" },
];

// The locked axis, snapped to the nearest multiple of 16 and kept on the rail.
// A pair is a shape's if EITHER side derives the other; candidates are ranked
// by how close their true ratio is to the pair's.
const snap16 = (n: number) =>
  Math.min(SIZE_RANGE[1], Math.max(SIZE_RANGE[0], Math.round(n / 16) * 16));
const heightFor = (width: number, a: Aspect) => snap16((width * a.rh) / a.rw);
const widthFor = (height: number, a: Aspect) => snap16((height * a.rw) / a.rh);
const aspectOf = (width: number, height: number) => {
  const off = (a: Aspect) => Math.abs(Math.log(width / height) - Math.log(a.rw / a.rh));
  const fits = ASPECTS.filter(
    (a) => heightFor(width, a) === height || widthFor(height, a) === width,
  ).sort((x, y) => off(x) - off(y));
  return fits[0]?.value ?? CUSTOM;
};

// Eight authored examples — two pages of four (D465). Every one names a
// subject AND a way of rendering it (medium, light, lens, texture).
const STARTERS: Starter[] = [
  {
    name: "Lighthouse at dusk",
    icon: StarterIcons.landscape,
    prompt:
      "An oil painting of a lighthouse on a granite cliff at golden hour, heavy visible brush " +
      "strokes, warm rim light down the tower, cold blue sea below, dramatic clouds.",
  },
  {
    name: "Coffee shop cutaway",
    icon: StarterIcons.cube,
    prompt:
      "Isometric cutaway of a two-storey corner coffee shop, warm interior lighting, tiny " +
      "figures at the tables, plants on every windowsill, detailed miniature-diorama look.",
  },
  {
    name: "Chrome robot",
    icon: StarterIcons.robot,
    prompt:
      "Studio photograph of a polished chrome robot holding a single daisy, soft shadows, " +
      "seamless grey backdrop, shallow depth of field, 85mm lens.",
  },
  {
    name: "Night market",
    icon: StarterIcons.camera,
    prompt:
      "Street photograph of a rainy night market, neon signs reflected in the puddles, steam " +
      "rising off the food stalls, motion-blurred crowd, shot wide open at f/1.8.",
  },
  {
    name: "Antique chart",
    icon: StarterIcons.map,
    prompt:
      "An invented island drawn as an antique nautical chart: hand-lettered place names, a " +
      "compass rose, sea monsters in the margins, aged parchment texture, ink and sepia.",
  },
  {
    name: "Cat in orbit",
    icon: StarterIcons.sparkle,
    prompt:
      "A ginger cat in a hand-stitched astronaut suit floating in a cupola window, Earth's " +
      "day-night terminator behind it, photorealistic, gentle backlight, fine dust in the air.",
  },
  {
    name: "Botanical plate",
    icon: StarterIcons.leaf,
    prompt:
      "A botanical illustration plate of a fruit that does not exist: cross-section beside the " +
      "whole fruit, fine ink outlines with watercolour washes, a Latin label underneath.",
  },
  {
    name: "Bauhaus poster",
    icon: StarterIcons.pen,
    prompt:
      "A Bauhaus-style concert poster in three flat colours — red, cream and black — bold " +
      "geometric shapes, large sans-serif type blocked out, slight offset-print texture.",
  },
];

// Three examples that bring their own PHOTO (D467): the pill attaches the
// picture and fills in the prompt, and Generate is the only thing left to
// press. The photos are Unsplash's, re-encoded and credited in
// `static/samples/CREDITS.md`.
const EDIT_STARTERS: Starter[] = [
  {
    name: "Ghibli coast",
    icon: StarterIcons.sparkle,
    image: "/static/samples/coast.jpg",
    prompt:
      "Redraw this photograph as a frame from a hand-painted animated film: soft gouache sky, " +
      "clean ink outlines, saturated greens, a few drifting cumulus clouds, warm afternoon light.",
  },
  {
    name: "Watercolour bridge",
    icon: StarterIcons.pen,
    image: "/static/samples/bridge.jpg",
    prompt:
      "Turn this night photograph into a loose watercolour: wet-on-wet washes in the sky, ink " +
      "lines on the cables and towers, warm lamplight bleeding into the water, paper texture.",
  },
  {
    name: "Pencil study",
    icon: StarterIcons.bowl,
    image: "/static/samples/mug.jpg",
    prompt:
      "Redraw this as a graphite pencil study on toned paper: cross-hatched shadows, white " +
      "chalk highlights on the rim, the background left as bare paper.",
  },
];

// One pool, shuffled ONCE per page load: the eleven above interleaved. Once,
// at module scope, and never per render — a row that reorders itself under
// the cursor is a slot machine.
const ALL_STARTERS: Starter[] = shuffleOnce([...STARTERS, ...EDIT_STARTERS]);

/** Fisher-Yates, over a copy. */
function shuffleOnce(samples: Starter[]): Starter[] {
  const order = [...samples];
  for (let i = order.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [order[i], order[j]] = [order[j], order[i]];
  }
  return order;
}

// The three formats the SERVER can read a size out of — `_image_pixel_size`
// parses PNG, JPEG and WebP headers and nothing else. Both a filter and a
// check, from this one list.
const ATTACH_EXTENSIONS = [".png", ".jpg", ".jpeg", ".webp"] as const;
const ATTACH_TYPES = ATTACH_EXTENSIONS.map((e) => e.slice(1));

interface Run {
  started: ImageStarted;
  job: Job | null;
  done: boolean;
  // Set by the final <img>'s onError. `done` alone is not proof the file
  // exists — this stage's artefact IS the image, so its own <img> tag is the
  // read-back.
  readFailed: boolean;
}

export function ImageStage({ model, entry }: { model: string; entry: AiCatalogModel }) {
  // The model's own benchmarked step count, when the curation measured one.
  const modelSteps = entry.defaults?.steps ?? FALLBACK_STEPS;
  // The model's own curated guidance scale, where the curation names one.
  const modelGuidance = entry.defaults?.guidance ?? DEFAULTS.guidance;
  // The middle rung is dropped rather than duplicated where a model's own count
  // leaves no distinct number between it and the ceiling (`middleSteps`).
  const middle = middleSteps(modelSteps);
  const speedChips =
    entry.defaults?.steps != null
      ? [
          { value: "quick", label: `Quick · ${modelSteps}`, title: `${modelSteps} steps — what this model was benchmarked at`, steps: modelSteps },
          ...(middle != null
            ? [{ value: "balanced", label: `Finer · ${middle}`, title: "More denoising steps — slower, sometimes cleaner", steps: middle }]
            : []),
          { value: "fine", label: `Max · ${SERVER_STEPS}`, title: `${SERVER_STEPS} steps — the server's generic default`, steps: SERVER_STEPS },
        ]
      : null;

  const [prompt, setPrompt] = useState(() => readParam("prompt") ?? "");
  // Clamped to the rail's own ranges: a slider pinned off its own scale by a
  // URL is a control that lies about what will run.
  const [width, setWidth] = useState(() => numParam("w", DEFAULTS.width, ...SIZE_RANGE));
  const [height, setHeight] = useState(() => numParam("h", DEFAULTS.height, ...SIZE_RANGE));
  // Which shape is locked — a chip value, or `custom` for two free sliders. Not
  // a URL param: `w`/`h` round-trip and the chip is recovered from them.
  const [aspect, setAspect] = useState<string>(() => aspectOf(width, height));
  const [steps, setSteps] = useState(() => numParam("steps", modelSteps, ...STEPS_RANGE));
  const [guidance, setGuidance] = useState(() =>
    numParam("guidance", modelGuidance, ...GUIDANCE_RANGE),
  );
  const [seed, setSeed] = useState<string>(() => readParam("seed") ?? "");
  const [run, setRun] = useState<Run | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [previewTick, setPreviewTick] = useState(0);
  const [previewLive, setPreviewLive] = useState(false);
  const { open: configOpen, toggle: toggleConfig, touched: configTouched } = useConfigOpen();

  // Can THIS model be handed a base image at all (AI-9f)? The server's own
  // answer, read through imageInput.ts.
  const editable = canEdit(entry.acceptsImage);
  // The base image, as the server path a render will be pointed at. In the URL
  // (`img`): this stage is keyed by model id, so picking another model
  // REMOUNTS it, and "same photo, different model" should be free.
  const [attachment, setAttachment] = useState<AttachedImage | null>(() => {
    const path = readParam("img");
    return path ? { path, name: path.split("/").pop() ?? path } : null;
  });
  // Whose size wins. With an image attached the SERVER derives the render's
  // size from that image (AI-9f), so the stage stops sending `w`/`h` at all.
  const [sizeFromImage, setSizeFromImage] = useState(true);
  // The base image this render will actually edit — the same rule the request
  // below applies, read from one place (imageInput.ts).
  const base = usableBase(entry.acceptsImage, attachment);
  // The attached picture's OWN pixel size, read off the decoded image — see
  // `fitToImage`. Null until the probe answers, and after a failure to decode.
  const [natural, setNatural] = useState<Size | null>(null);
  const [attaching, setAttaching] = useState(false);
  // Is the attached picture open at full size?
  const [showBase, setShowBase] = useState(false);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      writeParams({
        prompt: prompt ? prompt : null,
        w: width !== DEFAULTS.width ? String(width) : null,
        h: height !== DEFAULTS.height ? String(height) : null,
        steps: steps !== modelSteps ? String(steps) : null,
        guidance: guidance !== modelGuidance ? String(guidance) : null,
        seed: seed ? seed : null,
        // Written only where this model could use one. The key is OMITTED
        // rather than nulled on a model that cannot edit.
        ...(editable ? { img: attachment ? attachment.path : null } : {}),
      });
    }, 300);
    return () => window.clearTimeout(timer);
  }, [prompt, width, height, steps, guidance, seed, modelSteps, modelGuidance, editable, attachment]);

  const abortRef = useRef<AbortController | null>(null);
  const { ref: boxRef } = useAutoGrow(prompt);
  // Set on the way in as well as cleared on the way out: a continuation that
  // lands after an unmount must not write state from a dead component.
  const aliveRef = useRef(true);
  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
      abortRef.current?.abort();
    };
  }, []);

  // The webcam, shared with the text stage (webcam.tsx).
  const webcam = useWebcam({ onError: setError });

  // The picture's own dimensions, off a decode in the browser rather than a new
  // endpoint. Re-probed rather than persisted.
  useEffect(() => {
    setNatural(null);
    if (!base) return;
    let alive = true;
    const probe = new Image();
    probe.onload = () => {
      if (alive && probe.naturalWidth && probe.naturalHeight) {
        setNatural({ width: probe.naturalWidth, height: probe.naturalHeight });
      }
    };
    probe.src = rawUrl(base.path);
    return () => {
      alive = false;
    };
  }, [base?.path]);

  /** Point the render at a file that is ALREADY on this disk — no copy, no
   *  upload, the user's own path (`/api/fs/pick-file`). */
  const choose = async () => {
    setError(null);
    setAttaching(true);
    try {
      const path = await pickFile({
        title: "Choose a picture to edit",
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

  /** Bytes into the cache dir and onto this stage: `saveToCache` (webcam.tsx)
   *  does the writing, this adds the stage's own error line, its busy flag,
   *  and the `attach` that a bare write knows nothing about. */
  const save = async (data: Blob, name: string, stamped = true): Promise<AttachedImage | null> => {
    setError(null);
    setAttaching(true);
    try {
      const landed = await saveToCache(data, name, stamped);
      if (!aliveRef.current) return null;
      attach(landed);
      // Handed back as well as put in state: a caller that wants to RUN on this
      // picture cannot read the state it just set.
      return landed;
    } catch (e) {
      if (aliveRef.current) setError((e as Error).message);
      return null;
    } finally {
      if (aliveRef.current) setAttaching(false);
    }
  };

  /** Take a sample: its prompt in the box, its photo attached, and RUN — the
   *  same one click a prompt-only sample is. The bytes are the app's own,
   *  served from `/static/samples`, copied to a path because `/api/ai/image`
   *  reads a file off the disk. */
  const takeSample = async (sample: Starter) => {
    if (!sample.image) {
      void generate(sample.prompt);
      return;
    }
    setError(null);
    setAttaching(true);
    try {
      const res = await fetch(sample.image);
      if (!res.ok) throw new Error(`could not read the sample picture (${res.status})`);
      const blob = await res.blob();
      const measured = await measure(blob);
      if (!aliveRef.current) return;
      const landed = await save(blob, sample.image.split("/").pop() || "sample.jpg", false);
      if (!landed || !aliveRef.current) return;
      void generate(sample.prompt, {
        base: landed,
        fitted: measured ? fitToImage(measured) : null,
      });
    } catch (e) {
      if (aliveRef.current) {
        setError((e as Error).message);
        setAttaching(false);
      }
    }
  };

  /** A blob's pixel size, or null if the browser could not decode it. */
  const measure = (data: Blob): Promise<Size | null> =>
    new Promise((resolve) => {
      const url = URL.createObjectURL(data);
      const probe = new Image();
      probe.onload = () => {
        URL.revokeObjectURL(url);
        resolve(
          probe.naturalWidth && probe.naturalHeight
            ? { width: probe.naturalWidth, height: probe.naturalHeight }
            : null,
        );
      };
      probe.onerror = () => {
        URL.revokeObjectURL(url);
        resolve(null);
      };
      probe.src = url;
    });

  const attach = (picked: AttachedImage) => {
    setAttachment(picked);
    // A fresh image is a fresh size question, and the honest default is that
    // image's own size — even where the last one had been overridden.
    setSizeFromImage(true);
  };

  const openCamera = async () => {
    setError(null);
    await webcam.start();
  };

  const capture = () => webcam.capture((blob) => void save(blob, "webcam.png"));

  // Keyed on the STARTED reply, not on `run`: the watch's onTick rewrites
  // `run` every poll, so an effect keyed on the whole object was torn down and
  // rebuilt each second and the 1500ms timer never fired once.
  const rendering = run && !run.done ? run.started : null;
  useEffect(() => {
    if (!rendering) return;
    const timer = window.setInterval(() => setPreviewTick((n) => n + 1), 1500);
    return () => window.clearInterval(timer);
  }, [rendering]);

  const locked = ASPECTS.find((a) => a.value === aspect) ?? null;
  // The slider handlers: under a locked shape the other side follows.
  const changeWidth = (w: number) => {
    setWidth(w);
    if (locked) setHeight(heightFor(w, locked));
  };
  const changeHeight = (h: number) => {
    setHeight(h);
    if (locked) setWidth(widthFor(h, locked));
  };
  const speed = speedChips?.find((c) => c.steps === steps)?.value ?? null;
  // Is the size the PICTURE's? Only with a base image, and only until somebody
  // picks one themselves.
  const sizeIsTheImages = base !== null && sizeFromImage;
  // …and what that comes to in pixels. Null while the probe is out.
  const fitted = sizeIsTheImages && natural ? fitToImage(natural) : null;

  const generate = async (
    asked?: string,
    // What to edit, when the caller knows it and this component's state does
    // not yet — a sample pill attaching a photo and running in one click.
    using?: { base: AttachedImage; fitted: Size | null },
  ) => {
    const wanted = (asked ?? prompt).trim();
    if (!wanted || (run && !run.done)) return;
    if (asked) setPrompt(asked);
    setError(null);
    setPreviewLive(false);
    try {
      // Published BEFORE the first await: unmounting while this POST is in
      // flight used to leave the ref null, so the cleanup aborted nothing.
      const controller = new AbortController();
      abortRef.current = controller;
      const started = await startImage({
        prompt: wanted,
        model,
        // `steps`/`guidance` always: the stage's defaults are its own. The
        // SIZE is the one pair that can be left to the server, and only with
        // an image attached.
        ...(using
          ? imageFields(using.base, true, using.fitted, width, height)
          : imageFields(base, sizeFromImage, fitted, width, height)),
        steps,
        guidance,
        ...(seed.trim() !== "" ? { seed: Number(seed) } : {}),
      });
      setRun({ started, job: null, done: false, readFailed: false });
      // The seed the server settled on lands in the box, so the render can be
      // reproduced or nudged.
      setSeed(String(started.seed));
      try {
        const outcome = await watchJob(started.jobId, controller.signal, (job) =>
          setRun((r) => (r && r.started.jobId === started.jobId ? { ...r, job } : r)),
        );
        // Stop was pressed. The worker died before writing the output.
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

  // Back to empty: the prompt, the result AND the attached picture, which is
  // part of the request rather than part of the setup. Settings stay put.
  const clear = () => {
    setPrompt("");
    setRun(null);
    setError(null);
    setPreviewLive(false);
    setAttachment(null);
    boxRef.current?.focus();
  };

  const busy = !!run && !run.done;
  const job = busy ? run.job : null;
  const pct = job && job.total ? Math.min(100, ((job.done ?? 0) / job.total) * 100) : null;
  // One box for shimmer, preview and final picture, so the column cannot
  // resize mid-render. Full parent width: the run's own ratio gives the height.
  const shot = run
    ? {
        aspectRatio: `${run.started.width} / ${run.started.height}`,
        width: "100%",
      }
    : undefined;

  return (
    <Card className="w-full flex-none gap-3 px-(--card-spacing) [--card-spacing:--spacing(6)]">
      <StageHeader title="Describe a picture" configOpen={configOpen} onToggleConfig={toggleConfig} />

      <ComposerCard stacked>
        <textarea
          ref={boxRef}
          rows={3}
          value={prompt}
          className={cn(composerTextareaClass, "flex-none pr-16")}
          placeholder={base ? "Describe the change…" : "Describe the picture…"}
          onChange={(e) => setPrompt(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              void generate();
            }
          }}
        />
        {/* Clear, floating in the box's top-right corner: it appears only once
            there is a picture to throw away, and adds no height. */}
        {!busy && run && (
          <ClearButton corner title="Clear the prompt and the picture" onClick={clear} />
        )}
        {/* The composer's floor: the two ways to attach a picture, then
            Generate — one cluster in the bottom-right corner. */}
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
                <span>{base ? "Replace" : "Add an image"}</span>
              </AttachButton>
              <AttachButton
                active={webcam.open}
                title="Take one with the webcam"
                disabled={attaching}
                onClick={() => (webcam.open ? webcam.stop() : void openCamera())}
              >
                {StarterIcons.camera}
                <span>Webcam</span>
              </AttachButton>
              {attaching && <Tiny>Working…</Tiny>}
            </div>
          )}
          <ComposerSide floor={false}>
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
        </div>
      </ComposerCard>

      <ConfigPanel open={configOpen} animated={configTouched.current}>
        {/* Chips lead the panel; sliders and the seed follow. Hidden, not
            disabled, while the attached image decides the size. */}
        {(!sizeIsTheImages || speedChips) && (
          <div className="flex flex-col gap-2">
            {!sizeIsTheImages && (
              <RailChips
                label="Aspect ratio"
                options={ASPECT_CHIPS}
                active={aspect}
                onPick={(value) => {
                  setAspect(value);
                  const pick = ASPECTS.find((a) => a.value === value);
                  if (pick) {
                    setWidth(pick.width);
                    setHeight(pick.height);
                  }
                }}
              />
            )}
            {speedChips && (
              <RailChips
                label="Speed"
                options={speedChips.map(({ value, label, title }) => ({ value, label, title }))}
                active={speed}
                onPick={(value) => {
                  const pick = speedChips.find((c) => c.value === value);
                  if (pick) setSteps(pick.steps);
                }}
              />
            )}
          </div>
        )}
        {sizeIsTheImages ? (
          <RailField
            label="Size"
            action={<RailReset onClick={() => setSizeFromImage(false)}>Set a size</RailReset>}
            hint={
              fitted && natural && (natural.width > fitted.width || natural.height > fitted.height)
                ? `Scaled down from ${natural.width} × ${natural.height}: an edit at the full ` +
                  "size takes minutes. Set a size to render it bigger."
                : `The picture's own shape, longest side up to ${EDIT_LONGEST_SIDE}.`
            }
          >
            <span className="text-xs">
              {fitted
                ? `${fitted.width} × ${fitted.height} — the picture's shape`
                : "Read from the attached picture"}
            </span>
          </RailField>
        ) : (
          <>
            <RailSlider
              label="Width"
              hint={
                locked
                  ? `Height follows to keep ${locked.label}. Pick Custom to move them apart.`
                  : "Snapped to a multiple of 16 by the server."
              }
              min={SIZE_RANGE[0]}
              max={SIZE_RANGE[1]}
              step={16}
              value={width}
              fallback={DEFAULTS.width}
              onChange={changeWidth}
            />
            <RailSlider
              label="Height"
              hint="Bigger is slower and needs more memory."
              min={SIZE_RANGE[0]}
              max={SIZE_RANGE[1]}
              step={16}
              value={height}
              fallback={DEFAULTS.height}
              onChange={changeHeight}
            />
          </>
        )}
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
          hint={
            entry.defaults?.guidance != null
              ? `This model wants ${modelGuidance} — raise it for stricter prompt-following, lower it for more variety.`
              : "How literally the prompt is followed. Distilled models want 1; raise it only for classic models. Very high looks overcooked."
          }
          min={GUIDANCE_RANGE[0]}
          max={GUIDANCE_RANGE[1]}
          step={0.5}
          value={guidance}
          fallback={modelGuidance}
          onChange={setGuidance}
        />
        <RailField label="Seed" hint="Same seed + same prompt + same settings = the same picture.">
          <Input
            type="text"
            inputMode="numeric"
            value={seed}
            placeholder="Random each time"
            onChange={(e) => setSeed(e.target.value.replace(/[^0-9]/g, ""))}
          />
        </RailField>
      </ConfigPanel>

      {/* The attached picture at full size. Deliberately the whole modal: an
          image and a way out — the ✕ on the chip already removes it. */}
      {base && (
        <Lightbox open={showBase} onClose={() => setShowBase(false)} label="The attached picture">
          <img
            src={rawUrl(base.path)}
            alt=""
            className="max-h-[calc(100vh-6rem)] max-w-full rounded-md object-contain"
          />
        </Lightbox>
      )}

      {webcam.open && (
        <WebcamOverlay videoRef={webcam.videoRef} onCapture={capture} onClose={webcam.stop} />
      )}

      {/* Examples first, under the box they fill; hidden once a picture is on
          screen. The photo examples are only offered where a photo can be
          sent. */}
      {!run && (
        <StarterCards
          samples={editable ? ALL_STARTERS : ALL_STARTERS.filter((sample) => !sample.image)}
          onPick={(s) => void takeSample(s)}
        />
      )}

      {error && <StatusLine status="error">{error}</StatusLine>}

      {!run ? (
        <ResultSlot
          label="Result"
          capability="text-to-image"
          note={
            base
              ? "The edited picture appears here. Describe the change above, then Generate."
              : "Your picture appears here. Describe one above, then Generate."
          }
        />
      ) : (
        <AnswerBlock label="Result" status={busy ? "running" : null}>
          <figure className="m-0 flex flex-col gap-2">
            {run.done && run.readFailed ? (
              // `watchJob` said done but the file this <img> asked for does not
              // actually exist. Say that plainly.
              <div className="relative max-w-full self-start rounded-lg border border-border bg-muted/30" style={shot}>
                <Tiny className="block px-4 py-6 text-sm leading-normal">
                  The image could not be read back — the render may have been interrupted.
                </Tiny>
              </div>
            ) : run.done ? (
              <div className="relative max-w-full self-start leading-none" style={shot}>
                <img
                  className="block size-full rounded-lg border border-border bg-muted/30 object-contain"
                  src={rawUrl(run.started.path) + "&t=" + run.started.jobId}
                  alt={run.started.prompt}
                  onError={() =>
                    setRun((r) =>
                      r && r.started.jobId === run.started.jobId ? { ...r, readFailed: true } : r,
                    )
                  }
                />
                {/* A download link, not a clipboard write: ClipboardItem takes
                    image/png only and the render's format is unknown here. */}
                <Button
                  variant="outline"
                  size="icon-xs"
                  className="absolute top-2 right-2 bg-background/80 backdrop-blur-sm"
                  render={
                    <a
                      href={rawUrl(run.started.path)}
                      download={run.started.path.split("/").pop() || "picture.png"}
                    />
                  }
                  title="Save this picture"
                  aria-label="Save this picture"
                >
                  <Download />
                </Button>
              </div>
            ) : (
              <div className="relative max-w-full self-start leading-none" style={shot}>
                <img
                  className="block size-full rounded-lg border border-border bg-muted/30 object-contain"
                  src={rawUrl(run.started.previewPath) + "&t=" + previewTick}
                  alt="Render in progress"
                  style={previewLive ? undefined : { display: "none" }}
                  onLoad={() => setPreviewLive(true)}
                  onError={() => setPreviewLive(false)}
                />
                {!previewLive && (
                  <div
                    className="size-full rounded-lg border border-border bg-muted motion-safe:animate-pulse"
                    aria-hidden="true"
                  />
                )}
              </div>
            )}
            {/* Progress only; the invented seed is surfaced by pre-filling the
                Seed box instead. */}
            {busy && (
              <figcaption className="flex flex-col gap-1.5 text-xs text-muted-foreground tabular-nums">
                <span>{job?.detail || "Starting — a cold model loads first…"}</span>
                {pct !== null && <ProgressBar pct={pct} />}
              </figcaption>
            )}
          </figure>
        </AnswerBlock>
      )}
    </Card>
  );
}
