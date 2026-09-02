---
name: fused-render-ai
description: Use fused.ai from a fused-render page or .py file — text (Claude or a local model), image, video, transcription, embeddings, and the local model catalog. Load when a page needs a model, when picking a model or provider, or when an AI call rejects (model_loading, ai_unavailable, unavailable, bad_request, timeout, cancelled).
---

# fused.ai — building with models in a fused-render page

`window.fused.ai` is injected into every rendered page. Five verbs, one options object each, one result frame for all of them. Learn the shape once, then reach for the recipes below.

```js
const r = await fused.ai.text({ prompt: "Three words about rain." });
r.text;               // the payload — one verb-named key per verb
r.provider;           // "claude" | "local" — the tier that answered
r.response.modelId;   // the model that actually ran (no top-level `model`)
r.usage;              // { inputTokens, outputTokens, totalTokens } | null
r.finishReason;       // "stop" | "length" | "cancelled"
r.warnings;           // [] or [{ type: "unsupported-setting", setting, message }]
r.providerMetadata;   // { local: {...} } — seed, sizes, file paths, seconds: read it, never echo your request
```

| Verb | Required | Payload keys on the frame |
|---|---|---|
| `fused.ai.text({prompt})` | `prompt` | `text` |
| `fused.ai.image({prompt})` | `prompt` | `images: [{path, url, mediaType}]` |
| `fused.ai.video({prompt})` | `prompt` | `videos: [{path, url, mediaType}]` |
| `fused.ai.transcribe({path})` | `path` | `text`, `segments: [{text, startSecond, endSecond, speaker?, words?}]`, `language`, `durationInSeconds` |
| `fused.ai.embed({texts})` | `texts` or `paths` | `embeddings: number[][]`, `values` |

Rules that hold on every verb:

- **Options are closed.** A key the verb does not take rejects `bad_request` naming it, before any request. Typos never silently do nothing. The exact lists are in each section below.
- **`provider`, `model`, `abortSignal`** mean the same thing everywhere. `onChunk` is the streaming callback (text, transcribe); `onProgress(job)` is the job-row callback (image, video, transcribe).
- **Relative file paths resolve beside the calling page.** `"photo.png"` means the file next to this `.html`. Nothing is uploaded; the server opens the file.
- **Every rejection is an `Error` with `.type`** (table at the end) and, when a job existed, `.jobId`.
- **Calls run concurrently.** Nothing stops a double-click firing two calls. Disable the button while one is in flight.
- **`fused.ai` never works on an exported page.** See "Export" before you ship.

## Choosing provider and model

Two tiers, fixed order. `provider` pins one; omitted, the model's shape decides.

| `provider` | `model` | Runs on |
|---|---|---|
| omitted | omitted | Claude, the user's default (haiku unless configured) |
| omitted | `"sonnet"`, `"opus"`, a `claude-*` id | Claude Code CLI, the user's login. Text only. |
| omitted | contains `/` or ends in `.gguf` | this machine, a resident local worker |
| `"claude"` | alias or omitted | Claude. A repo id here is `bad_request` |
| `"local"` | repo id or omitted | this machine; omitted = the catalog's default text model |

`.image`, `.video`, `.transcribe`, `.embed` are local-only today: omitted means `"local"`, `"claude"` rejects `unavailable`.

**Take local ids from `fused.ai.models.catalog()` and treat them as opaque.** Ids are backend-specific (MLX on Apple Silicon, GGUF/ONNX elsewhere) and not always `org/name` — a curated GGUF id is a bare filename. A hard-coded id is an unusable download on the next machine. Never `id.split("/")`.

```js
const cat = await fused.ai.models.catalog();       // { capabilities: [row...], unsupported, ramGb }
const row = cat.capabilities.find(r => r.capability === "text-generation");
row.available;   // false → nothing here serves it; row.reason says why
row.default;     // the id a bare call uses — null when there is no recommendation
row.models;      // [{ id, label, size_gb, downloaded, loaded, recommended, source, ... }]
```

