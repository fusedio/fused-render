---
name: fused-render-authoring
description: How to author HTML views and Python data files for fused-render (the local HTML explorer with a fused.runPython() bridge, URL-synced params, and file IO helpers). Use when creating, editing, or debugging an .html view, a .py data file, or a preview template; when a view renders blank, shows a traceback overlay, or params don't sync to the URL; or when the user mentions fused.runPython/params/readFile/writeFile or asks for "a view for <file/data>".
---

# Authoring fused-render views

fused-render is a local file explorer that renders `.html` files live in the browser and lets them call local Python for data. A "view" is usually a **pair of sibling files**: an `.html` page (UI) and a `.py` file (data). The user opens the html in the explorer; the page fetches data through Python and stores its UI state in URL params so any view is refresh-proof and bookmarkable.

## Mental model

```
.html file (rendered in an iframe)
   │  window.fused  ← injected runtime, do NOT <script src> anything for it
   │
   ├─ fused.runPython("./data.py", {limit: 50})    ← executes the .py's @fused.udf main()
   │        └─ returns a Promise of main()'s JSON return value
   │
   ├─ fused.params            ← string key/values mirrored into the browser URL
   │        └─ ?limit=50      ← refresh/bookmark restores exact view state
   │
   └─ fused.readFile / writeFile / stat / rawUrl   ← direct file IO, no Python needed
```

Three primitives — `runPython`, `params`, and the file IO helpers — are the entire API. Everything else is ordinary HTML/CSS/JS (no framework, no build step, ES2020 fine).

## The Python side: `@fused.udf` contract

A data file registers **one entry point named `main`** with the `@fused.udf` decorator. `import fused` works inside the execution sandbox automatically — nothing to install. Third-party deps are declared in a PEP 723 header (omit it for stdlib-only scripts):

```python
# /// script
# dependencies = ["pandas"]
# ///
import os

import fused
import pandas as pd

@fused.udf
def main(path: str = ".", limit: int = 50, min_size: float = 0.0):
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

- **Params arrive exactly as the JS sent them — no coercion.** Pass numbers as numbers from the page (`{limit: 50}`, not `{limit: "50"}`); URL params are strings, so convert where you read them (`parseInt`/`parseFloat`/`Number`). Annotations on `main` are documentation only.
- **Give every parameter a default** unless it is genuinely required; a missing required arg raises an ordinary `TypeError` shown to the page.
- **Declare third-party imports in a `# /// script` comment header** (see "Declaring external dependencies" below). The server's environment is never visible to your script, so an undeclared import fails even if it's installed where the server runs; stdlib-only scripts need no header.
- **Return JSON-native values only** (dict / list / str / int / float / bool / None). A DataFrame or bytes return is an error — convert first: `df.to_dict("records")`. Non-JSON scalars inside structures (datetime, Decimal, numpy types) also break serialization — stringify or cast them (`str(ts)`, `float(x)`).
- **Relative paths inside `main()` resolve next to the .py file** (the working directory is re-homed there for the call). `open("./data.csv")` next to your script just works. Module-level code runs in a temp exec dir — do file work inside `main()`.
- **Each call is a fresh subprocess.** Edits to the .py apply on the next call — but so does full import cost (pandas ≈ 1 s per call). No state survives between calls; don't cache in globals.
- **`print()` output goes to the browser console** (prefixed `[python]`) — use it freely for debugging; it cannot corrupt the result.
- **Calls time out at 30 s.** Errors reach the page as a **cleaned traceback** pointing at your script's real path and line — delivered as an `Error` whose `.message` is the traceback's last line, `.traceback` the full text, and `.stdout` any captured prints.
- A parameterless script may skip the udf entirely and set a module-level `result = {...}` instead. But a plain `def main` **without** the `@fused.udf` decorator is never called — the page gets `null` back and params are silently ignored.
- **Scripts never import from the server's environment.** Each call runs in an **openfused-managed venv** (D56), not the interpreter that launched the server — an undeclared import fails even if it's installed where the server runs. What's importable:
  - **Standard library** — always (the default venv is stdlib-only).
  - **Third-party** — only what the `# /// script` header declares. The engine resolves the header into a cached per-requirements venv (uv; first call per set takes seconds, then reused). This is identical for a `pip install -e .` checkout and the packaged `.app`.
  - **Offline (`.app`):** PEP 723 installs resolve **first** from the bundle's wheelhouse, which ships numpy, pandas, requests, duckdb, polars, matplotlib, scipy, pillow, openpyxl, shapely, geopandas (+ pyarrow) — a view built on those works with no network. Anything outside that set needs network to reach PyPI (an editable checkout always resolves from PyPI). Guard optional imports and return a clear error rather than letting a raw `ImportError` hit the overlay.

