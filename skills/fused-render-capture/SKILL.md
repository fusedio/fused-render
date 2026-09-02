---
name: fused-render-capture
description: Use when a page records the screen or microphone or grabs a screenshot — fused.capture.screen/audio/screenshot/sources — or a capture option is refused on a platform.
---

# fused.capture

Native capture → the result is a FILE on this machine: path known before recording ends (feeds `fused.ai.transcribe({path})` directly), survives navigation (it's a download-manager job row). Local only — no `fused.capture` on exported pages.

**Draw UI off `await fused.capture.sources()`** (never prompts) — `{video: {available, reason}, displays, microphones}`. `displays` is empty on Linux/Wayland by design. Don't sniff the platform; read refusals and `reason`s.

Platform matrix:

| | macOS | Windows | Linux |
|---|---|---|---|
| screen recording | native, no picker | browser share picker | browser share picker |
| survives page reload | yes | no (file kept) | no (file kept) |
| `display`/`rect`/`cursor` on screen() | yes | refused | refused |
| same on screenshot() | yes | yes | rect only |
| `device` on audio() | refused (system input; use screen's `audio:"mic"`) | yes | yes |
| container | .mov/.m4a | .mp4/.webm | .mp4/.webm |

So: don't hardcode an extension in `path` — omit it or read `rec.path`. Recordings that matter: keep the page open on Windows/Linux.

## API

`screen({audio: false|"mic"|"system"|"both", rect, path, maxSeconds})` and `audio({device, path, maxSeconds})` resolve **when recording starts**, with a handle `{id, jobId, path, url, state, stop(), cancel()}`. `stop()` → `{path, url, mime, seconds, bytes}`. `cancel()` (and the job row's ✕) stops AND DELETES — treat as "user doesn't want it". Elapsed seconds: `fused.watchJob(rec.jobId).watch(...)` — no onTick. `maxSeconds` default 30 min; hitting it stops and keeps the file.

`screenshot({rect, cursor, path})` — no handle, no job row, native on every platform (no share prompt → can shoot cross-origin panes). Filename picks png/jpeg; no `format` option.

`fused.capture.list()` finds live recordings (incl. pre-reload ones); `attach(id)` returns the handle. Check on load before starting a second recording. Microphone names are empty until browser mic permission granted once.

Rejections: `.type` `"unavailable"` (machine can't — show `.message`) or `"bad_request"` (your args).

## Two rules

- **Previews must not record** — gate boot on `_preview` (`fused-render-authoring`).
- The recording outlives the page: the job row IS the real UI → `fused-render-jobs`.