Capability strings: `"text-generation"`, `"text-to-image"`, `"text-to-video"`, `"automatic-speech-recognition"`, `"embeddings"`.

## Text

Options: `prompt`, `provider`, `model`, `systemPrompt`, `effort` (`"low"|"medium"|"high"|"xhigh"`, Claude only), `history`, `raw`, `images`, `temperature`, `maxTokens`, `topP`, `onChunk`, `abortSignal`.

Tier split: `history`, `raw`, `images` are **local-only and reject 400 on Claude** — dropping them would answer a different question. `temperature`/`maxTokens`/`topP` on Claude and `effort` on local are **dropped with a `warnings[]` entry**; the call succeeds. Default `effort` is `"low"` (no extended thinking).

### Ask, stream, and show what ran

```js
const res = await fused.ai.text({
  prompt,
  systemPrompt: "Answer in one paragraph.",
  onChunk: (delta) => { out.textContent += delta; },   // optional — same frame resolves either way
});
out.textContent = res.text;
meta.textContent = `${res.provider} · ${res.response.modelId}`;
if (res.finishReason === "length") meta.textContent += " · truncated";
```

### Feed aggregates, not the dataset

Compute in Python, reduce to a few numbers, hand the model those. A raw table blows the token budget and drowns the signal.

```js
const data = await fused.runPython("./data.py", { days });      // full data for the chart
const context = JSON.stringify({ total: data.total, byRegion: data.by_region });
const res = await fused.ai.text({
  prompt: `Data (JSON):\n${context}\n\nQuestion: ${q}`,
  systemPrompt: "You are a data analyst. Answer only from the JSON. Cite figures.",
});
```

### Structured output

Ask for JSON, parse defensively, never trust the fence.

```js
const res = await fused.ai.text({
  prompt: `Classify each title. Reply with ONLY a JSON array of {"title","tag"}.\n${titles.join("\n")}`,
  systemPrompt: "Output strict JSON. No prose, no code fences.",
});
const json = res.text.replace(/^```(?:json)?\s*|\s*```$/g, "");
let rows; try { rows = JSON.parse(json); } catch { rows = []; }    // retry or degrade, never crash
```

### Chat with memory, and vision — local models

```js
const history = [];                                    // [{role: "user"|"assistant", content}]
async function turn(userText) {
  const res = await fused.ai.text({ prompt: userText, history, model: chatModelId });
  history.push({ role: "user", content: userText }, { role: "assistant", content: res.text });
  return res.text;
}
// A vision-language model takes base images for THIS turn, beside the page:
await fused.ai.text({ prompt: "What is in this chart?", images: ["chart.png"], model: visionModelId });
```

`chatModelId` must be a local id (`history` and `images` are refused on Claude). Pick one from `catalog()`; a model's `acceptsImage` flag says whether `images` will work.

### A stop button

```js
const ctl = new AbortController();
stopBtn.onclick = () => ctl.abort();
try {
  await fused.ai.text({ prompt, onChunk, abortSignal: ctl.signal });
} catch (err) {
  if (err.type !== "cancelled") throw err;           // a stop is not a failure
}
```

Works on every verb. On image/video/transcribe it cancels the job — the same as the ✕ on its row.

### The cold-start handler — write it once

The first text or embed call naming a local model that is not resident rejects `model_loading` **having already started the download**. Watch it, then retry.

```js
async function withModel(call) {
  try { return await call(); }
  catch (err) {
    if (err.type !== "model_loading") throw err;
    status.textContent = "Loading model…";
    await fused.watchJob(err.jobId).watch((job) => {
      if (job.total) status.textContent = `Loading… ${Math.round(100 * job.done / job.total)}%`;   // bytes
    });
    return call();                                   // resident now
  }
}
const res = await withModel(() => fused.ai.text({ prompt, model: localId }));
```

