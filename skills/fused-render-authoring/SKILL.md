---
name: fused-render-authoring
description: Author .html views and .py data files for fused-render — the fused.runPython() bridge, URL-synced params, file IO, preview mode. Use when creating, editing or debugging a view, a data file or a preview template; when a view renders blank or shows a traceback overlay; or on any mention of fused.runPython/params/readFile/writeFile. Routes to the neighbouring fused-render-* skills for AI, capture, jobs and theming.
---

# Authoring fused-render views

fused-render is a local file explorer that renders `.html` files live in the browser and lets them call local Python for data. A "view" is usually a **pair of sibling files**: an `.html` page (UI) and a `.py` file (data). The user opens the html in the explorer; the page fetches data through Python and stores its UI state in URL params so any view is refresh-proof and bookmarkable.

## Mental model

```
.html file (rendered in an iframe)
   │  window.fused  ← injected runtime, do NOT <script src> anything for it
   │
   ├─ fused.runPython("./data.py", {limit: "50"})   ← executes main() of the .py
   │        └─ returns a Promise of main()'s JSON return value
   │
   ├─ fused.params            ← string key/values mirrored into the browser URL
   │        └─ ?limit=50      ← refresh/bookmark restores exact view state
   │
   ├─ fused.readFile / writeFile / stat / rawUrl   ← direct file IO, no Python needed
   │
   ├─ fused.ai("...")          ← the claude CLI, or a local model; {text, model, usage}
   │
   └─ fused.trackJob({...})         ← report long work to the shell's download manager
```

**Mark every app entry page with `<meta name="fused-app" />`**, near the top of `<head>` — detection reads only the first 4 KiB of the file. The marker is **the only thing that makes a folder a fused app**: filenames, `index.html` included, declare nothing. Without it the page never reaches the /apps hub, never resolves as a folder's entry, and is never registered when rendered. The starter template carries it; add it by hand to any entry page you author, and to any app you adopt from outside `~/Fused` (that one line is the whole migration — rendering the page then registers the folder automatically).

```html
<head>
<meta charset="utf-8" />
<meta name="fused-app" />
```

**Optional app icon: `icon.svg`.** Drop an `icon.svg` (that exact lowercase name) next to the entry page and the shell picks it up with no registration: it becomes the app's glyph in the sidebar's Projects list and the browser-tab favicon on the app's page (`/apps/<folder>`) and on any of its files opened in the explorer. Skip it and the generic mark is used. Keep it a single flat shape, legible at 14–16 px, no text. Both places use it as a **mask** — only the shape's alpha matters; the sidebar colours it from the theme, the tab icon paints it yellow on the black rounded square of the fused mark — so draw solid shapes on a transparent background (any fill colour works; a filled background rectangle would render as a solid square).

Three primitives — `runPython`, `params`, and the file IO helpers — are the core API; the table below has the rest. Everything else is ordinary HTML/CSS/JS: no framework, no build step, ES2020 fine.

Four neighbouring skills own the bigger surfaces, and this one keeps only enough to know when you need them: **`fused-render-ai`** (`fused.ai` and every model call), **`fused-render-capture`** (native screen/audio/screenshot), **`fused-render-jobs`** (work longer than one call, and the download manager), **`fused-render-theming`** (light/dark).

## The Python side: `main()` contract

A data file exposes **one plain function named `main`**. No decorator, no import, no registration:

```python
def main(path: str = ".", limit: int = 50, min_size: float = 0.0):
    import os
    entries = []
    for name in os.listdir(path):
        full = os.path.join(path, name)
        if os.path.isfile(full):
            size = os.path.getsize(full)
            if size >= min_size:
                entries.append({"name": name, "size": size})
    entries.sort(key=lambda e: -e["size"])
    return {"entries": entries[:limit], "total": len(entries)}
```

Rules that matter (each has a reason):

