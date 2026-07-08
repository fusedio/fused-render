# fused-render

A local file explorer for your whole computer. Browse any directory in the
browser, preview files, and author your own interactive views: any `.html`
file you open gets a tiny injected runtime that can call a Python `main()`
function and sync its state to the URL.

Runs entirely on `127.0.0.1`. No accounts, no cloud, no sandboxing — your own
machine, your own trusted code. See `SPEC.md` / `ARCHITECTURE.md` / `DECISIONS.md`
for the full design.

## Install

```
pip install -e .
```

Requires Python 3.10+. Installs FastAPI, uvicorn, and pyarrow (used by the
built-in parquet preview).

### Shell development

The browser shell is a React + TypeScript app in `frontend/`. Its build
output (`fused_render/static/shell-dist/`) is not committed — a source
checkout needs one build before the server will start (Node 22).

The one-command dev loop (shell watch-build + python server together,
Ctrl-C stops both; extra args go to `fused-render`):

```
scripts/dev.sh                # e.g. scripts/dev.sh --port 9000
```

Edit anything under `frontend/src/` and refresh the browser — the watch
rebuilds and the server serves files per-request with no-cache. Manual
equivalent: `cd frontend && npm install && npm run build` (or `watch`).
The watch skips type checking for speed; `npm run typecheck` (or a full
`npm run build`) before committing.

Wheels and the DMG build the shell automatically at package time
(`scripts/hatch_build.py`), so end users never need node.

### macOS app (DMG)

```
bash scripts/build_dmg.sh
```

Builds a standalone `FusedRender.app` via py2app and packages it as
`dist/FusedRender-<version>.dmg`.

**Signing is credential-driven** ([docs/signing.md](docs/signing.md)):

- **No credentials (default):** ad-hoc signed — launches locally, testers
  right-click → Open on first launch. Not distributable without Gatekeeper
  warnings.
- **Developer ID cert in your keychain:** auto-detected (or named via
  `FUSED_RENDER_CODESIGN_IDENTITY`) and signed with the hardened runtime.
  Distributable, and it also stops the repeated Downloads/Desktop/Documents
  permission prompts (one stable Team ID lets macOS attribute the executor's
  subprocess file access to the app).
- **Signed + notarized + stapled:** additionally set
  `FUSED_RENDER_NOTARY_PROFILE=<stored notarytool profile>`.

```
# distributable, signed + notarized:
FUSED_RENDER_NOTARY_PROFILE=FUSED_RENDER_NOTARY bash scripts/build_dmg.sh
```

## Run

```
fused-render
```

Opens a browser tab at `http://127.0.0.1:8765/`, starting in your home
directory. Useful flags:

```
fused-render --start-dir ~/data --port 9000 --no-browser
```

`--start-dir` only sets the initial location — the whole filesystem stays
browsable from there.

### Execution engine

Python files run in a fresh subprocess per call, through the built-in runner
**by default** — whether or not the `fused` package is installed. Opt in to
fused's local compute backend with `FUSED_RENDER_ENGINE=auto` (uses it iff
`fused` is importable, else falls back to builtin) or `=fused` (require it —
fails loudly at startup if missing); `pip install "fused-render[fused]"` first
if it isn't already. Under the fused engine, PEP 723 `# /// script` inline
requirements resolve into cached venvs, and — in addition to the bare `main()`
convention below — a file may expose a `@fused.udf`-decorated function (any
name; params arrive as raw JSON types) or assign `result = ...` directly. The
active engine shows in `GET /api/config`.

## Export for hosted serving

fused-render is local-only, but the running server can pack a page into a portable
bundle that a hosting layer (the `fused` wheel) can serve:

```
curl -X POST http://127.0.0.1:8765/api/export \
  -H 'Content-Type: application/json' -H 'X-Fused: 1' \
  -d '{"page": "/abs/path/to/examples/sine.html", "out": "/abs/path/to/bundle"}'
```

Both `page` and `out` must be absolute filesystem paths (same convention as every
other endpoint — see the module docstring in `server.py`). It collects the page's
`runPython`/`rawUrl` dependencies into a self-contained bundle. Only the portable
subset of the runtime API is supported (no `writeFile`, `stat`, or live-reload).
See `docs/EXPORT.md` for the bundle format and rules.

