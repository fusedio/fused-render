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
   ├─ fused.readFile / writeFile / stat / rawUrl   ← direct file IO, no Python needed
   │
   ├─ fused.ai("...")          ← runs the local claude CLI, returns {text, model, usage}
   │
   └─ fused.trackJob({...})         ← report long work to the shell's download manager
```

Three primitives — `runPython`, `params`, and the file IO helpers — are the core API (plus `fused.ai` for asking an AI model through the local claude CLI and `fused.trackJob` for reporting long-running work — each gets its own section below — and two auxiliary members, `fused.env` and `fused.autoReload`, covered in the table). Everything else is ordinary HTML/CSS/JS (no framework, no build step, ES2020 fine).

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

Write `main()` against **stdlib plus the supported library set** below — a folder with no `pyproject.toml` runs on the app's own interpreter, which ships exactly these with no download and no first-run wait. Dev installs get the same set via `pip install -e ".[bundled]"` (the authoritative list is the `[bundled]` extra in the repo's `pyproject.toml`, plus core deps):

- **Data:** numpy, pandas, polars, pyarrow, duckdb, scipy, openpyxl, msgpack
- **Geospatial:** shapely, geopandas, rasterio, zarr
- **Plots & images:** matplotlib, pillow
- **Documents:** pymupdf, pikepdf, fpdf2, python-pptx
- **Network & cloud:** requests, httpx, botocore, google-auth
- **Logs:** drain3

Anything outside this set (e.g. torch, sklearn, xarray, plotly) is missing by default. Reaching for one is a deliberate choice with a cost, so prefer rewriting with the supported set — but when the set genuinely cannot do the job, the folder can declare its own dependencies (next section).

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

- **The declaration is the COMPLETE list.** The venv contains exactly what you name and nothing else — the bundled set above is *not* unioned in. Declare numpy if you import numpy, even though the app ships it.
- **It is all-or-nothing per folder.** Adding a `pyproject.toml` to get one extra package means every import in every `.py` under that folder must be listed. A folder with no manifest runs on the app's own interpreter and gets the whole bundled set for free — which is why the supported set is still the better default.
- **Only the project root counts.** That's the app folder, a template folder, or the *topmost* ancestor holding a `pyproject.toml`. One in a subfolder is inert; the inspector flags it.
- **First render triggers an install.** The user sees a loader while `uv sync` runs, then the run is retried automatically. Commit the `uv.lock` it writes — that's what makes the folder resolve identically elsewhere.

Adding a dependency later is just an edit: save `pyproject.toml`, re-render, and the environment is reconciled. Never run `uv sync` by hand in the folder — it would create an in-folder `.venv` that diverges from the one the app actually uses (venvs live centrally under `~/.fused-render/`).

**Per-file `# /// script` headers are not read.** A leftover block is an ordinary comment — silently ignored, not merged and not warned about. Never write one; if you see one, move its `dependencies` into the project root's `pyproject.toml` and delete it, because the packages it names are not being installed.

Versions are not pinned — each install resolves its own. When a version matters (an API that changed between majors, a feature gated on a release), **probe the live environment** instead of guessing: `/api/run` executes in the exact interpreter that runs page code. Write a throwaway probe and POST it:

```bash
cat > /tmp/probe.py <<'EOF'
def main(names: str = "pandas,numpy"):
    from importlib import metadata
    out = {}
    for n in names.split(","):
        try: out[n] = metadata.version(n)
        except Exception: out[n] = None
    return out
EOF
curl -s -X POST http://127.0.0.1:1777/api/run -H 'X-Fused: 1' \
  -H 'Content-Type: application/json' \
  -d '{"py": "/tmp/probe.py", "params": {"names": "pandas,geopandas,duckdb"}}'
```