Image, video and transcribe do **not** throw this: they load the model inside their own job (`done`/`total` are `null` while the weights arrive — guard the division).

## Images: `fused.ai.image({prompt, ...})`

Options: `prompt`, `model`, `provider`, `width`, `height`, `steps`, `guidance`, `seed`, `image`, `onProgress`, `abortSignal`.

| Option | Default | Range | Notes |
|---|---|---|---|
| `width` / `height` | 1024 | 256–2048 | clamped, snapped down to a multiple of 16 |
| `steps` | 28 | 1–100 | clamped |
| `guidance` | 4.0 | 0–20 | clamped |
| `seed` | random | 0–2147483647 | always returned, so a render can be re-requested |
| `image` | — | one page-relative file | edit this base image instead of rendering from scratch; defaults become the file's size, `steps` 4, `guidance` 1.0 |

No negative prompt, batch, LoRA or `strength`. One prompt, one PNG, one row. Resolves with `images: [{path, url, mediaType: "image/png"}]`, `usage: {imagesGenerated: 1}`, `response.id` = the job id, and the render that actually happened under `providerMetadata.local`: `seed, width, height, steps, guidance, prompt, previewPath`, plus `image` when you passed one. **Read sizes off the reply**, never echo your request as the caption.

### Generate with a live preview

```js
el.onerror = () => { el.hidden = true; };             // early ticks have no frame yet
const img = await fused.ai.image({
  prompt,
  onProgress: (job) => {
    if (job.total) bar.value = job.done / job.total;  // denoising steps, not bytes
    if (job.previewUrl) { el.src = job.previewUrl; el.hidden = false; }   // ~32px thumbnail, already cache-busted — blur it in CSS
  },
});
el.src = img.images[0].url;                           // previewUrl is null on the last tick: swap here
seedLabel.textContent = img.providerMetadata.local.seed;
```

### Edit a photo, and degrade when the engine cannot

Only some engines edit. The picked model's `acceptsImage` flag in `catalog()` says so up front; the call says so again as a `bad_request` naming the engine.

```js
try {
  const edited = await fused.ai.image({ prompt: "make the sky stormy", image: "photo.png" });
} catch (err) {
  if (err.type === "bad_request" && /engine/i.test(err.message)) showPlainRenderInstead();   // only the Apple Silicon engine edits
  else throw err;
}
```

Renders take minutes on CPU, are capped at 900 s (past that: `ai_error`), and **serialize**: a second render waits with no queue message on its row. Keep default sliders modest; disable the button.

## Video: `fused.ai.video({prompt, ...})`

Options: `prompt`, `model`, `provider`, `width`, `height`, `frames`, `steps`, `seed`, `image`, `onProgress`, `abortSignal`. No `guidance`, no preview.

Apple Silicon only, no fallback. **Check before you draw the button:**

```js
const row = (await fused.ai.models.catalog()).capabilities.find(r => r.capability === "text-to-video");
if (!row.available || !row.default) { panel.hidden = true; note.textContent = row.reason; return; }
const t = row.videoTraits;    // { defaultWidth, defaultHeight, defaultFrames, defaultSteps, minFrames, maxFrames, framesStep, supportsImage }
```

| Option | Default | Range | Notes |
|---|---|---|---|
| `width` / `height` | 704 / 480 | 256–1344, `w*h ≤ 768*1344` | snapped down to a multiple of 32 |
| `frames` | 97 (~4 s at 24 fps) | 9–169 on a `1 + 8n` grid | rounded **up** to the grid: 100 → 105 |
| `steps` | 8 | 2–50 | clamped |
| `seed` | random | 0–2147483647 | returned |
| `image` | — | one page-relative file | conditions frame 0; sizes default from the image. Offer it only if `videoTraits.supportsImage` |

```js
const vid = await fused.ai.video({ prompt, onProgress: (job) => { if (job.total) bar.value = job.done / job.total; } });
video.src = vid.videos[0].url;                        // mp4 with audio muxed in
```

