# fused-render

A local file explorer for your whole computer. Browse any directory in the
browser, preview files, and author your own interactive views: any `.html`
file you open gets a tiny injected runtime that can call a Python `main()`
function and sync its state to the URL.

Runs entirely on `127.0.0.1`. No accounts, no cloud, no sandboxing — your own
machine, your own trusted code. See `SPEC.md` / `ARCHITECTURE.md` / `DECISIONS.md`
for the full design.

<video src="https://github.com/user-attachments/assets/abc2149a-40be-4333-a3d0-0b2489f58c5d" controls muted playsinline width="100%"></video>

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

Built-in preview templates (parquet tables, images, text/code files) are
themselves just HTML files built on these same two primitives — open
`fused_render/templates/` to see how.

See `examples_seed/sine/sine.py` + `examples_seed/sine/sine.html` for a complete working example.

## Configuration & advanced usage

Runtime features and settings live in [docs/usage.md](docs/usage.md):

- [Execution engine](docs/usage.md#execution-engine) — built-in subprocess
  runner vs. the `fused` local compute backend (PEP 723 inline requirements in
  cached venvs), and `FUSED_RENDER_ENGINE`.
- [Remote storage (mounts)](docs/usage.md#remote-storage-mounts) — mount
  S3-compatible stores, Google Drive, and anything else rclone speaks, as local
  folders.
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
