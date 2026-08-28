---
name: fused-render-ai
description: Call an AI model from a fused-render page or .py file — fused.ai text and streaming, .image, .video, .transcribe, .embed, .models, and `import fused_ai`. Use when a page needs a model, or an AI call rejects (ai_unavailable, model_loading, timeout, stalled).
---

# AI in a fused-render Page

## Overview

`fused.ai` is one call with **two destinations**, and the model id alone decides which:

| `opts.model` | Where it runs | Credential |
|---|---|---|
| Contains a `/` (a Hugging Face repo id), **or ends in `.gguf`** (a llamacpp-text curated filename id) | **This machine.** A resident worker process holding the weights. | none |
| Anything else — `"sonnet"`, `"claude-haiku-4-5-20251001"`, omitted | The local **`claude` (Claude Code) CLI**. | the user's Claude Code login |

That rule (`"/" in model or model.endsWith(".gguf")`) is the whole seam: a page swapping `model: "opus"` for a local id changes nothing else — same call, same resolved shape. **The local id is not always `org/name`.** Most engines address a model by its Hugging Face repo id, but `llamacpp-text`'s curated ids are the GGUF's OWN FILENAME instead (`"Qwen3.5-4B-Q5_K_M.gguf"`, no slash at all) — a repo commonly ships two dozen quantizations, and the filename is what tells them apart. Never split an id on `/` or build a Hub URL from one assuming that shape; treat it as opaque.

**Never hard-code a repo id, and treat the ones in this file as illustrations.** A repo belongs to a *backend*, not to a capability: an MLX-packed repo is an unusable download on Windows, Linux, or a Mac switched to llama.cpp. Always take ids from `fused.ai.models.catalog()`, which answers for the engine actually serving this machine.

Both destinations are **local-only** — there is no hosted path — so an exported page can call neither. See "Surviving Export".

## When to Use

- A page asks a model something: text, chat, streaming.
- A page generates an image (`fused.ai.image`), a video (`fused.ai.video`), a 3D mesh from an image (`fused.ai.mesh`), or transcribes a recording (`fused.ai.transcribe`) locally.
- A page manages what this machine is holding in memory (`fused.ai.models.*`).
- An AI call rejects and you need to know whose fault it is.
- A `.py` data file, or a process outside the browser entirely, wants the same AI calls — see "Calling from Python".