### Declaring external dependencies (PEP 723)

Third-party packages are declared **inline, in a comment header** at the top of the `.py` — the [PEP 723](https://peps.python.org/pep-0723/) `# /// script` block. There is no `requirements.txt`, no separate `pip install` step, and no reliance on the server's environment: the engine reads the header, builds a cached venv for exactly that dependency set, and runs `main()` inside it.

```python
# /// script
# dependencies = [
#     "pandas",
#     "shapely>=2.0",
#     "duckdb==1.1.3",
# ]
# ///
import fused
import pandas as pd

@fused.udf
def main(...): ...
```

- **It stays a comment block, so the file remains a runnable, importable `.py`.** Every line starts with `#`; the fence is exactly `# /// script` … `# ///`. `dependencies` is a TOML list of [PEP 508](https://peps.python.org/pep-0508/) requirement strings — bare (`"pandas"`), pinned (`"duckdb==1.1.3"`), or ranged (`"shapely>=2.0"`).
- **One venv per distinct dependency set.** The set is hashed order-independently, so two scripts declaring the same `dependencies` share a venv and install once; the first call for a new set builds it (seconds), then it's cached under `~/.openfused/venvs`. Pin consistently across files to maximize reuse — `"pandas"` and `"pandas==2.2"` count as different sets.
- **Omit the header entirely for stdlib-only scripts** — they run in the bare stdlib venv with no build step.
- **Malformed TOML** in the header surfaces as a structured error on the page, not a 500 — fix or remove the block.
- **Offline (`.app`):** these installs resolve from the bundled wheelhouse first (the package list above); anything outside it needs network.

### Keep `main()` statically checkable

Type the signature so a checker (mypy/pyright) can verify the data file on its own — each `.py` is imported standalone, so it type-checks standalone. The built-in readers model the convention: `def main(file: str, offset: int = 0, limit: int = 100) -> dict:`.

- **Params arrive as raw JSON, so annotate them with whatever JSON type the page actually sends.** `main(limit: int)` is honest when the page calls `runPython("./x.py", { limit: 50 })` with a real number; `main(ids: list[str])` is honest when it passes an array. There is **no runtime coercion** — an annotation the caller contradicts (a string passed for an `int`) is a lie the checker trusts but the runtime won't fix.
- **`fused.params` values are always strings — convert at the JS boundary, not with an annotation.** `runPython("./x.py", { limit: parseInt(fused.params.get("limit") || "50", 10) })`. Annotating the param `int` does not turn an incoming string into one.
- **Annotate the return type.** `-> dict` matches the repo; tighten to a `TypedDict` when you want the exact JSON shape documented and checked — it doubles as the contract the HTML consumes. Keep every field JSON-native (the runtime rejects anything else), so a JSON-native TypedDict is both checkable and honest.
- **Keep the module import-clean:** imports resolvable, no top-level side effects, no work at module scope — so `mypy data.py` / `pyright data.py` passes without the server running. Lazy-importing a heavy lib inside `main` (for call-cost) is fine and still checks.
- No type-checker config ships with the project; run your checker ad hoc against the file. Treat annotations as a **documentation + static-check aid**, never as runtime enforcement.

## The HTML side: `window.fused` API

The runtime is injected automatically when the explorer renders the page. Never add a script tag for it; just use the global.

| Call | Behavior |
|---|---|
| `await fused.runPython(pyPath, params)` | Runs the `@fused.udf` `main(**params)` of the file at `pyPath` — relative to **this html file's directory**, or absolute. Params pass through as raw JSON types. Resolves with the return value; rejects with an `Error` whose `.message` is the traceback's last line (`"ZeroDivisionError: division by zero"`), plus `.traceback` (full text) and `.stdout`. |
| `fused.params.get(k)` | Current value from the URL, as a **string** (or `undefined`). |
| `fused.params.getAll()` | All non-reserved params as an object — plus `_file` (read-only) when the page was opened as a preview template, even though `_file` is otherwise a reserved key. |
| `fused.params.set(k, v)` | Writes to the URL (replaceState — no history spam). **Throws unless `v` is a string** — do `String(n)` yourself. Then fires `onChange`. |
| `fused.params.onChange(cb)` | `cb(allParams)` after every applied `set`. Returns an unsubscribe function. |
| `fused.params.get("_file")` | Read-only: the target file a **preview template** was opened for. Keys starting `_` are reserved — `set()` on them throws. |
| `await fused.readFile(path)` | File contents as **text** (UTF-8). Rejects with an `Error` on failure. Use when a view just needs the bytes as a string — no reader `.py` required. |
| `await fused.stat(path)` | Metadata object `{path, name, is_dir, size, mtime, templates}` (`templates` is the ordered mode-list array, usually irrelevant to page code). Use for size guards before reading big files, and to capture `mtime` before editing. |
| `await fused.writeFile(path, content, opts?)` | Writes UTF-8 text **atomically** (never a half-written file). `opts.expectedMtime` arms an optimistic lock: if the file changed on disk since that mtime, rejects with an error whose `.type === "conflict"` (and `.mtime` = current on-disk value) instead of clobbering. Omit it to write unconditionally — also how you create a new file. Resolves with a fresh stat object; keep its `.mtime` to re-arm the lock for the next save. |
| `fused.rawUrl(path)` | **Sync**, returns a URL string serving the file's raw bytes. This is for embedding — `<img src>`, `<video src>`, `<embed>`, download links — where you need a URL, not text. |

Notes:
- **URL params are strings only, always** (`fused.params`). But `runPython` params are **typed JSON** — parse at the boundary: `const limit = parseInt(fused.params.get("limit") || "50", 10)` then `runPython("./x.py", { limit })`. Passing the raw string means `main` receives a string.
- Uncaught `runPython` rejections auto-show a red traceback overlay — good default for debugging; catch the rejection yourself when you want custom error UI.
- Concurrent `runPython` calls are fine; responses can arrive out of order — guard with a request counter if a stale response could overwrite a fresh one.
- **Reach the filesystem only through these helpers**, never by fetching the server's `/api/fs/*` endpoints yourself — the helpers are the stable contract and carry required headers (writes are rejected without them).
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
  else throw err;
}
```

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
      const limit = parseInt(fused.params.get("limit") || "20", 10);  // URL wins; parse at the boundary
      limitEl.value = String(limit);                      // reflect state INTO controls
      out.textContent = "Loading…";
      try {
        const data = await fused.runPython("./largest.py", { limit });  // typed param
        out.innerHTML = renderTable(data.entries);        // author's own rendering
      } catch (err) {
        out.textContent = err.message;                    // or rethrow for the overlay
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
- Types are converted once, where the param is read — everything downstream (control, `runPython`) gets the right type.

Style: views render inside a dark-themed explorer. Match it (dark background, light text) unless the user wants otherwise; there is no imposed CSS — the iframe is a blank canvas.

## Preview templates (views for a file format)

A template is the same kind of html file, but the explorer opens it *for* a target file and hands the path over as the read-only `_file` param:

```js
const file = fused.params.get("_file");
if (!file) { /* show "no file selected" state */ }
const offset = parseInt(fused.params.get("offset") || "0", 10);
const page = await fused.runPython("./my_reader.py", { file, offset });
```

A reader `.py` is only needed when Python adds value (parsing parquet/xlsx, paging, aggregation). Text formats can skip it entirely — `fused.stat` for a size guard, then `fused.readFile(file)` and render in JS (the markdown/JSON/code templates work this way); media formats just point a tag at `fused.rawUrl(file)`.

Ship the reader `.py` next to the template html and call it with a relative path. Paging/sort/filter state goes in normal params (`offset`, `sort` …) exactly like any view. Built-in templates live one folder per template under `fused_render/templates/<name>/` and follow this pattern (see `templates/table/template.html` + `templates/table/reader.py` for a worked example); each extension maps to an **ordered list of mode names** (first = default) in the `TEMPLATES` dict in `fused_render/server.py`. **User-owned** templates that override, reorder, or extend that list live under `~/.fused-render/` and are bound via `registry.json` — layout, the mode-list/registry grammar, and registration are covered by the `fused-render-custom-templates` skill (this skill still owns how the html/py themselves are written).

## Testing in the browser: URL paths & modes

Verify a view by opening it in a real browser against the running server — do not rely on reading the files alone. Start the server (`fused-render --port 8765 --no-browser` keeps it from stealing focus) and open one of these on `http://127.0.0.1:<port>`:

| Path | What it renders | Use it to |
|---|---|---|
| `/` | The explorer at `start_dir` — file listing with chrome. | Browse to a file by clicking. |
| `/embed/<abs-path-without-leading-slash>` | **Embed mode**: the page chrome-free (no sidebar/breadcrumb/header). | **The default way to open and test a view** — you see just the view itself. |
| `/view/<abs-path-without-leading-slash>` | **Full-shell mode**: the same page inside the explorer shell — sidebar, breadcrumb, preview header — with your page in an iframe. | Check how the view sits inside the explorer chrome, or when browsing. |

**Default to embed.** When you open a link to test a view or show it to the user, use `/embed/` — it renders the view alone, which is what you're iterating on. Reach for `/view/` only to inspect the surrounding chrome or when the user is browsing.

Path encoding: the fs path rides in the URL after the prefix with its **leading slash dropped** and each segment URL-encoded. `/Users/me/proj/dash.html` → `http://127.0.0.1:8765/embed/Users/me/proj/dash.html`. A space becomes `%20`, etc.

**View vs embed** is a fixed page-load mode (the prefix picks it; it cannot toggle without a full navigation). Both serve the same shell and route identically — embed just hides chrome. Params sync the same way in both; in nested embeds, param sync stops at each embed shell boundary so a tab's params stay tab-independent.

**Preview templates** open at the target file's path (`/embed/<abs path to the data file>`) — the shell resolves the template by extension and hands it the file via the read-only `_file` param. To test a template's html directly, open it and pass the target yourself: `/embed/<abs path to template>.html?_file=<abs target path>`.

**API endpoints** (`/api/config`, `/api/fs/stat|list|raw|events`, `/api/fs/write`, `/api/run`) back the runtime — reach them only through the `fused.*` helpers, never by hand (see the note above). They're listed here only so you recognize them in the network tab while debugging.

Sanity loop: page renders → interact with a control → URL query updates → hard refresh → identical view. Python errors appear as the red overlay (with full traceback) and `print()` output in the browser console (prefixed `[python]`).

## Long-running work and the 30 s timeout

Every `fused.runPython` call runs `main()` in a fresh subprocess that the backend **kills at 30 s** (`timeout_seconds` on the `LocalPythonComputeBackend` in `fused_render/engine.py`). On timeout the call rejects with an error whose `.message` is `Execution timed out after 30s` — which, uncaught, becomes the red overlay. The `/api/run` route does not expose a per-call override, so you cannot raise the limit from the page; design around it instead:

- **Precompute and cache to disk.** Do the expensive work once, write the result next to the script (`.json`/`.parquet`), and have `main()` return the cached bytes when they're fresh (compare mtimes) — recompute only when the input changed. Reading a cached file is near-instant.
- **Chunk / paginate.** Slice the work so each call stays well under 30 s, pass an `offset`/`page` param, and accumulate results in JS across several `runPython` calls. This also keeps the UI responsive.
- **Move the heavy job out of band.** For a genuinely long build, run it as a separate process/script that writes an output file, and have the view just `fused.readFile`/`runPython` the finished result.
- **Cut per-call cost.** Each call re-pays import cost (pandas ≈ 1 s); import lazily inside `main`, and debounce sliders (~150 ms) so a drag doesn't spawn a subprocess per tick.

Escape hatch: because fused-render runs your own trusted code on your own machine, you *can* raise the `timeout_seconds=30` argument to `LocalPythonComputeBackend(...)` in `fused_render/engine.py`'s `get_backend()` — but that's editing the package, applies globally, and lets any view hang a worker that long. Prefer the caching/chunking patterns; reach for the constant only for a deliberate, local one-off.

## Pitfalls checklist

- `fused.params.set("n", 5)` → **throws** (number). Use `String(5)`. (URL params are strings; `runPython` params are the opposite — typed.)
- Passing URL-param strings straight into `runPython` (`{limit: fused.params.get("limit")}`) → `main` receives `"50"` (string) and comparisons silently misbehave. Parse at the boundary; annotations no longer coerce.
- Forgetting `@fused.udf` on `main` → the script runs top-to-bottom, `main` is never called, the page gets `null`, params silently ignored. No error — check the decorator first when a view gets nothing back.
- Importing a third-party package without a `# /// script` dependencies header → `ModuleNotFoundError`, even if the package is installed where the server runs (scripts get their own venv).
- Reading `input.value` inside `draw()` instead of `fused.params.get()` → refresh loses state.
- `main` returning a DataFrame / datetime / Decimal / numpy value → serialization error; convert to JSON-native first.
- Expecting module state to persist between `runPython` calls → each call is a fresh process.
- Opening files at module level → wrong cwd (temp exec dir). Do relative-path file work inside `main()`, where cwd is the script's directory.
- Adding `<script src=".../runtime.js">` manually → double-injection; the explorer injects it.
- Heavy import + slider wired without debounce → one full subprocess per tick; debounce inputs ~150 ms when `main` is slow.
- Fetching `/api/fs/raw` (or POSTing `/api/fs/write`) directly instead of using the helpers → writes get rejected (missing required header) and you're coupled to internals.
- `writeFile` without `expectedMtime` on an *existing* file → silently clobbers whatever is on disk now. Fine for new files; for edits, arm the lock and handle `.type === "conflict"`.
- Using `readFile` for an image/video and stuffing bytes into the DOM → use `fused.rawUrl(path)` as the element's `src` instead.