- **Type-annotate parameters.** Params arrive as strings (they live in URLs). Annotations drive coercion: `limit: int` receives `int("50")`; `bool` accepts `"true"/"1"/"yes"/"on"`. Unannotated args get the raw string — a classic source of `"50" < 10` bugs.
- **Give every parameter a default** unless it is genuinely required; missing required args become an error shown to the page.
- **Return JSON-native values only** (dict / list / str / int / float / bool / None). A DataFrame or bytes return is an error — convert first: `df.to_dict("records")`. Non-JSON scalars inside structures (datetime, Decimal, numpy types) also break serialization — stringify or cast them (`str(ts)`, `float(x)`).
- **Relative paths in your code resolve next to the .py file** (the working directory is set there). `open("./data.csv")` next to your script just works.
- **Each call is a fresh subprocess.** Edits to the .py apply on the next call — but so does full import cost (pandas ≈ 1 s per call). No state survives between calls; don't cache in globals.
- **`print()` output goes to the browser console** (prefixed `[python]`) — use it freely for debugging; it cannot corrupt the result.
- **Calls time out at 60 s** and errors return `{type, message, traceback}` to the page.

### Available Python libraries

Write `main()` against **stdlib plus the bundled set** below — a folder with no `pyproject.toml` runs on the app's own interpreter, which ships exactly these with no download and no first-run wait. (Dev installs get the same via `pip install -e ".[bundled]"`; the authoritative list is that extra plus the core dependencies.)

- **Data:** `numpy` `pandas` `pyarrow` `duckdb` `openpyxl` `msgpack`
- **Images:** `pillow`
- **Documents:** `python-pptx` `fpdf2` (its import name is *fpdf*)
- **Network & cloud:** `requests` `httpx` `botocore` `google-auth`
- **Logs:** `drain3`

Anything else — polars, matplotlib, scipy, geopandas, shapely, rasterio, rio-tiler, zarr, pymupdf, pikepdf, torch, sklearn, xarray, plotly — must be declared in a folder `pyproject.toml`, and costs the user a one-time install they sit and wait through. Prefer rewriting against the bundled set; declare dependencies (next section) only when it genuinely cannot do the job.

### Declaring extra dependencies: `pyproject.toml`

A `.py`'s environment is decided by **the folder it belongs to**, never by anything written in the file. Put a `pyproject.toml` at the project root and every `.py` under it shares one venv:

```toml
[project]
name = "my-view"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["xarray", "netCDF4"]

# This folder is a set of scripts, not a distribution to build and install.
[tool.uv]
package = false
```

Four things to know before you write one:

- **The declaration is the COMPLETE list.** The venv contains exactly what you name — the bundled set is *not* unioned in. Declare numpy if you import numpy, even though the app ships it.
- **It is all-or-nothing per folder.** One extra package means every import in every `.py` under that folder must be listed. That is why a folder with no manifest — which gets the whole bundled set free — is still the better default.
- **Only the project root counts.** That's the app folder, a template folder, or the *topmost* ancestor holding a `pyproject.toml`. One in a subfolder is inert; the inspector flags it.
- **First render triggers an install.** The user sees a loader while `uv sync` runs, then the run is retried automatically. Commit the `uv.lock` it writes — that's what makes the folder resolve identically elsewhere.

Adding a dependency later is just an edit: save `pyproject.toml`, re-render, and the environment is reconciled. Never run `uv sync` by hand in the folder — it would create an in-folder `.venv` that diverges from the one the app actually uses (venvs live centrally under `~/.fused-render/`).

**Per-file `# /// script` headers are not read.** A leftover block is an ordinary comment — silently ignored, not merged and not warned about. Never write one; if you see one, move its `dependencies` into the project root's `pyproject.toml` and delete it, because the packages it names are not being installed.

Versions are not pinned — each install resolves its own. When a version matters, **probe the live environment** rather than guessing: `/api/run` executes in the exact interpreter that runs page code, so a throwaway `main()` returning `importlib.metadata.version(...)` for each name answers it. A `null` version means the package is missing from *this* environment — the same result an `import` would hit.

```bash
curl -s -X POST http://127.0.0.1:1777/api/run -H 'X-Fused: 1' \
  -H 'Content-Type: application/json' \
  -d '{"py": "/tmp/probe.py", "params": {"names": "pandas,duckdb,geopandas"}}'
```

## The HTML side: `window.fused` API

The runtime is injected automatically when the explorer renders the page. Never add a script tag for it; just use the global.

