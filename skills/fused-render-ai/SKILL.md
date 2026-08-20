---
name: fused-render-ai
description: Use when a fused-render page needs an AI model — calling fused.ai for text, streaming tokens, holding a conversation, generating an image with fused.ai.image, transcribing audio or video with fused.ai.transcribe, or driving local models with fused.ai.models (list/catalog/load/download/unload) and fused.ai.cancel. Also use when an AI call rejects with ai_unavailable, model_loading, unavailable, cancelled, or timeout, when a model download needs watching, or when a page that calls AI must survive export.
---

# AI in a fused-render Page

## Overview

`fused.ai` is one call with **two destinations**, and the model id alone decides which:

| `opts.model` | Where it runs | Credential |
|---|---|---|
| Contains a `/` — a Hugging Face repo id | **This machine.** A resident worker process holding the weights. | none |
| Anything else — `"sonnet"`, `"claude-haiku-4-5-20251001"`, omitted | The local **`claude` (Claude Code) CLI**. | the user's Claude Code login |

That one rule (`"/" in model`) is the whole seam: a page swapping `model: "opus"` for a repo id changes nothing else — same call, same resolved shape.

**Never hard-code a repo id, and treat the ones in this file as illustrations.** A repo belongs to a *backend*, not to a capability: an MLX-packed repo is an unusable download on Windows, Linux, or a Mac switched to Transformers. Always take ids from `fused.ai.models.catalog()`, which answers for the engine actually serving this machine.

Both destinations are **local-only** — there is no hosted path — so an exported page can call neither. See "Surviving Export".

## When to Use

- A page asks a model something: text, chat, streaming.
- A page generates an image (`fused.ai.image`) or transcribes a recording (`fused.ai.transcribe`) locally.
- A page manages what this machine is holding in memory (`fused.ai.models.*`).
- An AI call rejects and you need to know whose fault it is.

For `runPython`, params, or file IO → **`fused-render-authoring`**. For opening/running the app → **`fused-render-usage`**.

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
| `downloaded` | On this disk. Always `true` for `"cached"`. |
| `loaded` | A worker is holding the weights **right now**. |

