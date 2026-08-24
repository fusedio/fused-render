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
import { CameraIcon, DownloadIcon, ImageIcon, XIcon } from "lucide-react";
import { cancelJob, type Job } from "@platform/lib/jobs";
import { getConfig, mkdir, pickFile, rawUrl, type AiCatalogModel } from "@platform/lib/api";
import { startImage, uploadFile, watchJob, type ImageStarted } from "./client";
import {
  Field,
  FieldContent,
  FieldDescription,
  FieldTitle,
  RailChips,
  RailSlider,
  ResultSlot,
  AnswerBlock,
  StageShell,
  StarterCards,
  useAutoGrow,
  type Starter,
} from "./controls";
import { StarterIcons } from "./starterIcons";
import { Alert, AlertDescription } from "@apps/ai_models/ui/alert";
import { Button } from "@apps/ai_models/ui/button";
import { InputGroup, InputGroupAddon, InputGroupTextarea } from "@apps/ai_models/ui/input-group";
import { Input } from "@apps/ai_models/ui/input";
import { Kbd } from "@apps/ai_models/ui/kbd";
import { Progress } from "@apps/ai_models/ui/progress";
import { Skeleton } from "@apps/ai_models/ui/skeleton";
import { Spinner } from "@apps/ai_models/ui/spinner";
import { cn } from "@apps/ai_models/ui/utils";
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
  const [camera, setCamera] = useState(false);

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
  const { ref: boxRef, grow } = useAutoGrow();
  const streamRef = useRef<MediaStream | null>(null);
  const videoRef = useRef<HTMLVideoElement | null>(null);
  // Set on the way in as well as cleared on the way out, the same shape
  // TranscribeStage's own flag has: an upload awaits the config, a mkdir and
  // the POST, and a continuation that lands after an unmount must not write
  // state (or leave a camera running) from a dead component.
  const aliveRef = useRef(true);
  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
      abortRef.current?.abort();
      streamRef.current?.getTracks().forEach((track) => track.stop());
      streamRef.current = null;
    };
  }, []);

  // The live view is attached HERE, not in the click that opened the camera:
  // the <video> does not exist until this render, so `srcObject` has nothing
  // to be set on until the panel is mounted.
  useEffect(() => {
    if (!camera) return;
    const video = videoRef.current;
    const stream = streamRef.current;
    if (!video || !stream) return;
    video.srcObject = stream;
    void video.play().catch(() => {});
  }, [camera]);

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

  const stopCamera = () => {
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
    setCamera(false);
  };

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

  /** A webcam frame has no path — it does not exist anywhere yet — so this one
   *  case genuinely has to be written before it can be pointed at. It lands in
   *  the app's own scratch dir, `<cache>/image-playground`
   *  (`~/.fused-render/cache/…`), and NOT anywhere in the user's home: bytes
   *  this app invented belong with the app's state, and a capture dropped in
   *  `ai/images` — a folder the user browses, holding renders — is a picture
   *  nobody can tell from a generated one. Both levels are mkdir'd, because
   *  `/api/fs/mkdir` creates ONE directory by design (a typo must not spawn a
   *  tree) and on a fresh machine neither exists. */
  const save = async (data: Blob, name: string, stamped = true): Promise<AttachedImage | null> => {
    setError(null);
    setAttaching(true);
    try {
      const config = await getConfig();
      await mkdir(config.cache_dir).catch(() => {});
      const dir = `${config.cache_dir}/image-playground`;
      await mkdir(dir).catch(() => {});
      // A capture is stamped — every one is a different picture and losing the
      // last one would be a surprise. A shipped SAMPLE is not: the same bytes
      // land at the same path however many times the pill is clicked, so the
      // examples cannot slowly fill the cache with copies of themselves.
      const stamp = stamped
        ? new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19) + "-"
        : "sample-";
      const path = `${dir}/${stamp}${name}`;
      await uploadFile(path, data, name);
      if (!aliveRef.current) return null;
      const landed = { path, name };
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
    try {
      streamRef.current = await navigator.mediaDevices.getUserMedia({
        video: { width: { ideal: 1280 }, height: { ideal: 720 } },
      });
      if (!aliveRef.current) {
        stopCamera();
        return;
      }
      setCamera(true);
    } catch (e) {
      setError(
        (e as Error).name === "NotAllowedError"
          ? "Camera access was refused — allow it in the browser and try again."
          : (e as Error).message,
      );
    }
  };

  /** One frame off the live view, at the camera's own pixels. PNG because
   *  `toBlob` is guaranteed to produce one and the server reads it. */
  const capture = () => {
    const video = videoRef.current;
    if (!video || !video.videoWidth) return;
    const canvas = document.createElement("canvas");
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    canvas.getContext("2d")?.drawImage(video, 0, 0);
    stopCamera();
    canvas.toBlob((blob) => {
      if (blob) void save(blob, "webcam.png");
    }, "image/png");
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

  // Chips lead the panel; sliders and the seed follow.
  const config = (
    <>
      <div className="flex flex-col gap-3">
        {/* Hidden, not disabled, while the attached image decides the size:
            a chip row where nothing is lit and a slider parked on 480 are
            both controls saying something about a render that will come back
            at the photo's own size instead. One line replaces them, and it
            is also the way back to picking a size by hand. */}
        {!sizeIsTheImages && (
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
        <Field>
          <FieldContent>
            <div className="flex items-center gap-2">
              <FieldTitle className="flex-1">Size</FieldTitle>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="h-6 px-1.5 text-xs text-muted-foreground"
                onClick={() => setSizeFromImage(false)}
              >
                Set a size
              </Button>
            </div>
            <p className="text-sm">
              {fitted
                ? `${fitted.width} × ${fitted.height} — the picture's shape`
                : "Read from the attached picture"}
            </p>
            <FieldDescription>
              {fitted && natural && (natural.width > fitted.width || natural.height > fitted.height)
                ? `Scaled down from ${natural.width} × ${natural.height}: an edit at the full ` +
                  "size takes minutes. Set a size to render it bigger."
                : `The picture's own shape, longest side up to ${EDIT_LONGEST_SIDE}.`}
            </FieldDescription>
          </FieldContent>
        </Field>
      ) : (
        <>
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
      <Field>
        <FieldContent>
          <FieldTitle>Seed</FieldTitle>
          <Input
            type="text"
            inputMode="numeric"
            value={seed}
            placeholder="Random each time"
            onChange={(e) => setSeed(e.target.value.replace(/[^0-9]/g, ""))}
          />
          <FieldDescription>
            Same seed + same prompt + same settings = the same picture.
          </FieldDescription>
        </FieldContent>
      </Field>
    </>
  );

  return (
    <StageShell
      title="Describe a picture"
      configOpen={configOpen}
      onToggleConfig={() => setConfigOpen((open) => !open)}
      config={config}
    >
      <InputGroup>
        <InputGroupTextarea
          ref={boxRef}
          rows={3}
          value={prompt}
          placeholder={base ? "Describe the change…" : "Describe the picture…"}
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
        {/* Clear in the box's top-right corner: it appears only once there is
            a picture to throw away, and a slot of its own on the floor would
            cost the box a permanent row of height. */}
        {!busy && run && (
          <InputGroupAddon align="inline-end">
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="text-muted-foreground"
              title="Clear the prompt and the picture"
              onClick={clear}
            >
              Clear
            </Button>
          </InputGroupAddon>
        )}
        {/* The composer's floor: the two ways to attach a picture, then
            Generate — one cluster, attach beside Generate rather than across
            the box from it. */}
        <InputGroupAddon align="block-end">
          {/* The attached photo, on the floor's own line: thumbnail and ✕
              only — the filename said nothing a look at the picture does not,
              and the sentence about editing was a caption on a control nobody
              had asked a question about. */}
          {base && (
            <span className="flex items-center gap-1">
              <button
                type="button"
                className="overflow-hidden rounded-md border outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50"
                title="See this picture"
                aria-label="See this picture"
                onClick={() => setShowBase(true)}
              >
                <img src={rawUrl(base.path)} alt="" className="size-7 object-cover" />
              </button>
              <Button
                type="button"
                variant="ghost"
                size="icon-sm"
                className="size-6 text-muted-foreground"
                title="Remove this image"
                aria-label="Remove this image"
                onClick={() => setAttachment(null)}
              >
                <XIcon />
              </Button>
            </span>
          )}
          {editable && (
            <>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="text-muted-foreground"
                title="Point at a picture already on this disk — nothing is copied"
                disabled={attaching}
                onClick={() => void choose()}
              >
                <ImageIcon />
                {base ? "Replace" : "Add an image"}
              </Button>
              <Button
                type="button"
                variant={camera ? "secondary" : "ghost"}
                size="sm"
                className={cn(!camera && "text-muted-foreground")}
                title="Take one with the webcam"
                disabled={attaching}
                onClick={() => (camera ? stopCamera() : void openCamera())}
              >
                <CameraIcon />
                Webcam
              </Button>
              {attaching && (
                <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
                  <Spinner className="size-3" />
                  Working…
                </span>
              )}
            </>
          )}
          {busy ? (
            <Button
              type="button"
              variant="secondary"
              size="sm"
              className="ml-auto"
              onClick={() => void cancelJob(run.started.jobId).catch(() => {})}
            >
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
              onClick={() => void generate()}
            >
              Generate <Kbd className="bg-transparent text-inherit">⏎</Kbd>
            </Button>
          )}
        </InputGroupAddon>
      </InputGroup>

      {/* The live view, while the webcam is open — right below the composer
          whose Webcam button opened it. */}
      {camera && (
        <div className="relative overflow-hidden rounded-lg border bg-card">
          <video ref={videoRef} playsInline muted className="block w-full" />
          <div className="absolute right-2 bottom-2 flex gap-2">
            <Button type="button" size="sm" onClick={capture}>
              Capture
            </Button>
            <Button type="button" variant="secondary" size="sm" onClick={stopCamera}>
              Cancel
            </Button>
          </div>
        </div>
      )}

      {/* The attached picture at full size. Deliberately the whole modal: an
          image and a way out, no title bar, no filename, no actions — the ✕ on
          the row below already removes it, and this is only here because a
          28px thumbnail cannot be looked at. Click the backdrop or press
          Escape to close, the two things anybody tries. */}
      {base && showBase && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-8"
          role="dialog"
          aria-label="The attached picture"
          onClick={() => setShowBase(false)}
        >
          <img
            src={rawUrl(base.path)}
            alt=""
            className="max-h-full max-w-full rounded-lg"
            onClick={(e) => e.stopPropagation()}
          />
          <Button
            type="button"
            variant="secondary"
            size="icon-sm"
            className="absolute top-4 right-4"
            title="Close"
            aria-label="Close"
            onClick={() => setShowBase(false)}
          >
            <XIcon />
          </Button>
        </div>
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

      {error && (
        <Alert variant="destructive">
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

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
        <AnswerBlock label="Result">
          <figure className="flex flex-col gap-2">
            {run.done && run.readFailed ? (
              // `watchJob` said done (including a `gone` it reads as done —
              // see the `Run.readFailed` comment) but the file this <img>
              // asked for does not actually exist. Say that plainly rather
              // than leaving a broken-image icon and a save link to nothing.
              <div
                className="flex items-center justify-center overflow-hidden rounded-lg border bg-card"
                style={shot}
              >
                <p className="p-4 text-sm text-muted-foreground">
                  The image could not be read back — the render may have been
                  interrupted.
                </p>
              </div>
            ) : run.done ? (
              <div className="group relative overflow-hidden rounded-lg border bg-card" style={shot}>
                <img
                  src={rawUrl(run.started.path) + "&t=" + run.started.jobId}
                  alt={run.started.prompt}
                  className="size-full object-cover"
                  onError={() =>
                    setRun((r) =>
                      r && r.started.jobId === run.started.jobId ? { ...r, readFailed: true } : r,
                    )
                  }
                />
                {/* A download link, not a clipboard write: ClipboardItem takes
                    image/png only and the render's format is unknown here. */}
                <a
                  className="absolute top-2 right-2 inline-flex size-8 items-center justify-center rounded-md bg-background/70 text-muted-foreground backdrop-blur-sm transition-colors hover:bg-accent hover:text-accent-foreground [&_svg]:size-4"
                  href={rawUrl(run.started.path)}
                  download={run.started.path.split("/").pop() || "picture.png"}
                  title="Save this picture"
                  aria-label="Save this picture"
                >
                  <DownloadIcon />
                </a>
              </div>
            ) : (
              <div className="relative overflow-hidden rounded-lg border bg-card" style={shot}>
                <img
                  src={rawUrl(run.started.previewPath) + "&t=" + previewTick}
                  alt="Render in progress"
                  className="size-full object-cover"
                  style={previewLive ? undefined : { display: "none" }}
                  onLoad={() => setPreviewLive(true)}
                  onError={() => setPreviewLive(false)}
                />
                {!previewLive && <Skeleton className="absolute inset-0 rounded-none" aria-hidden="true" />}
              </div>
            )}
            {/* Progress only. The settled parameters were dropped by request,
                and D429's seed-reuse button with them: an invented seed is now
                surfaced nowhere, so a random render cannot be reproduced. */}
            {busy && (
              <figcaption className="flex items-center gap-3 text-xs text-muted-foreground">
                <span className="min-w-0 flex-1 truncate">
                  {job?.detail || "Starting — a cold model loads first…"}
                </span>
                {pct !== null && <Progress value={pct} className="h-1.5 w-40 shrink-0" />}
              </figcaption>
            )}
          </figure>
        </AnswerBlock>
      )}
    </StageShell>
  );
}
