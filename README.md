# fused-render

**[Download for macOS, Windows, and Linux →](https://render.fused.io)**

A local file explorer for your whole computer. Browse any directory in the
browser, preview files, and author your own interactive views: any `.html`
file you open gets a tiny injected runtime that can call a Python `main()`
function and sync its state to the URL.

Runs entirely on `127.0.0.1`. No accounts, no cloud, no sandboxing — your own
machine, your own trusted code. See `SPEC.md` / `ARCHITECTURE.md` / `DECISIONS.md`
for the full design.

![fused-render: right-click a file in Explorer, pick "Open with" → fused-render, and it opens in the browser](core_apps/learn/assets/open_with_right_click.gif)

Right-click a file in Explorer → **Open with** → fused-render, and it opens in
your browser. See [Windows: Explorer "Open with"](#windows-explorer-open-with)
to enable it.

## Install

**macOS app** — the packaged FusedRender.app (bundles the `fused` CLI and
rclone; no Python required):

```
brew install --cask fusedio/tap/fused-render
```

or download the DMG from the [releases page](https://github.com/fusedio/fused-render/releases).

**Python package** — each release also attaches a wheel (see the release
notes for its URL): `pip install <wheel-url>`. From a source checkout:

```
pip install -e .
```

Requires Python 3.10+. Installs FastAPI, uvicorn, and pyarrow (used by the
built-in parquet preview).

Building from source and the local dev loop live in
[CONTRIBUTING.md](CONTRIBUTING.md).

## Run

```
fused-render
```

Opens a browser tab at `http://127.0.0.1:1777/`, starting in your home
directory. Useful flags:

```
fused-render --start-dir ~/data --port 9000 --no-browser
```

`--start-dir` only sets the initial location — the whole filesystem stays
browsable from there.

### Windows: Explorer "Open with"

```
fused-render-open --register
```

Registers fused-render into Explorer's right-click "Open with" menu (HKCU
only, no admin) for every format it previews — double-clicking a file, or
picking "fused-render" from Open With, reuses a running server or starts one
detached, then opens the file. `fused-render-open --unregister`
removes the associations.

## Deploy to a hosted URL

A renderable page's preview header has a **Deploy** button: it exports the page
to a self-contained bundle and publishes it as a public URL through the `fused`
CLI. fused-render itself hosts nothing and mints no URLs — it runs the CLI on
your behalf. The link is public and needs no sign-in to view — an unguessable
random token by default, or a custom name you pick for a memorable URL.
Redeploying the same page keeps the **same URL**; a **Change link** action mints
a new one (taking the old down), and revoking takes it down.

Signing in and first-time setup happen in the app, no terminal required: sign in
to Fused once, then set up a **managed hosted environment** in one click — the
default deploy target. From the same account view you can list the pages you've
deployed, revoke any of them, and inspect the recent errors behind a hosted
page's failures — the traceback and output the local error overlay shows you here.

The packaged macOS app ships the `fused` CLI built in, so there's nothing to
install. On a `pip` install, add it with the extra:
`pip install "fused-render[fused]"`. Self-hosted AWS environments work as a
deploy target too, provisioned through the `fused` CLI in a terminal. Deploying
stays off until you enable it in **Preferences** (see
[Preferences](docs/usage.md#preferences)). For scripting the export yourself,
see [Export for hosted serving](docs/usage.md#export-for-hosted-serving).

## Authoring model

Any `.py` file is a runnable target as long as it defines a `main()`
function:

```python
# sine.py
import math

def main(n: int = 80, freq: float = 1.0):
    return {"points": [[i / n, math.sin(2 * math.pi * freq * i / n)] for i in range(n)]}
```

Any `.html` file can call it and bind the result to the URL:

```html
<input id="freq" type="range" min="0.1" max="5" step="0.1" />
<script>
  const slider = document.getElementById("freq");
  slider.addEventListener("input", () => fused.params.set("freq", slider.value));
  fused.params.onChange(draw);

  async function draw() {
    const freq = fused.params.get("freq") || "1.0";
    const { points } = await fused.runPython("./sine.py", { freq });
    // ...render points...
  }
  draw();
</script>
```

- `fused.runPython(pyPath, params)` — runs `main(**params)` of a local `.py`
  file in a fresh subprocess and returns its JSON result. `pyPath` may be
  relative (to the HTML file) or absolute.
- `fused.params` — a string-only key/value store synced into the browser
  URL (`get`, `getAll`, `set`, `onChange`). Refreshing or bookmarking a view
  reproduces its exact state.
- `fused.ai(prompt, opts?)` — ask an AI model through the `claude` (Claude
  Code) CLI on your machine; resolves with `{text, model, usage}`. Pass a
  Hugging Face repo id as `model` (`"Qwen/Qwen3-4B-Instruct-2507"`) and the same
  call runs a model **locally** instead — the slash is what tells them apart.
  Local chat works on every platform: MLX on Apple Silicon, PyTorch on Windows,
  Linux and Intel Macs, picked for you. The AI Models page suggests models that
  suit whichever one your machine got.
  Local calls also take `history` (prior `{role, content}` turns, for a
  conversation rather than one question) and can be stopped mid-answer with
  `fused.ai.cancel()`.
- `fused.ai.image({prompt, ...})` — text to image, locally; resolves with the
  PNG's path, a ready-made URL to point an `<img>` at, and the seed used (one is
  chosen for you if you don't pass one, so a render is always repeatable). It
  runs for minutes, so `onProgress` fires per denoising step and the download
  manager's ✕ really stops it.
- `fused.ai.transcribe({path, ...})` — speech to text, locally: point it at an
  audio or video file on this machine and it resolves with the words plus the
  `{start, end, text}` segments that carry their timestamps. `task` picks
  between transcribing in the original language and translating to English; the
  language is auto-detected unless you name one. It runs for minutes, so
  `onProgress` fires with seconds of audio and the download manager's ✕ really
  stops it, and the transcript is written to a file so it outlives the tab.
- `fused.ai.models.list() / load(id) / unload(id)` — what this machine is
  holding in memory and what it costs. See the **AI Models** page
  ([docs](docs/usage.md#ai-models)).
- `fused.trackJob(spec)` — report work **your page is doing** that runs longer
  than the page itself (exporting a few thousand tiles, converting a folder) to
  the **download manager** in the bottom-right corner, so it stays visible after
  you browse away:

  ```js
  const job = fused.trackJob({ title: "Export tiles", kind: "export",
                          unit: "items", cancellable: true });
  job.update({ done: 1200, total: 4096, detail: "zoom 12" });
  if (job.cancelRequested) stopTheWork();   // the manager's ✕ asked
  job.finish("Exported");                   // or .fail(err) / .cancelled()
  ```

  Reporting never throws and never blocks: a failed report cannot break the work
  it describes. Your page is the only thing that can stop its own work, so the
  ✕ here is a *request* you honour by checking `cancelRequested`.
- `fused.watchJob(id)` — the other half: watch work the **server** is doing
  (`fused.ai.image()`, `fused.ai.transcribe()` and `fused.ai.models.load()` all
  hand you an id) with
  `.onUpdate(cb)`, `.get()` and a `.cancel()` that really stops it — the server
  owns those processes, so its ✕ is an act rather than a request.

Built-in preview templates (parquet tables, images, text/code files) are
themselves just HTML files built on these same two primitives — open
`fused_render/templates/` to see how.

## Configuration & advanced usage

Runtime features and settings live in [docs/usage.md](docs/usage.md):

- [Execution engine](docs/usage.md#execution-engine) — built-in subprocess
  runner vs. the `fused` local compute backend (folder-level `pyproject.toml`
  dependencies in cached venvs), and `FUSED_RENDER_ENGINE`.
- [Remote storage (mounts)](docs/usage.md#remote-storage-mounts) — mount
  S3-compatible stores, Google Drive, and anything else rclone speaks, as local
  folders.
- [AI Models](docs/usage.md#ai-models) — what the Hugging Face cache
  holds on this machine, what it costs on disk, how to clear it, and a search of
  the Hub that says which results you already have, beside a short list of
  suggested models you can download.
- [Preferences](docs/usage.md#preferences) — the in-app settings panel
  (execution engine, deploy toggle, logs, template registry).
- [Export for hosted serving](docs/usage.md#export-for-hosted-serving) — the
  programmatic `POST /api/export` bundle format behind the Deploy button.

## Claude Code plugin

This repo doubles as a [Claude Code](https://code.claude.com/docs) plugin
marketplace. Installing the plugin adds skills that teach Claude how to use a fused-render
project (running the explorer, opening views by URL), author fused-render
views (the `fused.runPython` bridge, URL-synced params, file IO helpers), and
build custom preview templates.

From inside Claude Code:

```
/plugin marketplace add fusedio/fused-render
/plugin install fused-render@fused-render
```

Or from the command line:

```
claude plugin marketplace add fusedio/fused-render
claude plugin install fused-render@fused-render
```

The manifests live in `.claude-plugin/` (`marketplace.json` +
`plugin.json`); the skills themselves are under `skills/`.

Installing is optional and only affects *your own* Claude Code sessions. Chats
started from inside fused-render (the Claude and split-view templates, and app
scaffolding) already get these skills: the app assembles the same plugin under
`~/.fused-render/skill-plugin/` and loads it per session with `--plugin-dir`,
so they work on a plain wheel or DMG install with nothing cloned.