| Call | Behavior |
|---|---|
| `await fused.runPython(pyPath, params, opts?)` | Runs `main(**params)` of the file at `pyPath` — relative to **this html file's directory**, or absolute. Resolves with the return value; rejects with an `Error` carrying `.type`, `.message`, `.traceback`, `.stdout`. **Stale-request cancellation is on by default**, keyed by `pyPath`: a new call for a file aborts the prior in-flight call for that same file, and the superseded promise **never settles** (its `.then`/`await` just stops, so nothing stale is drawn) — a slider drag therefore computes only the value it lands on, for free. Calls to *different* files are independent. `opts.key` regroups the channel, `opts.key: null` opts out into full concurrency (polling loops, per-tile fetches, a write that must finish), and `opts.signal` takes your own `AbortSignal` to cancel on something other than the next call. |
| `fused.params.get(k)` | Current value from the URL, as a **string** (or `undefined`). `_`-prefixed keys return `undefined`; `_file` is the one exception — read-only, the target file a preview template was opened for. |
| `fused.params.getAll()` | All non-reserved params as an object, plus `_file` when present. |
| `fused.params.set(k, v)` | Writes to the URL (replaceState — no history spam), then fires `onChange`. **Throws** on a reserved `_` key, and unless `v` is a string — do `String(n)` yourself. |
| `fused.params.onChange(cb)` | `cb(allParams)` after every applied `set`. Returns an unsubscribe function. |
| `await fused.readFile(path)` | File contents as **text** (UTF-8). Rejects with an `Error` on failure. Use when a view just needs the bytes as a string — no reader `.py` required. |
| `await fused.stat(path)` | `{path, name, is_dir, size, mtime, writable, remote, templates}`. Use it for a size guard before reading something big, to capture `mtime` before editing, to check `writable` before offering an edit UI, and to notice `remote` (a mounted remote bucket — keep reads bounded there). May also carry `template_error` (a bad registry name). |
| `await fused.writeFile(path, content, opts?)` | Writes UTF-8 text **atomically** (never a half-written file). `opts.expectedMtime` arms an optimistic lock: a file changed on disk since that mtime rejects with `.type === "conflict"` (and `.mtime` = the current value) instead of clobbering. `opts.create` writes only if the path is absent — an existing path rejects with `.type === "exists"` and nothing is written, which is how you create a file without a stat-then-write race. A read-only file rejects with `.type === "readonly"`. Resolves with a fresh stat; keep its `.mtime` to re-arm the lock. |
| `fused.rawUrl(path)` | **Sync**, returns a URL serving the file's raw bytes — for `<img src>`, `<video src>`, `<embed>`, download links. |
| `await fused.ai(prompt, opts?)` | Ask an AI model; resolves with `{text, model, usage}`. Local-only. See **"AI calls"** below. |
| `await fused.fileIndex.search({root, q, limit})` / `.query({sql, limit})` | Read the machine-wide **file index** — one folder's corpus, or one read-only SQL statement over the `files`/`dirs` views (totals, per-extension breakdowns, path matches). Both resolve with `ready: {indexed, scanning, stale, reason}`, so an empty result is never mistaken for "no matches" when the truth is "no index yet". Use this instead of walking the filesystem in Python. Details, and the Python direct-parquet reader for bulk reads: **`fused-render-index`**. |
| `await fused.capture.screen(opts)` / `.audio(opts)` / `.screenshot(opts)` / `.sources()` | Record the screen, record the microphone, grab a still — **natively**, so the result is a FILE on this machine, not a `MediaRecorder` blob. See **`fused-render-capture`**. |
| `fused.trackJob(spec)` | Report a long-running operation to the shell's **download manager**, so it stays visible after the user browses away. Returns a handle; see **`fused-render-jobs`**. Never throws, never rejects. |
| `fused.env` | `"local"` (this server) vs `"hosted"` (the exported runtime). Branch on it only if a view must behave differently when exported. |
| `fused.autoReload(enabled)` | Toggle reload-on-file-change for this page. Pass `false` from an in-page editor that manages its own saves and shouldn't reload under the user. |

Notes:
- **`_`-prefixed param names are the shell's**, and `_preview` is the one a page should read — only ever to do *less* work. See **"Reserved params and preview mode"**.
- Params are **strings only, always**. Parse numbers yourself (`parseInt(fused.params.get("limit") || "50", 10)`), JSON-encode structure yourself if you need it.
- Uncaught `runPython` rejections auto-show a red traceback overlay — a good debugging default; catch the rejection yourself when you want custom error UI.
- **Reach the filesystem only through these helpers**, never by fetching `/api/fs/*` yourself — the helpers are the stable contract and carry required headers (writes are rejected without them).
- `readFile`/`rawUrl` split: text you'll process → `readFile`; anything the browser should load itself (images, media, PDFs, download links) → `rawUrl`.