- **Render the whole of `models[]`.** Filtering to `source === "curated"` hides the model the user deliberately downloaded — the exact bug these flags exist to end. Mark states instead: `loaded` → ready now, `downloaded` → instant load, neither → a `size_gb` download first. (The app's own AI Models page filters, because its Local tab already lists the full cache; your page has no such tab.)
- **Every entry is one the engine serving that capability can actually load** — a cached repo in a format that backend does not read is left out, so the list moves when the user switches engines.
- **Lists are ordered smallest download first, `default` is the first CURATED entry.** So omitting `model` gets the *smallest* model, not the best one. **Read `default`, never `models[0]`**: cached entries are appended after the curated ones, and an engine with no shortlist reports `default: null` — "no recommended model" is an answer to respect.
- `note` is `null` on a cached entry and `size_gb` is its measured on-disk footprint. Render `label || id`.

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
| `onProgress(job)` | — | — | per denoising step |

**That is the whole surface**: no negative prompt, no image-to-image or inpainting, no batch count, no scheduler or LoRA. One prompt in, one PNG out; two pictures means two calls.

Resolves with `{jobId, path, url, previewUrl, previewPath, model, prompt, width, height, steps, guidance, seed}` — the render that will actually happen, not the one you asked for.

- **`seed` comes back whether or not you passed one**, so "make that one again" is always one call away.
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

Options: `path` (required), `model`, `language`, `task`, `initialPrompt`, `vad`, `diarize`, `speakers`, `onProgress`, `onSegment`.

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
- `vad` (default `true`) runs a Silero speech detector and skips silence, the same filter on all three transcription engines. Because it does, `job.done` legitimately finishes short of `job.total` on a recording that trails off quietly — not an off-by-one to work around. Timestamps are always positions in the original file.
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
- **Same shape as `rec.segments`** — `{start, end, text}`, plus `speaker` when diarizing. One rendering path for both.
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
- **Default `false` and additive**: a call without it is unchanged, and a transcript written without it has no `speaker` and no `speakers` at all. All three transcription engines run the same two models, so labels don't depend on which served you.
- **It does not change what progress means.** `job.done`/`job.total` stay seconds of audio; diarization is a fast pre-pass with its own line on the row ("Finding speakers…") and an indeterminate bar.
- **First use on a machine downloads ~33MB** (a speaker segmenter and a voice-embedding model), once, then works offline. Unlike the VAD these are *not* pre-fetched by a model Download.

### Three engines, one of them not Whisper

**Take the model from `catalog()`, never from memory.** Speech repos come in four mutually unloadable formats — CTranslate2 (`model.bin`), MLX Whisper (`weights.npz`), NeMo/Parakeet (`model.safetensors` beside a NeMo ASR config) and plain transformers — and which loads depends on the engine serving this machine, not on the model being "the good one". `openai/whisper-large-v3` is the repo everyone reaches for and **no shipping runner reads it**.

Two things about the call are therefore engine-dependent:

- **Progress resolution.** MLX Whisper reports once per decoded window (up to 30s), Parakeet once per 60s chunk, so `job.done` can sit still and then jump. It is always a real position in the recording.
- **Parakeet refuses three options rather than ignoring them**: `task: "translate"`, `language` (it detects among its 25 European languages and cannot be pinned) and `initialPrompt`. Each rejects `bad_request` **before a job opens**, naming the engine. If your page needs any of the three, check `active` in `fused.ai.models.list()` and say so in the UI — the user chose that engine, your page did not.

Everything else — the result shape, the two files, `onSegment`, the speaker labels, `vad` — is identical whichever one served you.

## What Actually Runs Locally Today

Eleven runners, three capabilities, all taking **Hugging Face repo ids**:

| Capability | Runners (default first) | Reality |
|---|---|---|
| `text-generation` | MLX, then Transformers (CPU), then Transformers (CUDA), then Transformers (ROCm) | **Everywhere.** MLX on Apple Silicon; the **CPU** torch build everywhere else, and as the Apple Silicon fallback — it answers slowly but it answers. The CUDA and ROCm builds are the same runner on a different wheel: opt-in from the Engines tab, and offered only where the app can see a usable NVIDIA or AMD GPU. |
| `text-to-image` | MLX FLUX, then Diffusers (CPU), then Diffusers (CUDA), then Diffusers (ROCm) | **Everywhere.** MLX FLUX takes Apple Silicon (quicker, smaller download, much more memory); Diffusers (CPU) serves everywhere else and is one Engines-tab switch away on a Mac — minutes per image rather than seconds. The CUDA and ROCm variants are the same opt-in, hardware-gated arrangement as text generation. |
| `automatic-speech-recognition` | MLX Whisper, then Parakeet TDT, then Faster Whisper (CTranslate2) | **Everywhere.** MLX Whisper (GPU) is the Apple Silicon default; Parakeet TDT is an Apple-Silicon opt-in — quicker and more accurate in English, but 25 European languages only; CTranslate2 serves both Mac architectures, Linux and Windows. There is deliberately **no** GPU variant here off Apple Silicon, so transcription on an NVIDIA or AMD machine runs on the CPU. |

Those three strings are the capability vocabulary — what `unload({capability})` and `cancel(capability)` take, and what `catalog()` groups by.

**Which runner serves you is not purely a hardware fact.** The user can override the default per capability from the AI Models page's Engines tab, so a Mac may deliberately be running the CTranslate2 path. Each row in `fused.ai.models.list()`'s `runners` therefore carries **both** `available` (can this backend run here at all) and `active` (is it serving the capability right now). Read `active` to say what is running, `available` to say what this machine could do. Never hard-code either, and let `unavailable` messages reach the user.

**And a switch EVICTS.** A model resident under the outgoing engine is unloaded as the preference is written — it belongs to the backend that loaded it. A page holding it gets `model_loading` on its next `fused.ai()` call (the cold-start path it already handles); the artefact calls reload inside their own job and just take longer.

## Surviving Export

The exporter **rejects any page containing the string `fused.ai(`** (SPEC RH-11) — matched textually, so `if (fused.env === "local")` does **not** make a page exportable, and aliasing the call to dodge the match only trades a clear refusal for a page that ships broken. An exported page has no CLI and no worker; the call could only fail at the reader.

If a view must export, **keep AI out of it** and gate the feature at the page level — a local-only companion view, or a UI that hides the AI panel when `fused.env !== "local"` with no `fused.ai(` in the file at all. `fused.trackJob` exports fine (it no-ops hosted); `fused.ai` never will.

**The DOTTED calls are a trap in the opposite direction.** The check matches `fused.ai(` specifically, so `fused.ai.image(`, `fused.ai.transcribe(` and `fused.ai.models.*` slip past it — and then fail at the reader, since a hosted page has no worker, no `<home>/ai/` directories and no `/api/fs/raw`, making every `url`, `previewUrl` and `output` a dead address. Gate them on `fused.env === "local"` yourself; nothing will stop you at export time. (Recorded as an open question in `docs/EXPORT.md`.)

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

**Images**

- **Waiting for `model_loading` from `fused.ai.image`** → it never comes; the load happens inside the render's own job with `done`/`total` null, and the bytes are on the model's own row. Dividing by a null total shows `NaN` for the whole download.
- **Firing two renders and waiting** → they serialize and the second row says nothing about queueing. Disable the button.
- **Echoing your request as the image's caption** → sides snap to a multiple of 16 and everything is clamped. Read the reply.
- **Letting the user paste any Hub diffusion repo on a Mac** → MLX FLUX loads exactly one. Offer `catalog()`'s entries, or name the Engines tab.
- **Adding your own cache-buster to `previewUrl`** → it already has one keyed on the step.

**Transcription**

- **Expecting the words from the job** → the row only says when; the text is read off `output`. For words *during* the run, pass `onSegment`.
- **Reading progress as bytes or steps** → it is `unit: "s"`, seconds of audio.
- **`fused.ai.cancel()` to stop a transcription** → it defaults to `"text-generation"`; name the capability or use the row's ✕.
- **Loading `openai/whisper-large-v3`** → transformers format, which no shipping runner reads. Take the id from `catalog()`.

**Export**

- **Gating `fused.ai` on `fused.env` and expecting export to pass** → the match is textual.
- **Assuming `fused.ai.image`/`.transcribe` are safe to export because they pass the check** → they do, and then fail at the reader. Gate them yourself.
