---
name: fused-render-capture
description: Use when page records screen/mic or grabs screenshot — fused.capture — or capture option refused on a platform.
---

# fused.capture

Native capture → result = FILE on this machine: path known before recording ends, file written live — `rec.path`/`rec.url` usable mid-recording (tail it, feed `fused.ai.transcribe({path})` after). Survives navigation (it's a download-manager job row). Local only — no `fused.capture` on exported pages.

**Draw UI off `await fused.capture.sources()`** (never prompts — the permission dialog rides the first real capture) — `{video, audio, systemAudio, screenshot}`, EACH `{available, granted, reason}`, plus `displays` and `microphones`. Gate every control off its own key (mic button off `audio`, `audio: "system"`/`"both"` off `systemAudio`) — `available: false` always carries a `reason`, and a start rejects `unavailable` with that same sentence. `displays` empty on Linux/Wayland by design. Don't sniff platform; read refusals + `reason`s.

Platform matrix:

| | macOS | Linux |
|---|---|---|
| screen recording | native, no picker | browser share picker |
| survives page reload | yes | no (file kept) |
| `display`/`rect`/`cursor` on screen() | yes | refused |
| same on screenshot() | yes | rect only |
| `device` on audio() | `source:"mic"`: refused (system input; use screen's `audio:"mic"`); `"system"`: refused; `"both"`: yes | yes |
| `audio({source:"system"\|"both"})` | yes (14.2+: Core Audio tap, "System Audio Recording" grant; 13.0–14.1: Screen Recording grant) | refused (record the screen with `audio:"system"`) |
| container | .mov/.m4a | .mp4/.webm |

So: don't hardcode extension in `path` — omit or read `rec.path`. Recordings that matter: keep page open on Linux.

## API

`screen({audio: false|"mic"|"system"|"both", display, rect, cursor, device, path, maxSeconds, title})` and `audio({source: "mic"|"system"|"both", device, path, maxSeconds, title})` resolve **when recording starts**, handle = `{id, jobId, path, url, state, paused, pause(), resume(), stop(), cancel()}`. `stop()` → `{path, url, mime, seconds, bytes}` (+ `warning` when a system-audio tap heard nothing in ≥1 s: either the Mac was silent or this app lacks System Audio Recording — macOS can't tell which, so it is a warning on a valid file, not an error; show it). `cancel()` (and job row ✕) stops AND DELETES — treat as "user doesn't want it". Elapsed seconds: `fused.watchJob(rec.jobId).watch(...)` — no onTick. `maxSeconds` default 30 min; hitting it stops, keeps file.

`pause()`/`resume()` (macOS audio recordings; `paused` reads the state) hold the recording: paused time is not in the file and does not count toward `maxSeconds`. Linux and screen recordings reject with `.type "unavailable"`. On macOS a floating pill (icon, level meter, pause, stop) shows while an audio recording runs and its stop is the same as `stop()` — so a page may call `stop()` after it and just gets the finished record. Don't draw a second recording-indicator for it.

macOS 13.0–14.1 system audio (`source:"system"|"both"`): needs the Screen Recording permission (only sound is recorded, no picture); the app must be quit and reopened after the grant; the file includes ALL system sound, notifications included. `warning` there also fires on silence or no audio reaching the recording.

`screenshot({display, rect, cursor, path})` — no handle, no job row, native every platform (no share prompt → can shoot cross-origin panes). Filename picks png/jpeg; no `format` option.

`fused.capture.list()` finds live recordings (incl. pre-reload); `attach(id)` returns handle. Check on load before starting second recording. Microphone names empty until browser mic permission granted once.

Rejections: `.type` `"unavailable"` (machine can't — show `.message`), `"bad_request"` (your args), or — on `stop()`/`cancel()` only — `"capture_error"`: the call went through but the file failed to write, so there is no playable recording. Handle all three; a two-branch `switch` swallows a lost take.

## Two rules

- **Previews must not record** — gate boot on `_preview` (`fused-render-authoring`).
- Recording outlives page: job row IS the real UI → `fused-render-jobs`.