The editing pattern (used by the built-in code editor template):

```js
const st = await fused.stat(file);       // 1. arm the lock
let mtime = st.mtime;
const text = await fused.readFile(file); // 2. load
// … user edits `doc` …
try {
  const fresh = await fused.writeFile(file, doc, { expectedMtime: mtime });
  mtime = fresh.mtime;                   // 3. re-arm for the next save
} catch (err) {
  if (err.type === "conflict") { /* offer reload vs overwrite (writeFile without expectedMtime) */ }
  else if (err.type === "readonly") { /* file isn't writable — disable the save UI */ }
  else throw err;
}
```

## AI calls (`fused.ai`)

`await fused.ai(prompt, opts?)` resolves with **exactly** `{text, model, usage}` — the server normalizes, so no guarding is needed. `model` is the full id that actually ran; `usage` is `null` or `{input_tokens, output_tokens}` (Anthropic-style names — `prompt_tokens` reads `undefined`). The model id picks the destination: an id containing a `/` or ending in `.gguf` runs on **this machine**, anything else goes to the **`claude` (Claude Code) CLI** on the user's own login. Common options are `systemPrompt`, `model`, `effort` and `onChunk`.

Three things decide whether a page using it behaves:

- **It is local-only, and the exporter enforces that textually.** Any page containing the string `fused.ai(` is rejected for export (SPEC RH-11) — an `if (fused.env === "local")` guard does not help. Keep AI out of a view that must export.
- **Feed it aggregates, not the dataset.** Compute in Python, reduce to a compact summary, and hand the model that. A full table blows the token budget and drowns the signal.
- **There is no stale-cancel channel.** Calls run fully concurrent, so a double-click fires two paid calls — disable the button while one is in flight.

Rejections carry `.type`: `model_loading` (a local model is loading — `err.jobId` is the download it just started, not a failure), `ai_unavailable` (show a friendly state, not a raw overlay), `bad_request`, `ai_error`, `timeout`.

Everything else — the full options and rejection tables, `fused.ai.models.*`, `.image()`, `.video()`, `.transcribe()`, `.embed()`, `.cancel()`, calling AI from Python, and diagnosing a failing call → **`fused-render-ai`**.

## The canonical wiring pattern

Every interactive view is the same loop: **params are the state; controls write params; `onChange` re-renders.** Never store view state only in JS variables — put it in params, so refresh and bookmarks reproduce the view.

```html
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Largest files</title></head>
<body>
  <label>limit <input id="limit" type="range" min="5" max="100"></label>
  <div id="out">Loading…</div>
  <script>
    const limitEl = document.getElementById("limit");
    const out = document.getElementById("out");

    async function draw() {
      const limit = fused.params.get("limit") || "20";   // URL wins; default only when absent
      limitEl.value = limit;                              // reflect state INTO controls
      out.textContent = "Loading…";
      try {
        // Dragging the slider supersedes stale in-flight runs by default (keyed
        // by pyPath) — only the value the slider lands on is computed and drawn.
        const data = await fused.runPython("./largest.py", { limit });
        out.innerHTML = renderTable(data.entries);        // author's own rendering
      } catch (err) {
        out.textContent = `${err.type}: ${err.message}`;  // or rethrow for the overlay
      }
    }

    limitEl.addEventListener("input", () => fused.params.set("limit", limitEl.value));
    fused.params.onChange(draw);   // set() above triggers this
    draw();                        // initial render reads URL state
  </script>
</body>
</html>
```

Why this shape:
- `draw()` reads **params, not the control**, so a bookmarked/refreshed URL renders identically before any interaction.
- The control writes the param and nothing else; `onChange` is the single re-render path — no double-render logic, no drift between URL and UI.
- Values passed to `runPython` can stay strings; annotations on `main` coerce them.

## Reserved params and preview mode

### The `_` namespace is the shell's

**Every query key starting with `_` belongs to the shell, not to your page.** The runtime enforces it: `set()` throws, `get()` returns `undefined`, `getAll()` filters them out — `_file` being the single readable exception. So:

- **Name page params plainly** (`limit`, `sort`, `offset`, `theme`). The shell's set — `_file`, `_mode`, `_preview`, `_nofocus`, `_layout`, `_side`, `_panel`, `_tab`, `_listing`, `_render`, `_rev` — grows without notice, and squatting on one means the shell overwrites your state or you overwrite the shell's.
- **Read `_` params only through `fused.params`.** Reaching around it with `new URLSearchParams(location.search)` reads a value whose meaning is the shell's and can change under you. `_preview` is the one exception, below.
- **Write the URL only through `fused.params.set`.** A `history.replaceState` built from your own params drops the shell's `_` keys and breaks the pane or tab the page is mounted in.

### `_preview=1`: this render is a picture of the page, not a use of it

The shell renders pages nobody asked to *open*: `/apps` cards, listing-pane peeks, hover previews. Those frames are stamped `_preview=1` (usually with `_nofocus=1`), they mount and unmount as the pointer moves, and **many boot at once on a home or listing page** for apps the user has not opened and may never open. A page that boots identically in a preview turns that listing into N simultaneous cold starts — the most common way an app folder makes the home page unusable.

**Read the flag at boot and return early**, rendering something cheap and static — a title, a cached thumbnail, an inert placeholder. Under preview, start none of: `runPython` calls that import something heavy, scan a directory or hit the network; model loads and downloads; daemon starts; `fused.capture.*`, `writeFile`, `trackJob`; polling loops, `setInterval`, websockets, EventSource; anything that records an "open", mutates state, or unpacks on first use.

Read it by climbing, because the flag is **inherited** — your page may be framed by a template that was the one stamped, and only the ancestor's URL carries it (this mirrors the runtime's own `selfOrAncestorHasFlag`):

```js
function isPreview() {
  const has = (s) => {
    try { return new URLSearchParams(String(s).replace(/^\?/, "")).get("_preview") === "1"; }
    catch (e) { return false; }
  };
  if (has(location.search)) return true;
  try {
    let w = window;
    while (w.parent && w.parent !== w) {
      w = w.parent;
      if (has(w.location.search)) return true;
    }
  } catch (e) { /* cross-origin ancestor: the climb ends here */ }
  return false;
}

const PREVIEW = isPreview();

async function boot() {
  if (PREVIEW) return renderPlaceholder();   // synchronous, no Python, no network
  await loadEverything();                    // the real thing, only on a real open
}
boot();
```

Two more rules:

- **Forward the stamps to frames you mount yourself**: `frame.src = url + "?_preview=1&_nofocus=1"` when `PREVIEW`. Otherwise the inner page reads a clean URL and does the work you just skipped.
- **A preview that *can* show real content shows a cached one, never computes one.** Ask for the already-extracted artifact and fall back to the placeholder when it is absent; never let a peek be the thing that populates the cache.

The runtime covers two of these for you — `fused.daemon.*` rejects in a preview frame, and a preview never writes the shell's URL. **Everything else — your Python calls, AI calls, timers, fetches — runs exactly as it would in a real open unless you gate it.** `fused_render/templates/fusedapp/template.html` is the worked example.

## Style and theming

There is no imposed CSS — the iframe is a blank canvas, and nothing is written into your document by default. But the explorer around it follows the OS light/dark preference with a Light/Dark override in Preferences, so a hardcoded palette will sooner or later sit inside the opposite one. The quickest correct answer is `data-fused-theme="shell"` on your `<html>`: the runtime then writes `data-theme="light"`/`"dark"` on that element before your stylesheet is parsed and keeps it in step, so you author two `:root` token blocks and nothing else.

The four strategies, when each is right, and the two rules that make a second palette work (every colour from a token; colours handed to canvas/charts/maplibre must be re-read on change) → **`fused-render-theming`**.

## Preview templates (views for a file format)

A template is the same kind of html file, but the explorer opens it *for* a target file and hands the path over as the read-only `_file` param:

```js
const file = fused.params.get("_file");
if (!file) { /* show "no file selected" state */ }
const page = await fused.runPython("./my_reader.py", { file, offset: fused.params.get("offset") || "0" });
```