A `null` version means the package is missing from *this* environment — the same result an `import` in `main()` would hit.

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
| `await fused.stat(path)` | Metadata object `{path, name, is_dir, size, mtime, writable, remote, templates}` (`templates` is the ordered mode-list array, usually irrelevant to page code; `writable` is false for read-only files — check it before offering an edit UI; `remote` is true for files on a mounted remote bucket — keep reads bounded there). May also carry `template_error` (a bad registry name). Use for size guards before reading big files, and to capture `mtime` before editing. |
| `await fused.writeFile(path, content, opts?)` | Writes UTF-8 text **atomically** (never a half-written file). `opts.expectedMtime` arms an optimistic lock: if the file changed on disk since that mtime, rejects with an error whose `.type === "conflict"` (and `.mtime` = current on-disk value) instead of clobbering. A read-only file rejects with `.type === "readonly"` (check `stat().writable` first to avoid it). Omit `expectedMtime` to write unconditionally. `opts.create` writes only if the path is absent: an existing path rejects with `.type === "exists"` and nothing is written, which is how you create a file without a stat-then-write race. Resolves with a fresh stat object; keep its `.mtime` to re-arm the lock for the next save. |
| `fused.rawUrl(path)` | **Sync**, returns a URL string serving the file's raw bytes. This is for embedding — `<img src>`, `<video src>`, `<embed>`, download links — where you need a URL, not text. |
| `await fused.ai(prompt, opts?)` | Ask an AI model; resolves with `{text, model, usage}`. Runs the local `claude` (Claude Code) CLI; local-only. See the **"AI calls"** section below for the options, error types, and the worked pattern. |
| `await fused.fileIndex.search({root, q, limit})` / `.query({sql, limit})` | Read the machine-wide **file index** — one folder's indexed corpus, or one read-only SQL statement over the `files`/`dirs` views (which is also how you get totals, per-extension breakdowns and path matches). Two methods, deliberately; scanning, roots/ignore config and the repos list are raw `fetch` + `X-Fused: 1`. Both resolve with `ready: {indexed, scanning, stale, reason}`, so an empty result can never be rendered as "no matches" when the truth is "no index yet". Use this instead of walking the filesystem in Python for anything machine-wide. For the readiness rule and the Python direct-parquet reader for bulk reads, read `skills/fused-render-index/SKILL.md`. |
| `fused.trackJob(spec)` | Report a long-running operation (a model download, a minutes-long generation) to the shell's **download manager**, so it stays visible after the user browses away from your page. Returns a handle; see the **"Long-running work"** section below. Never throws, never rejects. |
| `fused.env` | String `"local"` (this local server) vs `"hosted"` (the exported/hosted runtime). Branch on it only if a view must behave differently when exported. |
| `fused.autoReload(enabled)` | Toggle the automatic reload-on-file-change behavior for this page. Pass `false` to opt out (e.g. an in-page editor that manages its own saves and shouldn't reload under the user). |

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
  else if (err.type === "readonly") { /* file isn't writable — disable the save UI */ }
  else throw err;
}
```

## AI calls (`fused.ai`)

`fused.ai(prompt, opts?)` asks an AI model. It resolves with **exactly** this shape (the server normalizes — no guarding needed):

```json
{
  "text": "the completion text",
  "model": "claude-haiku-4-5-20251001",
  "usage": { "input_tokens": 544, "output_tokens": 73 }
}
```

- `text` — the completion (string).
- `model` — the **full model id that actually ran**; an alias request (`"sonnet"`) echoes the resolved id.
- `usage` — either `null` or exactly `{input_tokens, output_tokens}` (both integers). These are **Anthropic-style names** — there is NO `prompt_tokens`/`completion_tokens` (OpenAI names); reading those yields `undefined`.

The page never talks to a model directly: the server runs the call through the **`claude` (Claude Code) CLI** on the author's machine — the user's Claude Code login is the credential; the binary comes from `PATH`, overridable with the `FUSED_RENDER_CLAUDE_BIN` env var. That makes it **local-only**: an exported/hosted page has no local CLI to run, so the exporter rejects any page that calls `fused.ai` (SPEC RH-11). If a view must survive export, gate the AI UI on `fused.env === "local"` and keep the string `fused.ai(` out of the code path entirely (the exporter matches the call textually).

Options:

| Option | Meaning |
|---|---|
| `systemPrompt` | System message (string). Put role + ground rules here; put the data + question in `prompt`. |
| `model` | Model id. Default `claude-haiku-4-5-20251001`. |
| `effort` | `"low"` \| `"medium"` \| `"high"` → max_tokens 1024 / 4096 / 16384. Default medium. |
| `maxTokens` | Explicit token cap; overrides `effort`. |

Rejections carry an `Error` with `.type`:

| `.type` | Cause | UI response |
|---|---|---|
| `ai_unavailable` | `claude` binary not found/runnable — message says what to install or set. | Friendly "AI unavailable" state, not a raw overlay. |
| `bad_request` | Empty prompt / bad options. | Fix the call; surfacing it usually means a bug in your page. |
| `ai_error` | CLI ran but reported an error (bad model id, upstream failure). | Show `err.message`. |
| `timeout` | No answer within 120 s. | Offer retry; suggest lower `effort`. |

The canonical shape — compute data in Python, reduce it to **compact aggregates**, and hand the model those, never the raw dataset (a full table blows the token budget and drowns the signal):

```js
const data = await fused.runPython("./data.py", { days });   // full dataset for the UI
const context = JSON.stringify({                              // aggregates only, for the model
  total_revenue: data.total_revenue,
  revenue_by_region: data.by_region,
  daily_revenue: data.by_day,
});

async function ask(question) {
  const btn = document.getElementById("go"), out = document.getElementById("answer");
  btn.disabled = true;                    // fused.ai has NO stale-cancel — guard double-submits yourself
  out.textContent = "Thinking…";
  try {
    const res = await fused.ai(
      "Data (JSON):\n" + context + "\n\nQuestion: " + question,
      {
        systemPrompt: "You are a data analyst. Answer ONLY from the provided JSON data. " +
                      "Cite figures. A few sentences at most.",
        effort: "low",
      }
    );
    out.textContent = res.text;           // res.model / res.usage available for a meta line
  } catch (err) {
    if (err.type === "ai_unavailable")      out.textContent = "AI unavailable — " + err.message;
    else if (err.type === "bad_request")    out.textContent = "Bad request: " + err.message;
    else                                    out.textContent = (err.type || "error") + ": " + err.message;
  } finally {
    btn.disabled = false;
  }
}
```

Two behaviors that differ from `runPython`, each for a reason:

- **No stale-cancel channel.** An AI call is never a slider scrub — you asked a question and want the answer — so calls run fully concurrent. The flip side: nothing stops a double-click from firing two paid calls. Disable the button while a call is in flight (as above).
- **The relay times out at 120 s** server-side (vs 60 s for `runPython`) — generation is slower than computation. A `high`-effort call on a big model can legitimately take a while; keep the loading state honest.

When a call fails, check the CLI before blaming the page — same probe style as `/api/run`:

```bash
which claude && claude --version               # CLI installed? (or check $FUSED_RENDER_CLAUDE_BIN)
curl -s -X POST http://127.0.0.1:1777/api/ai -H 'X-Fused: 1' \
  -H 'Content-Type: application/json' \
  -d '{"prompt": "Reply with exactly the word pong.", "effort": "low"}'
```

The first failing means the claude CLI isn't installed (that's `ai_unavailable`, not your bug); the second exercises the exact endpoint the page uses.

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

## Style and theming

There is no imposed CSS — the iframe is a blank canvas, and by default **nothing is written into your document**: no class, no attribute, no stylesheet. But the explorer around it is not fixed. It follows the OS light/dark preference, with a Light/Dark override in Preferences → Appearance, so a hardcoded palette will sooner or later sit inside the opposite one.

Pick one of these and commit to it — the failure mode is picking none and half-following:

**1. Fixed palette.** Fine for a view with its own strong look (a dark map, a photo grid). Just don't pretend to follow.

**2. Follow the app** — one attribute, no JS. Put `data-fused-theme="shell"` on your `<html>` and the injected runtime resolves the app's setting, writes `data-theme="light"`/`"dark"` on that same element before your stylesheet is even parsed, and keeps it in step afterwards — including an in-app pin, an OS flip mid-session, and a change made in another window. Author against the attribute:

```html
<html data-fused-theme="shell">
```
```css
:root       { color-scheme: dark;  --bg: #101318; --text: #dce2ea; --line: #2a303a; }
:root[data-theme="light"]
            { color-scheme: light; --bg: #f7f8fa; --text: #1a1f27; --line: #d8dce3; }
body        { background: var(--bg); color: var(--text); }
```

This is what the built-in templates use (`SPEC.md` §30, AP-8/AP-9), and it is the only option that agrees with the app when the user pins Light or Dark.

**3. Follow the desktop** — `@media (prefers-color-scheme: light)` around the second `:root`, same tokens, no attribute. Tracks the OS, which is what the app's default System mode tracks too, so the two agree for the setting almost nobody changes. It does *not* see an in-app pin.

**4. Your own switcher.** Put the choice in a param (`fused.params.set("theme", …)`) so it is bookmarkable like the rest of your view state, and drive the same one `data-theme` attribute from it. **Don't combine this with option 2** — the runtime re-applies on every storage/OS event, so your button would silently lose to the app setting. That is exactly why the built-in log viewer dropped its own button.

Whatever you pick, two rules make the second palette actually work:

- **Every colour comes from a token.** Two blocks defining *the same token set*, and no colour literal anywhere else in the stylesheet. A stray `#1a1f27` in a rule is one the other mode cannot repaint — and it shows up as an unreadable smear, not an obvious bug.
- **Colours you hand to JS don't follow.** Canvas fills, chart ramps, maplibre paint expressions — `var()` does not resolve inside a JS string. Read them at *draw* time and redraw when the attribute changes:

  ```js
  const token = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  new MutationObserver(() => redraw())
    .observe(document.documentElement, { attributeFilter: ["data-theme"] });
  ```

Do not read the app's own `localStorage` key. It is private, it is not part of `window.fused`, and a view that reads it becomes a second copy of a resolution rule that will drift from the first — options 2 and 3 both get you the answer without one.

## Preview templates (views for a file format)

A template is the same kind of html file, but the explorer opens it *for* a target file and hands the path over as the read-only `_file` param:

```js
const file = fused.params.get("_file");
if (!file) { /* show "no file selected" state */ }
const page = await fused.runPython("./my_reader.py", { file, offset: fused.params.get("offset") || "0" });
```

A reader `.py` is only needed when Python adds value (parsing parquet/xlsx, paging, aggregation). Text formats can skip it entirely — `fused.stat` for a size guard, then `fused.readFile(file)` and render in JS (the markdown/JSON/code templates work this way); media formats just point a tag at `fused.rawUrl(file)`.

Ship the reader `.py` next to the template html and call it with a relative path. Paging/sort/filter state goes in normal params (`offset`, `sort` …) exactly like any view. Built-in templates live one folder per template under `fused_render/templates/<name>/` and follow this pattern (see `templates/xlsx/template.html` + `templates/xlsx/reader.py` for a worked example); each extension maps to an **ordered list of mode names** (first = default) in the built-in registry `fused_render/templates/registry.json`. **User-owned** templates that override, reorder, or extend that list live under `~/.fused-render/` and are bound via `registry.json` — layout, the mode-list/registry grammar, and registration are covered by the `fused-render-custom-templates` skill (this skill still owns how the html/py themselves are written).

## Testing in the browser: URL paths & modes

Verify a view by opening it in a real browser against the running server — do not rely on reading the files alone. Start the server (`fused-render --port 1777 --no-browser` keeps it from stealing focus) and open one of these on `http://127.0.0.1:<port>`:

| Path | What it renders | Use it to |
|---|---|---|
| `/` | Redirects to `/apps` (the app hub); `/explorer` is the file-explorer homepage. | Browse to a file by clicking. |
| `/explorer/embed/<abs-path-without-leading-slash>` | **Embed mode**: the page chrome-free (no sidebar/breadcrumb/header). | **The default way to open and test a view** — you see just the view itself. |
| `/explorer/view/<abs-path-without-leading-slash>` | **Full-shell mode**: the same page inside the explorer shell — sidebar, breadcrumb, preview header — with your page in an iframe. | Check how the view sits inside the explorer chrome, or when browsing. |

**Default to embed.** When you open a link to test a view or show it to the user, use `/explorer/embed/` — it renders the view alone, which is what you're iterating on. Reach for `/explorer/view/` only to inspect the surrounding chrome or when the user is browsing. (Legacy `/view/` and `/embed/` prefixes still redirect to the `/explorer/...` forms.)

Path encoding: the fs path rides in the URL after the prefix with its **leading slash dropped** and each segment URL-encoded. `/Users/me/proj/dash.html` → `http://127.0.0.1:1777/explorer/embed/Users/me/proj/dash.html`. A space becomes `%20`, etc.

**View vs embed** is a fixed page-load mode (the prefix picks it; it cannot toggle without a full navigation). Both serve the same shell and route identically — embed just hides chrome. Params sync the same way in both; in nested embeds, param sync stops at each embed shell boundary so a tab's params stay tab-independent.

**Preview templates** open at the target file's path (`/explorer/embed/<abs path to the data file>`) — the shell resolves the template by extension and hands it the file via the read-only `_file` param. To test a template's html directly, open it and pass the target yourself: `/explorer/embed/<abs path to template>.html?_file=<abs target path>`.

**API endpoints** (`/api/config`, `/api/fs/stat|list|raw|events`, `/api/fs/write`, `/api/run`) back the runtime — reach them only through the `fused.*` helpers, never by hand (see the note above). They're listed here only so you recognize them in the network tab while debugging.

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

## Long-running work and the 60 s timeout

Every `fused.runPython` call runs `main()` in a fresh subprocess that the server **kills at 60 s** (`DEFAULT_TIMEOUT` in `fused_render/executor.py`). On timeout the call rejects with a `TimeoutError` — which, uncaught, becomes the red overlay. The `/api/run` route does not expose a per-call override, so you cannot raise the limit from the page; design around it instead:

- **Precompute and cache to disk.** Do the expensive work once, write the result next to the script (`.json`/`.parquet`), and have `main()` return the cached bytes when they're fresh (compare mtimes) — recompute only when the input changed. Reading a cached file is near-instant.
- **Chunk / paginate.** Slice the work so each call stays well under 60 s, pass an `offset`/`page` param, and accumulate results in JS across several `runPython` calls. This also keeps the UI responsive.
- **Move the heavy job out of band.** For a genuinely long build, run it as a separate process/script that writes an output file, and have the view just `fused.readFile`/`runPython` the finished result.
- **Cut per-call cost.** Each call re-pays import cost (pandas ≈ 1 s); import lazily inside `main`, and debounce sliders (~150 ms) so a drag doesn't spawn a subprocess per tick.

### Show it in the download manager (`fused.trackJob`)

The out-of-band pattern above leaves a hole: a detached worker pulling an 8GB model keeps running when the user browses to another file, and the shell replaces your page's frame the moment they do — so your in-page progress bar disappears and the download becomes invisible. Report it instead, and the shell shows it in the **download manager** at the bottom right for as long as it runs, whatever page the user is on:

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
- **Cancel is a request you honor**, not something the shell can do — it has no idea which process is doing the work. The ✕ sets a flag; your poll loop notices it and stops the worker, then reports `job.cancelled()`. If you cannot stop the work, leave `cancellable` off and no ✕ is offered.
- **Omit `total` while you don't know it.** A job with no total draws a travelling "indeterminate" bar, which is the honest picture; a total of `0` is treated the same way rather than painted as complete.
- **Report the finish.** Without a terminal call the row goes "stalled" after 30 s and says the page that started it was closed — accurate if that is what happened, misleading if the work just ended. Report `finish`/`fail`/`cancelled` on every exit path of your poll loop.
- **Report from the WORKER, not only the page — this is the one that bites.** The shell replaces your page's frame on every navigation, so a page-only reporter freezes the row at its last number and the manager declares it stalled ~30s later while the download carries on. Your detached worker outlives the page, so let it report too. It cannot `import fused_render` (it runs in its own venv), but the endpoint is plain JSON on the origin every spawned child inherits:

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

  Use the **same job id** on both sides (derive it from something both know — the job directory name, the model id) so the two reporters share ONE row instead of opening two for the same work. Keep the page reporting as well: it is the only thing alive during `uv run`'s first-run environment build, before your worker executes a line. Rate-limit the worker's posts to ~1/s — a download callback fires per chunk.

  **The worker is also the only thing that can honor a cancel once the page is gone.** `cancel_requested` comes back in the reply to the tick you were already sending; check it in your progress callback and stop. If your long step is an opaque subprocess (`uv sync`), run a small daemon thread that posts a heartbeat, reads the flag, and kills the child — otherwise the ✕ does nothing for the minutes that matter most.

- **One job per user-meaningful operation**, not per file: aggregate a multi-file download into one row (sum the bytes) and put the current filename in `detail`.
- Reuse a **stable `id`** (`fused.trackJob({id: "flux:" + jobId, ...})`) when a page can be reloaded mid-work — the reopened page re-attaches to the existing row instead of opening a second one.
- Exports fine: `fused.trackJob` is a no-op on a hosted page (there is no manager there), so unlike `fused.ai` it does not block export.

Escape hatch: because fused-render runs your own trusted code on your own machine, you *can* raise `DEFAULT_TIMEOUT` in `fused_render/executor.py` — but that's editing the package, applies globally, and lets any view hang a worker that long. Prefer the caching/chunking patterns; reach for the constant only for a deliberate, local one-off.

## Pitfalls checklist

- `fused.params.set("n", 5)` → **throws** (number). Use `String(5)`.
- Reading `input.value` inside `draw()` instead of `fused.params.get()` → refresh loses state.
- `main` returning a DataFrame / datetime / Decimal / numpy value → serialization error; convert to JSON-native first.
- Missing annotation on a numeric param → `main` receives `"50"` (string) and comparisons silently misbehave.
- Expecting module state to persist between `runPython` calls → each call is a fresh process.
- Importing a library outside the supported set (torch, sklearn, xarray, plotly, ...) → `ModuleNotFoundError` in the packaged app; stick to the "Available Python libraries" list above.
- Adding `<script src=".../runtime.js">` manually → double-injection; the explorer injects it.
- Heavy import + slider wired without debounce → one full subprocess per tick; debounce inputs ~150 ms when `main` is slow.
- Fetching `/api/fs/raw` (or POSTing `/api/fs/write`) directly instead of using the helpers → writes get rejected (missing required header) and you're coupled to internals.
- `writeFile` without `expectedMtime` on an *existing* file → silently clobbers whatever is on disk now. For edits, arm the lock and handle `.type === "conflict"`; for "create this if it isn't there", pass `{create: true}` and handle `.type === "exists"` rather than stat-ing first (a stat that fails for any reason other than absence otherwise reads as "go ahead and write").
- Using `readFile` for an image/video and stuffing bytes into the DOM → use `fused.rawUrl(path)` as the element's `src` instead.
- Walking the filesystem (`os.walk`, `glob`, shelling out to `find`) to answer "how many / how big / where are all my X files" → the index already knows, without a single stat; use `fused.fileIndex.query()` or read its parquet directly (`fused-render-index`).
- `fused.ai` rejecting with `.type === "ai_unavailable"` → the claude CLI isn't installed or found (the message says what to install or set); show that state in the UI instead of a raw overlay.
- Dumping the full dataset into a `fused.ai` prompt → token blowout and a worse answer; reduce to compact aggregates first (see "AI calls" above).
- Forgetting `fused.ai` has no stale-cancel → a double-click fires two concurrent calls; disable the button while one is in flight.
- Calling `fused.ai` on a page meant for export → the exporter rejects it (SPEC RH-11); gate on `fused.env === "local"`.
- Starting long work with `fused.trackJob` and never calling `finish`/`fail`/`cancelled` → the row sits there and goes "stalled" after 30 s, telling the user the page was closed when really the job just ended. Report a terminal state on every exit path of the poll loop.
- Reporting progress ONLY from the page → the row freezes and goes "stalled" the moment the user opens another file, because the shell tears your frame down. Anything that outlives the page must report from the worker (see "Long-running work" above).
- Reporting "I wrote the files, try it" as if it were verification → after the page has been opened, `fused-render calls --page <page>` says whether it actually ran (see "Verifying your work" above). Zero records means the page's JS died before it reached Python — a different bug from a failing `main()`, and they look identical without the log.
