# fused-render

A local file explorer for your whole computer. Browse any directory in the
browser, preview files, and author your own interactive views: any `.html`
file you open gets a tiny injected runtime that can call a Python `main()`
function and sync its state to the URL.

Runs entirely on `127.0.0.1`. No accounts, no cloud, no sandboxing — your own
machine, your own trusted code. See `SPEC.md` / `ARCHITECTURE.md` / `DECISIONS.md`
for the full design.

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

### Building from source

A source checkout builds the React shell once before the server starts
(`cd frontend && npm install && npm run build`, Node 22) — or run
`scripts/dev.sh` for a watch + server dev loop. Wheels and the DMG build the
shell automatically at package time, so installed users never need Node.

Build the macOS app with `bash scripts/build_dmg.sh` (py2app →
`dist/FusedRender-<version>.dmg`); signing and notarization are
credential-driven — see [docs/signing.md](docs/signing.md).

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

### Execution engine

Python runs in a fresh subprocess per call through the built-in runner by
default. Opt into fused's local compute backend — which resolves PEP 723
`# /// script` inline requirements into cached venvs — with
`FUSED_RENDER_ENGINE=auto` (use it when `fused` is importable, else the builtin)
or `=fused` (require it); `pip install "fused-render[fused]"` first. Under the
fused engine a file may also expose a `@fused.udf`-decorated function or assign
`result = ...` directly instead of defining `main()`. You can also switch the
engine in **Preferences**.

## Export for hosted serving

The **Deploy** button (below) exports and publishes a page for you. For
scripting, the running server also exposes a programmatic export
(`POST /api/export`) that packs a page and its `runPython`/`rawUrl`
dependencies into a portable bundle a hosting layer can serve — see
[docs/EXPORT.md](docs/EXPORT.md) for the bundle format and rules.

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
stays off until you enable it in **Preferences**.

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

## Remote storage (mounts)

The cloud icon at the sidebar's bottom-left opens **Mounts**: remote storage —
S3-compatible object stores, Google Drive, and anything else
[rclone](https://rclone.org) speaks — mounted as local folders under
`~/.fused-render/mounts/`. Everything downstream (previews, readers, tile
servers) sees ordinary local paths.

- **No setup on macOS:** the packaged app bundles rclone itself — no
  install, nothing on PATH. Running from source, or on Linux, still needs
  rclone (`brew install rclone` / your distro's package). macOS mounts via
  the built-in NFS client — no macFUSE; Linux uses FUSE. Windows is not
  supported yet.
- **Credentials never touch fused-render** — they live in rclone's own
  config. S3-compatible remotes can be created from the page; for Google
  Drive and other sign-in backends, run `rclone config` in a terminal once.
- **Mount narrow prefixes** (`bucket/prefix`), not whole buckets — every
  folder listed inside a mount is a remote API call, and search inside a
  mount is capped for the same reason.
- **First open is slow, repeats are fast**: the first read of a large remote
  file downloads what it needs; a local cache (24h retention) makes repeat
  opens near-instant. How slow the first open is depends on the file's
  layout — cloud-optimized formats (COGs, small parquet row groups) behave
  far better than monolithic files.
- Mounts stay up until you unmount them — including across app restarts, and
  every mount is automatically remounted when the server starts.

## Preferences

The gear at the sidebar's bottom-left opens **Preferences**:

- **Execution engine** — switch `fused.runPython` between the built-in
  executor (fresh subprocess per call) and the fused engine (PEP 723 inline
  requirements in cached venvs). Applied to the next run, no restart; setting
  `FUSED_RENDER_ENGINE` pins the engine and locks the switch.
- **Deploy to Fused account** — the opt-in toggle for the preview header's
  Deploy button.
- **Logs** — the path to this run's log file, with an action to reveal it. The
  server writes this log for debugging: when something goes wrong (an "Internal
  Server Error" in the browser, or a misbehaving file-open) it has the traceback.
  Each run writes its own file in your system temp directory (the CLI also prints
  the path on startup; the packaged app reveals it from **menu bar → Open
  logs**). It's disposable — it rotates so it can't grow without bound, and
  living in temp means the OS reclaims it; set `FUSED_RENDER_LOG_DIR` to keep
  logs somewhere persistent instead.
- **Template registry** — the merged extension → templates bindings (built-in
  plus your own overrides), read-only.

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
