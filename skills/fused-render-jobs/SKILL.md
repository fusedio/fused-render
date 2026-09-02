---
name: fused-render-jobs
description: Work that outlives one fused.runPython call — the 60 s executor timeout and the ways around it, plus fused.trackJob reporting into the shell's download manager from the page and from a detached worker. Use on a timeout, or when a progress row goes stalled.
---

# Long-running work and the 60 s timeout

Every `fused.runPython` call runs `main()` in a fresh subprocess the server
**kills at 60 s** (`DEFAULT_TIMEOUT` in `fused_render/executor.py`). On timeout
the call rejects with a `TimeoutError` — uncaught, that becomes the red overlay.
There is no per-call override, so design around it:

- **Precompute and cache to disk.** Do the expensive work once, write the result next to the script (`.json`/`.parquet`), and have `main()` return the cached bytes when they're fresh (compare mtimes) — recompute only when the input changed.
- **Chunk / paginate.** Slice the work so each call stays well under 60 s, pass an `offset`/`page` param, and accumulate in JS across several calls. This also keeps the UI responsive.
- **Move the heavy job out of band.** For a genuinely long build, run it as a separate process that writes an output file, and have the view read the finished result.
- **Cut per-call cost.** Each call re-pays import cost (pandas ≈ 1 s); import lazily inside `main`, and debounce sliders (~150 ms) so a drag doesn't spawn a subprocess per tick.

State does not survive any of this: a folder that opts into `[tool.fused-render.app]`
with `main = "x.py"` gets a warm worker that keeps imports and globals alive
between calls, but it's still reaped after `idle_timeout_s` idle seconds
(default 900s / 15 min), reached from the page via `fused.daemon.run(params)`.
A folder that genuinely needs to keep running past that — a poll loop, a held
connection, a tray or menu-bar presence — wants its own resident daemon
(`daemon = "x.py"`) instead: see **`fused-render-background-apps`**.

## Show it in the download manager (`fused.trackJob`)

The out-of-band pattern leaves a hole: a detached worker pulling an 8 GB model
keeps running when the user browses to another file, and the shell replaces your
page's frame the moment they do — so your in-page progress bar disappears and the
download becomes invisible. Report it instead, and the shell shows it in the
**download manager** for as long as it runs, whatever page the user is on:

```js
const job = fused.trackJob({
  title: "FLUX.2-klein-4B",      // required — what is happening, in a few words
  kind: "download",              // "download" | "task"
  unit: "bytes",                 // "bytes" formats done/total as 1.2 / 8.1 GB
  cancellable: true,             // shows a ✕; omit if you cannot honor it
});

// on each poll tick, from whatever your worker wrote:
job.update({ done: s.bytes, total: s.size, detail: "transformer.gguf" });
if (job.cancelRequested) await stopTheWorker();   // the manager's ✕ was clicked

job.finish("Downloaded");        // or job.fail(err) / job.cancelled()
```

- **It cannot break your page.** Every method is fire-and-forget and never rejects; a failed report is swallowed. `await job.update(...)` only if you want to read `cancelRequested` at that exact point — the property is also readable synchronously between ticks.
- **Cancel is a request you honor**, not something the shell can do — it has no idea which process is doing the work. The ✕ sets a flag; your poll loop notices it, stops the worker, and reports `job.cancelled()`. If you cannot stop the work, leave `cancellable` off and no ✕ is offered.
- **Omit `total` while you don't know it.** A job with no total draws a travelling "indeterminate" bar, which is the honest picture; a total of `0` is treated the same way rather than painted as complete.
- **Report the finish.** Without a terminal call the row goes "stalled" after 30 s and says the page that started it was closed — accurate if that is what happened, misleading if the work just ended. Report `finish`/`fail`/`cancelled` on every exit path of your poll loop.
- **A step longer than 30 s needs a tick of its own.** The stale window does not care that your work is fine, only that nothing has reported — so a row wrapped around a single long `await` (one `fused.ai.text()` call, one slow `runPython`) says "No longer reporting" partway through and then succeeds. Beat it: `const beat = setInterval(() => job.update({}), 10_000)` before the await, `clearInterval(beat)` in a `finally`. An empty `update({})` is exactly "still here" — it carries no fields, so it cannot move the bar or overwrite your detail.
- **One job per user-meaningful operation**, not per file: aggregate a multi-file download into one row (sum the bytes) and put the current filename in `detail`.
- Reuse a **stable `id`** (`fused.trackJob({id: "flux:" + jobId, ...})`) when a page can be reloaded mid-work — the reopened page re-attaches to the existing row instead of opening a second one.
- **Exports fine.** `fused.trackJob` is a no-op on a hosted page, so unlike `fused.ai` it does not block export.

