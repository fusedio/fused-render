---
name: fused-render-ai
description: Use when page or .py calls fused.ai (text/image/video/transcribe/embed), picks model/provider, or AI call rejects.
---

# fused.ai

Five verbs, one options object each, one shared result frame. `fused.ai(prompt)` / `fused.ai.text("hi")` don't exist — always `fused.ai.text({prompt})`.

Result frame, every verb: `{<payload>, provider, finishReason, warnings, usage, response: {id, modelId, timestamp}, providerMetadata}`. Read `response.modelId` (no top-level `model`), `usage.inputTokens` (camelCase, or null), `providerMetadata.local` for seed/sizes/paths. Never echo own request as caption.

| Verb | Required | Payload |
|---|---|---|
| `text({prompt})` | prompt | `text` |
| `image({prompt})` | prompt | `images: [{path, url, mediaType}]` |
| `video({prompt})` | prompt | `videos: [...]` |
| `transcribe({path})` | path | `text`, `segments: [{text, startSecond, endSecond, speaker?, words?}]`, `language`, `durationInSeconds` |
| `embed({texts})` | texts or paths | `embeddings: number[][]` (unit-length → cosine = dot) |

Universal rules:

- **Options closed** — unknown key rejects `bad_request` before any request.
- `provider`, `model`, `abortSignal` everywhere; `onChunk` = streaming (text, transcribe); `onProgress(job)` = job row (image, video, transcribe).
- Relative file paths resolve beside page.
- Rejections: Error with `.type` (+ `.jobId` when job existed) — table below.
- Calls run concurrent — disable buttons in flight (image/video/transcribe serialize silently server-side).
- Needs a local runtime, so it depends on WHICH export. **Hosted** export refuses any page containing the literal text `fused.ai.text(` — `fused.env` guard does NOT help; other verbs pass the check, then fail served. Only there is a local-only companion page the answer. A **`.fused` app file** deliberately allows `fused.ai.text(` (D388): it opens inside the recipient's own fused-render, and one without claude or a local model just gets the `ai_unavailable` rejection you already handle.

## Provider / model

Two tiers. `provider: "local" | "claude"` pins; omitted → model shape decides: id with `/` or `.gguf` → local; `"sonnet"`/`"opus"`/`claude-*`/omitted → Claude CLI (text only; default tier haiku unless configured). image/video/transcribe/embed = local-only. **Take local ids from `fused.ai.models.catalog()`, treat as opaque** — never hardcode, never `split("/")`.

`catalog()` → `{capabilities: [{capability, available, reason, default, models[], videoTraits?}], unsupported, ramGb}`. Capabilities: `text-generation`, `text-to-image`, `text-to-video`, `automatic-speech-recognition`, `embeddings`. Model flags: `downloaded, loaded, recommended, size_gb, acceptsImage, acceptsPaths, promptScheme`.

`fused.ai.models`: `list()`, `load(id, {capability})` / `download(id, {capability})` → `{jobId}` (**always pass capability** — wrong runner otherwise), `unload({capability})` (by capability, not id), `fused.ai.cancel(capability?)` (default text-generation — name capability to stop anything else).

## Text and cold start

**text** options: `prompt, provider, model, systemPrompt, effort ("low"|"medium"|"high"|"xhigh", Claude only; default "low" = no extended thinking — ask for more explicitly), history, raw, images, temperature/maxTokens/topP, onChunk, abortSignal`. Tier split: `history`/`raw`/`images` local-only, **reject `bad_request` on Claude**; `temperature`/`maxTokens`/`topP` on Claude and `effort` on local dropped with `warnings[]` entry, call succeeds. `history` = `[{role: "user"|"assistant", content}]` — push user+assistant pair after each turn. Feed aggregates, not datasets — compute in Python first. Structured output: demand strict JSON, strip fences, `JSON.parse` in try/catch. Vision: `images` on local model with `acceptsImage`.

**Cold start**: first text/embed call naming non-resident local model rejects `model_loading` — download already started, `err.jobId` = it. `fused.watchJob(err.jobId)`, then retry. Not a failure. Image/video/transcribe load inside own job instead (`done`/`total` null while weights arrive — guard division).

