---
name: fused-render-ai
description: Use when a fused-render page needs an AI model — calling fused.ai for text, streaming tokens, holding a conversation, generating an image with fused.ai.image, transcribing audio or video with fused.ai.transcribe, or driving local models with fused.ai.models (list/catalog/load/download/unload) and fused.ai.cancel. Also use when an AI call rejects with ai_unavailable, model_loading, unavailable, cancelled, or timeout, when a model download needs watching, or when a page that calls AI must survive export.
---

# AI in a fused-render Page

## Overview

`fused.ai` is one call with **two destinations**, and the model id alone decides which:

| `opts.model` | Where it runs | Credential |
|---|---|---|
| Contains a `/` — `"mlx-community/Qwen3-8B-4bit"` | **This machine.** A resident worker process holding the weights. | none |
| Anything else — `"sonnet"`, `"claude-haiku-4-5-20251001"`, omitted | The local **`claude` (Claude Code) CLI**. | the user's Claude Code login |

That one rule (`"/" in model`) is the whole seam. A page swapping `model: "opus"` for `model: "mlx-community/Qwen3-8B-4bit"` changes nothing else — same call, same resolved shape.

**The slashed ids in this file illustrate the rule; they are not ids to copy.** `mlx-community/Qwen3-8B-4bit` is MLX-packed, so it is an unusable download on Windows or Linux — and on a Mac switched to Transformers from Preferences, where the suggestions are unquantized safetensors (`Qwen/Qwen3-8B` and friends). A repo belongs to a backend, not to a capability, for every capability here. Take the id from `fused.ai.models.catalog()`.

Both destinations are **local-only**: there is no hosted path. An exported page has neither a CLI nor a worker, so the exporter **rejects any page containing the string `fused.ai(`** (SPEC RH-11) — matched textually, so gating at runtime is not enough (see "Surviving export").

## When to Use

- A page asks a model something: text, chat, streaming.
- A page generates an image locally (`fused.ai.image`).
- A page turns a recording into text locally (`fused.ai.transcribe`).
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
- `usage` — `null`, or exactly `{input_tokens, output_tokens}`. **Anthropic names.** There is no `prompt_tokens`/`completion_tokens`; reading those gives `undefined`.

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

Every rejection is an `Error` with `.type`:

| `.type` | Cause | UI response |
|---|---|---|
| `model_loading` | **Local model not resident.** The call already **started the load** and `err.jobId` is it. | Not a failure — show the download (see below). |
| `ai_unavailable` | `claude` binary not found (Claude path), or the worker won't start (local). | Friendly unavailable state, not a raw overlay. |
| `bad_request` | Empty prompt, bad option, or a local-only option on the Claude path. | Your page's bug — read the message. |
| `ai_error` | Ran but errored (bad model id, upstream failure). | Show `err.message`. |
| `timeout` | No answer within **600 s** server-side. | Offer retry. |
| `unavailable` | 409 — a fact about **this machine**, not the request ("needs Apple Silicon", "the runner is not built yet"). From `fused.ai.models.*`, `fused.ai.image` and `fused.ai.transcribe`. | Show the reason; it explains itself. |
| `cancelled` | The row's ✕ stopped it. From the artefact calls (`fused.ai.image`, `fused.ai.transcribe`). | Not a failure — the user asked. |

The last two only reach you from the calls named; `fused.ai()` itself never produces them. `err.jobId` is set on every rejection that had a row.

**`model_loading` is the one that surprises people.** The first call naming a local model does not fail-and-forget; it returns 409 *having kicked off a multi-GB download*, and hands you the job id so the page can draw it:

```js
try {
  const res = await fused.ai(question, { model: "mlx-community/Qwen3-8B-4bit" });
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
| `fused.ai.models.catalog()` | Suggested models per capability, with what this machine can run. |
| `fused.ai.models.load(id, {capability}?)` | `{jobId}` — **not a loaded model.** |
| `fused.ai.models.download(id, {capability}?)` | `{jobId}` — weights only, no load. |
| `fused.ai.models.unload(idOrCapability)` | `{stopped, ...}` |
| `fused.ai.cancel(capability?)` | `boolean` — stops generation, **keeps the weights**. |

**Every list `catalog()` returns is ordered smallest download first, and each capability's `default` is simply that list's first entry** — so omitting `model` on an image or transcription call gets the *smallest* model, not the best one. Read the list and pick deliberately whenever quality matters.

`load`/`download` hand back a **job, not a result**: a cold load is multi-GB and nothing waits on it. Watch with `fused.watchJob(jobId)`.

**Pass `{capability}` to `load`/`download` whenever you know it, and you almost always do.** `capability` says which runner gets the repo — one of the three strings below. Left out, the server *infers* it: from the repo's cached files if they are on disk, then from `catalog()`'s curation, and only then falls back to `"text-generation"` for an id it has never seen. That fallback is the trap this argument avoids — a whisper or diffusion repo that is not downloaded yet has nothing to read, so an omitted capability sends it to the text runner, and what comes back is the *library's* complaint about a missing `config.json` or missing safetensors, which reads as a corrupt model rather than as a wrong-runner dispatch:

```js
fused.ai.models.load("mlx-community/FLUX.2-Klein-4B-4bit", { capability: "text-to-image" });
fused.ai.models.load("mlx-community/whisper-large-v3-turbo", { capability: "automatic-speech-recognition" });
```

A repo that *is* cached and that no engine here reads is refused with a sentence naming the repo, what it looks like, and what to pass — never a library traceback.

**Unload by capability, not by id.** A page's Unload button means "release whatever is resident", and the page does **not** reliably know what that is — another page or the AI Models tab may have loaded something else. Passing your dropdown's id unloads nothing and leaves the real model in memory:

```js
fused.ai.models.unload({ capability: "text-generation" });   // honest
fused.ai.models.unload(selectedId);                          // often a no-op
```

**Both `unload({capability})` and `cancel(capability)` take one of three strings** — `"text-generation"`, `"text-to-image"`, `"automatic-speech-recognition"` — and one model is resident per capability, which is what lets a chat model and a Whisper model be loaded at once without evicting each other. An unrecognised capability is a 400, not a no-op.

`fused.ai.cancel()` **defaults to `"text-generation"`**, so it stops a chat and nothing else. To stop an image or a transcription, either name the capability or press the ✕ on that job's row — the row is the route the artefact calls are built around, and it is what `onProgress` hands you. Resolving `false` is **not an error**: a Stop pressed as the last token lands is a no-op.

Runtime calls reject with `.type` `"unavailable"` (409 — a fact about this machine, e.g. "needs Apple Silicon") or `"bad_request"`.

## Images: `fused.ai.image({prompt, ...})`

The one call in the bridge that **resolves with a file**. Text streams, so `fused.ai` hands back words; an image is an artefact, so this hands back somewhere to point an `<img>`.

```js
const img = await fused.ai.image({
  prompt: "a topographic map of an island, engraved",
  onProgress: (job) => bar.value = job.done / job.total,   // DENOISING STEPS, not bytes
});
el.src = img.url;        // ready-made /api/fs/raw url — no need to build it
el.dataset.seed = img.seed;
```

Options: `prompt` (required), `model`, `width`, `height`, `steps`, `guidance`, `seed`, `onProgress`.

- **`seed` comes back whether or not you passed one** — invented server-side, so "make that one again" is always one call away.
- **Minutes, not seconds.** `onProgress` fires per denoising step with the download-manager record, and that row's ✕ really stops it (the work is the server's, not the page's).
- Rejects with `.type` `"cancelled"` | `"ai_error"` | `"unavailable"` (no image runner here — reason in the message).
- **`model` comes from `catalog()` here too.** The Diffusers and MLX FLUX runners take *different repos for the same model* — `black-forest-labs/FLUX.2-klein-4B` against `mlx-community/FLUX.2-Klein-4B-4bit` — so a hard-coded id becomes an unloadable download the moment the other one is serving.

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

Resolves with `{jobId, path, output, outputText, outputPartial, model, task, url, text, segments, language, duration, speakers}`.

**The result is read off DISK, not returned by the job** — this is the part that is not obvious. The worker writes `~/.fused-render/ai/transcripts/<time>-<name>-<uid>.json` plus a `.txt` beside it, and when the row reaches `done` the bridge does `readFile(output)` → `JSON.parse` and hands you the parsed fields. So:

- `output` is that JSON path, `outputText` the plain-text one, `outputPartial` the segments-as-they-decode one (see `onSegment` below), and `url` a ready-made `/api/fs/raw` address for `output`.
- A transcription **outlives the tab that asked for it**. The file is the result; the row only says when to read it. A page that navigated away mid-run can still open `output`.
- If the transcript cannot be read (deleted, truncated), the rejection is typed `ai_error` with `err.jobId` — not a bare `SyntaxError`.

Everything else worth knowing:

- **`path` is page-relative when relative**, exactly like `readFile`/`rawUrl`: `"meeting.m4a"` means beside this page. An absolute path is used verbatim. **Nothing is uploaded** — the worker is a process on this machine and opens the file itself.
- **Progress is `unit: "s"` — seconds of audio.** Not bytes (that is a download) and not steps (that is an image). `job.done` is the last decoded segment's end timestamp, `job.total` the audio duration, and the manager renders the pair as a clock (`12:00 / 1:30:00`).
- `task`: `"transcribe"` (same language) or `"translate"` (into English). Anything else is a **400 naming both**, never a silent default.
- `language` omitted means **auto-detect**, which is Whisper's own default. Pass one only if you know it.
- **`model` omitted loads the SMALLEST model the active engine offers** — `Systran/faster-whisper-small` or `mlx-community/whisper-small-mlx`, not the turbo one. That is deliberate: the catalog is ordered smallest-first and the default is simply its first entry, so a bare call is the cheapest download rather than the most accurate transcript. If accuracy matters, **pass a `model`** from `catalog()`; the turbo entries are the ones to reach for.
- `vad` (default `true`) runs a Silero speech detector and skips the silence — the same filter on both engines. Because it does, `job.done` legitimately finishes short of `job.total` on a recording that trails off quietly — that is not an off-by-one to work around. Timestamps are always positions in the original file, never in the filtered audio.
- **Hours, not minutes.** One transcription runs at a time; a second call **queues**, says so on its row, and its ✕ works while it waits.
- Rejects with `.type` `"cancelled"` | `"ai_error"` | `"unavailable"` | `"bad_request"` (a missing path, a path that is not a file, an unknown `task`, or `diarize` without a usable `speakers`).

### As it decodes: `onSegment`

```js
const rec = await fused.ai.transcribe({
  path: "meeting.m4a",
  onSegment: (s) => addCue(s.start, s.end, s.text),   // fires DURING the run
  onProgress: (job) => bar.value = job.done / job.total,
});
// …and `rec.segments` is the same list, whole, when it resolves.
```

- **Every segment, in order, exactly once** — including the ones that were already decoded before your first callback, and including the last ones. Append on each call and you have the transcript; you never have to de-duplicate or re-sort.
- **It costs one extra request per poll, and only if you pass it.** There is no second loop and no new connection: the tail rides the tick `onProgress` was already paying for. A call without `onSegment` makes exactly the requests it always did.
- **The segment is the same shape `rec.segments` carries** — `{start, end, text}`, plus `speaker` when diarizing. One rendering path for both, which is the point.
- **Resolution is the engine's, not the callback's.** faster-whisper produces a segment at a time; the MLX runner finishes a whole decoded window (up to 30s of audio) and then emits its segments together — so callbacks arrive in the same bursts `job.done` jumps in.
- **A transcription still outlives the tab.** `onSegment` is a live view, not the delivery mechanism; the file is. A page that navigated away and came back reads `output` and has everything.

**Underneath, and worth knowing because it is the salvage path:** the worker appends each finished segment to **`outputPartial`** — `<output minus .json>.partial.jsonl`, one JSON object per line, flushed per segment. The reply names it so you never derive it yourself. Its lifecycle is:

| The run | `outputPartial` afterwards |
|---|---|
| finished | **gone** — `output` is the answer and the partial file would be a duplicate of it |
| cancelled with the ✕ | **gone** — you asked for it to stop |
| **failed** | **left on disk**, holding every segment that decoded before it died |

That last row is the reason to know the path exists. A 90-minute recording that fails at minute 80 writes no `.json` at all, and the `.partial.jsonl` is the only place those 80 minutes survive. The rejection carries it, since a failed call never resolves with anything:

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

`err.output` and `err.outputPartial` are on `cancelled` rejections too, where the file is already gone — one shape for both, so you check the read rather than the rejection type. Nothing cleans a salvaged file up for you: delete it once the page has what it needs.

### Who said it: `diarize` + `speakers`

```js
const rec = await fused.ai.transcribe({
  path: "meeting.m4a",
  diarize: true,
  speakers: 3,          // REQUIRED with diarize — how many people are in the room
});
rec.speakers;                       // ["Speaker 1", "Speaker 2", "Speaker 3"]
rec.segments[0].speaker;            // "Speaker 1"  (or null — see below)
```

- **`speakers` is required and must be a whole number ≥ 1.** Omitting it, or passing `true`, `"3"` or `2.5`, rejects `bad_request` **before a job opens**. It is not an optimisation the app could work out for you: underneath, the clustering takes either a speaker count or a similarity threshold, and the threshold answers 2, 4 or 7 on the same recording across its plausible range. A guess would relabel the whole transcript with total confidence, so the app asks instead.
- Every segment gains **`speaker`**, and the reply gains **`speakers`** — the list of labels that actually landed on a segment, ready to build a colour map from without walking thousands of segments.
- **`speaker` is `null` where Whisper heard words but the segmenter heard nobody.** Handle it; do not assume a label.
- **Both engines run the same two models through the same code**, so the labels do not depend on which one served you.
- **Default `false`, and additive**: a call without it is unchanged in every byte, and a transcript written without it has no `speaker` and no `speakers` at all.
- **It does not change what progress means.** `job.done`/`job.total` stay seconds of audio of the *transcript*; diarization is a fast pre-pass that shows up as its own line on the row ("Finding speakers…") with an indeterminate bar.
- **First use on a machine downloads ~33MB** (a speaker segmenter and a voice-embedding model). It needs the network that once; after that it works offline. Unlike the VAD these are *not* pre-fetched by a model Download, because most callers never ask for them.

**Take the model from `fused.ai.models.catalog()`, never from memory.** Whisper repos come in three mutually unloadable formats — CTranslate2 (`model.bin`), MLX (`weights.npz`), transformers (`model.safetensors`) — and which one loads depends on the engine serving this machine, not on the model being "the good one". `openai/whisper-large-v3` is the repo everyone reaches for and loads under **none** of the runners that ship, and because the format is not in the task label the AI Models page offers a Load button anyway. The load error names the format you have, the format that runner needs, and a repo that works. `catalog()` already answers per engine, so it is the only source that cannot be wrong: on Apple Silicon it offers `mlx-community/whisper-large-v3-turbo` and friends, elsewhere `deepdml/faster-whisper-large-v3-turbo-ct2` and friends.

Progress resolution differs slightly by engine — the MLX runner reports once per decoded window (up to 30s of audio) rather than per segment, so `job.done` can sit still and then jump. It is always a real position in the recording. Everything else about the call is identical.

## What Actually Runs Locally Today

Six runners ship, serving three capabilities. All take **Hugging Face repo ids**, and each needs its own machine support:

| Capability | Runners (best first) | Reality |
|---|---|---|
| `text-generation` | MLX, then Transformers (PyTorch) | **Everywhere.** MLX on Apple Silicon; torch on Windows, Linux, and as the Apple Silicon fallback. A CPU-only machine answers slowly but answers. |
| `text-to-image` | MLX FLUX, then Diffusers (PyTorch) | **Everywhere.** MLX FLUX takes Apple Silicon — quicker to load and a smaller download, though it reserves much more memory, which is why Diffusers stays one Preferences switch away; Diffusers serves Windows and Linux. |
| `automatic-speech-recognition` | MLX Whisper, then Faster Whisper (CTranslate2) | **Everywhere.** MLX (on the GPU) for Apple Silicon; CTranslate2 for macOS on both architectures, Linux and Windows, where CPU is fine. |

Those three strings are the capability vocabulary — they are what `unload({capability})` and `cancel(capability)` take, and what `catalog()` groups by.

**Which runner serves you is not purely a hardware fact.** Where a capability has two, the machine picks the better one by default — and the user can override that from Preferences → Inference engines, so a Mac may deliberately be running the CTranslate2 path. Each row in `fused.ai.models.list()`'s `runners` therefore carries **both** `available` (can this backend run here at all) and `active` (is this the one serving the capability right now). Read `active` when you want to say what is running; read `available` when you want to say what this machine could do. Never hard-code either — and let `unavailable` messages reach the user, since they explain *why*.

**And a switch EVICTS.** A model resident under the outgoing engine is unloaded as the preference is written — it belongs to the backend that loaded it, so leaving it would spend gigabytes nothing will route to again. A page holding that model therefore gets `model_loading` on its next `fused.ai()` call — the cold-start path it already handles, not a new failure to code for. The artefact calls reload inside their own job and just take longer.

## Surviving Export

The exporter matches the **string** `fused.ai(`, not the executed path — so wrapping the call in `if (fused.env === "local")` does **not** make a page exportable. The guard is deliberate: an exported page has no CLI and no worker, so the call could only fail at the reader.

Don't try to smuggle the call past the textual match by aliasing it — that trades a clear export-time refusal for a page that ships broken. If a view must export, **keep AI out of it** and gate the feature at the page level (a local-only companion view, or a UI that hides the AI panel when `fused.env !== "local"` with no `fused.ai(` in the file at all).

`fused.trackJob` exports fine — it no-ops on a hosted page. `fused.ai` never will.

**The DOTTED calls are a trap, and in the opposite direction.** The check matches `fused.ai(` specifically, so `fused.ai.image(`, `fused.ai.transcribe(` and `fused.ai.models.*` slip past it: a page using only those **exports cleanly and then fails at the reader**, since a hosted page has no worker, no `<home>/ai/transcripts/`, and no filesystem to read a transcript off. Gate them on `fused.env === "local"` yourself — nothing will stop you at export time. (Whether that gap should be closed is an open question, recorded in `docs/EXPORT.md`; today it is the behaviour.)

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

- **Treating `model_loading` as a failure** → it started a download and gave you `err.jobId`; show it and retry.
- **Reading `usage.prompt_tokens`** → `undefined`. Anthropic names only.
- **Sending `temperature`/`history`/`raw` to the Claude path** → 400, by design. Branch on the destination, or only set them for a slashed model.
- **`unload(selectedId)`** → often unloads nothing; use `{capability}`.
- **Awaiting `models.load()` as if it returned a model** → it returns `{jobId}`.
- **Omitting `{capability}` on `load`/`download` for a repo that is not a chat model** → inference has nothing to read before the download lands, so an uncached whisper or diffusion repo still falls back to text generation and fails inside mlx-lm. Name it.
- **Assuming a capability's runner from the platform** → both text generation and transcription have two runners, and a user preference can pick either. Ask `fused.ai.models.list()` and read `active`.
- **Expecting `transcribe` to hand back the words from the job** → the row only says when; the text is read off `output` from disk. For words *during* the run, pass `onSegment` — not a bigger `detail` on the row.
- **Loading `openai/whisper-large-v3`** → transformers format, which no shipping runner reads, however willingly the page offers the button. Take the id from `catalog()`.
- **Carrying a Whisper repo id between machines** → the CT2 and MLX runners load different files; a repo that works on one engine is an unusable download on the other.
- **Reading transcription progress as bytes or steps** → it is `unit: "s"`, seconds of audio.
- **`fused.ai.cancel()` to stop a transcription** → it defaults to `"text-generation"`; name the capability or use the row's ✕.
- **Not disabling the submit button** → no stale-cancel; a double-click fires two calls.
- **Dumping a full dataset into a prompt** → token blowout, worse answer.
- **Gating `fused.ai` on `fused.env` and expecting export to pass** → the match is textual.
- **Expecting 120 s** → the relay allows **600 s**; keep loading states honest.