`fused.watchJob(id).watch(cb)` is the read side: it streams a row's updates to
your page — used to show elapsed seconds for a `fused.capture` recording, or to
follow a model download the runtime started for you. A watched row's `job.state`
can also read `"waiting"` — there's no way to set it yourself (`trackJob`'s
methods only ever move a row to running or one of the terminal states); it
shows up on a row you're only watching, such as an install parked on the
build-consent prompt, and means the row is stalled on a question only the
user can answer, not stuck and not an error.

## Report from the WORKER, not only the page — this is the one that bites

The shell replaces your page's frame on every navigation, so a page-only reporter
freezes the row at its last number and the manager declares it stalled ~30 s later
while the download carries on. Your detached worker outlives the page, so let it
report too. It cannot `import fused_render` (it runs in its own venv), but the
endpoint is plain JSON on the origin every spawned child inherits:

```python
import json, os, urllib.error, urllib.request

class JobReport:
    """Best-effort. Never raises: reporting must not break the work."""
    def __init__(self, job_id, title):
        self.url = (os.environ.get("FUSED_RENDER_ORIGIN") or "").rstrip("/") + "/api/jobs"
        self.id, self.enabled, self.cancel_requested = job_id, self.url.startswith("http"), False
        if self.enabled:
            self.post(title=title, kind="download", state="running", cancellable=True)

    def post(self, **fields):
        if not self.enabled:
            return None
        fields["id"] = self.id
        req = urllib.request.Request(self.url, data=json.dumps(fields).encode(),
                                     headers={"Content-Type": "application/json", "X-Fused": "1"})
        try:
            with urllib.request.urlopen(req, timeout=3) as r:
                record = json.loads(r.read().decode())
        except (urllib.error.URLError, OSError, ValueError):
            return None
        if isinstance(record, dict) and record.get("cancel_requested"):
            self.cancel_requested = True   # the manager's ✕ — act on it here
        return record
```

Use the **same job id** on both sides — derive it from something both know (the
job directory name, the model id) so the two reporters share ONE row instead of
opening two. Keep the page reporting as well: it is the only thing alive during
`uv run`'s first-run environment build, before your worker executes a line.
Rate-limit the worker's posts to ~1/s — a download callback fires per chunk.

**The worker is also the only thing that can honor a cancel once the page is
gone.** `cancel_requested` comes back in the reply to the tick you were already
sending; check it in your progress callback and stop. If your long step is an
opaque subprocess (`uv sync`), run a small daemon thread that posts a heartbeat,
reads the flag, and kills the child — otherwise the ✕ does nothing for the
minutes that matter most.

## Pitfalls

- Never calling `finish`/`fail`/`cancelled` → the row goes "stalled" after 30 s and tells the user the page was closed when the work simply ended.
- Reporting progress ONLY from the page → the row freezes the moment the user opens another file.
- A page-started job under `_preview=1` → every card on a listing starts the work at once. Gate boot on the preview flag (**`fused-render-authoring`**).
- One row per file in a multi-file download → a manager full of rows for one user-meaningful operation.

Related: **`fused-render-authoring`** (the `runPython` contract these limits
apply to), **`fused-render-background-apps`** (work that must outlive every
page), **`fused-render-capture`** (recordings, which are job rows too),
**`fused-render-ai`** (model downloads, which report themselves).