## Deploy to a hosted URL

The shell automates the whole export → publish chain: any renderable page's
preview header has a **Deploy** button (green dot = currently deployed) opening
a modal that exports the page to a temporary bundle and publishes it through
the separately-installed `fused` CLI (`fused share create <bundle> --public`) —
fused-render itself still hosts nothing and mints no URLs.

The modal handles the whole flow:

- **The `fused` package.** Deploying needs the `fused` CLI, which may not be in
  the venv running this server. If it's missing, the modal offers a one-click
  install of the wheel pinned by this package's `[fused]` extra (Python 3.11+),
  or names the manual command: `pip install "fused-render[fused]"`. An existing
  install is found via `FUSED_RENDER_FUSED_BIN`, the server venv's `bin/`, or
  `PATH`.
- **Environment choice.** Deploy targets are the *hosted* environments from the
  fused CLI's own store (`~/.openfused/envs.json`): a managed `fused` env (the
  default) or an `aws` env whose serving plane `fused infra serve` provisioned.
  `local` envs have no serving plane and are never offered.
- **The URL.** Deploys mint a **public share link** — an opaque, unguessable
  URL shown with copy/open actions. Redeploying the same page republishes to
  the **same URL**; Revoke takes it down (deploying again restores the link).
- **What's deployed.** A per-page pointer (`~/.fused-render/deployments.json`)
  marks deployed files in the preview header, and the modal's share list
  (`fused share list`) shows every mount on the chosen environment, joined back
  to the local pages that deployed them.

Whether a given backend accepts a *page bundle* is the installed `fused` CLI's
contract (its `spec/serve/fused-render.md`): AWS serving planes build the
hosted-page artifact today; the managed backend's inline-upload bundle
classification is an upstream follow-up — until then its CLI error shows in the
modal verbatim.

## Logs

The server writes an application log so that when something goes wrong — an
"Internal Server Error" in the browser, or a right-click "Open with
FusedRender" that misbehaves — there's a traceback to look at. It records:

- **startup** — a `boot:` line (version, python, platform) every launch, plus
  the bind address / start dir;
- **every browser request** — one line per request with status + duration
  (`GET /view/… -> 200 (3 ms)`), so the log reconstructs the sequence of calls
  a page made (static-asset fetches are skipped to keep it readable);
- **every 500** — its full traceback and the request that caused it;
- **failed Python runs** and the macOS app's file-open / reopen events.

Each run writes its own file, `fused-render-<pid>.log`, in your system temp
directory (e.g. `/tmp/fused-render-4521.log`; the CLI prints the exact path on
startup, and in the packaged app **menu bar → Open logs** reveals it). A file
per process means two instances running at once (say, on different ports) never
interleave or clobber each other's logs. It's disposable diagnostic output: it
rotates at 2 MB with one backup, and living in temp means the OS reclaims it —
and a reboot gives you a fresh slate — rather than logs piling up in a permanent
directory. Set `FUSED_RENDER_LOG_DIR` to keep logs somewhere persistent instead.

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

See `examples/sine.py` + `examples/sine.html` for a complete working example.

## Claude Code plugin

This repo doubles as a [Claude Code](https://code.claude.com/docs) plugin
marketplace. Installing the plugin adds skills that teach Claude how to use a fused-render
project (running the explorer, opening views by URL), author fused-render
views (the `fused.runPython` bridge, URL-synced params, file IO helpers), and
build custom preview templates.

This is a **private repo**, so add the marketplace by its **SSH git URL**
(the `owner/repo` shorthand only works for public repos). You need SSH access
to `fusedio/fused-render` — i.e. an SSH key registered with GitHub.

From inside Claude Code:

```
/plugin marketplace add git@github.com:fusedio/fused-render.git
/plugin install fused-render@fused-render
```

Or from the command line:

```
claude plugin marketplace add git@github.com:fusedio/fused-render.git
claude plugin install fused-render@fused-render
```

The manifests live in `.claude-plugin/` (`marketplace.json` +
`plugin.json`); the skills themselves are under `skills/`.