A reader `.py` is only needed when Python adds value (parsing parquet/xlsx, paging, aggregation). Text formats can skip it entirely — `fused.stat` for a size guard, then `fused.readFile(file)` and render in JS (the markdown/JSON/code templates work this way); media formats just point a tag at `fused.rawUrl(file)`.

Ship the reader `.py` next to the template html and call it with a relative path. Paging/sort/filter state goes in normal params (`offset`, `sort` …) exactly like any view. Built-in templates live one folder per template under `fused_render/templates/<name>/` — see `templates/xlsx/template.html` + `templates/xlsx/reader.py` — and each extension maps to an **ordered list of mode names** (first = default) in `fused_render/templates/registry.json`. **User-owned** templates that override, reorder or extend that list live under `~/.fused-render/`; the registry grammar and registration are covered by **`fused-render-custom-templates`** (this skill still owns how the html/py themselves are written).

## Testing in the browser: URL paths & modes

Verify a view by opening it in a real browser against the running server — do not rely on reading the files alone. Start the server (`fused-render --port 1777 --no-browser` keeps it from stealing focus) and open one of these on `http://127.0.0.1:<port>`:

| Path | What it renders | Use it to |
|---|---|---|
| `/` | Redirects to `/apps` (the app hub); `/explorer` is the file-explorer homepage. | Browse to a file by clicking. |
| `/explorer/embed/<abs-path-without-leading-slash>` | **Embed mode**: the page chrome-free (no sidebar/breadcrumb/header). | **The default way to open and test a view** — you see just the view itself. |
| `/explorer/view/<abs-path-without-leading-slash>` | **Full-shell mode**: the same page inside the explorer shell, your page in an iframe. | Check how the view sits inside the chrome, or when browsing. |

**Default to embed.** When you open a link to test a view or show it to the user, use `/explorer/embed/` — it renders the view alone, which is what you're iterating on. Reach for `/explorer/view/` only to inspect the surrounding chrome or when the user is browsing.

Path encoding: the fs path rides in the URL after the prefix with its **leading slash dropped** and each segment URL-encoded. `/Users/me/proj/dash.html` → `http://127.0.0.1:1777/explorer/embed/Users/me/proj/dash.html`. A space becomes `%20`, etc.

**View vs embed** is a fixed page-load mode (the prefix picks it; it cannot toggle without a full navigation). Both serve the same shell and route identically — embed just hides chrome. Params sync the same way in both; in nested embeds, param sync stops at each embed shell boundary so a tab's params stay tab-independent.

**Preview templates** open at the *target file's* path (`/explorer/embed/<abs path to the data file>`) — the shell resolves the template by extension and hands it the file. To test a template's html directly, open it and pass the target yourself: `/explorer/embed/<abs path to template>.html?_file=<abs target path>`.

Sanity loop: page renders → interact with a control → URL query updates → hard refresh → identical view. Python errors appear as the red overlay (with full traceback) and `print()` output in the browser console (prefixed `[python]`).

## Verifying your work: the call log

The overlay and the browser console only exist while someone is looking at the
page. Every API call a page makes is also **recorded** — so after the page has
been opened you can check what actually happened, from a terminal, without a
browser:

```
fused-render calls --page /abs/path/to/page.html --since 15m
fused-render calls --failed --since 1h        # only what broke
fused-render calls --json                     # digest as JSON
fused-render calls --follow --page <page>      # block until the next calls land
```

Read the digest, not the raw records — it is a per-target rollup (count, p50,
p95, errors) plus any failures in full, with each failure's traceback and the
exact params that produced it.

