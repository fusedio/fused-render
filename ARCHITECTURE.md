# fused-render — Implementation Blueprint (M1)

**Status:** Locked for M1 build — 2026-07-04.
Companion docs: `SPEC.md` (requirements, decision markers), `DECISIONS.md` (decision log + project context).
This file is the concrete contract an implementer can build from without further discussion.

---

## 1. Package layout

```
fused-render/
├── pyproject.toml              # hatchling; deps: fastapi, uvicorn, pyarrow; script: fused-render
├── SPEC.md  ARCHITECTURE.md  DECISIONS.md  README.md
├── fused_render/
│   ├── __init__.py             # __version__
│   ├── cli.py                  # arg parse → uvicorn.run + open browser
│   ├── server.py               # FastAPI app factory, all endpoints
│   ├── executor.py             # subprocess-per-call runner (EXISTS — keep)
│   ├── _child.py               # worker-process entry (EXISTS — keep)
│   ├── static/
│   │   ├── shell.html          # explorer SPA shell
│   │   ├── shell.css
│   │   ├── shell/              # ES modules, no build step
│   │   │   ├── main.js         # entry: config load + route() dispatcher
│   │   │   ├── router.js       # fs-path <-> URL codec, navigate(), route handler registry
│   │   │   ├── api.js          # fetch wrappers (config/list/stat/rawUrl)
│   │   │   ├── format.js       # escapeHtml/formatSize/formatMtime/basename (pure)
│   │   │   ├── bookmarks.js    # localStorage store (pure data, no DOM)
│   │   │   ├── sidebar.js      # sidebar UI: Home, bookmark rows, hover card, rename
│   │   │   ├── breadcrumb.js   # crumb bar + "+ Bookmark" button
│   │   │   └── views/
│   │   │       ├── listing.js  # dir table + sortable columns
│   │   │       ├── preview.js  # three-way dispatch: template/html/fallback
│   │   │       ├── layout-codec.js # shared _layout codec + embed helpers (M5/M6)
│   │   │       ├── panel.js    # split-pane grid (M5): tree ops + pane bars
│   │   │       └── tabs.js     # tab mode (M6): tab bar + lazy keep-alive iframes
│   │   └── runtime.js          # injected into every rendered HTML
│   └── templates/
│       ├── parquet_template.html
│       ├── parquet_reader.py
│       ├── image_template.html
│       └── text_template.html
└── examples/
    ├── sine.py
    └── sine.html
```

No frontend build step. Plain ES2020 JS, plain CSS. No JS dependencies.

---

## 2. CLI (`cli.py`)

```
fused-render [--start-dir DIR] [--port N] [--no-browser]
```

- `--start-dir` default `~` (home). UI starting location only — **whole filesystem is browsable** (no root-scoping concept anywhere).
- `--port` default `8765`.
- Binds `127.0.0.1` only. Prints URL, opens browser after short delay (threading.Timer) unless `--no-browser`.
- `uvicorn.run(app, host="127.0.0.1", port=port)`.

---

## 3. HTTP API

All paths in query strings are **absolute filesystem paths**. Server never scopes/rejects by location (v1 has no security layer — deliberate, see SPEC §9). Errors return `{"error": "<message>"}` with 4xx status. Every response carries `Cache-Control: no-cache` (middleware) — app code changes between restarts and user files change on disk; stale cached shell/runtime JS produced half-old UIs during development. The two mutating/executing POSTs (`/api/run`, `/api/fs/write`) require an `X-Fused: 1` header (missing/wrong → 403); it forces a CORS preflight so a foreign page can't fire them blind. Not auth — D3 stands (see DECISIONS.md D36).

### `GET /` and `GET /view/{path:path}` → shell.html
Same static shell for both; shell JS reads `location.pathname` to route. `/view/Users/vasu/data` means fs path `/Users/vasu/data` (strip `/view/`, prepend `/`).

