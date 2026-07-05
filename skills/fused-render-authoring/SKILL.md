---
name: fused-render-authoring
description: How to author HTML views and Python data files for fused-render — the local file explorer that live-renders HTML with a fused.runPython() bridge to local Python, URL-synced params, and file IO helpers (fused.readFile/writeFile/stat/rawUrl). Use this whenever the user asks to create, edit, or debug an .html view, a .py data file, or a preview template for fused-render; mentions fused.runPython, fused.params, fused.readFile, fused.writeFile, renderable HTML, preview templates, or _file; or asks for "a view for <some file/data>" or an editor for a file format inside a fused-render project. Also use it when a fused-render view renders blank, shows a red traceback overlay, or params don't sync to the URL.
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
| `await fused.runPython(pyPath, params)` | Runs `main(**params)` of the file at `pyPath` — relative to **this html file's directory**, or absolute. Resolves with the return value; rejects with an `Error` carrying `.type`, `.message`, `.traceback`, `.stdout`. |
| `fused.params.get(k)` | Current value from the URL, as a **string** (or `undefined`). |
| `fused.params.getAll()` | All non-reserved params as an object. |
| `fused.params.set(k, v)` | Writes to the URL (replaceState — no history spam). **Throws unless `v` is a string** — do `String(n)` yourself. Then fires `onChange`. |
| `fused.params.onChange(cb)` | `cb(allParams)` after every applied `set`. Returns an unsubscribe function. |
| `fused.params.get("_file")` | Read-only: the target file a **preview template** was opened for. Keys starting `_` are reserved — `set()` on them throws. |
| `await fused.readFile(path)` | File contents as **text** (UTF-8). Rejects with an `Error` on failure. Use when a view just needs the bytes as a string — no reader `.py` required. |
| `await fused.stat(path)` | Metadata object `{path, name, is_dir, size, mtime, template}`. Use for size guards before reading big files, and to capture `mtime` before editing. |
| `await fused.writeFile(path, content, opts?)` | Writes UTF-8 text **atomically** (never a half-written file). `opts.expectedMtime` arms an optimistic lock: if the file changed on disk since that mtime, rejects with an error whose `.type === "conflict"` (and `.mtime` = current on-disk value) instead of clobbering. Omit it to write unconditionally — also how you create a new file. Resolves with a fresh stat object; keep its `.mtime` to re-arm the lock for the next save. |
| `fused.rawUrl(path)` | **Sync**, returns a URL string serving the file's raw bytes. This is for embedding — `<img src>`, `<video src>`, `<embed>`, download links — where you need a URL, not text. |

Notes:
- Params are **strings only, always**. Parse numbers yourself (`parseInt(fused.params.get("limit") || "50", 10)`), JSON-encode structure yourself if you need it.
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
      const limit = fused.params.get("limit") || "20";   // URL wins; default only when absent
      limitEl.value = limit;                              // reflect state INTO controls
      out.textContent = "Loading…";
      try {
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

Ship the reader `.py` next to the template html and call it with a relative path. Paging/sort/filter state goes in normal params (`offset`, `sort` …) exactly like any view. Built-in templates live in `fused_render/templates/` and follow this pattern (see `parquet_template.html` + `parquet_reader.py` for a worked example); registering a new extension means adding it to the `TEMPLATES` dict in `fused_render/server.py`.

## Testing an authored view

With the server running (`fused-render --port 8765`), open:

```
http://127.0.0.1:8765/view/<absolute path to your .html>
```

Sanity loop: page renders → interact with a control → URL query updates → hard refresh → identical view. Python errors appear as the red overlay (with full traceback) and `print()` output in the browser console.

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
