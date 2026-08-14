---
name: fused-render-ai
description: Use when a fused-render page needs an AI model — calling fused.ai for text, streaming tokens, holding a conversation, generating an image with fused.ai.image, or driving local models with fused.ai.models (list/catalog/load/download/unload) and fused.ai.cancel. Also use when an AI call rejects with ai_unavailable, model_loading, unavailable, or timeout, when a model download needs watching, or when a page that calls AI must survive export.
---

# AI in a fused-render Page

## Overview

`fused.ai` is one call with **two destinations**, and the model id alone decides which:

| `opts.model` | Where it runs | Credential |
|---|---|---|
| Contains a `/` — `"mlx-community/Qwen3-8B-4bit"` | **This machine.** A resident worker process holding the weights. | none |
| Anything else — `"sonnet"`, `"claude-haiku-4-5-20251001"`, omitted | The local **`claude` (Claude Code) CLI**. | the user's Claude Code login |

That one rule (`"/" in model`) is the whole seam. A page swapping `model: "opus"` for `model: "mlx-community/Qwen3-8B-4bit"` changes nothing else — same call, same resolved shape.

Both destinations are **local-only**: there is no hosted path. An exported page has neither a CLI nor a worker, so the exporter **rejects any page containing the string `fused.ai(`** (SPEC RH-11) — matched textually, so gating at runtime is not enough (see "Surviving export").

## When to Use

- A page asks a model something: text, chat, streaming.
- A page generates an image locally (`fused.ai.image`).
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
| `fused.ai.models.load(id, opts?)` | `{jobId}` — **not a loaded model.** |
| `fused.ai.models.download(id, opts?)` | `{jobId}` — weights only, no load. |
| `fused.ai.models.unload(idOrCapability)` | `{stopped, ...}` |
| `fused.ai.cancel(capability?)` | `boolean` — stops generation, **keeps the weights**. |

`load`/`download` hand back a **job, not a result**: a cold load is multi-GB and nothing waits on it. Watch with `fused.watchJob(jobId)`.

**Unload by capability, not by id.** A page's Unload button means "release whatever is resident", and the page does **not** reliably know what that is — another page or the AI Models tab may have loaded something else. Passing your dropdown's id unloads nothing and leaves the real model in memory:

```js
fused.ai.models.unload({ capability: "text-generation" });   // honest
fused.ai.models.unload(selectedId);                          // often a no-op
```

`fused.ai.cancel()` resolving `false` is **not an error** — a Stop pressed as the last token lands is a no-op.

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

## What Actually Runs Locally Today

Two runners ship. Both are **Hugging Face repo ids**, and each needs its own machine support:

| Capability | Runner | Reality |
|---|---|---|
| `text-generation` | MLX | **Apple Silicon only.** Elsewhere: `unavailable` with the reason. |
| `text-to-image` | Diffusers (PyTorch) | Broader, but heavy. |

So on a non-Mac, `fused.ai` without a slash (the Claude CLI) is the only text path. Never hard-code the assumption — ask `fused.ai.models.list()` and let `unavailable` messages reach the user, since they explain *why*.

## Surviving Export

The exporter matches the **string** `fused.ai(`, not the executed path — so wrapping the call in `if (fused.env === "local")` does **not** make a page exportable. The guard is deliberate: an exported page has no CLI and no worker, so the call could only fail at the reader.

Don't try to smuggle the call past the textual match by aliasing it — that trades a clear export-time refusal for a page that ships broken. If a view must export, **keep AI out of it** and gate the feature at the page level (a local-only companion view, or a UI that hides the AI panel when `fused.env !== "local"` with no `fused.ai(` in the file at all).

`fused.trackJob` exports fine — it no-ops on a hosted page. `fused.ai` never will.

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
- **Assuming local text works everywhere** → MLX is Apple Silicon only.
- **Not disabling the submit button** → no stale-cancel; a double-click fires two calls.
- **Dumping a full dataset into a prompt** → token blowout, worse answer.
- **Gating `fused.ai` on `fused.env` and expecting export to pass** → the match is textual.
- **Expecting 120 s** → the relay allows **600 s**; keep loading states honest.