### `GET /api/fs/stat?path=<abs>`
```json
{
  "path": "/Users/vasu/data/trips.parquet",
  "name": "trips.parquet",
  "is_dir": false,
  "size": 123456,
  "mtime": 1751600000.0,
  "template": "/…/fused_render/templates/parquet_template.html"   // or null
}
```
`template` is the server-side registry lookup by extension (lowercased). `null` for dirs, `.html` files (those render live), and unmapped extensions. The user registry (§7, SPEC §16) is consulted first; a binding that exists but is unusable (bad JSON, unsafe folder name, missing `template.html`) falls back to the built-in and adds a `"template_error": "<reason>"` field to this response (absent otherwise).

### `GET /api/fs/list?path=<abs dir>`
```json
{
  "path": "/Users/vasu/data",
  "entries": [
    {"name": "sub", "is_dir": true,  "size": null,   "mtime": 1751500000.0},
    {"name": "trips.parquet", "is_dir": false, "size": 123456, "mtime": 1751600000.0}
  ]
}
```
Sorted: dirs first, then files, case-insensitive alpha. Includes dotfiles (FS-4 v1). Unreadable entries skipped silently. Non-dir path → 400.

### `GET /api/fs/raw?path=<abs file>`
`FileResponse` — correct MIME via `mimetypes.guess_type`, Range support (free from Starlette). 404 if missing.

### `POST /api/fs/write`  *(requires `X-Fused: 1`)*
Body `{path: <abs>, content: str, expected_mtime?: float}`. Rejects non-absolute paths, directories, and missing parent dirs. Atomic write (temp file in the same dir → fsync → `os.replace`), preserving the target's permission bits on overwrite. Optimistic lock: if `expected_mtime` is given, a changed **or deleted** file → HTTP 409 `{error: "conflict", mtime: <current|null>}`; omitting it writes unconditionally (also how new files are created). Response = the same shape as `/api/fs/stat` (fresh mtime/size) so the editor can re-arm the lock.

### `GET /render?path=<abs .html file>`
Reads file text, injects `<script src="/static/runtime.js"></script>`:
- if `<head>` present (case-insensitive): right after it;
- else: prepended to the document.
Returns `text/html`. Used as iframe src by the shell for both user HTML and templates.

### `POST /api/run`  *(requires `X-Fused: 1`)*
Request:
```json
{"py": "./sine.py", "html": "/Users/vasu/views/sine.html", "params": {"freq": "2.4"}}
```
- `py` relative → resolved against `dirname(html)`; absolute → used as-is. (`html` may be null only if `py` is absolute.)
- Response is the executor result verbatim (HTTP 200 even for user-code errors — the `ok` field carries success):
```json
{"ok": true,  "result": …, "stdout": "…"}
{"ok": false, "error": {"type": "ZeroDivisionError", "message": "…", "traceback": "…"}, "stdout": "…"}
```
Endpoint is sync `def` → FastAPI runs it in its threadpool → concurrent runPython calls work (RH-4).

### `GET /static/*`
StaticFiles mount for shell + runtime. Templates dir is NOT statically mounted — templates are served through `/render` like any HTML file.

---

## 4. Executor protocol (`executor.py` + `_child.py`) — ALREADY IMPLEMENTED

- `run_python(path, params, timeout=30.0) -> dict`: spawns `[sys.executable, _child.py]`, writes `{"path", "params"}` JSON to stdin, `subprocess.run(timeout=…)`, parses **last stdout line** as result JSON. Timeout → `TimeoutError` error dict. Garbage/no output → `ExecutorError` with stderr tail.
- `_child.py`: chdir to the .py's dir (relative data paths work), prepend dir to `sys.path`, import via `importlib.util.spec_from_file_location`, find callable `main`, bind params with annotation-based coercion (`"100"`→int, `"2.4"`→float, `"true"/"1"/"yes"/"on"`→bool), missing required arg / non-callable main → structured error. Extra params ignored unless `**kwargs`. Return value must be JSON-native, else clear TypeError suggesting `df.to_dict('records')`. User `print()` captured → returned as `stdout` field. Catches `BaseException` (incl. SystemExit).

Fresh process per call = fresh code every call (PY-9); the env is whatever Python launched the server.

---

