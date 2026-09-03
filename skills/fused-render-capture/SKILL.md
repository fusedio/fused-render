---
name: fused-render-capture
description: Use when page records screen/mic or grabs screenshot — fused.capture — or capture option refused on a platform.
---

# fused.capture

Native capture → result = FILE on this machine: path known before recording ends, file written live — `rec.path`/`rec.url` usable mid-recording (tail it, feed `fused.ai.transcribe({path})` after). Survives navigation (it's a download-manager job row). Local only — no `fused.capture` on exported pages.

**Draw UI off `await fused.capture.sources()`** (never prompts — the permission dialog rides the first real capture) — `{video, audio, systemAudio, screenshot}`, EACH `{available, granted, reason}`, plus `displays` and `microphones`. Gate every control off its own key (mic button off `audio`, `audio: "system"`/`"both"` off `systemAudio`) — `available: false` always carries a `reason`, and a start rejects `unavailable` with that same sentence. `displays` empty on Linux/Wayland by design. Don't sniff platform; read refusals + `reason`s.

Platform matrix:

| | macOS | Windows | Linux |
|---|---|---|---|
| screen recording | native, no picker | browser share picker | browser share picker |
| survives page reload | yes | no (file kept) | no (file kept) |
| `display`/`rect`/`cursor` on screen() | yes | refused | refused |
| same on screenshot() | yes | yes | rect only |
| `device` on audio() | refused (system input; use screen's `audio:"mic"`) | yes | yes |
| container | .mov/.m4a | .mp4/.webm | .mp4/.webm |

So: don't hardcode extension in `path` — omit or read `rec.path`. Recordings that matter: keep page open on Windows/Linux.

## API

`screen({audio: false|"mic"|"system"|"both", display, rect, cursor, device, path, maxSeconds, title})` and `audio({source: "mic", device, path, maxSeconds, title})` resolve **when recording starts**, handle = `{id, jobId, path, url, state, stop(), cancel()}`. `stop()` → `{path, url, mime, seconds, bytes}`. `cancel()` (and job row ✕) stops AND DELETES — treat as "user doesn't want it". Elapsed seconds: `fused.watchJob(rec.jobId).watch(...)` — no onTick. `maxSeconds` default 30 min; hitting it stops, keeps file.

`screenshot({display, rect, cursor, path})` — no handle, no job row, native every platform (no share prompt → can shoot cross-origin panes). Filename picks png/jpeg; no `format` option.

`fused.capture.list()` finds live recordings (incl. pre-reload); `attach(id)` returns handle. Check on load before starting second recording. Microphone names empty until browser mic permission granted once.

Rejections: `.type` `"unavailable"` (machine can't — show `.message`), `"bad_request"` (your args), or — on `stop()`/`cancel()` only — `"capture_error"`: the call went through but the file failed to write, so there is no playable recording. Handle all three; a two-branch `switch` swallows a lost take.

## Two rules

- **Previews must not record** — gate boot on `_preview` (`fused-render-authoring`).
- Recording outlives page: job row IS the real UI → `fused-render-jobs`.
