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
// with no CATALOG hint falls back to `FALLBACK_STEPS`, not to the server's
// 28 — see that constant.
//
// A model that can be handed a BASE IMAGE (AI-9f) also gets an attachment row
// at the top of the composer: pick a file, or take one with the webcam. Which
// models those are is the SERVER's answer — `entry.acceptsImage`, computed from
// the resolved engine and the model's own edit variant, the same two gates
// `/api/ai/image` refuses with — never a list of repo ids kept over here. The
// picked bytes land on disk first (`~/ai/inputs`) because the route takes a
// PATH, exactly as the transcribe stage's recording does.
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
import { MenuIcons } from "@platform/ui/MenuIcons";
import { Input } from "@platform/shadcn/ui/input";
import { Card } from "@platform/shadcn/ui/card";
import {
  ConfigPanel,
  useConfigOpen,
  RailChips,
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

const SERVER_STEPS = 28;
// What an UNCATALOGUED image model starts at. Not the server's 28, which is a
// generic diffusion default and wrong for everything this app actually ships:
// the shortlist is step-distilled (FLUX.2 klein was benchmarked at 4, D310),
// and the catalog says so per model. But the fallback is what a repo the
// curation has no row for gets, and those are overwhelmingly the same family
// under a different id -- `black-forest-labs/FLUX.2-klein-4B` is the base repo
// of the very entry that declares `steps: 4`, and it landed here on 28: seven
// times the denoising work for a model distilled not to need it, which reads
// as "the GPU path is slow" rather than "the default is wrong". 4 makes the
// unknown-model case start fast and be dragged up, instead of starting slow
// and having to be diagnosed. The rail and the Max chip still reach
// `SERVER_STEPS`; only the STARTING point moved.
const FALLBACK_STEPS = 4;
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
// Steps top out at the server's own 28: nothing in the shortlist gets better
// past it, and the Max chip already says 28 — a rail running to 100 only made
// the chip look like a mid-point.
const STEPS_RANGE = [1, 28] as const;
const GUIDANCE_RANGE = [0, 20] as const;

// Multiple-of-16 pairs, all small by default. The chip writes its pair into the
// same `w`/`h` params the sliders edit, and then LOCKS the shape: moving width
// re-derives height (and the other way round) until Custom is picked, so a
// bigger 16:9 no longer means working out the second side by hand. A saved
// link whose pair fits none of the shapes lights Custom.
const ASPECTS = [
  { value: "1:1", label: "1:1", title: "Square — 512×512", width: 512, height: 512, rw: 1, rh: 1 },
  { value: "3:4", label: "3:4", title: "Portrait — 480×640", width: 480, height: 640, rw: 3, rh: 4 },
  { value: "4:3", label: "4:3", title: "Landscape — 640×480", width: 640, height: 480, rw: 4, rh: 3 },
  // The default pair, so a fresh stage lights a chip rather than none. 480/272
  // is 1.76 rather than 1.778 — the nearest multiple-of-16 pair to the size
  // asked for, and the same rounding SDXL's own "16:9" bucket carries.
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
// Reproduces every preset pair from its own width (480 → 272 for 16:9), so the
// same functions decide which chip a saved size lights. A pair is a shape's if
// EITHER side derives the other: a height edit writes `widthFor(h)`, whose
// own `heightFor` can land one step off after rounding, and a link saved then
// must reload with the lock it was showing. Where the rail's edge clamped a
// side, more than one shape can derive the same pair (16:9 and 4:3 both reach
// 2048×1600), so the candidates are ranked by how close their true ratio is
// to the pair's rather than by list order.
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
// subject AND a way of rendering it (medium, light, lens, texture), because
// that pairing is the thing a newcomer to image models does not know to write
// and the difference it makes is the whole demonstration.
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
// press. An edit prompt with nothing to edit demonstrates nothing, and finding
// a suitable file is the step that stops somebody trying this at all — so the
// app ships three, one per kind of restyle worth showing (a painted animation
// still, a wash over a night photograph, a drawing of an object).
//
// Mixed into the prompt-only eight rather than fronting them, so the row's
// first page is not three photo pills in a block — eleven authored, four shown,
// which is three rotate pages (D465). The photos are Unsplash's, re-encoded and
// credited in `static/samples/CREDITS.md`.
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

// One pool, shuffled ONCE per page load: the eleven above interleaved, so a
// reader does not meet the three photo examples as a block at the head of the
// row and the pill they see first is not the same pill every session. Once, at
// module scope, and never per render — a row that reorders itself under the
// cursor is a slot machine (D465's own "no shuffle" note was about exactly
// that, and stands: this order is fixed for as long as the tab is open, and
// rotate steps through it in the same order both ways).
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
// parses PNG, JPEG and WebP headers and nothing else (there is no Pillow in
// the app process). A HEIC (what a Mac's Photos hands out by default) would
// fall through to the generic 1024² and stretch whatever renders.
//
// Both a filter and a check, from this one list. The OS dialog is narrowed to
// these (`pickFile`'s `types`), because a file that cannot work should not be
// offered in the first place — and the extension is still checked after the
// pick, because the filter does not reach a DRAG-DROP, a typed filename, or the
// Linux backends, which can only suggest. A refusal names the three formats.
const ATTACH_EXTENSIONS = [".png", ".jpg", ".jpeg", ".webp"] as const;
// Bare, no dot: what the dialog backends want, derived rather than written
// twice so the filter cannot drift from what the check accepts.
const ATTACH_TYPES = ATTACH_EXTENSIONS.map((e) => e.slice(1));

interface Run {
  started: ImageStarted;
  job: Job | null;
  done: boolean;
  // Set by the final <img>'s onError. `done` alone is not proof the file
  // exists — `watchJob`'s `gone` outcome (the row vanished between polls) is
  // deliberately read as `done` here (see client.ts's WatchOutcome), and
  // while the miss tolerance that guards that mostly rules out a genuinely
  // stopped render slipping through as `gone`, this is the belt-and-suspenders
  // check TranscribeStage already does by reading its artefact back — this
  // stage's artefact IS the image, so its own <img> tag is that read-back.
  readFailed: boolean;
}

export function ImageStage({ model, entry }: { model: string; entry: AiCatalogModel }) {
  // The model's own benchmarked step count, when the curation measured one.
  const modelSteps = entry.defaults?.steps ?? FALLBACK_STEPS;
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
  // Which shape is locked — a chip value, or `custom` for two free sliders. Not
  // a URL param: `w`/`h` round-trip and the chip is recovered from them.
  const [aspect, setAspect] = useState<string>(() => aspectOf(width, height));
  const [steps, setSteps] = useState(() => numParam("steps", modelSteps, ...STEPS_RANGE));
  const [guidance, setGuidance] = useState(() =>
    numParam("guidance", DEFAULTS.guidance, ...GUIDANCE_RANGE),
  );
  const [seed, setSeed] = useState<string>(() => readParam("seed") ?? "");
  const [run, setRun] = useState<Run | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [previewTick, setPreviewTick] = useState(0);
  const [previewLive, setPreviewLive] = useState(false);
  const { open: configOpen, toggle: toggleConfig, touched: configTouched } = useConfigOpen();

  // Can THIS model be handed a base image at all (AI-9f)? The server's own
  // answer, read through imageInput.ts so the row that is drawn and the field
  // that is sent cannot come to disagree.
  const editable = canEdit(entry.acceptsImage);
  // The base image, as the server path a render will be pointed at. In the URL
  // (`img`) for the reason the transcribe stage keeps `src` there: this stage
  // is keyed by model id, so picking another model REMOUNTS it, and "same
  // photo, different model" is the comparison a playground should make free.
  const [attachment, setAttachment] = useState<AttachedImage | null>(() => {
    const path = readParam("img");
    return path ? { path, name: path.split("/").pop() ?? path } : null;
  });
  // Whose size wins. With an image attached the SERVER derives the render's
  // size from that image (AI-9f), so the stage stops sending `w`/`h` at all —
  // and the size controls are collapsed to a line saying so rather than left
  // showing 480x272 next to a render that will come back 1024x688. Touching
  // any of them takes the size back, explicitly.
  const [sizeFromImage, setSizeFromImage] = useState(true);
  // The base image this render will actually edit — the same rule the request
  // below applies, read from one place (imageInput.ts) rather than restated in
  // the JSX: an attachment kept across a switch to a render-only model must
  // not be drawn there, or every request from that stage is a 400.
  const base = usableBase(entry.acceptsImage, attachment);
  // The attached picture's OWN pixel size, read off the decoded image. Needed
  // to render an edit at that picture's shape without asking the server to
  // derive it at 1024 — see `fitToImage`. Null until the probe answers, and
  // after a failure to decode.
  const [natural, setNatural] = useState<Size | null>(null);
  const [attaching, setAttaching] = useState(false);
  // Is the attached picture open at full size? A thumbnail 28px on a side is a
  // reminder of WHICH picture, not a look at it.
  const [showBase, setShowBase] = useState(false);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      writeParams({
        prompt: prompt ? prompt : null,
        w: width !== DEFAULTS.width ? String(width) : null,
        h: height !== DEFAULTS.height ? String(height) : null,
        steps: steps !== modelSteps ? String(steps) : null,
        guidance: guidance !== DEFAULTS.guidance ? String(guidance) : null,
        seed: seed ? seed : null,
        // Written only where this model could use one. The key is OMITTED
        // rather than nulled on a model that cannot edit — nulling would
        // delete an attachment the user picked under a model that can, so
        // switching to a render-only model and back would silently lose it.
        ...(editable ? { img: attachment ? attachment.path : null } : {}),
      });
    }, 300);
    return () => window.clearTimeout(timer);
  }, [prompt, width, height, steps, guidance, seed, modelSteps, editable, attachment]);

  const abortRef = useRef<AbortController | null>(null);
  const { ref: boxRef } = useAutoGrow(prompt);
  // Set on the way in as well as cleared on the way out, the same shape
  // TranscribeStage's own flag has: an upload awaits the config, a mkdir and
  // the POST, and a continuation that lands after an unmount must not write
  // state from a dead component. The camera's own lifetime is the webcam
  // hook's — it stops its stream on unmount, so this effect does not.
  const aliveRef = useRef(true);
  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
      abortRef.current?.abort();
    };
  }, []);

  // The webcam, shared with the text stage (webcam.tsx): the stream, the live
  // view, Escape, and the canvas draw all live there — this stage only says
  // what happens to the frame.
  const webcam = useWebcam({ onError: setError });

  // The picture's own dimensions, off a decode in the browser rather than a new
  // endpoint: the shell can already read this file through `/api/fs/raw` (the
  // thumbnail on the composer's floor IS this image), and the server's own
  // header parser answers at 1024 by contract. Re-probed rather than persisted,
  // since the attachment survives a model switch through the URL and a size in
  // the URL would be a second thing to keep true.
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

  // Escape closes the preview, which is what every overlay in this app answers
  // to and the one keystroke somebody will reach for before the ✕.
  useEffect(() => {
    if (!showBase) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setShowBase(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [showBase]);

  /** Point the render at a file that is ALREADY on this disk — no copy, no
   *  upload, the user's own path. `<input type=file>` cannot do this: a browser
   *  hands over bytes and strips the path on purpose, so the only way to a path
   *  is the OS dialog raised in the server process (`/api/fs/pick-file`). */
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
      // picture cannot read the state it just set, and passing the attachment
      // is honest where a `setTimeout` would be a guess.
      return landed;
    } catch (e) {
      if (aliveRef.current) setError((e as Error).message);
      return null;
    } finally {
      if (aliveRef.current) setAttaching(false);
    }
  };

  /** Take a sample: its prompt in the box, its photo attached, and RUN — the
   *  same one click a prompt-only sample is.
   *
   *  The bytes are the app's own, served from `/static/samples`, and they still
   *  have to be copied to a path: `/api/ai/image` reads a file off the disk and
   *  cannot be handed a URL inside the app's bundle (which on the packaged Mac
   *  is inside a signed .app). The cache dir is where that copy belongs.
   *
   *  Everything the render needs is passed to `generate` rather than left to
   *  state, for the ordinary React reason — `setAttachment` in this same click
   *  is not readable yet — and the picture is measured HERE, off the blob, so
   *  the first edit is fitted to its shape (fitToImage) instead of falling
   *  through to the server's slower 1024. */
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
  // …and what that comes to in pixels: the picture's shape, scaled down to
  // something this stage is willing to wait for. Null while the probe is out,
  // when the server's own 1024 derivation stands in.
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
      // flight used to leave the ref null, so the cleanup aborted nothing and
      // the watch below polled /api/jobs once a second from a dead component
      // for the whole render. watchJob checks the signal on entry, so an abort
      // that lands during the POST throws AbortError straight out.
      const controller = new AbortController();
      abortRef.current = controller;
      const started = await startImage({
        prompt: wanted,
        model,
        // `steps`/`guidance` always: the stage's defaults are its own (the
        // model's benchmarked steps, guidance 1), and leaving either off would
        // hand the server its generic 28 / 4.0. The SIZE is the one pair that
        // can be left to the server, and only with an image attached — then
        // the base image's own size is the better default than any of this
        // stage's, and the controls say so instead of showing a number that
        // will not be used.
        ...(using
          ? imageFields(using.base, true, using.fitted, width, height)
          : imageFields(base, sizeFromImage, fitted, width, height)),
        steps,
        guidance,
        ...(seed.trim() !== "" ? { seed: Number(seed) } : {}),
      });
      setRun({ started, job: null, done: false, readFailed: false });
      // The seed the server settled on — invented when the box was empty —
      // lands in the box, so the render can be reproduced or nudged. Repeat
      // Generates reuse it until the box is cleared.
      setSeed(String(started.seed));
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

  // Back to empty: the prompt, the result AND the attached picture, which is
  // part of the request rather than part of the setup. Settings stay put.
  const clear = () => {
    setPrompt("");
    setRun(null);
    setError(null);
    setPreviewLive(false);
    setAttachment(null);
    // The height follows the emptied prompt on its own (useAutoGrow).
    boxRef.current?.focus();
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
      <Card className="pg-work-card flex-none gap-3 px-(--card-spacing) [--card-spacing:--spacing(6)]">
      {/* The action, and the way to the settings. The hero card above names
          the model and its state. */}
      <StageHeader
        title="Describe a picture"
        configOpen={configOpen}
        onToggleConfig={toggleConfig}
      />

      <div className="pg-composer pg-composer-stack">
        <textarea
          ref={boxRef}
          rows={3}
          value={prompt}
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
            there is a picture to throw away, and in this stacked composer a
            slot of its own would have cost the box a permanent 40px of height
            (the shared Clear-above-Run stack is right where the prompt and the
            buttons share ONE row — here they do not). Absolute, so it adds
            none. */}
        {!busy && run && (
          <button
            type="button"
            className="pg-ghost-btn pg-clear pg-clear-corner"
            title="Clear the prompt and the picture"
            onClick={clear}
          >
            Clear
          </button>
        )}
        {/* The composer's floor: the two ways to attach a picture, then the
            Clear/Generate column — one cluster in the bottom-right corner,
            attach beside Generate rather than across the box from it. The
            prompt therefore spans the whole box here rather than sharing its
            row with the button column the other stages use, and Generate stays
            exactly where those stages put it. */}
        <div className="pg-composer-foot">
          {/* The attached photo, on the floor's own line: the space left of the
              buttons was empty, and the picture belongs beside the controls
              that put it there rather than in a band of its own above them.
              Thumbnail and ✕ only — the filename said nothing a look at the
              picture does not (and ellipsised to "Screenshot 2026-08-23 at
              12…", nothing at all), and the sentence about editing was a
              caption on a control nobody had asked a question about. */}
          {base && (
            <span className="pg-attach">
              <button
                type="button"
                className="pg-attach-open"
                title="See this picture"
                aria-label="See this picture"
                onClick={() => setShowBase(true)}
              >
                <img src={rawUrl(base.path)} alt="" />
              </button>
              <button
                type="button"
                className="pg-attach-drop"
                title="Remove this image"
                aria-label="Remove this image"
                onClick={() => setAttachment(null)}
              >
                ✕
              </button>
            </span>
          )}
          {editable && (
            <div className="pg-attach-row">
              <button
                type="button"
                className="pg-attach-btn"
                title="Point at a picture already on this disk — nothing is copied"
                disabled={attaching}
                onClick={() => void choose()}
              >
                {StarterIcons.landscape}
                <span>{base ? "Replace" : "Add an image"}</span>
              </button>
              <button
                type="button"
                className={"pg-attach-btn" + (webcam.open ? " active" : "")}
                title="Take one with the webcam"
                disabled={attaching}
                onClick={() => (webcam.open ? webcam.stop() : void openCamera())}
              >
                {StarterIcons.camera}
                <span>Webcam</span>
              </button>
              {attaching && <span className="pg-attach-note">Working…</span>}
            </div>
          )}
          {/* Generate alone in this column here: Clear floats in the box's
              top-right corner instead (see above), because on a STACKED
              composer the two stacked vertically made the floor 40px taller —
              a whole row of height for a button that appears only once there
              is something to clear. */}
          <div className="pg-composer-side">
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
      </div>

      <ConfigPanel open={configOpen} animated={configTouched.current}>
        <div className="pg-config-chips">
          {/* Hidden, not disabled, while the attached image decides the size:
              a chip row where nothing is lit and a slider parked on 480 are
              both controls saying something about a render that will come back
              at the photo's own size instead. One line replaces them, and it
              is also the way back to picking a size by hand. */}
          {!sizeIsTheImages && (
            <RailChips
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
              options={speedChips.map(({ value, label, title }) => ({ value, label, title }))}
              active={speed}
              onPick={(value) => {
                const pick = speedChips.find((c) => c.value === value);
                if (pick) setSteps(pick.steps);
              }}
            />
          )}
        </div>
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
          hint="How literally the prompt is followed. Distilled models want 1; raise it only for classic models. Very high looks overcooked."
          min={GUIDANCE_RANGE[0]}
          max={GUIDANCE_RANGE[1]}
          step={0.5}
          value={guidance}
          fallback={DEFAULTS.guidance}
          onChange={setGuidance}
        />
        <RailField
          label="Seed"
          hint="Same seed + same prompt + same settings = the same picture."
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

      {/* The attached picture at full size. Deliberately the whole modal: an
          image and a way out, no title bar, no filename, no actions — the ✕ on
          the row below already removes it, and this is only here because a
          28px thumbnail cannot be looked at. Click the backdrop or press
          Escape to close, the two things anybody tries. */}
      {base && showBase && (
        <div
          className="pg-lightbox"
          role="dialog"
          aria-label="The attached picture"
          onClick={() => setShowBase(false)}
        >
          <img src={rawUrl(base.path)} alt="" onClick={(e) => e.stopPropagation()} />
          <button
            type="button"
            className="pg-lightbox-close"
            title="Close"
            aria-label="Close"
            onClick={() => setShowBase(false)}
          >
            ✕
          </button>
        </div>
      )}

      {webcam.open && (
        <WebcamOverlay
          videoRef={webcam.videoRef}
          onCapture={capture}
          onClose={webcam.stop}
        />
      )}

      {/* Examples first, under the box they fill; hidden once a picture is on
          screen, which is what that space is then for. */}
      {!run && (
        <StarterCards
          // The photo examples are only offered where a photo can be sent;
          // elsewhere the same shuffled order stands with them filtered out.
          samples={editable ? ALL_STARTERS : ALL_STARTERS.filter((sample) => !sample.image)}
          onPick={(s) => void takeSample(s)}
        />
      )}

      {/* Chips lead the panel; sliders and the seed follow. */}

      {error && <p className="pg-error">{error}</p>}

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
          <div className="pg-answer-block">
          <p className="pg-answer-label">Result</p>
          <figure className="pg-image-result">
            {run.done && run.readFailed ? (
              // `watchJob` said done (including a `gone` it reads as done —
              // see the `Run.readFailed` comment) but the file this <img>
              // asked for does not actually exist. Say that plainly rather
              // than leaving a broken-image icon and a save link to nothing.
              <div className="pg-image-frame" style={shot}>
                <p className="pg-image-readfailed">
                  The image could not be read back — the render may have been
                  interrupted.
                </p>
              </div>
            ) : run.done ? (
              <div className="pg-image-frame" style={shot}>
                <img
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
                and D429's seed-reuse button with them; the invented seed is
                surfaced by pre-filling the Seed box instead. */}
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
      </Card>
    </div>
  );
}