## 5. Injected runtime (`runtime.js`)

Iframe is **same-origin** (src = `/render?path=…` on the same host) → no postMessage protocol; runtime touches an ancestor window directly. The param target is the **topmost same-origin ancestor** (D46): the runtime climbs `window.parent` while the next ancestor is same-origin (probed via a try/catch on `.location.href`) **and not a param boundary** — an ancestor with `_fusedParamBoundary` set stops the climb *below* it (only the tab-mode shell sets one, D47). In normal view/embed mode the direct parent is already the top, so nothing changes; in panel layout mode a pane's rendered page climbs past its embed shell to the layout shell, so all panes share the layout URL (param merging + cross-pane sync are structural); in tab mode the climb stops at each tab's own embed shell, so tab params stay tab-independent. Must also work when `/render?path=…` is opened as the top-level page (then `target === window`, also the fallback for a cross-origin ancestor; params live on the /render URL itself alongside `path` — `path` is owned by the server route, treat it as reserved too). Notification is a single channel: `set()` and any ancestor URL write both surface as a `fused:urlchange` event on the target window, and `onChange` fires only when the non-reserved param snapshot actually changed (diff guard — kills loops and the duplicate a self-`set` would otherwise cause). The target URL may carry the parenthesized `_layout` param, which contains literal `&` (D51, §11): the runtime never parses the target search with raw `URLSearchParams` — its `splitSearch` duplicate strips the raw `_layout=(…)` span first and `set()` reinserts it untouched and last.

```js
window.fused = {
  runPython(pyPath, params) -> Promise<result>,
  rawUrl(path) -> string,                         // sync; /api/fs/raw?path=…
  stat(path) -> Promise<statObj>,                 // GET /api/fs/stat
  readFile(path) -> Promise<string>,              // GET raw endpoint as text
  writeFile(path, content, opts?) -> Promise<statObj>,  // POST /api/fs/write
  params: { get(k), getAll(), set(k, v), onChange(cb) -> unsubscribe },
};
```

- **IO helpers:** `stat`/`readFile`/`writeFile` reject with an `Error` carrying the server's message (mirrors runPython's rejection style). `writeFile` opts = `{expectedMtime}` (optimistic lock); a 409 rejects with an error whose `.type === "conflict"` and `.mtime` = the server's current mtime, so a caller can offer reload/overwrite. `runPython` and `writeFile` send the `X-Fused: 1` header the server requires on its POSTs (see §3).

