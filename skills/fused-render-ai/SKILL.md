---
name: fused-render-ai
description: Use when a fused-render page or .py calls fused.ai (text/image/video/transcribe/embed), picks a model or provider, or an AI call rejects (model_loading, ai_unavailable, bad_request, timeout).
---

# fused.ai

Five verbs, one options object each, one shared result frame. `fused.ai(prompt)` / `fused.ai.text("hi")` do not exist — always `fused.ai.text({prompt})`.

Result frame on every verb: `{<payload>, provider, finishReason, warnings, usage, response: {id, modelId, timestamp}, providerMetadata}`. Read `response.modelId` (no top-level `model`), `usage.inputTokens` (camelCase, or null), `providerMetadata.local` for seed/sizes/paths — never echo your request as the caption.

| Verb | Required | Payload |
|---|---|---|
| `text({prompt})` | prompt | `text` |
| `image({prompt})` | prompt | `images: [{path, url, mediaType}]` |
| `video({prompt})` | prompt | `videos: [...]` |
| `transcribe({path})` | path | `text`, `segments: [{text, startSecond, endSecond, speaker?, words?}]`, `language`, `durationInSeconds` |
| `embed({texts})` | texts or paths | `embeddings: number[][]` (unit-length → cosine = dot) |

Universal rules:

- **Options are closed** — unknown key rejects `bad_request` before any request.
- `provider`, `model`, `abortSignal` everywhere; `onChunk` = streaming (text, transcribe); `onProgress(job)` = job row (image, video, transcribe).
- Relative file paths resolve beside the page.
- Rejections: Error with `.type` (+ `.jobId` when a job existed) — table below.
- Calls run concurrent — disable buttons in flight (image/video/transcribe serialize silently server-side).
- Local-only; export refuses pages containing the literal text `fused.ai.text(` — an `fused.env` guard does NOT help. Other verbs pass the check then fail hosted. Keep AI in a local-only companion page.

## Provider / model

Two tiers. `provider: "local" | "claude"` pins; omitted, model shape decides: id with `/` or `.gguf` → local; `"sonnet"`/`"opus"`/`claude-*`/omitted → Claude CLI (text only). image/video/transcribe/embed are local-only. **Take local ids from `fused.ai.models.catalog()` and treat as opaque** — never hardcode, never `split("/")`.

`catalog()` → `{capabilities: [{capability, available, reason, default, models[], videoTraits?}], unsupported, ramGb}`. Capabilities: `text-generation`, `text-to-image`, `text-to-video`, `automatic-speech-recognition`, `embeddings`. Model flags: `downloaded, loaded, recommended, size_gb, acceptsImage, acceptsPaths, promptScheme`.

`fused.ai.models`: `list()`, `load(id, {capability})` / `download(id, {capability})` → `{jobId}` (**always pass capability** — wrong runner otherwise), `unload({capability})` (by capability, not id), `fused.ai.cancel(capability?)` (defaults to text-generation — name the capability to stop anything else).

## Per-verb notes

**text** — options: `prompt, provider, model, systemPrompt, effort (Claude), history, raw, images (local-only, reject on Claude), temperature/maxTokens/topP (dropped on Claude w/ warning), onChunk, abortSignal`. Feed aggregates, not datasets (compute in Python first). Structured output: demand strict JSON, strip fences, `JSON.parse` in try/catch. Vision: `images` on a local model with `acceptsImage`.

**Cold start**: first text/embed call naming a non-resident local model rejects `model_loading` — the download already started, `err.jobId` is it. Watch `fused.watchJob(err.jobId)`, then retry. Not a failure. Image/video/transcribe load inside their own job instead (`done`/`total` null while weights arrive — guard the division).

**transcribe** — `language, task ("transcribe"|"translate"), initialPrompt, vad, diarize, speakers, words, onChunk (per segment), onProgress (seconds)`. Default model = smallest; pass bigger for accuracy. Output persisted under `~/.fused-render/ai/transcripts/` (paths in `providerMetadata.local`). Failed long run keeps `err.outputPartial` (.partial.jsonl). Word timings ±200 ms — don't cut clips on them; `translate` returns no words. One at a time, second queues.

**embed** — `texts` OR `paths` (≤64), `kind: "query"|"document"`. Prose encoder: `kind`, no paths. Dual encoder (CLIP/SigLIP): `paths`, no kind. Index with `"document"`, search with `"query"` — mixing silently drops recall. **Store `response.modelId` with vectors and compare before searching** — cross-model cosine is plausible garbage; dimensions don't catch it.

## Images: `fused.ai.image({prompt, ...})`

Options: `width`/`height` (1024, 256–2048, snapped down to /16), `steps` (28), `guidance` (4.0), `seed` (random; always returned — re-render with it), `image` (page-relative edit base — only some engines: check `acceptsImage`, else `bad_request` naming the engine; defaults become the file's size, steps 4, guidance 1.0), `onProgress`, `abortSignal`. No negative prompt / batch / LoRA / strength.

Resolves with `images: [{path, url, mediaType: "image/png"}]`, `usage: {imagesGenerated: 1}`, `response.id` = job id, and the render that happened in `providerMetadata.local: {seed, width, height, steps, guidance, prompt, previewPath}` (+ `image` on edits). Read sizes off the reply. `onProgress` job carries `done/total` (denoising steps) and `previewUrl` (~32px thumb, null on last tick — swap to `images[0].url`). One PNG per call; 900 s cap; renders serialize silently.

## Video: `fused.ai.video({prompt, ...})`

Apple Silicon only, no fallback — before drawing UI check the `catalog()` `text-to-video` row's `available` and take limits from its `videoTraits`. Options: `width`/`height` (704/480, snapped /32), `frames` (97; `1+8n` grid, rounded **up**), `steps` (8), `seed`, `image` (conditions frame 0; only if `videoTraits.supportsImage`), `onProgress`, `abortSignal`. No guidance, no preview.

Resolves with `videos: [{path, url, mediaType: "video/mp4"}]`, `usage: {videosGenerated: 1}`, `response.id` = job id, `providerMetadata.local: {seed, width, height, frames, steps, prompt, image?}`. Up to 2 h; serializes; `fused.ai.cancel("text-to-video")` keeps the model warm.

## From Python

`import fused_ai` works in any server-run `.py`, no path setup. Same option names, blocking; all verbs except video. `fused_ai.text(prompt, ...)` → str; `stream(...)` yields; others return the frame (dict). `wait=False`, `on_progress=`, `timeout=` on job-backed calls. Catch `ServerNotRunning` and `AiError` (`.type`). Outside the server: `sys.path.insert(0, server.json["shared"])`. Don't route live UI through it — `runPython` returns once, no streaming.

## Errors

| `.type` | Meaning |
|---|---|
| `model_loading` | Not resident; load started, watch `err.jobId`, retry |
| `ai_unavailable` | claude CLI missing / local worker won't start — friendly state, not overlay |
| `unavailable` | This machine can't (no runner, needs Apple Silicon, claude on non-text verb) |
| `bad_request` | Your call is wrong — read `.message` |
| `ai_error` | Ran and failed (bad id, OOM, crash, render past cap) |
| `timeout` | Text, 600 s |
| `cancelled` | abortSignal / cancel / row ✕ — not a failure |

Debug the machine (not the page): `which claude`; `GET /api/ai/runtime`; `POST /api/ai` with `X-Fused: 1` header (required on every POST).
