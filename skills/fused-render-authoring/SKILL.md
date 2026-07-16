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
   ├─ fused.runPython("./data.py", {limit: "50"})   ← executes main() of the .py
   │        └─ returns a Promise of main()'s JSON return value
   │
   ├─ fused.params            ← string key/values mirrored into the browser URL
   │        └─ ?limit=50      ← refresh/bookmark restores exact view state
   │
   └─ fused.readFile / writeFile / stat / rawUrl   ← direct file IO, no Python needed
```

Three primitives — `runPython`, `params`, and the file IO helpers — are the entire API. Everything else is ordinary HTML/CSS/JS (no framework, no build step, ES2020 fine).

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
- **Calls time out at 30 s** and errors return `{type, message, traceback}` to the page. The environment is whatever Python launched the server — assume stdlib plus whatever the user installed there.

## The HTML side: `window.fused` API

The runtime is injected automatically when the explorer renders the page. Never add a script tag for it; just use the global.

| Call | Behavior |
|---|---|
| `await fused.runPython(pyPath, params, opts?)` | Runs `main(**params)` of the file at `pyPath` — relative to **this html file's directory**, or absolute. Resolves with the return value; rejects with an `Error` carrying `.type`, `.message`, `.traceback`, `.stdout`. **Stale-request cancellation is on by default** (keyed by `pyPath`): a new call for a file aborts the prior in-flight call for that same file — so slider scrubs cancel the runs they move past. A superseded call's promise **never settles** (its `.then`/`await` just stops — nothing stale is drawn). `opts.key` regroups the channel (a string) or `opts.key: null` **opts out** (fully concurrent — use for polling loops, per-tile fetches, or writes that must finish); `opts.signal` is a standard `AbortSignal` that composes (an abort via *your* signal rejects with a benign `AbortError` the runtime swallows). |
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
- Params are **strings only, always**. Parse numbers yourself (`parseInt(fused.params.get("limit") || "50", 10)`), JSON-encode structure yourself if you need it.
- Uncaught `runPython` rejections auto-show a red traceback overlay — good default for debugging; catch the rejection yourself when you want custom error UI.
- **Stale requests to the same `.py` auto-cancel.** For the common slider/scrub case — a fast drag fires a request per intermediate value and only the last matters — you get this for free: a new `runPython("./x.py", …)` aborts any prior in-flight call to `./x.py`, and the superseded call's promise never settles (its continuation just stops, so nothing stale is drawn). Calls to **different** files are independent. When you genuinely need multiple concurrent calls to the **same** file to all finish — a polling loop, per-tile fetches, or a write that must complete — pass `{ key: null }` to opt out. Use a distinct `{ key: "…" }` to split one file into independent channels, or `{ signal }` (your own `AbortController`) to cancel on something other than the next call.
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

Style: views render inside a dark-themed explorer. Match it (dark background, light text) unless the user wants otherwise; there is no imposed CSS — the iframe is a blank canvas.

## Preview templates (views for a file format)

A template is the same kind of html file, but the explorer opens it *for* a target file and hands the path over as the read-only `_file` param:

```js
const file = fused.params.get("_file");
if (!file) { /* show "no file selected" state */ }
const page = await fused.runPython("./my_reader.py", { file, offset: fused.params.get("offset") || "0" });
```

A reader `.py` is only needed when Python adds value (parsing parquet/xlsx, paging, aggregation). Text formats can skip it entirely — `fused.stat` for a size guard, then `fused.readFile(file)` and render in JS (the markdown/JSON/code templates work this way); media formats just point a tag at `fused.rawUrl(file)`.

Ship the reader `.py` next to the template html and call it with a relative path. Paging/sort/filter state goes in normal params (`offset`, `sort` …) exactly like any view. Built-in templates live one folder per template under `fused_render/templates/<name>/` and follow this pattern (see `templates/table/template.html` + `templates/table/reader.py` for a worked example); each extension maps to an **ordered list of mode names** (first = default) in the built-in registry `fused_render/templates/registry.json`. **User-owned** templates that override, reorder, or extend that list live under `~/.fused-render/` and are bound via `registry.json` — layout, the mode-list/registry grammar, and registration are covered by the `fused-render-custom-templates` skill (this skill still owns how the html/py themselves are written).

## Testing in the browser: URL paths & modes

Verify a view by opening it in a real browser against the running server — do not rely on reading the files alone. Start the server (`fused-render --port 1777 --no-browser` keeps it from stealing focus) and open one of these on `http://127.0.0.1:<port>`:

| Path | What it renders | Use it to |
|---|---|---|
| `/` | The explorer at `start_dir` — file listing with chrome. | Browse to a file by clicking. |
| `/embed/<abs-path-without-leading-slash>` | **Embed mode**: the page chrome-free (no sidebar/breadcrumb/header). | **The default way to open and test a view** — you see just the view itself. |
| `/view/<abs-path-without-leading-slash>` | **Full-shell mode**: the same page inside the explorer shell — sidebar, breadcrumb, preview header — with your page in an iframe. | Check how the view sits inside the explorer chrome, or when browsing. |

**Default to embed.** When you open a link to test a view or show it to the user, use `/embed/` — it renders the view alone, which is what you're iterating on. Reach for `/view/` only to inspect the surrounding chrome or when the user is browsing.

Path encoding: the fs path rides in the URL after the prefix with its **leading slash dropped** and each segment URL-encoded. `/Users/me/proj/dash.html` → `http://127.0.0.1:1777/embed/Users/me/proj/dash.html`. A space becomes `%20`, etc.

**View vs embed** is a fixed page-load mode (the prefix picks it; it cannot toggle without a full navigation). Both serve the same shell and route identically — embed just hides chrome. Params sync the same way in both; in nested embeds, param sync stops at each embed shell boundary so a tab's params stay tab-independent.

**Preview templates** open at the target file's path (`/embed/<abs path to the data file>`) — the shell resolves the template by extension and hands it the file via the read-only `_file` param. To test a template's html directly, open it and pass the target yourself: `/embed/<abs path to template>.html?_file=<abs target path>`.

**API endpoints** (`/api/config`, `/api/fs/stat|list|raw|events`, `/api/fs/write`, `/api/run`) back the runtime — reach them only through the `fused.*` helpers, never by hand (see the note above). They're listed here only so you recognize them in the network tab while debugging.

Sanity loop: page renders → interact with a control → URL query updates → hard refresh → identical view. Python errors appear as the red overlay (with full traceback) and `print()` output in the browser console (prefixed `[python]`).

## Long-running work and the 30 s timeout

Every `fused.runPython` call runs `main()` in a fresh subprocess that the server **kills at 30 s** (`DEFAULT_TIMEOUT` in `fused_render/executor.py`). On timeout the call rejects with a `TimeoutError` — which, uncaught, becomes the red overlay. The `/api/run` route does not expose a per-call override, so you cannot raise the limit from the page; design around it instead:

- **Precompute and cache to disk.** Do the expensive work once, write the result next to the script (`.json`/`.parquet`), and have `main()` return the cached bytes when they're fresh (compare mtimes) — recompute only when the input changed. Reading a cached file is near-instant.
- **Chunk / paginate.** Slice the work so each call stays well under 30 s, pass an `offset`/`page` param, and accumulate results in JS across several `runPython` calls. This also keeps the UI responsive.
- **Move the heavy job out of band.** For a genuinely long build, run it as a separate process/script that writes an output file, and have the view just `fused.readFile`/`runPython` the finished result.
- **Cut per-call cost.** Each call re-pays import cost (pandas ≈ 1 s); import lazily inside `main`, and debounce sliders (~150 ms) so a drag doesn't spawn a subprocess per tick.

Escape hatch: because fused-render runs your own trusted code on your own machine, you *can* raise `DEFAULT_TIMEOUT` in `fused_render/executor.py` — but that's editing the package, applies globally, and lets any view hang a worker that long. Prefer the caching/chunking patterns; reach for the constant only for a deliberate, local one-off.

## Pitfalls checklist

- `fused.params.set("n", 5)` → **throws** (number). Use `String(5)`.
- Reading `input.value` inside `draw()` instead of `fused.params.get()` → refresh loses state.
- `main` returning a DataFrame / datetime / Decimal / numpy value → serialization error; convert to JSON-native first.
- Missing annotation on a numeric param → `main` receives `"50"` (string) and comparisons silently misbehave.
- Expecting module state to persist between `runPython` calls → each call is a fresh process.
- Adding `<script src=".../runtime.js">` manually → double-injection; the explorer injects it.
- Heavy import + slider wired without debounce → one full subprocess per tick; debounce inputs ~150 ms when `main` is slow.
- Fetching `/api/fs/raw` (or POSTing `/api/fs/write`) directly instead of using the helpers → writes get rejected (missing required header) and you're coupled to internals.
- `writeFile` without `expectedMtime` on an *existing* file → silently clobbers whatever is on disk now. Fine for new files; for edits, arm the lock and handle `.type === "conflict"`.
- Using `readFile` for an image/video and stuffing bytes into the DOM → use `fused.rawUrl(path)` as the element's `src` instead.
