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
│   │   ├── shell.js
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

All paths in query strings are **absolute filesystem paths**. Server never scopes/rejects by location (v1 has no security layer — deliberate, see SPEC §9). Errors return `{"error": "<message>"}` with 4xx status.

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
`template` is the server-side registry lookup by extension (lowercased). `null` for dirs, `.html` files (those render live), and unmapped extensions.

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

### `GET /render?path=<abs .html file>`
Reads file text, injects `<script src="/static/runtime.js"></script>`:
- if `<head>` present (case-insensitive): right after it;
- else: prepended to the document.
Returns `text/html`. Used as iframe src by the shell for both user HTML and templates.

### `POST /api/run`
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

Iframe is **same-origin** (src = `/render?path=…` on the same host) → no postMessage protocol; runtime touches `window.parent` directly. Must also work when `/render?path=…` is opened as the top-level page (then `parent === window`; params live on the /render URL itself alongside `path` — `path` is owned by the server route, treat it as reserved too).

```js
window.fused = {
  runPython(pyPath, params) -> Promise<result>,
  params: { get(k), getAll(), set(k, v), onChange(cb) -> unsubscribe },
};
```

Behavior:
- **runPython:** POST `/api/run` with `{py: pyPath, html: <own file path>, params}`. Own file path = `path` query param of the iframe's own URL. Non-ok response → reject with `Error` carrying `.type`, `.traceback`, `.stdout`. If `stdout` non-empty (ok or not), `console.log` it prefixed `[python]`.
- **params.get/getAll:** read `parent.location.search`, exclude reserved keys **except** expose `_file` read-only (templates need it). Reserved = keys starting `_`.
- **params.set(k, v):** throws if `k` starts with `_` or `v` is not a string. Updates parent URL via `parent.history.replaceState` (always replace — PR-3), then fires local onChange listeners. Strings only (PR-5).
- **onChange(cb):** called with `getAll()` result after every applied `set`. (No cross-source change feed in v1 — params only change via the page itself.)
- **Error overlay:** module-level helper — on unhandled promise rejection carrying `.traceback` (i.e. a runPython failure the page didn't catch), render a fixed-position red-bordered overlay with type, message, `<pre>` traceback. Author-handled rejections show nothing.

Top-level `path` handling in shell URL vs iframe URL:
- Shell URL: `/view/<fs-path>?freq=2.4` — params live here (source of truth, PR-1).
- Iframe URL: `/render?path=<abs html path>` — no user params needed on it; runtime reads/writes **parent's** query string.
- Standalone fallback (`parent === window`): read/write own URL's query, skipping `path`.

---

## 6. Shell (`shell.html/css/js`)

SPA, no framework. Routing from `location.pathname`:
- `/` → redirect (replaceState) to `/view/<start-dir>` (start dir from `GET /api/config` → `{"start_dir": "/Users/vasu"}` — add this tiny endpoint).
- `/view/<path>` → `stat` it:
  - **dir** → listing view
  - **file** → preview view

**Listing view:** breadcrumb bar (each segment navigates) + rows: icon (dir/file), name, human size, mtime. Click dir → `pushState` navigate. Click file → `pushState` navigate. `popstate` → re-route.

**Preview view:** breadcrumb + filename header with actions, then dispatch **exactly three-way** (no other file-type logic in shell):

1. `stat.template != null` → iframe `/render?path=<template>`, and ensure shell URL carries `?_file=<target file>` (replaceState merge before iframe insert).
2. extension `.html`/`.htm` → iframe `/render?path=<file itself>`. Header gets `Rendered | Source` toggle (Source shows fetched text in `<pre>`).
3. else → fallback: metadata card (name, size, mtime, path) + `Raw / download` link to `/api/fs/raw?path=…`.

Header actions always include `Raw` (opens raw endpoint in new tab). Iframe fills remaining viewport height, `border: none`.

**Param hygiene:** when navigating between files/dirs, drop old view params (fresh query string except `_file` set by dispatch).

### 6.5 Sidebar & bookmarks (M2)

Layout: `#app` becomes two-column flex — fixed sidebar (~220px, `--bg-alt`, right border) + existing content column (breadcrumb + content).

- **Home entry:** icon + "Home"; click → `navigate(config.home)`. `/api/config` response gains `"home": os.path.expanduser("~")`.
- **Bookmark capture:** ★ button right-aligned in the breadcrumb bar (present on every view). On click: `{id: crypto.randomUUID(), name: basename(currentFsPath), url: location.pathname + location.search, created_at: Date.now()}` appended to store; sidebar re-renders.
- **Store:** localStorage key `fused.bookmarks`, JSON array. Read/write helpers with try/catch (corrupt JSON → treat as empty, overwrite on next save).
- **Bookmark row:** name (ellipsized, `title` = url). Click → `location.href = bookmark.url` (plain redirect, no SPA routing). Hover reveals ✎ rename (inline `<input>`, Enter/blur commits, Escape cancels) and ✕ delete (no confirm).
- Order: creation time. Duplicates allowed.

---

## 7. Template contract

- Server-side registry (in `server.py`):
```python
TEMPLATES = {
  ".parquet": "parquet_template.html",
  ".png": "image_template.html", ".jpg": "image_template.html", ".jpeg": "image_template.html",
  ".gif": "image_template.html", ".webp": "image_template.html", ".svg": "image_template.html",
  ".txt": "text_template.html", ".py": "text_template.html", ".js": "text_template.html",
  ".ts": "text_template.html", ".json": "text_template.html", ".md": "text_template.html",
  ".csv": "text_template.html", ".log": "text_template.html", ".yaml": "text_template.html",
  ".yml": "text_template.html", ".toml": "text_template.html", ".sh": "text_template.html",
}
```
- Template receives target file as read-only param `_file`. Templates are ordinary renderable HTML: same runtime, same powers. Helper `.py` files sit next to the template; relative `runPython('./parquet_reader.py', …)` just works because `html` path sent to `/api/run` is the template's real path.

**M1 templates:**

- `parquet_template.html` + `parquet_reader.py`:
  - reader `main(file: str, offset: int = 0, limit: int = 100)` → `{"columns": [...], "rows": [...], "total_rows": N}` via pyarrow (`pq.read_table(file).slice(offset, limit).to_pylist()`); cell values must be JSON-safe — stringify non-JSON scalars (timestamps, bytes, decimals) in the reader.
  - UI: table, row-count line ("rows 0–99 of 12,345"), Prev/Next buttons paging via `offset` param → `fused.params.set('offset', …)` → onChange → refetch. Loading + error states.
- `image_template.html`: `<img src="/api/fs/raw?path=" + encodeURIComponent(fused.params.get('_file'))>`, centered, `max-width/height: 100%`, filename caption. No runPython needed.
- `text_template.html`: `fetch('/api/fs/raw?path=…')` → text → `<pre>`. Guard: file > 2 MB → show "too large" note with raw link instead. Monospace, preserved whitespace.

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
- Shell CSS: system font stack, clean neutral look, no framework.
- Error messages: always actionable — say what was wrong AND what shape was expected.