For `runPython`, params, or file IO → **`fused-render-authoring`**. For opening/running the app → **`fused-render-usage`**. For a folder that needs its own long-running daemon (outliving a page, surviving the warm worker's 15-minute idle-retire) rather than a per-call AI request → **`fused-render-background-apps`**.

## Text: `fused.ai(prompt, opts?)`

Resolves with **exactly** this — the server normalizes, so no guarding:

```json
{
  "text": "the completion",
  "model": "claude-haiku-4-5-20251001",
  "usage": { "input_tokens": 544, "output_tokens": 73 }
}
```

- `model` — the id that **actually ran**; an alias (`"sonnet"`) echoes back resolved.
- `usage` — `null`, or exactly `{input_tokens, output_tokens}`. **Anthropic names**; `prompt_tokens`/`completion_tokens` give `undefined`.

### Options, and which destination honours them

The local-only options are **refused with a 400, never silently dropped** — a setting you can watch have no effect is the failure mode the server is built to avoid.

| Option | Claude path | Local model |
|---|---|---|
| `systemPrompt` | ✅ | ✅ |
| `model` | ✅ | ✅ (a repo id) |
| `effort` `"low"｜"medium"｜"high"｜"xhigh"` | ✅ Claude Code's own thinking semantics | ignored |
| `onChunk(text)` | ✅ | ✅ |
| `history` `[{role, content}]` | ❌ **400** | ✅ |
| `raw` (no chat template) | ❌ **400** | ✅ |
| `temperature` / `topP` / `maxTokens` | ❌ **400** | ✅ |

Defaults: model `claude-haiku-4-5-20251001` (or the user's configured default); `effort` low = no extended thinking. `raw` and `history` are mutually exclusive — raw has nowhere to put prior turns.

`onChunk(text)` opts into streaming: it fires per delta and the promise **still resolves with the same `{text, model, usage}`**. Both destinations stream.

### Store the model beside the vectors

`embed` returns the `model` it actually used. **Persist it with anything you
persist the vectors in, and treat a query embedded under a different model as
invalid** — not as a result to rank, as a bug to refuse.

```js
const { vectors, dim, model } = await fused.ai.embed({ texts: chunks, kind: "document" });
await fused.writeFile("index.json", JSON.stringify({ model, dim, vectors }));

// …and at query time
const index = JSON.parse(await fused.readFile("index.json"));
const q = await fused.ai.embed({ texts: [question], kind: "query" });
if (q.model !== index.model) throw new Error(
  `index was built with ${index.model}, cannot search it with ${q.model}`);
```

Two models produce two different spaces. A cosine between them is not a low
score, it is a meaningless number — and it will not look like one, because
nearest neighbours come back ranked and plausible.

**A dimension check does NOT catch this, and that is the whole point.** The
dangerous case is same-dim, and it is the common one — both of these pairs are
768:

| Engine | These two models are both 768-dim |
|---|---|
| `onnx-embed` | `nomic-ai/nomic-embed-text-v1.5` and `onnx-community/siglip2-base-patch16-384-ONNX` |
| `mlx-embed` | `mlx-community/nomicai-modernbert-embed-base-bf16` and `google/siglip2-base-patch16-384` |

Index with one, switch models, query with the other: **same engine, same machine,
same `dim`, no error, wrong neighbours.** So `dim` is a sanity check on your own
storage, never a provenance check — the two so400m rows happening to be 1152
against 768 is luck, not protection.

This is the `kind` failure one level up. There, using the wrong prefix costs
recall silently; here, using the wrong model costs the answer entirely, just as
silently. Both are invisible to the endpoint, which sees one call and cannot know
what your index was built with — which is exactly why the field is returned to
you rather than checked for you.

### Rejections

Every rejection is an `Error` with `.type`, and `err.jobId` is set on any rejection that had a row:

| `.type` | Cause | UI response |
|---|---|---|
| `model_loading` | **Local model not resident.** The call already **started the load**; `err.jobId` is it. | Not a failure — show the download. |
| `ai_unavailable` | `claude` binary not found (Claude path), or the worker won't start (local). | Friendly unavailable state, not a raw overlay. |
| `bad_request` | Empty prompt, bad option, or a local-only option on the Claude path. | Your page's bug — read the message. |
| `ai_error` | Ran but errored (bad model id, upstream failure). | Show `err.message`. |
| `timeout` | No answer within **600 s** server-side. | Offer retry. |
| `unavailable` | 409 — a fact about **this machine** ("needs Apple Silicon", "the runner is not built yet"). Only from `fused.ai.models.*`, `fused.ai.image`, `fused.ai.transcribe`. | Show the reason; it explains itself. |
| `cancelled` | The row's ✕ stopped it. Only from `fused.ai.image` / `fused.ai.transcribe`. | Not a failure — the user asked. |

**`model_loading` is the one that surprises people.** The first call naming a local model does not fail-and-forget; it returns 409 *having kicked off a multi-GB download*, and hands you the job id so the page can draw it:

```js
try {
  const res = await fused.ai(question, { model: chosenRepoId });
  out.textContent = res.text;
} catch (err) {
  if (err.type === "model_loading") {
    // Not an error — the weights are arriving because we asked.
    out.textContent = "Loading the model…";
    await fused.watchJob(err.jobId).watch((job) => {
      out.textContent = `Loading… ${job.done}/${job.total}`;   // bytes
    });
    return ask(question);                                       // resident now
  }
  out.textContent = (err.type || "error") + ": " + err.message;
}
```

### No stale-cancel

Unlike `runPython`, AI calls run **fully concurrent** — an AI call is never a slider scrub. Nothing stops a double-click firing two calls, so **disable the button while one is in flight**. To stop a *local* generation mid-flight use `fused.ai.cancel()`.

### Feed it aggregates, not the dataset

Compute in Python, reduce to compact aggregates, hand the model those. A raw table blows the token budget and drowns the signal:

```js
const data = await fused.runPython("./data.py", { days });   // full data for the UI
const context = JSON.stringify({                              // aggregates for the model
  total_revenue: data.total_revenue,
  revenue_by_region: data.by_region,
});
const res = await fused.ai(`Data (JSON):\n${context}\n\nQuestion: ${q}`, {
  systemPrompt: "You are a data analyst. Answer ONLY from the provided JSON. Cite figures.",
  effort: "low",
});
```

## Local Models: `fused.ai.models`

Generation itself needs **nothing new** — a repo id in `fused.ai({model})` already reaches a local model. What needs an API is everything *around* it, because a model here is a resident process, not a request to someone else's datacentre.

| Call | Returns |
|---|---|
| `fused.ai.models.list()` | What is loaded right now, its memory, and which runners exist on this machine. |
| `fused.ai.models.catalog()` | Every model a capability can offer here — the curated shortlist **plus whatever is already on this disk** — with what this machine can run. |
| `fused.ai.models.load(id, {capability}?)` | `{jobId}` — **not a loaded model.** |
| `fused.ai.models.download(id, {capability}?)` | `{jobId}` — weights only, no load. |
| `fused.ai.models.unload(idOrCapability)` | `{stopped, ...}` |
| `fused.ai.cancel(capability?)` | `boolean` — stops generation, **keeps the weights**. |

### Reading `catalog()`

Each capability's `models[]` entry is `{id, label, size_gb, note}` plus three state flags:

| Field | Means |
|---|---|
| `source` | `"curated"` — the shortlist for the engine serving this capability. `"cached"` — a repo **found on this disk** that the shortlist has never heard of (the user downloaded it from the AI Models page's Hub search). |
| `downloaded` | On this disk, **completely**. A repo whose download was cancelled, crashed, or is still running does not count (D424) — half a snapshot has no loadable weights, and a picker offering one is a `load()` that fails. Always `true` for `"cached"`. |
| `loaded` | A worker is holding the weights **right now**. |

- **Render the whole of `models[]`.** Filtering to `source === "curated"` hides the model the user deliberately downloaded — the exact bug these flags exist to end. Mark states instead: `loaded` → ready now, `downloaded` → instant load, neither → a `size_gb` download first. (The app's own AI Models page filters, because its Local tab already lists the full cache; your page has no such tab.)
- **Every entry is one the engine serving that capability can actually load** — a cached repo in a format that backend does not read is left out, so the list moves when the user switches engines.
- **Lists are ordered smallest download first, `default` is the first CURATED entry.** So omitting `model` gets the *smallest* model, not the best one. **Read `default`, never `models[0]`**: cached entries are appended after the curated ones, and an engine with no shortlist reports `default: null` — "no recommended model" is an answer to respect.
- `note` is `null` on a cached entry and `size_gb` is its measured on-disk footprint. Render `label || id`.
- **`id` shapes can differ WITHIN one capability's `models[]`.** `text-generation`'s curated `llamacpp-text` entries are GGUF filenames; a "cached" GGUF repo the user found via Hub search is a bare repo id like every other engine's entries. Render `id` as an opaque label — never assume `id.split("/")` or a Hub-URL template works for every row.

### Loading, unloading, cancelling

`load`/`download` hand back a **job, not a result** — a cold load is multi-GB and nothing waits on it. Watch with `fused.watchJob(jobId)`.

**Pass `{capability}` whenever you know it, and you almost always do.** Omitted, the server infers it from the repo's cached files, then from curation, then falls back to `"text-generation"`. That fallback is the trap: an *undownloaded* whisper or diffusion repo has no files to read, so it goes to the text runner and fails inside mlx-lm with a complaint about a missing `config.json` — which reads as a corrupt model, not a wrong-runner dispatch.

```js
fused.ai.models.load(imageRepoId,  { capability: "text-to-image" });
fused.ai.models.load(speechRepoId, { capability: "automatic-speech-recognition" });
```

A repo that *is* cached and that no engine here reads is refused with a sentence naming the repo, what it looks like, and what to pass — never a library traceback.

**Unload by capability, not by id.** A page's Unload button means "release whatever is resident", and the page does not reliably know what that is — another page or the AI Models tab may have loaded something else:

```js
fused.ai.models.unload({ capability: "text-generation" });   // honest
fused.ai.models.unload(selectedId);                          // often a no-op
```

`unload({capability})` and `cancel(capability)` take one of three strings — `"text-generation"`, `"text-to-image"`, `"automatic-speech-recognition"`. One model is resident per capability, which is what lets a chat model and a Whisper model be loaded at once. An unrecognised capability is a 400, not a no-op.

`fused.ai.cancel()` **defaults to `"text-generation"`**, so it stops a chat and nothing else; to stop an image or transcription, name the capability or press the ✕ on that job's row. Resolving `false` is **not an error** — a Stop pressed as the last token lands is a no-op.

Runtime calls reject `"unavailable"` (409, a fact about this machine) or `"bad_request"`.

## Images: `fused.ai.image({prompt, ...})`

The one call in the bridge that **resolves with a file**. Text streams, so `fused.ai` hands back words; an image is an artefact, so this hands back somewhere to point an `<img>`.

```js
el.onerror = () => el.hidden = true;    // an early tick has no frame yet — see below

const img = await fused.ai.image({
  prompt: "a topographic map of an island, engraved",
  onProgress: (job) => {
    if (job.total) bar.value = job.done / job.total;   // DENOISING STEPS, not bytes
    if (job.previewUrl) { el.src = job.previewUrl; el.hidden = false; }
  },
});
el.src = img.url;        // ready-made /api/fs/raw url — no need to build it
el.dataset.seed = img.seed;
```

### Options and the reply

`prompt` is the only required one. Everything else has a default and a **hard range the server clamps to**, so read the reply rather than echoing your request:

| Option | Default | Range | Notes |
|---|---|---|---|
| `prompt` | — | non-empty | trimmed; empty or non-string is `bad_request` **before** a job opens |
| `model` | the `text-to-image` row's `default` (the *smallest* curated repo) | a repo the active engine reads | see "Choosing `model`" — this is the one that bites |
| `width` / `height` | `1024` | **256–2048** | clamped, then **snapped DOWN to a multiple of 16** (`1000 → 992`). A non-number becomes 1024 rather than a 400 |
| `steps` | `28` | **1–100** | clamped; not a number → 400 |
| `guidance` | `4.0` | **0–20** | clamped; not a number → 400 |
| `seed` | random | **0 – 2147483647** | clamped; not a whole number → 400 |
| `image` | — | one existing file, page-relative | edit this base image instead of rendering from `prompt` alone — **mflux only** (see below). A single string; an array or any other type → 400 |
| `onProgress(job)` | — | — | per denoising step |

**No negative prompt, no batch count, no scheduler or LoRA, and no `strength`** — the edit mechanism `image` uses does not take a strength knob at all, so there is nothing to pass. One prompt in, one PNG out; two pictures means two calls. Pass an option that is not in the table above — `strength`, a typo — and the call is refused `bad_request` rather than quietly rendering text-to-image and ignoring what you asked for; the request envelope is closed, both in the bridge and on the server, so a page cannot get a plausible-looking picture back from an option that was never honoured (D413).

Resolves with `{jobId, path, url, previewUrl, previewPath, model, prompt, width, height, steps, guidance, seed}`, plus `image` (the resolved absolute path) when you passed one — the render that will actually happen, not the one you asked for.

### Editing a base image: `{image}` — mflux only

```js
const edited = await fused.ai.image({
  prompt: "make the sky stormy",
  image: "photo.png",   // beside this page, like fused.ai.transcribe's `path`
});
```

- **This is an mflux-only capability.** The Diffusers engine — the default off Apple Silicon, and what a Mac switches to — refuses `image` outright rather than answering best-effort: `bad_request`, naming the Engines tab. **A page written on a Mac with mflux selected can fail on Linux with the identical call**, which is the one engine asymmetry worth testing for before you ship a page that relies on `image` — check the error's `.message` and degrade to a plain prompt (or tell the user to switch engines) rather than assuming every machine can edit.
- **`image` is one existing file, a single string.** Page-relative exactly like `fused.ai.transcribe`'s `path` (RH-1) — `"photo.png"` means beside this page, not beside wherever the server was launched from. A missing file, a directory, or anything that is not a plain string (an array included) is `bad_request` before a job opens.
- **`width`/`height` default from the base image, not from 1024².** The image is fit to a longest side of 1024 without upscaling, snapped down to a multiple of 16, floored at 256 — read the reply rather than assuming your own file's dimensions survive unchanged. An explicit `width`/`height` still wins. **The 256 floor overrides aspect on an extreme ratio**: a very wide or very tall base (a 20:1 banner, say) does not come back 20:1 — its short side floors at 256 regardless, so the reply's shape can be noticeably squarer than the file you sent. This is the arithmetic as confirmed on hardware, not a bug to route around; read `width`/`height` off the reply rather than assuming the ratio held.
- **`steps`/`guidance` default to `4`/`1.0` for an edit, not the `28`/`4.0` a plain render defaults to.** Omitting them on an edit gets the numbers tuned for editing; passing your own still works the same way it does for a plain render.
- **There is no `strength` option, and none is planned as an "unexercised knob".** The edit mechanism this app drives does not use image strength at all — it is instruction-following editing, not img2img blending — so there is nothing to defer.
- **On mflux, "make that one again" (below) means the same recorded seed, not the same pixels** — see that bullet for why, and note it holds for a plain render on this engine too, not only for an edit.

- **`seed` comes back whether or not you passed one**, so a page can always ask for the SAME SEED again. **On mflux, that is not the same as asking for the same PICTURE again.** Two bare mflux renders with an identical seed, prompt, step count and guidance came back different PNGs on the hardware D432 was verified on — a fact about that engine as it ships today, present for a plain render exactly as much as for an edit, and neither caused nor fixed by the `image` option. Treat a repeated `seed` on mflux as "the request you asked for again", not "the picture you got again"; this codebase has not verified either way whether the Diffusers engine reproduces byte-identically, so do not assume it does either — check for yourself before building a "regenerate identically" feature on any engine.
- **The server owns where the PNG goes**: `<home>/ai/images/<YYYYmmdd-HHMMSS>-<uid>.png` under `~/.fused-render`, never beside the page (which may be read-only), time-ordered so the folder sorts chronologically. **It outlives the tab** — a page that navigated away mid-render can still open `path`. Nothing cleans these up, so a page that renders in a loop fills a directory the user browses.
- **One row and one file per render.** Two calls get two `jobId`s and two paths; nothing overwrites.

### Choosing `model`

Take it from `catalog()`. For images the engines are stricter than anywhere else:

| Engine serving `text-to-image` | What it loads |
|---|---|
| **Diffusers (CPU)**, and its **(CUDA)** / **(ROCm)** variants — the default everywhere but Apple Silicon, and what a Mac switches to | the curated FLUX.2 klein repo, plus any ordinary diffusers repo (a `model_index.json`), via `AutoPipelineForText2Image`. The three read exactly the same repos — they differ only in which torch wheel is installed, so nothing about `model` changes between them |
| **MLX FLUX** — the Apple Silicon default | the one MLX repo it names a variant class for, and **nothing else** |

That second row is a hard limit, not a summary of the catalog: mflux has no `AutoPipeline`, so any other MLX diffusion repo is refused with a sentence pointing at the working id or at the Engines tab. **On a Mac, "let the user paste a Hub id" has exactly one valid answer** — `catalog()` is the only honest picker.

The same model also downloads differently per engine: ~4.6GB as one MLX repo against ~10.8GB for the Diffusers recipe, which assembles a quantized transformer from a second repo. So `size_gb` is **every byte the download fetches across every repo it touches**, and the extra repo appears in the model cache as a component nobody chose — not a model, excluded from `catalog()`, and load-bearing.

**Memory is the failure mode to expect, and which memory depends on the engine.** On Apple Silicon, MLX FLUX reserves far more than Diffusers and is untested below 32GB. Off it, the default engine is the CPU build, which holds the whole pipeline in **system RAM** rather than in a card's — so this is no longer an Apple-Silicon-shaped problem, and a machine that renders fine with a CUDA or ROCm engine selected can OOM on the default one. Either way the OOM arrives as `ai_error`, so show `err.message` and name the Engines tab rather than working around it.

### Progress: the row, and the cold start

`onProgress` fires per denoising step with the download-manager record, and that row's ✕ really stops the render (the work is the server's, not the page's). `unit` is `""`, so steps read as a bare pair (`14 / 28`), not the clock a transcription draws; `detail` goes from "Preparing…" to `Saved <filename>`, and the row is titled with the prompt truncated to 80 characters.

**The cold-model path is NOT `model_loading`** — the biggest difference from `fused.ai()`. `fused.ai.image` does not reject when the model is cold; it loads it inside the job you are already watching, so there is no retry to write. But:

- While the weights arrive, `detail` reads `Waiting for <repo> — …` and **`done`/`total` are null**. Guard the division (`if (job.total)`) or draw an indeterminate bar — a bar that divides straight through shows `NaN` for the whole download.
- **The bytes are on the model's own row**, never on the image job. For a download percentage, take that row's id from `fused.ai.models.list()` (the `loaded[]` entry carries `jobId` while it is still loading) rather than spelling it yourself.
- The wait is bounded at **3600 s**; past that the render fails naming the model's job id.

### Watch it being made: `job.previewUrl`

Every tick carries a ready-made URL for a **~32px thumbnail of the image so far** — the worker rewrites that one file each denoising step, so an `<img>` kept on it shows a picture emerging out of noise instead of a number going up. It costs about 1% of the render; blur and upscale it in CSS. `previewPath` is the same file as a path.

- **It is a promise about a path, not a file.** An early tick can 404 (the first step writes no frame), and a model whose latent space has no fitted projection never writes one — so keep the `onerror` that hides the `<img>`. From step 2 on you see the model's *current guess* at the finished image, not the raw latent.
- **It goes null at the end, and that is the cue to swap.** The preview file is deleted the moment the real PNG lands, so `previewUrl` is null on the last tick and on the resolved object — end on `img.url`.
- **The URL is already cache-busted** (`&step=<n>`). Adding your own is redundant; dropping the given one shows frame 2 forever.
- Which engine served you makes no difference: the projection is keyed by the model's latent space, not the repo.

### Slow and failed renders

- **Minutes, not seconds — and capped at 900 s (15 min)** server-side, separately from the 600 s the text relay allows. **The default engine off Apple Silicon is the CPU build even on a machine with a capable GPU** — CUDA and ROCm are an Engines-tab opt-in — so this cap is reachable on ordinary hardware and not only where there is no GPU to use. A 2048² 100-step render can hit it and comes back `ai_error` "the image process did not answer", so don't hand a slider the full clamp range.
- **On the ROCm engine, a big render can take the user's desktop with it.** Compute and display share one ring on a single-GPU machine, so a long generation can starve the compositor until the driver resets it — observed on gfx1200, where the process the kernel killed was the window manager, not the renderer. The GPU recovers; the session may not. You cannot catch this — there is no rejection, because the browser goes down too — so treat it as a reason not to offer a 2048²/100-step button as the default on Linux, and to keep your own defaults modest.
- **Renders serialize.** One generation at a time per worker (neither pipeline is thread-safe, and an accelerated engine has one device to serialize on), and a second call **waits with no queue message on its row** — unlike transcription, which says so. Two renders fired together read as one stalled bar. Disable the button; if your page queues, say so in your own UI.
- **An aged-out row still answers off the file.** A backgrounded tab can sleep past job retention; the bridge then `stat`s `path` and **resolves normally** if the PNG is there. Only a missing row *and* no file rejects `ai_error` "the image job is no longer being reported". A missing row is not a failed render.
- Rejects `.type` `"cancelled"` | `"ai_error"` | `"unavailable"` (no image runner here — reason in the message). `fused.ai.cancel("text-to-image")` stops the render; the bare `fused.ai.cancel()` will not.

## Video: `fused.ai.video({prompt, ...})`

`fused.ai.image`'s twin — job-backed, resolves with a file — for text-to-video with audio, on LTX-2.3 (`ltx-2-mlx`). **Apple Silicon only, with no fallback on any other platform**: this is the first local capability that can be genuinely unservable on the machine running your page, so always handle `.type === "unavailable"`.

```js
const vid = await fused.ai.video({
  prompt: "a paper boat drifting down a rain-soaked street, cinematic",
  onProgress: (job) => { if (job.total) bar.value = job.done / job.total; },
});
video.src = vid.url;   // ready-made /api/fs/raw url, with audio muxed in
```

### Options and the reply

| Option | Default | Range | Notes |
|---|---|---|---|
| `prompt` | — | non-empty | trimmed; empty or non-string is `bad_request` **before** a job opens |
| `model` | the `text-to-video` row's `default` (`null` if this machine cannot serve the capability at all) | the int4 LTX-2.3 tier | two curated tiers (int4 ~28.5 GB, int8 ~37.8 GB) |
| `width` / `height` | `704` / `480` — the engine's own default | **256–1344**, and `width * height <= 768 * 1344` | clamped, then **snapped DOWN to a multiple of 32**; an over-large canvas is shrunk further to fit the pixel ceiling |
| `frames` | `97` (~4s at 24fps) | the engine's grid, `1 + 8n`, `n` 1–21 (9–169 frames) | **rounded UP to the next valid value** — never down, and never to "nearest" — `100` becomes `105`, `30` becomes `33`; a value below 9 becomes 9 (the grid starts at its second point, `n=1`) |
| `steps` | `8` | **2–50** | clamped; not a number → 400. The floor is 2, not 1 |
| `seed` | random | **0 – 2147483647** | clamped; not a whole number → 400 |
| `onProgress(job)` | — | — | per denoising step |

**These numbers are the RESOLVED ENGINE's, not the route's** — the frame grid, canvas default and step default all come from whichever runner serves the request (`registry.VideoTraits`), and `fused.ai.models.catalog()`'s `videoTraits` hands you the same numbers if you need to draw a control that agrees with the server. The rails around them (`n` in 1–21, steps 2–50, the 32-multiple canvas and the pixel ceiling) are the app's own and hold for every engine.

**No `guidance`** — the engine is CFG-distilled and takes no such parameter; passing one is refused `bad_request` like any other unsupported option, the same envelope rule `fused.ai.image` documents (D413). **No live preview either** (`previewUrl`/`previewPath` do not exist on this reply).

Resolves with `{jobId, path, url, model, prompt, width, height, frames, steps, seed}` — the render that actually happened, not the one you asked for (dimensions may have shrunk, `frames` may have moved to the nearest grid value).

- **`seed` comes back whether or not you passed one.**
- **The server owns where the mp4 goes**: `<home>/ai/videos/<YYYYmmdd-HHMMSS>-<uid>.mp4`, time-ordered, outlives the tab, nothing cleans these up.
- **One row and one file per render.**

### Availability: check it before you build the button

Video generation has no "everywhere" runner — off Apple Silicon, `fused.ai.models.catalog()` reports `default: null` for `text-to-video` and every call rejects `.type "unavailable"` with the reason ("needs Apple Silicon…") in `.message`. Read `catalog()` first and hide or explain the feature rather than offering a button that always fails.

### Slow renders, and cancelling one

- **Hours, not minutes are the honest expectation** — the server allows up to **2 hours** per render (`VIDEO_TIMEOUT_S`), far past the image path's 15-minute cap, because a high-resolution render on real hardware can take that long.
- **Renders serialize**, exactly like images: one at a time per worker, and a second call waits with no queue message on its row.
- **An aged-out row still answers off the file** — the same backgrounded-tab recovery `fused.ai.image` documents, and more likely to matter here given how long a render can run.
- `fused.ai.cancel("text-to-video")` stops the render and keeps the model resident, so the next call starts warm.
- Rejects `.type` `"cancelled"` | `"ai_error"` | `"unavailable"`.

## Image to 3D: `fused.ai.mesh({image, ...})`

`fused.ai.video`'s sibling in shape — job-backed, resolves with a file — for image-to-3D shape generation, on Hunyuan3D-2.1 (`hy3dshape`, MLX). **Apple Silicon only, with no fallback on any other platform**, exactly like video: always handle `.type === "unavailable"`. Unlike `fused.ai.image`'s *optional* `image` (which switches it into edit mode), `image` here is **required** — there is no prompt-only mode for a pipeline that only ever reads a picture.

```js
const mesh = await fused.ai.mesh({
  image: "chair-photo.jpg",   // page-relative, like fused.ai.transcribe's `path`
  onProgress: (job) => { if (job.total) bar.value = job.done / job.total; },
});
viewer.load(mesh.url);   // a .glb — open it in the `glb` template, or a three.js viewer
```

### Options and the reply

| Option | Default | Range | Notes |
|---|---|---|---|
| `image` | — | non-empty | **required.** Page-relative (resolves beside the calling page, like `fused.ai.transcribe`'s `path`) or absolute; missing/empty/non-string is `bad_request` **before** a job opens |
| `model` | the `image-to-3d` row's `default` (`null` if this machine cannot serve the capability at all) | the curated Hunyuan3D-2.1 MLX weights (~7.4 GB, shape only) | |
| `steps` | `50` — the pipeline's own default | **1–100** | clamped; not a number → 400 |
| `guidance` | `5.0` — the pipeline's own default | **0–20** | clamped; not a number → 400 |
| `octreeResolution` | `256` — the pipeline's own default | **16–512** | clamped; this is also the capability's face-count ceiling — there is no separate face-limit option, higher resolution means more triangles |
| `seed` | random | **0 – 2147483647** | clamped; not a whole number → 400 |
| `onProgress(job)` | — | — | fires **once**, at the start of the render — this pipeline exposes no mid-render step hook, unlike video's per-denoising-step ticks |

Resolves with `{jobId, path, url, model, image, steps, guidance, octreeResolution, seed}` — the render that actually happened, not the one you asked for (`steps`/`guidance`/`octreeResolution` may have been clamped).

- **`seed` comes back whether or not you passed one.**
- **The server owns where the glb goes**: `<home>/ai/meshes/<YYYYmmdd-HHMMSS>-<uid>.glb`, time-ordered, outlives the tab, nothing cleans these up.
- **Shape only — no texture.** The result is an untextured mesh; there is no PBR texture stage in this build.
- **One row and one file per render.**

### Availability: check it before you build the button

Image-to-3D has no "everywhere" runner — off Apple Silicon, `fused.ai.models.catalog()` reports `default: null` for `image-to-3d` and every call rejects `.type "unavailable"` with the reason ("needs Apple Silicon…") in `.message`. Read `catalog()` first, the same rule `fused.ai.video` documents.

### Slow renders, and cancelling one

- **Minutes are the honest expectation** — the server allows up to **30 minutes** per render (`MESH_TIMEOUT_S`), narrower than video's 2-hour cap: a shape-only render is one blocking call with no internal upscaling stage.
- **Renders serialize**, exactly like video and images: one at a time per worker.
- **An aged-out row still answers off the file** — the same backgrounded-tab recovery `fused.ai.image` documents.
- `fused.ai.cancel("image-to-3d")` stops the render, but **takes effect only once the underlying call returns** — there is no mid-render checkpoint to interrupt at, unlike video's per-step cancel.
- Rejects `.type` `"cancelled"` | `"ai_error"` | `"unavailable"`.

## Transcription: `fused.ai.transcribe({path, ...})`

Speech to text, locally. Job-backed and file-producing like `fused.ai.image` — but it hands you the **words as well**, because a transcript is text and the caller almost always wants it now.

```js
const rec = await fused.ai.transcribe({
  path: "meeting.m4a",                                      // beside this page
  onProgress: (job) => bar.value = job.done / job.total,    // SECONDS OF AUDIO
});
out.textContent = rec.text;
for (const s of rec.segments) addCue(s.start, s.end, s.text);   // {start, end, text}
```

Options: `path` (required), `model`, `language`, `task`, `initialPrompt`, `vad`, `diarize`, `speakers`, `words`, `onProgress`, `onSegment`.

Resolves with `{jobId, path, output, outputText, outputPartial, model, task, url, text, segments, language, duration, speakers, estimatedSpeakers}`.

**The result is read off DISK, not returned by the job** — the part that is not obvious. The worker writes `~/.fused-render/ai/transcripts/<time>-<name>-<uid>.json` plus a `.txt` beside it; when the row reaches `done` the bridge does `readFile(output)` → `JSON.parse` and hands you the parsed fields. So:

- `output` is that JSON path, `outputText` the plain-text one, `outputPartial` the segments-as-they-decode one, and `url` a ready-made `/api/fs/raw` address for `output`.
- A transcription **outlives the tab that asked for it** — the file is the result, the row only says when to read it.
- If the transcript cannot be read (deleted, truncated), the rejection is typed `ai_error` with `err.jobId`, not a bare `SyntaxError`.

Everything else worth knowing:

- **`path` is page-relative when relative**, exactly like `readFile`/`rawUrl`; an absolute path is used verbatim. **Nothing is uploaded** — the worker opens the file itself.
- **Progress is `unit: "s"` — seconds of audio.** Not bytes (a download) and not steps (an image). `job.done` is the last decoded segment's end timestamp, `job.total` the duration, and the manager draws them as a clock (`12:00 / 1:30:00`).
- `task`: `"transcribe"` (same language) or `"translate"` (into English). Anything else is a **400 naming both**, never a silent default.
- `language` omitted means **auto-detect**. Pass one only if you know it.
- **`model` omitted loads the SMALLEST model the active engine offers**, not the turbo one — the catalog is smallest-first and `default` is its first entry. If accuracy matters, pass a `model` from `catalog()`; the turbo entries are the ones to reach for.
- `vad` (default `true`) runs a Silero speech detector and skips silence, the same filter on both transcription engines. Because it does, `job.done` legitimately finishes short of `job.total` on a recording that trails off quietly — not an off-by-one to work around. Timestamps are always positions in the original file.
- **Hours, not minutes.** One transcription runs at a time; a second call **queues**, says so on its row, and its ✕ works while it waits.
- Rejects `.type` `"cancelled"` | `"ai_error"` | `"unavailable"` | `"bad_request"` (missing path, not a file, unknown `task`, or an unusable `speakers`).

### As it decodes: `onSegment`

```js
const rec = await fused.ai.transcribe({
  path: "meeting.m4a",
  onSegment: (s) => addCue(s.start, s.end, s.text),   // fires DURING the run
  onProgress: (job) => bar.value = job.done / job.total,
});
// …and `rec.segments` is the same list, whole, when it resolves.
```

- **Every segment, in order, exactly once** — including ones decoded before your first callback, and the last ones. Append on each call and you have the transcript; never de-duplicate or re-sort.
- **Same shape as `rec.segments`** — `{start, end, text}`, plus `speaker` when diarizing and `words` when asked for and available. One rendering path for both.
- **It costs one extra request per poll, and only if you pass it** — the tail rides the tick `onProgress` was already paying for.
- **Resolution is the engine's, not the callback's.** faster-whisper emits a segment at a time; the MLX runner finishes a whole decoded window (up to 30s) and emits its segments together — so callbacks arrive in the same bursts `job.done` jumps in.
- `onSegment` is a live view, not the delivery mechanism: the file is. It stops when the promise does, and the last reads in flight are delivered *before* the rejection, so a `catch` that clears the transcript pane keeps it clear.

**The salvage path.** The worker appends each finished segment to **`outputPartial`** (`<output minus .json>.partial.jsonl`, one JSON object per line, flushed per segment). It is **gone** after a finished or cancelled run — `output` is the answer — and **left on disk after a failure**, holding every segment that decoded before it died. A 90-minute recording that fails at minute 80 writes no `.json` at all, so this is the only place those 80 minutes survive:

```js
try {
  await fused.ai.transcribe({ path: "meeting.m4a" });
} catch (err) {
  if (err.type === "ai_error" && err.outputPartial) {
    const salvaged = (await fused.readFile(err.outputPartial))
      .split("\n").filter(Boolean).map(JSON.parse);   // {start, end, text}[]
  }
}
```

`err.output` and `err.outputPartial` ride every rejection that had a run — including `cancelled` and "job is no longer being reported", where the file is already gone — so check the read, not the rejection type. Nothing cleans a salvaged file up for you.

### Who said it: `diarize` + `speakers`

```js
const rec = await fused.ai.transcribe({
  path: "meeting.m4a",
  diarize: true,
  speakers: 3,          // OPTIONAL — leave it out and the count is estimated
});
rec.speakers;                       // ["Speaker 1", "Speaker 2", "Speaker 3"]
rec.segments[0].speaker;            // "Speaker 1"  (or null — see below)
rec.estimatedSpeakers;              // 3 — only on a run that had to work it out
```

- **`speakers` is an optional hint.** A whole number 1–100 fixes the clustering to exactly that many voices; omitted (`undefined`, `null`, `""`) the count is **estimated** and `estimatedSpeakers` says what it decided. A value that is present and unusable (`0`, `2.5`, `"3"`, `true`, over 100) rejects `bad_request` **before a job opens** — that is a typo, not a request to estimate.
- **An estimate can be wrong** either way (one person across two mics can split; similar voices can merge), so pass the count when you know it. `estimatedSpeakers` is how a page shows what was assumed and offers a re-run.
- `estimatedSpeakers` counts voices the **segmenter** heard, which can exceed `speakers.length`: someone who spoke where Whisper transcribed no words is in the first and not the second.
- Every segment gains **`speaker`** and the reply gains **`speakers`** — the labels that actually landed, ready for a colour map without walking thousands of segments. **`speaker` is `null` where Whisper heard words but the segmenter heard nobody.**
- **Default `false` and additive**: a call without it is unchanged, and a transcript written without it has no `speaker` and no `speakers` at all. Both transcription engines run the same two models, so labels don't depend on which served you.
- **It does not change what progress means.** `job.done`/`job.total` stay seconds of audio; diarization is a fast pre-pass with its own line on the row ("Finding speakers…") and an indeterminate bar.
- **First use on a machine downloads ~33MB** (a speaker segmenter and a voice-embedding model), once, then works offline. Unlike the VAD these are *not* pre-fetched by a model Download.

### Word timings: `words: true`

A segment is a whole sentence or several, so `{start, end, text}` is too coarse for a karaoke highlight or a click-a-word-to-seek player. `words: true` times each word inside it.

```js
const rec = await fused.ai.transcribe({ path: "meeting.m4a", words: true });
for (const s of rec.segments) {
  for (const w of s.words || []) hi(w.start, w.end, w.word);   // {start, end, word}
}
```

- **BEST-EFFORT, and the only option here that is.** Asking never fails. An engine that has no word timings just leaves `words` off its segments — so write **`s.words || []`** and one page works on every machine, instead of asking which engine it landed on before it asks for anything. This is deliberately unlike `task`/`language`/`initialPrompt`, which are **refused** when an engine cannot do them: an ignored `task` is undetectable, whereas a missing `words` key is right there on the segment.
- **Only the MLX Whisper engine (Apple Silicon) produces them today.** CTranslate2 *could* — it is not wired — so treat "no `words`" as normal, not as an error to report.
- **How good are the timings?** Whisper infers them by dynamic time warping its cross-attention, which it was not trained to do. The [WhisperX paper](https://arxiv.org/abs/2303.00747) (Table 2, 200ms collar) scores that mechanism at 78.9 precision / 52.1 recall on AMI-IHM against 84.1/60.3 for external forced alignment — so roughly *half* the words land more than 200ms off on hard conversational audio. Fine for a highlight that follows along; do not build phonetic or clip-cutting work on it.
- **`word` keeps its leading space**, so `s.words.map(w => w.word).join("")` reconstructs `s.text`. Timings are positions in the **original file**, like a segment's, and always inside their own segment.
- **With `vad` on (the default) a word that spanned a removed pause comes back SHORTER than it was spoken.** Silence is cut out before decoding, so a word the model timed across the cut is placed on the side most of it was heard on and keeps only that part — a highlight lets go of it early rather than sitting on it for the whole pause. It never *stretches*: no word is longer than the audio it came from.
- **`task: "translate"` returns no words on any engine.** Word timings are positions in the audio and a translation's words were never spoken in it, so there is nothing to align them to. (The library only *warns* and returns numbers anyway; this app declines instead, because a karaoke UI built on them highlights the wrong word for the whole file.)
- **Not free, unlike `diarize`.** An extra pass per decoded window — measured at 0.16s → 0.22s on a 7.4s recording (+40%) — plus a **one-time ~1.3s** on the first worded transcription in a worker process while Metal compiles the extra graph. And it **changes the decode**: the library's hallucination pruning only runs with word timings on, so the *same file* can come back with a different number of segments than a call without it. Ask for it when a page needs it.
- **No per-word confidence, deliberately** — it is a number only some engines have, and the reply must not come to depend on which one ran.
- Default `false` and additive: a transcript written without it has no `words` key anywhere.
- `onSegment` segments carry `words` too, same shape — one rendering path for the live view and the final list.

### Two engines, and a format that loads nowhere

**Take the model from `catalog()`, never from memory.** Speech repos come in three mutually unloadable formats — CTranslate2 (`model.bin`), MLX Whisper (`weights.npz`) and plain transformers — and which loads depends on the engine serving this machine, not on the model being "the good one". `openai/whisper-large-v3` is the repo everyone reaches for and **no shipping runner reads it**. A fourth format, NeMo/Parakeet (`model.safetensors` beside a NeMo ASR config), is recognised but loads **nowhere at all**: no engine here claims it, so a cached repo in that shape gets no engine tag and no Load button rather than being mistaken for a chat model.

One thing about the call is therefore engine-dependent:

- **Progress resolution.** MLX Whisper reports once per decoded window (up to 30s), so `job.done` can sit still and then jump. It is always a real position in the recording.

Every ASR option `fused.ai.transcribe` takes — `task`, `language`, `initialPrompt` — is honoured by both engines today, so nothing here needs a per-engine check. Everything else — the result shape, the two files, `onSegment`, the speaker labels, `vad` — is identical whichever one served you.

## Embeddings: `fused.ai.embed({texts, ...})`

Text into a vector, locally — and on a model with a vision tower, images into
the **same** vector space, so a typed phrase can rank photographs. Not
job-backed and not streaming: a batch of at most **64** items is one forward pass
through a small encoder, over before a progress row would have drawn, so the
reply IS the result.

```js
const { vectors, dim, model } = await fused.ai.embed({ texts: ["a cat", "a dog"] });
```

Vectors come back **unit-length**, so a cosine similarity between two of them is
a plain dot product — `a[i]*b[i]` summed, with no magnitude to divide by. That
is the one guarantee to build on; everything else about a vector is the model's.

### Options, and the two that are refused PER MODEL

| Option | Meaning |
|---|---|
| `texts` | Up to 64 non-empty strings. Exactly one of `texts`/`paths`, never both — refused in the bridge, before the request is even sent. |
| `paths` | Up to 64 image paths, absolute or relative to the calling page. **Only on a model with a vision tower.** |
| `kind` | `"query"` or `"document"` — which half of a retrieval model's prompt pair to put in front of these texts. **Only on a model with a retrieval convention.** Omitted means `"document"`. |
| `model` | A repo id. Omitted takes the capability's default (`catalog()`'s `default`). |

**The capability serves two shapes of checkpoint, and that is why two of the four
options are per-model rather than per-endpoint:**

- a **dual encoder** (SigLIP, CLIP) has a text tower and a vision tower
  projecting into one space. It answers `texts` and `paths`, and has **no**
  retrieval convention — so `kind` on one is a 400.
- a **prose encoder** (BERT-family, 512–8192 tokens) has one tower. It answers
  `texts` and `kind`, and `paths` on one is a 400. This is the half that makes
  RAG, document search and clustering possible at all: a SigLIP text tower
  truncates at **64 tokens**, so no chunk size turns it into a paragraph encoder.

**Ask before you send.** `fused.ai.models.catalog()` reports both facts per
model — `acceptsPaths` (a boolean; **absent on an older server, so test `===
true`**) and `promptScheme` (the scheme's name, or **`null` where the model has
none**). Draw an image affordance off the first and a query/document toggle off
the second; a control drawn off anything else — the repo id looking like a
SigLIP, the capability being embeddings — is a control whose request comes back
400.

### Why `kind` is not optional politeness

A retrieval encoder was trained with a question and a passage marked
differently in its input, and using one side for both **costs real recall — and
costs it silently**. The vectors still come back, still unit length, still
comparable, just worse; nothing downstream can detect it. So:

```js
// Index once, as documents.
const corpus = await fused.ai.embed({ texts: chunks, kind: "document" });
// Then every search, as a query.
const q = await fused.ai.embed({ texts: [question], kind: "query" });
```

Omitting it means `"document"`, which is the internally-consistent fallback:
every text in the system carries one prefix, which is the symmetric behaviour
every one of these models supports. It is not optimal, and it degrades
gracefully rather than to the mismatched state that using the *wrong* side
produces.

An unrecognised value — `kind: "queries"` — is a **400, never defaulted
through**, for the same reason: a typo that fell back to the default would
return plausible vectors computed against the wrong prefix.

### Rejections

Same `{ok, error:{type, message}}` shape `fused.ai` itself uses, and the same
`.type` on the thrown `Error`:

| `.type` | When | What a page should do |
|---|---|---|
| `bad_request` | Both `texts` and `paths`; an empty list; over 64 items; a non-string item; `kind` on a model with no scheme; `paths` on a model with no vision tower; a `kind` value outside the pair | Fix the call. These name the problem — and the per-model two name the MODEL, because the fix is to pick a different one. |
| `model_loading` | The model is not resident yet. Carries `jobId` — the load this call just started | `fused.watchJob(err.jobId)`, then retry. The same cold-start dance `fused.ai()` has. |
| `unavailable` | Nothing here serves embeddings, or no curated default exists | Show `err.message`; it names the reason (e.g. an engine that is not built yet). |
| `ai_error` | The worker failed | Surface the message. |

### Two engines, one vector space

MLX Embeddings on Apple Silicon, ONNX Embeddings everywhere else (plus three
opt-in accelerated ONNX builds — DirectML, CUDA, ROCm — gated on the hardware
being there). Unusually for this app, **the two engines produce vectors in the
same space**: cosine similarities agree to about three decimals on the same
model, so a page that indexed a folder on one engine and searches it from the
other gets sensible answers.

**Their catalogs are still different, and that is about the FILES.** MLX reads
safetensors and ONNX reads a graph export, so the two engines' curated lists name
different repos — sometimes for the same checkpoint
(`google/siglip2-base-patch16-384` against
`onnx-community/siglip2-base-patch16-384-ONNX`), and sometimes for a different
one, because the two formats do not always exist for the same model: each
engine's default is a nomic prose encoder, but MLX's is
`mlx-community/nomicai-modernbert-embed-base-bf16` (8192 tokens) and ONNX's is
`nomic-ai/nomic-embed-text-v1.5` (2048). So **take the model from `catalog()`,
never from memory** — the same rule the speech section states, and here it is
also why: an id that works on one machine may name a format the other's engine
cannot open, and switching engines can change which model a bare call loads.

## What Actually Runs Locally Today

Fifteen runners, five capabilities, taking **either** a Hugging Face repo id **or** — for `llamacpp-text` and its Vulkan variant — a GGUF filename id; see the Overview for why the shape is not uniform:

| Capability | Runners (default first) | Reality |
|---|---|---|
| `text-generation` | MLX, then llama.cpp (CPU), then llama.cpp (Vulkan) | **Everywhere.** MLX on Apple Silicon; **llama.cpp (CPU)** everywhere else, and as the Apple Silicon fallback — the same index's wheel links Metal, so it is on the GPU there too. **Local text ids are GGUF**: the curated ones are the GGUF's own filename, and any other GGUF repo resolves generically once picked from Hub search or loaded by its bare repo id. A plain safetensors repo is loadable only by MLX, i.e. only on Apple Silicon. Vulkan is the one opt-in row: it needs a working loader AND driver ICD from the GPU vendor or the Load button refuses with a reason naming which is missing; once loaded, a model too large for the card degrades to partial or full CPU offload rather than failing the load. |
| `text-to-image` | MLX FLUX, then Diffusers (CPU), then Diffusers (CUDA), then Diffusers (ROCm) | **Everywhere.** MLX FLUX takes Apple Silicon (quicker, smaller download, much more memory); Diffusers (CPU) serves everywhere else and is one Engines-tab switch away on a Mac — minutes per image rather than seconds. The CUDA and ROCm variants are opt-in and hardware-gated: offered only where the app sees a usable NVIDIA or AMD GPU, greyed out with the reason otherwise. |
| `automatic-speech-recognition` | MLX Whisper, then Faster Whisper (CTranslate2) | **Everywhere.** MLX Whisper (GPU) is the Apple Silicon default; CTranslate2 serves both Mac architectures, Linux and Windows and is one Engines-tab switch away on a Mac. There is deliberately **no** GPU variant here off Apple Silicon, so transcription on an NVIDIA or AMD machine runs on the CPU. |
| `embeddings` | MLX Embeddings, then ONNX Embeddings (CPU), then ONNX (DirectML), (CUDA), (ROCm) | **Everywhere.** MLX takes Apple Silicon; **ONNX Embeddings (CPU)** serves everywhere else and is the Apple Silicon fallback, so **local embedding ids are ONNX exports** on every machine but a Mac (a parity gate asserts ≥0.999 cosine against the torch weights). The three accelerated rows — DirectML, CUDA, ROCm — are opt-in and hardware-gated; none of them is about speed, and `auto` never reaches one. |
| `text-to-video` | LTX-2.3 (Apple Silicon) | **NOT everywhere — the first capability with no fallback row.** LTX-2.3 runs on `ltx-2-mlx`, which is MLX-only; there is no CPU, CUDA or ROCm engine for it. Off Apple Silicon, `catalog()` reports `default: null` and every call to `fused.ai.video` rejects `.type "unavailable"`. |
| `image-to-3d` | Hunyuan3D-2.1 (Apple Silicon) | **NOT everywhere — the second capability with no fallback row.** Runs on `hy3dshape`, MLX-only; no CPU, CUDA or ROCm engine. Off Apple Silicon, `catalog()` reports `default: null` and every call to `fused.ai.mesh` rejects `.type "unavailable"`. Shape only — no PBR texture stage. |

Those five strings are the capability vocabulary — what `unload({capability})` and `cancel(capability)` take, and what `catalog()` groups by.

**Which runner serves you is not purely a hardware fact.** The user can override the default per capability from the AI Models page's Engines tab, so a Mac may deliberately be running the CTranslate2 path. Each row in `fused.ai.models.list()`'s `runners` therefore carries **both** `available` (can this backend run here at all) and `active` (is it serving the capability right now). Read `active` to say what is running, `available` to say what this machine could do. Never hard-code either, and let `unavailable` messages reach the user.

**And a switch EVICTS.** A model resident under the outgoing engine is unloaded as the preference is written — it belongs to the backend that loaded it. A page holding it gets `model_loading` on its next `fused.ai()` call (the cold-start path it already handles); the artefact calls reload inside their own job and just take longer.

## Calling from Python: `fused_ai`

Everything above is also reachable **without a browser**. `fused_render/templates/shared/fused_ai.py` is a stdlib-only Python client mirroring `fused.ai` 1:1 — same names, same option names, same closed-envelope rejections (D413) — so a `.py` data file or an external process never has to reinvent the model layer (SPEC PY-19, D470-D472).

### `import fused_ai` — no install, no path setup

A user `.py` running under the server just imports it:

```python
import fused_ai

def main(path: str = "meeting.m4a"):
    rec = fused_ai.transcribe(path=path)   # blocks until the transcript is ready
    return rec["text"]
```

That's the whole thing. Both execution engines append `templates/shared` onto the module's own `sys.path` before it runs, so `import fused_ai` resolves the way `import pandas` does — no `sys.path.insert`, no `pip install fused-render`, nothing to configure.

### The surface

Same names, same options as `fused.ai` above:

| Python | JS equivalent |
|---|---|
| `fused_ai.text(prompt, model=, effort=, system_prompt=)` → `str` | `fused.ai(prompt, opts)` |
| `fused_ai.stream(prompt, model=, effort=, system_prompt=)` → generator of `str` | `fused.ai(prompt, {onChunk})` |
| `fused_ai.transcribe(path=, model=, language=, task=, initial_prompt=, vad=, diarize=, speakers=, words=, ...)` | `fused.ai.transcribe({...})` |
| `fused_ai.image(prompt=, model=, width=, height=, steps=, guidance=, seed=, image=, ...)` | `fused.ai.image({...})` |
| `fused_ai.embed(texts=, paths=, model=, kind=)` | same `/api/ai/embed` endpoint — one forward pass, not job-backed; see **Embeddings** below for the two per-model refusals |
| `fused_ai.models.list()` / `.catalog()` / `.load(id, capability=)` / `.download(id)` / `.unload(id)` | `fused.ai.models.*` |
| `fused_ai.cancel(capability)` | `fused.ai.cancel(capability)` |

`from fused_ai import ai` gives the same functions as `ai.text(...)`, `ai.models.load(...)`, etc., if you prefer that spelling — one module, two ways to reach it.

### Job-backed calls block by default

`transcribe`, `image`, and `models.load`/`models.download` start a job and hand back a job id over HTTP — identical to the JS bridge. But a Python caller has a thread to spend, where a page has a promise to keep, so the wrapper does the waiting for you: **`await` becomes `return`.**

```python
rec = fused_ai.transcribe(path="meeting.m4a")   # does not return until the job is DONE
```

`fused_ai.transcribe()`'s settled reply carries the transcript's **contents**, not just its paths — same as `fused.ai.transcribe`'s own resolve above: `{**reply, "text", "segments", "language", "duration", "speakers", "estimatedSpeakers"}`, read off `rec["output"]` for you (`rec["output"]`/`rec["outputText"]` still ride along too, if you want the raw files). `fused_ai.image()` is different, and correctly so: `fused.ai.image` resolves with `{path, url, previewUrl, seed, ...}` and no pixel data, so `img["path"]` is genuinely the whole answer there — nothing is being left for you to read that the JS bridge already hands over.

For anything else, three keyword arguments:

- **`wait=False`** returns the immediate reply (`{"jobId", "path", ...}`) instead of blocking, for a caller that wants to drive its own loop.
- **`on_progress=`** — a callable that receives each polled job row (the same shape `GET /api/jobs` returns), so a long-running caller can print/log ticks instead of sitting silent.
- **`timeout=`** bounds the wait; past it, `AiError(type="timeout")`.

### Two exceptions, for two different situations

```python
try:
    text = fused_ai.text("summarise this")
except fused_ai.ServerNotRunning:
    print("no fused-render server is reachable from here")
except fused_ai.AiError as e:
    print(f"{e.type}: {e.message}")
```

- **`ServerNotRunning`** — there is nothing to call at all. Different remedy from a failed call: start the app, or give up, not retry.
- **`AiError`** — the app is there and the call failed. Carries `.type`/`.message`/`.status` off the same `{ok, error:{type, message}}` shape (or the plainer `{"error": "..."}` a job-backed endpoint's own validation returns) the rejections table above already documents — `model_loading`, `ai_unavailable`, `bad_request`, `ai_error`, `timeout`, `unavailable`, `cancelled` all show up here as `.type` too, plus `stalled` (the job stopped reporting progress) which the JS bridge has no equivalent for.

### From outside a running server: the `server.json` bootstrap

A process the server did not spawn — a menubar app, a notebook, a standalone script — has no `sys.path` seeded for it and no `FUSED_RENDER_ORIGIN` in its environment. The server publishes both facts it needs to `~/.fused-render/server.json` at startup (branch-nested like everything else under the shell home dir):

```python
import json, os, sys
with open(os.path.expanduser("~/.fused-render/server.json")) as f:
    sys.path.insert(0, json.load(f)["shared"])
import fused_ai
```

`fused_ai.resolve_origin()` (called internally by every function above) already does this lookup for you *after* checking `FUSED_RENDER_ORIGIN` — the snippet above is only for the one thing it cannot do itself: putting `fused_ai.py` on `sys.path` in the first place. If nothing is running, `resolve_origin()` raises `ServerNotRunning` rather than guessing a port.

### When NOT to use it

**If the page wants streaming tokens or live progress in its UI, call `fused.ai` in JavaScript — not `fused_ai` through `runPython()`.** A `runPython()` round trip is one request and one response; there is no channel back into the page while Python is still running, so a blocking `fused_ai.transcribe()` call behind `runPython()` cannot feed a progress bar or a token-by-token stream no matter what `on_progress=` you pass it — that callback runs INSIDE the same `runPython()` subprocess, with nothing connecting it to the browser until the whole call returns; a `print()` inside it lands in that subprocess's captured stdout, not on the page, and the page sees one lump reply when the call ends. A page that wants to *watch* a transcription arrive, or show text appearing as the model writes it, has to call `fused.ai`/`fused.ai.transcribe` directly and let the browser hold the connection open.

Reach for `fused_ai` when the AI call is incidental to work that is already in Python — a batch script, a scheduled job, a `main()` that happens to want a summary alongside a DataFrame — not as a Python-flavored way to build a chat UI.

### Export

An exported page has no server behind it, so this is moot rather than dangerous: no `/api/ai`, no job registry, nothing for `fused_ai` to call — and `export.py` does not copy `templates/shared/` into the exported bundle in the first place, so `import fused_ai` fails before it could even try.

## Surviving Export

The exporter **rejects any page containing the string `fused.ai(`** (SPEC RH-11) — matched textually, so `if (fused.env === "local")` does **not** make a page exportable, and aliasing the call to dodge the match only trades a clear refusal for a page that ships broken. An exported page has no CLI and no worker; the call could only fail at the reader.

If a view must export, **keep AI out of it** and gate the feature at the page level — a local-only companion view, or a UI that hides the AI panel when `fused.env !== "local"` with no `fused.ai(` in the file at all. `fused.trackJob` exports fine (it no-ops hosted); `fused.ai` never will.

**The DOTTED calls are a trap in the opposite direction.** The check matches `fused.ai(` specifically, so `fused.ai.image(`, `fused.ai.transcribe(` and `fused.ai.models.*` slip past it — and then fail at the reader, since a hosted page has no worker, no `<home>/ai/` directories and no `/api/fs/raw`, making every `url`, `previewUrl` and `output` a dead address. Gate them on `fused.env === "local"` yourself; nothing will stop you at export time. (Recorded as an open question in `docs/EXPORT.md`.)

**Same story for the Python client.** `export.py` does not copy `templates/shared/` into an exported bundle at all, so `import fused_ai` fails outright rather than resolving to a dead server — moot rather than dangerous, but worth knowing before you assume a `.py` data file survives export unchanged.

## Debugging

Check the destination before blaming the page:

```bash
which claude && claude --version         # Claude path installed? (or $FUSED_RENDER_CLAUDE_BIN)
curl -s -X POST http://127.0.0.1:1777/api/ai -H 'X-Fused: 1' \
  -H 'Content-Type: application/json' \
  -d '{"prompt": "Reply with exactly the word pong.", "effort": "low"}'
curl -s http://127.0.0.1:1777/api/ai/runtime      # what is resident locally
```

First failing = `ai_unavailable`, not your bug. `X-Fused: 1` is required on every mutating POST (D36) — omit it and you get a rejection that looks like a broken endpoint.

## Common Mistakes

**Text**

- **Treating `model_loading` as a failure** → it started a download and gave you `err.jobId`; show it and retry.
- **Reading `usage.prompt_tokens`** → `undefined`. Anthropic names only.
- **Sending `temperature`/`history`/`raw` to the Claude path** → 400, by design. Set them only for a slashed model.
- **Not disabling the submit button** → no stale-cancel; a double-click fires two calls.
- **Dumping a full dataset into a prompt** → token blowout, worse answer.
- **Expecting 120 s** → the relay allows **600 s**; keep loading states honest.

**Models**

- **Awaiting `models.load()` as if it returned a model** → it returns `{jobId}`.
- **Omitting `{capability}` on `load`/`download` for a repo that is not a chat model** → an uncached whisper or diffusion repo falls back to the text runner and fails inside mlx-lm. Name it.
- **`unload(selectedId)`** → often unloads nothing; use `{capability}`.
- **Rendering only `catalog()`'s curated entries** → the model the user just downloaded is missing from your picker. Render every entry; mark them.
- **Assuming a capability's runner from the platform** → every capability has more than one and a user preference can pick any. Read `active` from `fused.ai.models.list()`.
- **Hard-coding a repo id, or carrying one between engines** → formats are backend-specific; a repo that works on one engine is an unusable download on the other.
- **Assuming a model id is always `org/name`** → `llamacpp-text`'s curated ids are the GGUF's own filename (`Qwen3.5-4B-Q4_K_M.gguf`, no slash). Splitting on `/` or building a Hub URL from `id` breaks on one; treat it as opaque.

**Images**

- **Waiting for `model_loading` from `fused.ai.image`** → it never comes; the load happens inside the render's own job with `done`/`total` null, and the bytes are on the model's own row. Dividing by a null total shows `NaN` for the whole download.
- **Firing two renders and waiting** → they serialize and the second row says nothing about queueing. Disable the button.
- **Echoing your request as the image's caption** → sides snap to a multiple of 16 and everything is clamped. Read the reply.
- **Letting the user paste any Hub diffusion repo on a Mac** → MLX FLUX loads exactly one. Offer `catalog()`'s entries, or name the Engines tab.
- **Adding your own cache-buster to `previewUrl`** → it already has one keyed on the step.
- **Passing `image`/`strength` expecting image-to-image or inpainting** → there is no such option (see "That is the whole surface" above); the call is now refused `bad_request` naming the option, instead of quietly rendering text-to-image from the prompt alone and leaving the base image ignored.

**Transcription**

- **Expecting the words from the job** → the row only says when; the text is read off `output`. For words *during* the run, pass `onSegment`.
- **Reading progress as bytes or steps** → it is `unit: "s"`, seconds of audio.
- **`fused.ai.cancel()` to stop a transcription** → it defaults to `"text-generation"`; name the capability or use the row's ✕.
- **Loading `openai/whisper-large-v3`** → transformers format, which no shipping runner reads. Take the id from `catalog()`.
- **Loading a plain safetensors text model such as `Qwen/Qwen3.5-9B`** → only MLX reads safetensors, so it loads on Apple Silicon and nowhere else. Off a Mac take the GGUF id from `catalog()` — the same model, a quarter of the download.

**Export**

- **Gating `fused.ai` on `fused.env` and expecting export to pass** → the match is textual.
- **Assuming `fused.ai.image`/`.transcribe` are safe to export because they pass the check** → they do, and then fail at the reader. Gate them yourself.

**Python**

- **Calling `fused_ai` from a page that wants live progress or streamed tokens on screen** → a `runPython()` round trip has no channel back until it returns; use `fused.ai` in JavaScript instead. See "When NOT to use it" above.
- **Expecting `fused_ai.image()` to hand back pixel data** → it returns the same paths `fused.ai.image` does (`img["path"]`); there is no bytes-returning form on either surface. `fused_ai.transcribe()` is the opposite case — it already reads the transcript back for you (`rec["text"]`, `rec["segments"]`), so there is no file to open yourself there.
- **Catching only `AiError`** → a call made when nothing is running raises `ServerNotRunning`, not `AiError` — catch both, or catch neither and let it surface.
- **Assuming a user `.py` needs `sys.path.insert` or `pip install` to reach `fused_ai`** → both engines already seed `templates/shared` onto its `sys.path`; a bare `import fused_ai` just works.