Behavior:
- **runPython:** POST `/api/run` with `{py: pyPath, html: <own file path>, params}`. Own file path = `path` query param of the iframe's own URL. Non-ok response → reject with `Error` carrying `.type`, `.traceback`, `.stdout`. If `stdout` non-empty (ok or not), `console.log` it prefixed `[python]`.
- **params.get/getAll:** read `parent.location.search`, excluding reserved keys (`_`-prefixed). `_file` is special: read-only, sourced from the iframe's **own** URL query (the shell puts it on the iframe src), so the shell URL never duplicates the path.
- **params.set(k, v):** throws if `k` starts with `_` or `v` is not a string. Updates parent URL via `parent.history.replaceState` (always replace — PR-3), then fires local onChange listeners. Strings only (PR-5).
- **onChange(cb):** called with `getAll()` result after every applied `set`. (No cross-source change feed in v1 — params only change via the page itself.)
- **Error overlay:** module-level helper — on unhandled promise rejection carrying `.traceback` (i.e. a runPython failure the page didn't catch), render a fixed-position red-bordered overlay with type, message, `<pre>` traceback. Author-handled rejections show nothing.

Top-level `path` handling in shell URL vs iframe URL:
- Shell URL: `/view/<fs-path>?freq=2.4` — params live here (source of truth, PR-1).
- Iframe URL: `/render?path=<abs html path>` — no user params needed on it; runtime reads/writes **parent's** query string.
- Standalone fallback (`parent === window`): read/write own URL's query, skipping `path`.

---

## 6. Shell (`shell.html/css/js`)

SPA, no framework, native ES modules (`<script type="module">`, no build step). Dependency direction is one-way: `main → views/sidebar/breadcrumb → router/api/bookmarks/format`; router never imports UI (route handler is registered by main), the bookmark store never touches the DOM. `views/panel.js` and `views/tabs.js` import `router`/`format` plus the shared `views/layout-codec.js` (which itself imports only `router` for the embed prefix — one source of truth); `breadcrumb.js` may import `views/layout-codec.js` (Split button segment encoder) + `views/panel.js` (`panelUrl`), and `sidebar.js` may import `views/tabs.js` (`composeFolderTabsUrl`), since no view imports back — no cycles. Routing from `location.pathname`:
- `/` → redirect (replaceState) to `/view/<start-dir>` (start dir from `GET /api/config` → `{"start_dir": "/Users/vasu", "home": …, "source_template": <abs code_template.html>}`).
- `/view/<path>` → `stat` it:
  - **dir** → listing view
  - **file** → preview view

**Listing view:** breadcrumb bar (each segment navigates) + rows: icon (dir/file), name, human size, mtime. Columns sortable — sort key/order live in URL params (`?sort=name|size|mtime&order=asc|desc`, replaceState), dirs always group before files, ties fall back to name. Click dir → `pushState` navigate. Click file → `pushState` navigate. `popstate` → re-route.

**Preview view:** breadcrumb + filename header with actions, then dispatch **exactly three-way** (no other file-type logic in shell):

1. `stat.template != null` → iframe `/render?path=<template>&_file=<target file>` — `_file` rides on the iframe's own URL; the shell URL stays clean (its pathname already names the file). The runtime reads `_file` from its own URL first and falls back to the shell URL, so manually opening `/view/<template>.html?_file=<target>` (old bookmarks) also works.
2. extension `.html`/`.htm` → iframe `/render?path=<file itself>`. Header gets `Rendered | Source` toggle. Source loads the code template pointed at the HTML file — `/render?path=<source_template>&_file=<file>` (source_template = code_template's abs path, from `/api/config`) — an editable CodeMirror buffer, so HTML source editing comes free.
3. else → fallback: metadata card (name, size, mtime, path) + `Raw / download` link to `/api/fs/raw?path=…`.

Header actions always include `Raw` (opens raw endpoint in new tab). Iframe fills remaining viewport height, `border: none`.

**Param hygiene:** when navigating between files/dirs, drop old view params (fresh query string except `_file` set by dispatch).

### 6.5 Sidebar & bookmarks (M2)

Layout: `#app` becomes two-column flex — fixed sidebar (~220px, `--bg-alt`, right border) + existing content column (breadcrumb + content).

- **Home entry:** icon + "Home"; click → `navigate(config.home)`. `/api/config` response gains `"home": os.path.expanduser("~")`.
- **Bookmark capture:** "+ Bookmark" button right-aligned in the breadcrumb bar (present on every view); shows accent "starred" state when the current URL is already bookmarked. On click: `{id: crypto.randomUUID(), name: basename(currentFsPath), url: location.pathname + location.search, created_at: Date.now()}` appended to store; sidebar re-renders.
- **Store:** localStorage key `fused.bookmarks`, JSON array. Read/write helpers with try/catch (corrupt JSON → treat as empty, overwrite on next save).
- **Bookmark row:** name ellipsized, rendered as a real `<a href="<url>">` (verbatim URL per D20; href kept for middle-click/copy-link). Plain click is intercepted: it **arms** the bookmark for update tracking and routes in-shell via `navigateUrl(url)` (pushState that preserves the query string, unlike `navigate()`). Hover shows a floating card beside the sidebar: decoded target path + saved params as a key/value grid ("no params" when none); card hides during rename/delete. Hover also reveals ✎ rename (inline `<input>`, Enter/blur commits, Escape cancels) and ✕ delete (no confirm). Active bookmark (url == current URL) is highlighted.
- Order: creation time. Duplicates allowed.
- **Bookmark updating (D38):** the armed bookmark `{id, url}` lives in sessionStorage `fused.armedBookmark` (survives refresh, not new tabs). `breadcrumb.js` renders a hidden "Update bookmark" button left of "+ Bookmark"; `syncUpdateButton()` shows it iff armed, same pathname, and `location.search` differs from the armed url's search. Clicking it overwrites the bookmark's url with the current one and re-arms against it. A pathname change disarms permanently; deleting the armed bookmark disarms. Param changes are observed by `main.js` wrapping `history.replaceState` (the iframe runtime writes params through the parent's replaceState, which fires no native event) to dispatch a `fused:urlchange` window event; sidebar delete also dispatches it instead of importing breadcrumb (one-way deps, D28).

---

## 7. Template contract

- Server-side registry (in `server.py`):
```python
TEMPLATES = {
  ".parquet": "parquet_template.html",
  ".png": "image_template.html", ".jpg": "image_template.html", ".jpeg": "image_template.html",
  ".gif": "image_template.html", ".webp": "image_template.html", ".svg": "image_template.html",
  ".md": "markdown_template.html",
  ".csv": "csv_template.html", ".tsv": "csv_template.html",
  ".json": "json_template.html", ".geojson": "json_template.html",
  ".xlsx": "xlsx_template.html",
  ".pdf": "pdf_template.html",
  ".mp4": "media_template.html", ".mov": "media_template.html", ".m4v": "media_template.html",
  ".webm": "media_template.html", ".mp3": "media_template.html", ".wav": "media_template.html",
  ".m4a": "media_template.html", ".ogg": "media_template.html", ".flac": "media_template.html",
  ".py": "code_template.html", ".js": "code_template.html", ".ts": "code_template.html",
  ".sh": "code_template.html", ".yaml": "code_template.html", ".yml": "code_template.html",
  ".toml": "code_template.html", ".css": "code_template.html",
  ".txt": "text_template.html", ".log": "text_template.html",
}
```
- **User overrides (M7, SPEC §16):** `_template_for()` consults `~/.fused-render/registry.json` before the dict above (after the `.html`/`.htm` exemption). Keys are dotted extensions matched **longest-suffix, case-insensitive** against the basename (so `.tar.gz` works and beats `.gz`); values are a folder name — resolving to `~/.fused-render/<name>/template.html` — or `null` (no template at all: shell fallback). Folder names must be a single safe path segment (no `/`, `\`, `.`, `..`) since they're joined into a path — correctness guard, not auth (D3). The registry is read on every resolution (no restart, no cache); missing dir/registry is a clean no-op; an unusable binding falls back to the built-in with `template_error` on the stat payload. Constants `USER_TEMPLATES_DIR`/`USER_REGISTRY` in `server.py`; zero shell/runtime changes — the shell obeys `template` as always, and M4 auto-reload already live-reloads previews when the user edits their template or readers (registry edits apply on next stat, open previews don't watch it).
- Template receives target file as read-only param `_file`. Templates are ordinary renderable HTML: same runtime, same powers. Templates reach the filesystem through the runtime IO helpers (`fused.rawUrl`/`stat`/`readFile`/`writeFile`), never by fetching `/api/fs/*` URLs directly — one code path, and the write guard/lock come for free. Helper `.py` files sit next to the template; relative `runPython('./parquet_reader.py', …)` just works because `html` path sent to `/api/run` is the template's real path.
- Vendored JS libraries (marked, CodeMirror) live in `fused_render/templates/vendor/` and are served from a dedicated absolute mount `GET /template-assets/*` (a relative `<script src>` in a template would resolve against `/render`, not the templates dir). All committed local files — no CDN/network at runtime (D3). Regenerate the CodeMirror bundle via `scripts/vendor-codemirror/build.sh` (Node 22).

**M1 templates:**

- `parquet_template.html` + `parquet_reader.py`:
  - reader `main(file: str, offset: int = 0, limit: int = 100)` → `{"columns": [...], "rows": [...], "total_rows": N}` via pyarrow (`pq.read_table(file).slice(offset, limit).to_pylist()`); cell values must be JSON-safe — stringify non-JSON scalars (timestamps, bytes, decimals) in the reader.
  - UI: table, row-count line ("rows 0–99 of 12,345"), Prev/Next buttons paging via `offset` param → `fused.params.set('offset', …)` → onChange → refetch. Loading + error states.
- `image_template.html`: `<img src="/api/fs/raw?path=" + encodeURIComponent(fused.params.get('_file'))>`, centered, `max-width/height: 100%`, filename caption. No runPython needed.
- `text_template.html`: `fetch('/api/fs/raw?path=…')` → text → `<pre>`. Guard: file > 2 MB → show "too large" note with raw link instead. Monospace, preserved whitespace.

**M2 templates** (added alongside M1; same runtime, same `_file` contract, same dark palette):

- `markdown_template.html`: `fetch` raw → render with vendored `marked` (`/template-assets/marked.min.js`). GitHub-ish readable column (~46rem, centered). No sanitizer by design — local trust model (D3). Guard: file > 2 MB → "too large" note + raw link.
- `csv_template.html` + `csv_reader.py`: same UX as parquet (table, "rows X–Y of N", Prev/Next via `offset` param). Reader `main(file, offset=0, limit=100)` via pandas; `.tsv` → tab sep, else comma. Reads the full file once for an honest `total_rows`, returns only the page. Same JSON-safe cell stringifying as `parquet_reader.py` (NaN → null, timestamps/bytes/decimals coerced).
- `json_template.html`: `fetch` raw → `JSON.parse` → collapsible tree in pure JS (no library). Objects/arrays fold (▾/▸), keys/primitives type-colored, arrays/objects show count, nodes deeper than depth 2 start collapsed. Parse failure → error + first 2 KB raw. Guard: file > 5 MB → "too large" note + raw link. Also serves `.geojson`.
- `xlsx_template.html` + `xlsx_reader.py`: openpyxl `read_only=True`, first row is header. Reader `main(file, sheet="", offset=0, limit=100)` → `{sheets, sheet, columns, rows, total_rows}`. Template adds a sheet `<select>` (shown when >1 sheet) wired to a `sheet` param (resets `offset` on change); paging like csv. JSON-safe cells (datetimes → isoformat, None → null).
- `pdf_template.html`: thin filename header + full-height `<embed type="application/pdf">` of the raw endpoint.
- `media_template.html`: branches on extension — `<video>` for mp4/mov/m4v/webm, `<audio>` for mp3/wav/m4a/ogg/flac. `controls`, centered, filename caption, video constrained to viewport.
- `code_template.html`: **editable** CodeMirror 6 (vendored `/template-assets/codemirror.bundle.js`, global `CM`), `CM.oneDark` theme to match the shell. `basicSetup` line numbers; language chosen by extension (py/js/ts/json/yaml/html/css + StreamLanguage shell/toml; unknown → plain). Guard: file > 2 MB → "too large" note + raw link (no editor). Top bar (matches other templates' `#bar`): filename + Saved/Modified status + Save button (disabled when clean). Save flow: `fused.stat` arms the mtime on load → `fused.writeFile(file, doc, {expectedMtime})` on save; Cmd/Ctrl+S bound at the window (CM's `keymap` isn't in the bundle); dirty tracked via `EditorView.updateListener` (docChanged); `beforeunload` warns when dirty. On a 409 conflict a bar banner offers **Reload** (refetch + re-arm, discard local) or **Overwrite** (write with no lock, re-arm).

---

## 8. Examples

- `examples/sine.py` — `main(n: int = 80, freq: float = 1.0)` → `{"points": [[x, y], …]}` (math.sin, stdlib only).
- `examples/sine.html` — range slider bound to `freq` param, SVG polyline chart (hand-rolled, no deps), wiring pattern:
  slider input → `fused.params.set('freq', value)`; `fused.params.onChange(draw)`; initial `draw()` reads param-or-default. Demonstrates: URL sync, refresh restores state, runPython round-trip, python print → browser console.

---

## 9. Verification checklist (M1 done =)

Automatable (curl / CLI):
1. `python -c "import fused_render.server"` etc. — all modules import.
2. Start `fused-render --no-browser --port <test>`; then:
   - `/api/config` → start_dir
   - `/api/fs/list?path=/tmp`-equivalent → entries
   - `/api/fs/stat` on a `.parquet` → template field points at parquet_template.html
   - `/api/fs/raw` on a text file → bytes + MIME
   - `/render?path=<examples/sine.html>` → contains `runtime.js` script tag
   - `POST /api/run` `{py: <abs examples/sine.py>, params: {freq: "2"}}` → `ok: true`, points array
   - `POST /api/run` with missing main / raising main / non-JSON return → `ok: false`, structured error
   - executor timeout: `main` sleeping past a short timeout → TimeoutError dict
3. Parquet reader: generate small parquet via pyarrow in a temp dir, `POST /api/run` the reader with offset/limit → correct slice + total.

Manual (browser, after build): browse dirs, click parquet → paged table, click png → image, click sine.html → slider updates URL live, refresh restores, back/forward navigates dirs.

---

## 10. Style constraints

- Python: stdlib + fastapi + uvicorn + pyarrow only. Type hints on public functions. No classes where a function does.
- JS: no dependencies, no build. `const`/`let`, template literals, async/await. Small files > clever files.
- Shell CSS: system font stack, no framework. Dark theme is the product look — single palette in shell.css `:root` vars (bg #131417, panel #1b1d21, border #2a2d33, text #e8eaed, accent #5b9dff), `color-scheme: dark`; templates and examples match it.
- Error messages: always actionable — say what was wrong AND what shape was expected.

---

## 11. Panel mode (M5) — contracts

Split-pane grid of `/embed` iframes; the whole arrangement + per-pane locations + all params live in one bookmarkable URL. Full requirements in SPEC §14 (LM-1..LM-12), decisions D45/D46.

**Route sentinel.** `/view/_panel` (and `/embed/_panel`) is a sentinel pathname, not a file. `main.js` `route()` intercepts it under both prefixes **before** the `statPath` call, rendering the layout view + the layout-mode breadcrumb (sidebar only outside embed). The pane tree lives in the reserved `_layout` query param. Zero server changes — the server already serves the shell for any `/view/*` and `/embed/*`.

**`_layout` codec** (`views/layout-codec.js`, shared with tab mode §12). The pane tree lives in the reserved query param `_layout` (`_` prefix → invisible to `fused.params`, PR-6). `,` = row (side by side), `;` = column (stacked), `(…)` groups for nesting; a leaf = the pane's fs path + optional pane-local query. Within a segment the structural chars `, ; ( ) %` (and `?` inside the path, so the first `?` always separates path from query) are percent-encoded (`%25 %2C %3B %28 %29 %3F`) so the delimiters stay unambiguous; one left-to-right decode pass reverses it (`%25` → `%` and scanning continues, so literal escaped chars survive). URL grammar (D51): the whole value is **parenthesized and emitted last** — `?global=1&_layout=(…)` — and `&` is **literal inside the parens**, so the codec string keeps `, ; ( ) / ? & =` literal for a readable address bar; only `% #`/space are escaped when placing it inside the parens (one `decodeURIComponent` pass reverses that). Because `&` is literal, plain `URLSearchParams` cannot parse a layout URL: every shell-query read goes through the codec's `splitShellSearch` (balanced-paren scan — safe because literal parens inside segments are codec-escaped, so the only literal parens in the span are structural and balanced; returns the decoded codec string + the remaining params, excluding the span even when it is broken). Strict read: an unwrapped `_layout` value is not this grammar and reads as absent; an unbalanced span (paste-truncated trailing `)`, accepted breakage) is invalid → the mode's missing-layout fallback. The runtime (injected standalone, imports nothing) duplicates the scan as `splitSearch`: `fused.params` get/getAll parse only the non-layout remainder, and `set()` rebuilds the query with the raw `_layout=(…)` span untouched and last — layout URLs stay readable across param writes.

**Merged vs pane-local params.** Non-underscore params on the layout URL form one merged pool shared by every pane — a pane's runtime climbs to the layout shell (D46) and reads them directly, so merging is structural. Pane-local shell state (listing `sort`/`order`, `_mode`) stays on the pane's own embed URL, captured per-pane inside the `_layout` segment. The Split entry (`breadcrumb.js`) partitions the current view's params accordingly: `_`-prefixed + `sort`/`order` → pane segment; everything else → merged top-level pool.

**Panes.** Each pane is an `/embed/<path>` iframe (D39) with a bar: clickable path crumbs (click navigates that pane's iframe), split-right, split-down (new pane duplicates the pane's live location), maximize (transient — a `.maximized` class, `position:absolute inset:6px` inside the `position:relative` `.layout-root`, never encoded in the URL), close. Closing collapses single-child splits; closing the last pane exits to `/view/<that pane's path><query>`.

**URL sync up.** The panel view observes each pane's live location on the iframe `load` event **and** the pane window's `fused:urlchange` event (attached via the codec's shared `attachEmbedUrlChange` — a window-expando marker `_fusedUrlHooked` re-attached after each load, since the embed shell dispatches the event on client-side SPA navigation that fires no `load`). On either, it reads the pane's same-origin `contentWindow.location` (pathname under `/embed/`), updates that leaf, and re-encodes `_layout` via `history.replaceState` — guarded to only write when the encoded value changed. That replaceState fires the shell's own `fused:urlchange` (main.js wraps both `replaceState` and `pushState`), so the update-bookmark button reacts (D38). `stopPanel()` (parallel to `stopListingWatch()`) detaches the pane listeners when navigating away; `main.js` calls it at the top of `route()`.

## 12. Tab mode (M6) — contracts

Tabbed set of `/embed` iframes, one visible at a time; same URL-is-state model as §11. Full requirements SPEC §15 (TM-1..TM-10), decisions D47/D48.

**Route sentinel.** `/view/_tab` and `/embed/_tab`, intercepted in `route()` exactly like `_panel`. The tab list is a **flat top-level `,` row** of the shared `_layout` codec (§11); nested `;`/`()` structure is defensively flattened to leaves on parse. Missing/unparseable `_layout` → single tab of the start dir.

**Param independence.** Tab mode inverts §11's merged pool: `renderTabs()` sets `window._fusedParamBoundary = true` (cleared in `stopTabs()` — the shell window survives SPA navigation, a stale flag would corrupt the next view), and the runtime's ancestor climb stops below it (§5). Each tab's pages therefore read/write their **own pane's `/embed` URL**; the ordinary URL-sync captures the full pane query — user params included — **segment-local** inside `_layout`. The tab URL's top-level query carries no user params. A nested `_panel` inside a tab keeps merged-pool semantics among its own panes (its climb halts at the panel shell, just below the boundary) while isolated from other tabs.

**Tabs** (`views/tabs.js`). Iframes are **lazy-mounted on first activation and kept alive** (`display:none` when inactive) — state survives switching; iframes are never re-parented (that would reload them), only the bar is rebuilt. Tab label = basename of the tab's live path (sentinels label as `Panel`/`Tabs`); per-tab close `×`; trailing `+` opens a new tab at the start dir. The **active tab is not encoded in the URL** (refresh/bookmark restores the first tab — deliberate, avoids update-bookmark churn). Closing the last tab exits to a plain view of its live location in the active prefix. URL sync + `fused:urlchange` attachment (the codec's shared `attachEmbedUrlChange`/`detachEmbedUrlChange`, expando `_fusedUrlHooked`) and `stopTabs()` teardown mirror §11.

**Folder entry** (`sidebar.js` → `composeFolderTabsUrl`, the documented acyclic import). Clicking a folder's name/row expands the folder (if collapsed) and opens `/view/_tab?_layout=(<children>)` — each child bookmark's pathname becomes the segment path and its **entire saved query stays segment-local** (no hoisting, no collisions; a `_panel`/`_tab` child just works since a segment path may be a sentinel). Only the folder glyph toggles collapse without opening. Folder click arms nothing; ★ Bookmark on the tab view saves the composed URL as a normal bookmark with the full D38 update flow.