Resolves with `videos: [{path, url, mediaType: "video/mp4"}]`, `usage: {videosGenerated: 1}`, `response.id` = the job id, `providerMetadata.local: {seed, width, height, frames, steps, prompt, image?}`. Renders can take up to 2 hours and serialize. `fused.ai.cancel("text-to-video")` or `abortSignal` stops one and keeps the model warm.

## Transcription: `fused.ai.transcribe({path, ...})`

Options: `path`, `model`, `provider`, `language` (omit = auto-detect), `task` (`"transcribe"` | `"translate"` into English), `initialPrompt`, `vad` (default `true`), `diarize`, `speakers`, `words`, `onProgress`, `onChunk`, `abortSignal`.

Progress `done`/`total` are **seconds of audio**. The transcript is written to `~/.fused-render/ai/transcripts/` and outlives the tab; paths are under `providerMetadata.local` (`output`, `outputText`, `outputPartial`, `url`). `model` omitted loads the **smallest** model — pass a larger one from `catalog()` when accuracy matters.

### Live transcript, then the whole thing

```js
const rec = await fused.ai.transcribe({
  path: "meeting.m4a",
  onChunk: (s) => addCue(s.startSecond, s.endSecond, s.text),   // every segment, in order, exactly once, during the run
  onProgress: (job) => { if (job.total) bar.value = job.done / job.total; },
});
// rec.segments is the same list, whole. rec.text is the joined transcript.
```

### Who said it

```js
const rec = await fused.ai.transcribe({ path, diarize: true });           // speakers: 3 fixes the count; omitted = estimated
const labels = rec.providerMetadata.local.speakers;                        // ["Speaker 1", ...] for a colour map
rec.segments.forEach(s => paint(s.speaker));                              // null where words were heard but no voice segmented
rec.providerMetadata.local.estimatedSpeakers;                              // present only when it had to guess — show it, offer a re-run
```

### Click-a-word player

```js
const rec = await fused.ai.transcribe({ path, words: true });
for (const s of rec.segments)
  for (const w of s.words || [])                     // best-effort: some engines return no words — always `|| []`
    addWord(w.word, w.startSecond, w.endSecond);    // `word` keeps its leading space; join("") rebuilds s.text
```

Word timings follow along fine for a highlight; roughly half land >200 ms off on conversational audio, so do not cut clips on them. `task: "translate"` returns no words.

### Salvage a failed long run

The worker flushes each segment to `outputPartial` (`.partial.jsonl`). It is deleted on success and cancel, kept on failure:

```js
try { await fused.ai.transcribe({ path }); }
catch (err) {
  if (err.type === "ai_error" && err.outputPartial) {
    const segs = (await fused.readFile(err.outputPartial)).split("\n").filter(Boolean).map(JSON.parse);   // raw worker rows: {start, end, text}
  }
}
```

One transcription runs at a time; a second **queues** and says so on its row.

## Embeddings: `fused.ai.embed({texts, ...})`

Options: `texts` **or** `paths` (never both, ≤ 64 items), `model`, `kind` (`"query"` | `"document"`), `provider`, `abortSignal`. Not job-backed: the reply is the result. Vectors are **unit-length**, so cosine is a plain dot product.

Two model shapes serve this capability, and two options are per-model: a **prose encoder** takes `kind` and refuses `paths`; a **dual encoder** (SigLIP/CLIP) takes `paths` and refuses `kind`. Read the flags off `catalog()` before drawing a control: `acceptsPaths === true` for an image affordance, `promptScheme !== null` for a query/document toggle.

### Semantic search over a folder

