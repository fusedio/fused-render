---
name: fused-render-jobs
description: Use on a runPython 60 s timeout, when work outlives one call, or when a download-manager progress row goes stalled — fused.trackJob from page and worker.
---

# Long work and the 60 s timeout

`runPython` kills `main()` at 60 s (`DEFAULT_TIMEOUT`, `fused_render/executor.py`); no per-call override. Strategies: cache to `.fused/cache/` (see `fused-render-authoring`); chunk behind an `offset` param; move a long build into a detached process that writes an output file; import lazily + debounce sliders. Warm state between calls → `[tool.fused-render.app]` `main =` (reaped after 15 min idle) or `daemon =` → `fused-render-background-apps`.

## `fused.trackJob`

The shell replaces your page's frame on navigation — in-page progress bars die. Report long work to the download manager instead:

```js
const job = fused.trackJob({ title, kind: "download"|"task", unit: "bytes"?, cancellable: true?, id? });
job.update({ done, total, detail });   // per tick
job.finish("msg") / job.fail(err) / job.cancelled();
if (job.cancelRequested) stopTheWork();
```

Rules:

- Fire-and-forget, never rejects, can't break the page. No-op on exported pages (doesn't block export).
- **Cancel is a request YOU honor** — the ✕ sets a flag; your loop notices, stops, reports `cancelled()`. Can't stop it? omit `cancellable`.
- Omit `total` while unknown (indeterminate bar); 0 is treated the same.
- **Report a terminal call on every exit path** — otherwise the row goes "stalled" after 30 s.
- A single await longer than 30 s trips the stall window even when fine — heartbeat with `setInterval(() => job.update({}), 10_000)`, clear in `finally`.
- One row per user-meaningful operation, current filename in `detail`. Stable `id` lets a reloaded page re-attach instead of opening a second row.

`fused.watchJob(id).watch(cb)` = read side (follow a model download, a recording's elapsed seconds). A watched row's `state: "waiting"` = parked on a user question (e.g. build consent), not stuck.

## Report from the WORKER too

Page-only reporting freezes the row the moment the user navigates. The detached worker outlives the page: POST plain JSON to `{FUSED_RENDER_ORIGIN}/api/jobs` with headers `Content-Type: application/json` and `X-Fused: 1`, body `{id, title, kind, state, done, total, detail}`. The reply carries `cancel_requested` — the worker is the only thing that can honor ✕ once the page is gone. Best-effort: swallow all errors, rate-limit ~1/s. **Same job id on both sides** (derive from something both know) = one row, not two. Keep the page reporting too — it's the only reporter alive during a first-run env build.

## Pitfalls

- No terminal call → stalled row lying about a closed page.
- Page-only reporting; two rows from mismatched ids; one row per file.
- Job started under `_preview=1` → every listing card starts the work (gate boot — `fused-render-authoring`).