**You cannot render the page yourself** — nothing executes its JavaScript from
a terminal. So the loop is: write the files → test the `.py` directly if you
want (`fused.runPython`'s target is just a `main()`) → ask the user to open the
page → read the log. `--follow` makes that one round trip instead of two.

What the record count tells you, before you read anything else:

| What you see | What it means |
|---|---|
| calls, all `ok` | It works. Report the timings. |
| **zero records** | The page never called Python — its **JS** failed first. Look for a `page error` in the output: that is `window.onerror`, with the message and line number. |
| one `error` | Python raised. The traceback and params are in the record. |
| far more calls than interactions | A render loop — usually an `onChange` handler that calls `params.set` without a guard, so each write re-triggers itself. |
| a high `stale` count | The page is issuing calls it throws away (superseded by the next one). Normal for a slider drag; suspicious otherwise. |

The same data is in the **Calls** view mode on any page that has records
(charts + the per-target table), and the raw store is JSONL under
`~/.fused-render/logs/<app>/` (one directory per app; whole-store queries glob `logs/*/*.calls.jsonl`) if you want to `jq` it. Parameters are recorded by
default, so treat the log as containing whatever your page passes around.

## Native capture (`fused.capture`)

`fused.capture.screen()`, `.audio()` and `.screenshot()` record or grab **natively**, so the result is a file this machine owns — the path is known before a recording ends (it feeds `fused.ai.transcribe({path})` directly) and the recording survives your page being navigated away from. Call `fused.capture.sources()` first and draw your UI off the answer: it never prompts, and what is available differs per platform.

The per-platform matrix, the recording handle, and the rejection types → **`fused-render-capture`**.

## Long-running work and the 60 s timeout

Every `fused.runPython` call runs `main()` in a fresh subprocess the server **kills at 60 s** (`DEFAULT_TIMEOUT` in `fused_render/executor.py`), and there is no per-call override — an uncaught timeout becomes the red overlay. Design around it: precompute and cache to disk, chunk behind an `offset` param, move a genuinely long build out of band into a separate process, and cut per-call import cost.

Work that outlives one call needs to be *visible* once the user browses away, because the shell replaces your page's frame: report it to the download manager with `fused.trackJob`, from the detached worker as well as from the page. The strategies, the job API, and the worker-side reporter → **`fused-render-jobs`**.

Nothing here holds state indefinitely — even `fused.engine(py)`, the warm variant of `runPython`, idle-retires its worker after 15 minutes. A folder that must keep running past that wants a resident daemon: **`fused-render-background-apps`**.

## Pitfalls checklist

- `fused.params.set("n", 5)` → **throws** (number). Use `String(5)`.
- Reading `input.value` inside `draw()` instead of `fused.params.get()` → refresh loses state.
- Naming a page param with a leading underscore → `set()` throws, and routing around it collides with a shell param.
- Building the URL query yourself (`history.replaceState`) → drops the shell's `_layout`/`_side`/`_file` and breaks the pane. Use `fused.params.set`.
- Booting the full app under `_preview=1` → every card on a listing cold-starts at once for apps nobody opened. Gate boot on the flag.
- Reading `_preview` off `location.search` alone → misses the inherited case where an outer frame carries the stamp. Climb the ancestors.
- Mounting your own iframe in a preview without forwarding `?_preview=1&_nofocus=1` → the inner page does all the work you just skipped.
- `main` returning a DataFrame / datetime / Decimal / numpy value → serialization error; convert to JSON-native first.
- Missing annotation on a numeric param → `main` receives `"50"` and comparisons silently misbehave.
- Expecting module state to persist between `runPython` calls → each call is a fresh process.
- Importing outside the bundled set without a folder `pyproject.toml` → `ModuleNotFoundError` in the packaged app.
- Adding `<script src=".../runtime.js">` manually → double-injection; the explorer injects it.
- Heavy import + slider wired without debounce → one full subprocess per tick; debounce ~150 ms when `main` is slow.
- Fetching `/api/fs/raw` or POSTing `/api/fs/write` directly → writes get rejected (missing header) and you're coupled to internals.
- `writeFile` without `expectedMtime` on an *existing* file → silently clobbers what is on disk now. For edits arm the lock and handle `.type === "conflict"`; for "create if absent" pass `{create: true}` and handle `.type === "exists"` rather than stat-ing first (a stat that fails for any reason other than absence otherwise reads as "go ahead and write").
- Using `readFile` for an image/video and stuffing bytes into the DOM → point the element's `src` at `fused.rawUrl(path)`.
- Walking the filesystem (`os.walk`, `glob`, `find`) to answer "how many / how big / where are all my X files" → the index already knows; use `fused.fileIndex.query()` (`fused-render-index`).
- Calling `fused.ai` on a page meant for export → the exporter rejects it textually (SPEC RH-11); a `fused.env` guard does not help.
- Reporting "I wrote the files, try it" as verification → `fused-render calls --page <page>` says whether it actually ran. Zero records means the page's JS died before reaching Python — a different bug from a failing `main()`, and they look identical without the log.