```js
// Index once. kind:"document" here, kind:"query" at search time — using one side for both silently costs recall.
const r = await fused.ai.embed({ texts: chunks, kind: "document" });
await fused.writeFile("index.json", JSON.stringify({
  model: r.response.modelId,                          // the space these vectors live in — store it
  chunks, embeddings: r.embeddings,
}));

// Search.
const index = JSON.parse(await fused.readFile("index.json"));
const q = await fused.ai.embed({ texts: [question], kind: "query" });
if (q.response.modelId !== index.model)
  throw new Error(`index built with ${index.model}; rebuild before searching with ${q.response.modelId}`);
const dot = (a, b) => a.reduce((s, x, i) => s + x * b[i], 0);
const hits = index.embeddings.map((v, i) => [dot(v, q.embeddings[0]), index.chunks[i]])
                             .sort((a, b) => b[0] - a[0]).slice(0, 5);
```

Two models produce two spaces. A cosine between them is a meaningless number that **looks** ranked and plausible, and a dimension check does not catch it — several unrelated models are 768-dim. Storing and comparing `response.modelId` is the only guard.

### Search photos with a sentence

```js
const m = row.models.find(m => m.acceptsPaths === true);          // a dual encoder
const imgs = await fused.ai.embed({ paths: photoPaths, model: m.id });   // page-relative or absolute
const txt  = await fused.ai.embed({ texts: ["a dog on a beach"], model: m.id });   // no `kind` on this model
```

## The model picker: `fused.ai.models`

| Call | Returns |
|---|---|
| `catalog()` | `{capabilities: [{capability, available, reason, default, models[], videoTraits?}], unsupported, ramGb}` |
| `list()` | `{loaded: [...], downloading, runners: [{code, capability, available, active, ...}], totalResidentBytes, memoryCeilingBytes}` |
| `load(id, {capability})` / `download(id, {capability})` | `{jobId}` — a job, not a model. Watch with `fused.watchJob(jobId)` |
| `unload({capability})` | `{stopped, ...}` |
| `fused.ai.cancel(capability?)` | `boolean` — stops generation, keeps weights. Defaults to `"text-generation"` |

```js
const row = cat.capabilities.find(r => r.capability === cap);
for (const m of row.models) {                        // render ALL of them — filtering to curated hides what the user downloaded
  const state = m.loaded ? "ready" : m.downloaded ? "instant load" : `${m.size_gb} GB download`;
  addOption(m.id, `${m.label || m.id} — ${state}`, { recommended: m.recommended, isDefault: m.id === row.default });
}
```

- **Always pass `{capability}` to `load`/`download`.** Without it an undownloaded whisper or diffusion repo is guessed as a text model and fails inside the wrong runner.
- **Unload by capability, not id.** The resident model may not be the one your dropdown shows; `unload({capability: cap})` releases whatever is there.
- Lists are smallest-first and `default` is the smallest curated entry, so a bare call gets the quickest model, not the best. Offer `recommended` ones for quality.
- Switching engines on the AI Models page evicts the resident model; your next call sees `model_loading` again. The cold-start handler already covers it.

## From Python: `import fused_ai`

Same option names, blocking by default; every verb except `video`. Any `.py` the server runs can `import fused_ai` with no path setup.

```python
import fused_ai

def main(path: str = "meeting.m4a"):
    rec = fused_ai.transcribe(path=path)                         # blocks until done; same frame as the JS side
    summary = fused_ai.text("Summarise:\n" + rec["text"], model="sonnet")   # returns str
    vecs = fused_ai.embed(texts=[rec["text"]], kind="document")  # frame: vecs["embeddings"], vecs["response"]["modelId"]
    return {"summary": summary, "speakers": rec["providerMetadata"]["local"].get("speakers")}
```

- `fused_ai.text(prompt, model=, effort=, system_prompt=, provider=)` → `str`; `fused_ai.stream(...)` yields chunks. `transcribe`/`image`/`embed` return the frame.
- Job-backed calls take `wait=False` (get `{jobId}` back), `on_progress=` (each job row), `timeout=`.
- Catch `fused_ai.ServerNotRunning` (nothing to call) separately from `fused_ai.AiError` (`.type`, `.message`, same types as JS plus `stalled`).
- Outside the server (a script, a notebook): `~/.fused-render/server.json` has `"shared"`; `sys.path.insert(0, that)` then import.
- **Do not route a UI through it.** `runPython()` returns once, so a `fused_ai` call behind it cannot stream tokens or move a progress bar. For anything live, call `fused.ai` from JavaScript.

