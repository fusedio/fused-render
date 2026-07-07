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
`dist/FusedRender-<version>.dmg`. The build is ad-hoc signed — testers
right-click → Open on first launch.

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
