---
name: fused-render-capture
description: Record the screen, record the microphone, or grab a screenshot natively from a fused-render page — fused.capture.screen/audio/screenshot/sources. Use when a page records or captures anything, or a capture option is refused on this platform.
---

# Native capture (`fused.capture`)

Three verbs: `screen()`, `audio()`, `screenshot()`. All three write a file this
machine owns, which is the whole reason to use them over `getDisplayMedia` /
`MediaRecorder`: the path is known before the recording ends, so it feeds
`fused.ai.transcribe({path})` directly, and the recording survives your page
being navigated away from (it is a download-manager job row).

Local only — a hosted/exported page has no `fused.capture`.

## Ask first, and it never prompts

What is possible differs per machine and per browser, so draw your UI off the
answer rather than off a try/catch:

```js
const src = await fused.capture.sources();
if (!src.video.available) { hideRecordButton(src.video.reason); return; }
// src.displays -> [{id, width, height, main}], src.microphones -> [{id, name, default}]
// `displays` is empty on Linux and on Wayland by design — nothing to name there.
```

## Three platforms, one API, and deliberately no field telling you which you got

Read the refusals and the `reason`s instead of sniffing the platform.

| | macOS | Windows | Linux |
|---|---|---|---|
| screen recording | native, no picker | browser share picker | browser share picker |
| survives a page reload | yes | no (file is kept) | no (file is kept) |
| `display` / `rect` / `cursor` on `screen()` | honoured | refused | refused |
| `display` / `rect` / `cursor` on `screenshot()` | honoured | honoured | `rect` only |
| `device` on `audio()` | refused | honoured | honoured |
| container | `.mov` / `.m4a` | `.mp4` or `.webm` | `.mp4` or `.webm` |

So: do not hardcode an extension when you pass `path` — let the default name the
file, or read `rec.path`. And if the recording matters, keep the page that
started it open on Windows and Linux; a reload ends it (the file is kept and the
row says so, but it is shorter than the user expected).

## Recording is a handle, not a promise that resolves at the end

```js
const rec = await fused.capture.screen({
  audio: "both",              // false | "mic" | "system" | "both" — "system" is
                              // the one a browser cannot do on macOS at all
  rect: [0, 0, 1200, 800],    // macOS only — refused where the browser's own
                              // share picker chooses the region
  path: "walkthrough.mov",    // optional; relative = beside THIS page. Omit it
                              // unless you know the container (see the table)
  maxSeconds: 600,            // default 30 min; hitting it STOPS (keeps the file)
});
// elapsed seconds come off the recording's job row — there is no onTick:
fused.watchJob(rec.jobId).watch((row) => (label.textContent = row.done + "s"));
// rec.path / rec.url are already usable here — the file is being written to them
const out = await rec.stop();          // {path, url, mime, seconds, bytes}
const words = await fused.ai.transcribe({ path: out.path, words: true });
```

`screen()` and `audio()` resolve **when the recording is running**, with a handle
`{id, jobId, path, url, state, stop(), cancel()}`. `stop()` resolves with the
finished file; `cancel()` deletes it.

- `rec.cancel()` stops **and deletes**. So does the ✕ on its download-manager
  row — that is the one control left once your page is gone, so treat a cancel
  as "the user does not want this recording", not as "stop".
- `fused.capture.audio()` records the microphone alone. On macOS it uses the
  system's current input and **refuses** a `device` (the error names the
  alternative: a screen recording's `audio: "mic"` takes one); on Windows and
  Linux a `device` from `sources().microphones` works. Those names are empty
  until the browser's microphone permission has been granted once.
- `fused.capture.list()` finds live recordings — including one your page started
  before a reload — and `attach(id)` gives the handle back. Check it on load
  instead of starting a second recording.
- `screenshot({rect, cursor, path})` has no handle and no job row. The output
  **filename** picks png or jpeg — there is no `format`, so a path and a format
  cannot disagree about what was written. Native on **every** platform, so it
  needs no readable document and raises no share prompt — which is what makes a
  cross-origin pane shootable.
- Rejections carry `.type`: `"unavailable"` (this machine cannot — show
  `.message`, it names the reason), `"bad_request"` (your arguments).

## Two rules that catch most capture bugs

- **A preview must not record.** A page rendered as a card thumbnail or listing
  peek is stamped `_preview=1`; starting a capture there records the machine of
  someone who was merely browsing. Gate boot on the flag — see "Reserved params
  and preview mode" in **`fused-render-authoring`**.
- **The recording outlives the page, so the job row is the real UI.** Progress
  and cancellation live on the download-manager row, not in your DOM. For how
  those rows work, and for reporting your own long work into them, see
  **`fused-render-jobs`**.

Related: **`fused-render-authoring`** (the rest of `window.fused`),
**`fused-render-ai`** (`fused.ai.transcribe`, the usual next call after a
recording).