## Errors

```js
try { ... } catch (err) {
  switch (err.type) {
    case "model_loading": /* watch err.jobId, retry */ break;
    case "cancelled":     /* user asked; not a failure */ break;
    case "unavailable":   /* this machine cannot: show err.message */ break;
    case "ai_unavailable":/* claude CLI missing or worker won't start: friendly state */ break;
    case "bad_request":   /* your call is wrong: read err.message */ break;
    case "timeout":       /* offer retry */ break;
    default:              /* ai_error: show err.message */
  }
}
```

| `.type` | Meaning | From |
|---|---|---|
| `model_loading` | Local model not resident; the load already started, `err.jobId` is it | text, embed |
| `ai_unavailable` | `claude` CLI not found, or the local worker will not start | text |
| `unavailable` | A fact about this machine: no runner, needs Apple Silicon, `"claude"` on a non-text verb | image, video, transcribe, embed, models |
| `bad_request` | Unknown option, missing/empty required field, local-only option on Claude, unusable value | all |
| `ai_error` | Ran and failed: bad model id, OOM, worker crash, transcript unreadable, or a render past its cap (image 900 s, video 2 h — "the … process did not answer") | all |
| `timeout` | No answer within 600 s | text |
| `cancelled` | `abortSignal`, `fused.ai.cancel`, or the row's ✕ | all |

## Export

The exporter refuses any page containing the text `fused.ai.text(` — matched literally, so `if (fused.env === "local")` does not help. The other verbs **pass the check and then fail hosted** (no worker, no `/api/fs/raw`). To ship an exportable view, keep AI in a separate local-only companion page, or gate every AI feature on `fused.env === "local"` and keep `fused.ai.text(` out of the file entirely. `import fused_ai` is not copied into an export either.

## Debugging

```bash
which claude && claude --version                      # Claude tier present?
curl -s http://127.0.0.1:1777/api/ai/runtime          # what is resident locally
curl -s -X POST http://127.0.0.1:1777/api/ai -H 'X-Fused: 1' -H 'Content-Type: application/json' \
  -d '{"prompt": "Reply with exactly the word pong."}'
```

`X-Fused: 1` is required on every POST; without it the rejection looks like a broken endpoint. If the curl fails, it is the machine, not your page.

## Pitfalls

- `fused.ai(prompt)` and `fused.ai.text("hi")` → gone. One options object: `fused.ai.text({ prompt })`.
- `res.model`, `usage.input_tokens`, `seg.start` → `undefined`. Use `response.modelId`, `usage.inputTokens`, `startSecond`.
- Reading `res.url` / `res.seed` on an image → `images[0].url`, `providerMetadata.local.seed`.
- Treating `model_loading` as a failure → it is a download you asked for. Watch and retry.
- Dividing by `job.total` on an image job while the model loads → `NaN`. Guard it.
- Hard-coding a model id → wrong format on the next machine. Read `catalog()`.
- `load(id)` without `{capability}` for a non-text repo → fails in the wrong runner.
- `unload(selectedId)` → often a no-op. `unload({capability})`.
- `fused.ai.cancel()` to stop an image or transcription → only stops text. Name the capability or use `abortSignal`.
- `embed` without `kind` on a retrieval model → recall quietly drops. `"document"` to index, `"query"` to search.
- Mixing vectors from two models → plausible garbage. Store and compare `response.modelId`.
- Firing two renders → they serialize silently. Disable the button.
- Gating on `fused.env` and expecting export to pass → textual match. Move the call to a companion page.