## Images: `fused.ai.image({prompt, ...})`

Options: `width`/`height` (1024, 256–2048, snapped down to /16), `steps` (28), `guidance` (4.0), `seed` (random; always returned — re-render with it), `image` (page-relative edit base — only some engines: check `acceptsImage`, else `bad_request` naming engine; defaults become file's size, steps 4, guidance 1.0), `onProgress`, `abortSignal`. No negative prompt / batch / LoRA / strength.

Resolves `images: [{path, url, mediaType: "image/png"}]`, `usage: {imagesGenerated: 1}`, `response.id` = job id, actual render in `providerMetadata.local: {seed, width, height, steps, guidance, prompt, previewPath}` (+ `image` on edits). Read sizes off reply. `onProgress` job carries `done/total` (denoising steps) + `previewUrl` (~32px thumb, null on last tick — swap to `images[0].url`). One PNG per call; 900 s cap; renders serialize silently.

## Video: `fused.ai.video({prompt, ...})`

Apple Silicon only, no fallback — before drawing UI check `catalog()` `text-to-video` row `available`, take limits from its `videoTraits`. Options: `width`/`height` (704/480, snapped /32), `frames` (97; `1+8n` grid, rounded **up**), `steps` (8), `seed`, `image` (conditions frame 0; only if `videoTraits.supportsImage`), `onProgress`, `abortSignal`. No guidance, no preview.

Resolves `videos: [{path, url, mediaType: "video/mp4"}]`, `usage: {videosGenerated: 1}`, `response.id` = job id, `providerMetadata.local: {seed, width, height, frames, steps, prompt, image?}`. Up to 2 h; serializes; `fused.ai.cancel("text-to-video")` keeps model warm.

## Transcribe and embed

**transcribe** — `language, task ("transcribe"|"translate"), initialPrompt, vad, diarize, speakers, words, onChunk (per segment), onProgress (seconds)`. Default model = smallest; pass bigger for accuracy. Output persisted under `~/.fused-render/ai/transcripts/` (paths in `providerMetadata.local`). Failed long run keeps `err.outputPartial` (.partial.jsonl) — rows there are RAW `{start, end, text}`, not the resolved `startSecond`/`endSecond` shape. Word timings ±200 ms — don't cut clips on them; `translate` returns no words. One at a time, second queues.

**embed** — `texts` OR `paths` (≤64), `kind: "query"|"document"`. Prose encoder: `kind`, no paths. Dual encoder (CLIP/SigLIP): `paths`, no kind. Index with `"document"`, search with `"query"` — mixing silently drops recall. **Store `response.modelId` with vectors, compare before searching** — cross-model cosine = plausible garbage; dimensions don't catch it.

## From Python

`import fused_ai` works in any server-run `.py`, no path setup. Same option names, blocking; all verbs except video. `fused_ai.text(prompt, ...)` → str; `stream(...)` yields; others return frame (dict). `wait=False`, `on_progress=`, `timeout=` on job-backed calls. Catch `ServerNotRunning`, `AiError` (`.type`). Outside server: `sys.path.insert(0, server.json["shared"])`. No live UI through it — `runPython` returns once, no streaming.

## Errors

| `.type` | Meaning |
|---|---|
| `model_loading` | Not resident; load started — watch `err.jobId`, retry |
| `ai_unavailable` | claude CLI missing / local worker won't start — friendly state, not overlay |
| `unavailable` | Machine can't (no runner, needs Apple Silicon, claude on non-text verb) |
| `bad_request` | Call wrong — read `.message` |
| `ai_error` | Ran, failed (bad id, OOM, crash, render past cap) |
| `timeout` | Text: 600 s on Claude, 900 s local (image 900 s, video 2 h, transcribe far longer) — size a UI timeout off the tier you're calling, not off 600 |
| `cancelled` | abortSignal / cancel / row ✕ — not a failure |

Debug machine (not page): `which claude`; `GET /api/ai/runtime`; `POST /api/ai` with `X-Fused: 1` (required on every POST).

Full option semantics + edge cases: `fused_render/static/runtime.js` header comment (SPEC §40 refs); Python signatures: `fused_render/templates/shared/fused_ai.py`.
