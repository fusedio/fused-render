---
name: fused-render-jobs
description: Use on runPython 60 s timeout, work outliving one call, or stalled download-manager progress row — fused.trackJob.
---

# Long work and the 60 s timeout

`runPython` kills `main()` at 60 s (`DEFAULT_TIMEOUT`, `fused_render/executor.py`); no per-call override. Strategies: cache to `.fused/cache/` (`fused-render-authoring`); chunk behind `offset` param; move long build into detached process writing output file; import lazy + debounce sliders (~150 ms). Warm state between calls → `[tool.fused-render.app]` `main =`, page calls it via `fused.daemon.run(params)` (reaped after 15 min idle) or `daemon =` → `fused-render-background-apps`.

## `fused.trackJob`

Shell replaces page frame on navigation — in-page progress bars die. Report long work to download manager:

```js
const job = fused.trackJob({ title, kind: "download"|"task", unit: "bytes"?, cancellable: true?, id? });
job.update({ done, total, detail });   // per tick
job.finish("msg") / job.fail(err) / job.cancelled();
if (job.cancelRequested) stopTheWork();
```

Rules:

- Fire-and-forget, never rejects, can't break page. No-op on exported pages (doesn't block export).
- **Cancel = request YOU honor** — ✕ sets flag; loop notices, stops, reports `cancelled()`. Can't stop? omit `cancellable`.
- Omit `total` while unknown (indeterminate bar); 0 treated same.
- **Terminal call on every exit path** — else row goes "stalled" after 30 s.
- Single await >30 s trips stall window even when fine — heartbeat `setInterval(() => job.update({}), 10_000)`, clear in `finally`.
- One row per user-meaningful operation, current filename in `detail`. Stable `id` → reloaded page re-attaches, no second row.

`fused.watchJob(id).watch(cb)` = read side (follow model download, recording's elapsed seconds). Watched row `state: "waiting"` = parked on user question (e.g. build consent), not stuck.

## Report from the WORKER too

Page-only reporting freezes row when user navigates. Detached worker outlives page — and runs own venv, no `import fused_render`, so it speaks plain JSON over HTTP: POST `{FUSED_RENDER_ORIGIN}/api/jobs`, headers `Content-Type: application/json` + `X-Fused: 1`, body `{id, title, kind, state, done, total, detail}`. Reply carries `cancel_requested` — worker is only thing able to honor ✕ once page gone. Best-effort: swallow all errors, short timeout (~3 s), rate-limit ~1/s. **Same job id both sides** (derive from something both know) = one row, not two. Keep page reporting too — only reporter alive during first-run env build.

## Pitfalls

- No terminal call → stalled row lying about closed page.
- Page-only reporting; two rows from mismatched ids; one row per file.
- Job started under `_preview=1` → every listing card starts work (gate boot — `fused-render-authoring`).
