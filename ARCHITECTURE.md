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
├── frontend/                   # React shell source (D52/D53): Vite + React 18, TypeScript
│   ├── package.json  vite.config.js  index.html
│   └── src/
│       ├── main.tsx            # bootstrap: history wrapping, embed class, config load, mount
│       ├── App.tsx             # route dispatch: "/" redirect, _panel/_tab sentinels, stat -> listing/preview
│       ├── shell.css           # the shell stylesheet (same selectors as the vanilla shell)
│       ├── lib/                # non-React modules (ported ~verbatim from the vanilla shell)
│       │   ├── router.ts       # fs-path <-> URL codec, navigate(); dispatches "fused:navigate"
│       │   ├── api.ts          # fetch wrappers (config/list/stat/rawUrl)
│       │   ├── format.ts       # formatSize/formatMtime/basename (pure)
│       │   ├── bookmarks.ts    # bookmark store: sync in-memory cache + async PUT (pure data, no DOM)
│       │   ├── recents.ts      # recents store + useRecentsTracking (server file ~/.fused-render/recents.json, D115)
│       │   ├── layout-codec.ts # shared _layout codec + embed helpers (M5/M6)
│       │   └── hooks.ts        # useNavEpoch/useUrlVersion/useBookmarksVersion signals
│       ├── components/
│       │   ├── Sidebar.tsx     # Home, bookmark rows, folders, hover card, rename, DnD
│       │   └── Breadcrumb.tsx  # crumb bar + Bookmark/Update/split-icon buttons
│       └── views/
│           ├── Listing.tsx     # dir table + sortable columns + WS dir watch
│           ├── Preview.tsx     # two-way dispatch: templates non-empty → TemplatePreview, else fallback
│           ├── Panel.tsx       # split-pane grid (M5): tree ops + pane bars
│           └── Tabs.tsx        # tab mode (M6): tab bar + lazy keep-alive iframes
├── fused_render/
│   ├── __init__.py             # __version__
│   ├── cli.py                  # arg parse → uvicorn.run + open browser
│   ├── server.py               # FastAPI app factory, all endpoints
│   ├── executor.py             # runner: in-process for first-party helpers, subprocess for user code (D72)
│   ├── _child.py               # worker-process entry (subprocess path)
│   ├── _binding.py             # param coercion shared by both execution paths
│   ├── logs.py                 # rotating app log for 500 / right-click-open diagnostics (D68)
│   ├── static/
│   │   ├── shell-dist/         # Vite build of frontend/ (gitignored, D54; built by dev / packaging hook)
│   │   └── runtime.js          # injected into every rendered HTML (plain JS, NOT part of the React app)
│   └── templates/              # one self-contained folder per template (M8); folder name = template name = _mode value
│       ├── table/              # template.html + reader.py + icon.svg   (was parquet_template.html)
│       ├── csv/                # template.html + reader.py + icon.svg
│       ├── xlsx/               # template.html + reader.py + icon.svg
│       ├── tree/               # template.html + icon.svg               (was json_template.html)
│       ├── markdown/           # template.html + icon.svg
│       ├── image/              # template.html + icon.svg
│       ├── media/              # template.html + icon.svg
│       ├── pdf/                # template.html + icon.svg
│       ├── code/               # template.html + icon.svg
│       ├── text/               # template.html + icon.svg
│       ├── shared/             # first-party ESM shared by sci templates (/template-shared mount) — no template.html, never a template name
│       └── vendor/             # vendored JS libs (/template-assets mount) — no template.html, never a template name
└── examples_seed/           # repo-root seed set, force-included into wheel
    └── sine/
        ├── sine.py
        └── sine.html
```

Shell = React 18 + Vite + TypeScript (D52/D53; strict tsc gated in the build). Build with `cd frontend && npm run build` — output is NOT committed (D54): dev machines need node, wheels/DMG build it via the hatch hook (scripts/hatch_build.py). Templates, examples and `runtime.js` stay plain ES2020 JS with no build step and no JS dependencies — the rendering primitive is framework-free by design.

---

## 2. CLI (`cli.py`)

```
fused-render [--start-dir DIR] [--port N] [--no-browser]
```

- `--start-dir` default `~` (home). UI starting location only — **whole filesystem is browsable** (no root-scoping concept anywhere).
- `--port` default `1777`.
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
  "templates": [
    {"mode": "table", "path": "/…/fused_render/templates/table/template.html", "icon": "/…/fused_render/templates/table/icon.svg"},
    {"mode": "code",  "path": "/…/fused_render/templates/code/template.html",  "icon": null}
  ]
}
```
`templates` is the server-side registry lookup on the basename (SPEC PT-7/PT-8, CT-3): the ordered **mode list**, first entry = default. Each entry carries the template **name** (`mode`), the resolved abs `template.html` path, and the abs path of the `icon.svg` sitting next to the resolved `template.html` (user folder's icon when a user template resolved) or `null` when absent. `templates` is `[]` for unmapped file extensions and `null` registry bindings. A **directory** always matches at least the universal `/` key (`["_listing"]`, SPEC PT-13/D81) and previews like a file — a `.zarr` directory matches `".zarr/"` → `[{"mode": "zarr", …}, {"mode": "_listing", …}]`; it is `[]` only when a `null` binding disables it (the shell then lists anyway). `.html`/`.htm` default to `["_render", "code"]` via the built-in registry (user-rebindable since D73); `_render` is a **sentinel mode** (SPEC PT-12) — `_`-prefixed, no template folder behind it — emitted without touching the filesystem:

```json
"templates": [
  {"mode": "_render", "path": null, "icon": null},
  {"mode": "code", "path": "/…/fused_render/templates/code/template.html", "icon": "/…/fused_render/templates/code/icon.svg"}
]
```

A `_`-prefixed name appearing in a registry list is valid only for the known sentinels (`KNOWN_SENTINELS = {"_render", "_listing"}`, D73/D81); any other is invalid (dropped + `template_error`) — the rest of the sentinel namespace is shell-owned. The user registry (§7, SPEC §16) is consulted first; validation is **per entry** — an entry whose name can't resolve (unsafe name, `template.html` missing in both locations) is dropped from the list and a `"template_error": "<reason>"` field names the first problem (absent otherwise); if the user's value resolves to nothing at all, the built-in list for the extension is used. There is no singular `template` field — removed in M8, no compat alias (shell is same repo).

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
{"ok": false, "error": {"type": "ZeroDivisionError", "message": "…", "traceback": "…", "where": {"file": "/abs/user.py", "line": 5, "func": "main", "source": "return 1 / x"}}, "stdout": "…"}
```
Endpoint is sync `def` → FastAPI runs it in its threadpool → concurrent runPython calls work (RH-4).

### `GET /static/*`
StaticFiles mount for shell + runtime. Templates dir is NOT statically mounted — templates are served through `/render` like any HTML file.

---

## 4. Executor protocol (`executor.py` + `_child.py`) — ALREADY IMPLEMENTED

- `run_python(path, params, timeout=30.0) -> dict`: routes by target (D72). A `.py` on the **`INPROCESS_HELPERS` allowlist** (the table/csv/xlsx readers + api inspector — trusted, fast, never import/exec user code) runs **in-process** via `_run_inprocess`; anything else (user scripts, user template readers, and other shipped `templates/` helpers like the claude agent / geo tile servers) **spawns `[sys.executable, _child.py]`**, writes `{"path", "params"}` JSON to stdin, `subprocess.run(timeout=…)`, parses **last stdout line** as result JSON. Timeout → `TimeoutError` error dict. Garbage/no output → `ExecutorError` with stderr tail.
- `_child.py` (subprocess path): chdir to the .py's dir (relative data paths work), prepend dir to `sys.path`, import via `importlib.util.spec_from_file_location`, find callable `main`, bind params (via `_binding.bind_params`) with annotation-based coercion (`"100"`→int, `"2.4"`→float, `"true"/"1"/"yes"/"on"`→bool), missing required arg / non-callable main → structured error. Extra params ignored unless `**kwargs`. Return value must be JSON-native, else clear TypeError suggesting `df.to_dict('records')`. User `print()` captured → returned as `stdout` field. Catches `BaseException` (incl. SystemExit).
- `_run_inprocess` (in-process path): imports the helper, binds params with the same `_binding` coercion, calls `main`, JSON-checks the result, catches `BaseException` → error dict. No chdir (helpers take absolute paths), no stdout capture (helpers don't print; global `sys.stdout` redirect would race the threadpool), no timeout (bounded reads / `ast` parse). Shares the app's macOS TCC grant because it runs in the server (= app) process — that is the point (D72).
- `_binding.py`: `coerce` / `bind_params` / `ParamError`, shared by both paths so param binding is identical, plus the shared error-diagnostics helpers `trim_harness_frames` / `user_location` (D128).
- Error DX (D128): on either path the `traceback` string is **trimmed to start at the caller's code** — leading runner frames (`_child.py`/`executor.py` and `_binding.py`, plus `<frozen importlib…>` bootstrap) are dropped; a harness-raised error (bad params, missing `main`, unserializable return) formats as the exception line only, no stack. `error.where` = `{file, line, func, source}` of the **deepest frame in the user's own file** (an error inside a library points at the user line that called it; a SyntaxError's location comes off the exception), or `null` when the error never touched the user's file (harness errors, timeout, missing file, worker crash). The fused engine (§20) fills the same `where` by parsing its already-cleaned traceback string.

Fresh process per call = fresh code every call for user code (PY-9); the env is whatever Python launched the server. First-party helpers run in-process (D72).

---

## 5. Injected runtime (`runtime.js`)

Iframe is **same-origin** (src = `/render?path=…` on the same host) → no postMessage protocol; runtime touches an ancestor window directly. The param target is the **topmost same-origin ancestor** (D46): the runtime climbs `window.parent` while the next ancestor is same-origin (probed via a try/catch on `.location.href`) **and not a param boundary** — an ancestor with `_fusedParamBoundary` set stops the climb *below* it (both layout shells set one, D47/D72). In normal view/embed mode the direct parent is already the top, so nothing changes; in a layout mode (panel or tab) the climb stops at each pane's/tab's own embed shell, so params stay pane-local — captured segment-local inside `_layout` by the shell's URL sync. Reads (get/getAll) additionally merge the same-origin ancestor chain *above* the boundary: hand-typed globals on the layout shell URL are visible in every pane (nearer ancestor wins, pane-local wins over all; D72); `set()` only ever writes the target, so a pane setting a globally-present key shadows it locally. Must also work when `/render?path=…` is opened as the top-level page (then `target === window`, also the fallback for a cross-origin ancestor; params live on the /render URL itself alongside `path` — `path` is owned by the server route, treat it as reserved too). Notification is a single channel: `set()` and any ancestor URL write both surface as a `fused:urlchange` event on the target window, and `onChange` fires only when the non-reserved param snapshot actually changed (diff guard — kills loops and the duplicate a self-`set` would otherwise cause). The target URL may carry the parenthesized `_layout` param, which contains literal `&` (D51, §11): the runtime never parses the target search with raw `URLSearchParams` — its `splitSearch` duplicate strips the raw `_layout=(…)` span first and `set()` reinserts it untouched and last.

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
- **runPython:** POST `/api/run` with `{py: pyPath, html: <own file path>, params}`. Own file path = `path` query param of the iframe's own URL. Non-ok response → reject with `Error` carrying `.type`, `.traceback`, `.where` (user-script location or null, §4), `.stdout`. If `stdout` non-empty (ok or not), `console.log` it prefixed `[python]`.
- **params.get/getAll:** read `parent.location.search`, excluding reserved keys (`_`-prefixed). `_file` is special: read-only, sourced from the iframe's **own** URL query (the shell puts it on the iframe src), so the shell URL never duplicates the path.
- **params.set(k, v):** throws if `k` starts with `_` or `v` is not a string. Updates parent URL via `parent.history.replaceState` (always replace — PR-3), then fires local onChange listeners. Strings only (PR-5).
- **onChange(cb):** called with `getAll()` result after every applied `set`. (No cross-source change feed in v1 — params only change via the page itself.)
- **Error overlay:** module-level helper — on unhandled promise rejection carrying `.traceback` (i.e. a runPython failure the page didn't catch), render a fixed-position red-bordered overlay with type, message, and a `.where` headline (`<file>, line N, in <func>` + the source line — the failing line of the user's script) when known. The headline is the culprit; the full traceback is collapsed by default (a `Show traceback` toggle reveals it) so it doesn't repeat the line the trimmed traceback now leads with. Collapse is done by rendering the `<pre>` **sr-only** (`clip`-rect, off-screen) rather than `display:none`, so a whole-page select-all still captures the full traceback (verified: `clip`-rect survives select-all where `display:none`/`<details>` do not) — one node, so expanding never double-copies it. A **`Copy error` button** is the guaranteed way to copy the entire debug message (type + message + full traceback) via the clipboard API, with a hidden-textarea + `execCommand` fallback for the sandboxed iframe. Two cases show the traceback outright (no headline) instead: no `.where` (harness error, timeout, missing file — the traceback is the only content), and a `SyntaxError`/`IndentationError`/`TabError` (its traceback is the `format_exception_only` caret block, whose `^` column marker isn't captured in `.where`). Author-handled rejections show nothing.

Top-level `path` handling in shell URL vs iframe URL:
- Shell URL: `/view/<fs-path>?freq=2.4` — params live here (source of truth, PR-1).
- Iframe URL: `/render?path=<abs html path>` — no user params needed on it; runtime reads/writes **parent's** query string.
- Standalone fallback (`parent === window`): read/write own URL's query, skipping `path`.

---

## 6. Shell (`frontend/` → `static/shell-dist/`)

SPA, React 18 + Vite (D52/D53; TypeScript, strict). `src/lib/` is non-React and ported ~verbatim from the vanilla shell (router/api/format/bookmarks/layout-codec — same contracts as before); components consume it. Dependency direction is one-way as before: `App → views/Sidebar/Breadcrumb → lib/*`; the router never imports UI (it dispatches a `fused:navigate` event; `lib/hooks.ts` turns it plus `popstate` into a **nav epoch** that keys — i.e. remounts — the active view, the React equivalent of the vanilla per-route DOM rebuild), the bookmark store never touches the DOM (mutations signal via `notifyBookmarksChanged()`). `Breadcrumb.tsx` may import `views/Panel.tsx` (`panelUrl`), and `Sidebar.tsx` may import `views/Tabs.tsx` (`composeFolderTabsUrl`), since no view imports back — no cycles. The history replaceState/pushState wrapping (→ `fused:urlchange`) lives in `main.tsx` and is load-bearing for the iframe runtimes (D46), not just for the shell's own re-renders; chrome (bookmark buttons, active highlight) re-renders on a **url version** signal that also counts `fused:urlchange`, without remounting views. Layout-mode iframes freeze their `src` at mount — React never rewrites it (a src write reloads an iframe); pane crumb clicks write it imperatively via a ref, and tab frames render as a flat keyed list that only appends/removes (never re-parents/reorders). Routing from `location.pathname`:
- `/` → redirect (replaceState) to `/view/<start-dir>` (start dir from `GET /api/config` → `{"start_dir": "/Users/vasu", "home": …}` — `source_template` was dropped with the html sentinel modes (D62); the code-view path arrives via `stat.templates` like everything else, and the shell `Config` type dropped it too).
- `/view/<path>` → `stat` it:
  - a target with a non-empty `stat.templates` → preview view — **including a directory** (every directory resolves at least the universal `/` key → `["_listing"]`, SPEC PT-13/D81; the built-in listing is the `_listing` sentinel mode)
  - **dir** with an empty `templates` (a `null` binding disabled it) → listing view — the shell's safety net (a folder must always render something)
  - **file** → preview view (templates or fallback)

**Listing view:** breadcrumb bar (each segment navigates) + rows: icon (dir/file), name, human size, mtime. Columns sortable — sort key/order live in URL params (`?sort=name|size|mtime&order=asc|desc`, replaceState), dirs always group before files, ties fall back to name. Click dir → `pushState` navigate. Click file → `pushState` navigate. `popstate` → re-route.

**Preview view:** breadcrumb + filename header with actions, then dispatch **exactly two-way** (no extension checks left in the shell — `HtmlPreview` is deleted; html arrives through `stat.templates` via the `_render` sentinel, SPEC PT-12):

1. `stat.templates` non-empty → `TemplatePreview`: pick the active entry — `_mode=<name>` on the **shell URL** selects by `mode`, absent or unknown/stale value → `templates[0]` (the default; old `_mode=source` bookmarks land here silently — accepted break, the mode is now named `code`). Ordinary entries: iframe src = `/render?path=<entry.path>&_file=<target file>` with `key={mode}` — a mode switch swaps the src and gets a fresh document. **Sentinel entries** (`_`-prefixed, `path: null`): `mode === "_render"` → iframe src `/render?path=<the file itself>` (no `_file`); `mode === "_listing"` → the shell **mounts its `Listing` component in place of the iframe** (no `_file`, no iframe — SPEC PT-12/D81), so a directory's file listing is a switchable mode; unrecognized sentinels are filtered out defensively. `_file` rides on the iframe's own URL; the shell URL stays clean (its pathname already names the file). The runtime reads `_file` from its own URL first and falls back to the shell URL, so manually opening `/view/<template>.html?_file=<target>` (old bookmarks) also works. Selecting the default mode **deletes** `_mode` (replaceState, clean URLs). Accepted quirk: non-reserved template params (`offset`, …) persist on the shell URL across switches; two modes using one param name differently collide — documented, not prevented.
2. else → fallback: metadata card (name, size, mtime, path) + `Raw / download` link to `/api/fs/raw?path=…`.

**Mode switcher** (in `Preview.tsx`): rendered only when there is more than one entry (`templates.length > 1`); positioned right side of the preview header bar. One **icon-only button** per mode; mode name via native `title` tooltip; active mode tinted accent. Icons load through `GET /api/fs/raw?path=<entry.icon>` (existing endpoint, no new routes) and are **monochrome SVGs** tinted via CSS `mask-image: url(...)` + `background-color: currentColor`, so active/inactive coloring is free. `entry.icon === null` → placeholder: first letter of the mode name in a small rounded box (shell-rendered, no file involved) — except the sentinel modes, which get **shell-baked SVGs** (component-local; sentinels have no folder to ship `icon.svg`): `_render` an eye, `_listing` a list glyph (D81). Clicking a mode writes/deletes `_mode` via `history.replaceState` (D8; same mechanics as the old D40 toggle).

Header actions always include `Raw` (opens raw endpoint in new tab). Iframe fills remaining viewport height, `border: none`.

**Directory views (SPEC PT-13/D65, revised D81):** every directory resolves through the registry like a file — the universal `/` key gives a plain folder `["_listing"]` and `.zarr/` gives `["zarr", "_listing"]`. The built-in listing is the `_listing` sentinel mode, so it rides the ordinary switcher and `_mode` selection: a plain folder shows the listing (single mode, no switcher); a `.zarr` store shows the zarr map with `_listing` a click away (`_mode=_listing`), `TemplatePreview` rendering `<Listing>` in the body for that mode. The old one-way `?listing=1` "Browse contents" button is **removed** (D81) — the switcher's `_listing` icon replaces it. Because embed hides the whole `.preview-header` (and switcher), a `.preview-browse-chip` corner button remains in embed only (`.preview-body` is `position: relative`), toggling the `_listing` mode (writing/deleting `_mode`) so an embedded directory can still reach its members. Annotate is not offered for `_listing` (no iframe).

**Param hygiene:** when navigating between files/dirs, drop old view params (fresh query string except `_file` set by dispatch).

### 6.5 Sidebar & bookmarks (M2)

Layout: `#app` becomes two-column flex — fixed sidebar (~220px, `--bg-alt`, right border) + existing content column (breadcrumb + content).

- **Home entry:** icon + "Home"; click → `navigate(config.home)`. `/api/config` response gains `"home": os.path.expanduser("~")`.
- **Bookmark capture:** "+ Bookmark" button right-aligned in the breadcrumb bar (present on every view); shows accent "starred" state when the current URL is already bookmarked. On click: `{id: crypto.randomUUID(), name: renderedTitle || basename(currentFsPath), url: location.pathname + location.search, created_at: Date.now()}` appended to store; sidebar re-renders. `renderedTitle` is the previewed page's own `<title>` when known (StatView, threaded through `Breadcrumb`) — preferred over the file's basename so a page's authored title wins.
- **Store (D75):** server-side `~/.fused-render/bookmarks.json`, JSON array (tree). Backend in `fused_render/shell/` — `storage.py` (home dir via `home_dir()`/`FUSED_RENDER_HOME`, atomic `read_json`/`write_json`) + `bookmarks.py` (`APIRouter`: `GET /api/bookmarks` → `{exists, bookmarks, missing}`; `PUT` whole-tree, atomic, last-write-wins, `X-Fused` guard). Frontend `bookmarks.ts` keeps an in-memory cache (`loadBookmarks()`/`allBookmarks()` sync off it), hydrated once at boot by `hydrateBookmarks()`; each mutation clones → `await`s the PUT → advances the cache (no optimism/rollback — cache never holds unpersisted state). The one-time legacy `localStorage["fused.bookmarks"]` import (D75) has been removed (D104) — every pre-D75 install has long since migrated. A 30 s `setInterval` in `main.tsx` calls `refreshBookmarks()` (a `GET` through the same serial queue, re-rendering only on a real diff) so another tab's/window's edits converge (D77 — eventual ≤30 s, last-write-wins on simultaneous writes). Hydration, every mutation, and the poll all run through one `enqueue` chain, so no read/write ever interleaves (closes the hydration/mutation race Bugbot flagged). `shell/` is the seam for future shell-state backends, kept out of `server.py`'s fs/render internals (and acyclic — it never imports `server`).
- **Missing-file flag (D127):** `GET /api/bookmarks`'s `missing` field is bookmark ids whose target is confirmed gone from disk — a display-only side-channel, recomputed fresh on every GET and never written into `bookmarks.json` or round-tripped through `PUT`. `bookmarks.py` flattens the tree (`_flatten_bookmarks`, arbitrary folder depth) and fans the existence checks out concurrently on a dedicated `ThreadPoolExecutor` under one wall-clock budget (`_MISSING_CHECK_BUDGET_S`), mirroring `recents.py`'s `CHECK_BUDGET_S`/`_CHECK_POOL` — a check that outlives the budget is NOT flagged (fail open). Existence is checked via `pathops.exists` (new alongside the existing `is_file` trio), mount-safe (routes a mount-backed path through `mounts.rc_stat_for`, never a kernel stat) and, unlike Recents' files-only contract, also accepts a directory (a bookmark may target a listing). Frontend `bookmarks.ts` tracks the ids in a separate `missingIds` set (never merged into the persisted tree), exposed via `isBookmarkMissing(id)`; `Sidebar.tsx` adds a warning glyph (own stacking context so the row's stretched-link overlay doesn't swallow its hover/title) and a hover-card note — the row's name keeps its normal color, on owner request. `main.tsx` also refreshes `missingIds` immediately on window `focus` (in-flight guarded, mirroring `ServerStatusBanner`'s probe, D126), in addition to the existing 30s poll. Recents (§29, D115) is unchanged — it stays hidden-when-missing by deliberate owner choice, so this flag is Bookmarks-only.
- **Bookmark row:** name ellipsized, rendered as a real `<a href="<url>">` (verbatim URL per D20; href kept for middle-click/copy-link). Plain click is intercepted: it **arms** the bookmark for update tracking and routes in-shell via `navigateUrl(url)` (pushState that preserves the query string, unlike `navigate()`). Hover shows a floating card beside the sidebar: decoded target path + saved params as a key/value grid ("no params" when none); card hides during rename/delete. Hover also reveals ✎ rename (inline `<input>`, Enter/blur commits, Escape cancels) and ✕ delete (no confirm). Active bookmark (url == current URL) is highlighted.
- Order: creation time. Duplicates allowed.
- **Bookmark updating (D38):** the armed bookmark `{id, url}` lives in sessionStorage `fused.armedBookmark` (survives refresh, not new tabs). `breadcrumb.js` renders a hidden "Update bookmark" button left of "+ Bookmark"; `syncUpdateButton()` shows it iff armed, same pathname, and `location.search` differs from the armed url's search. Clicking it overwrites the bookmark's url with the current one and re-arms against it. A pathname change disarms permanently; deleting the armed bookmark disarms. Param changes are observed by `main.js` wrapping `history.replaceState` (the iframe runtime writes params through the parent's replaceState, which fires no native event) to dispatch a `fused:urlchange` window event; sidebar delete also dispatches it instead of importing breadcrumb (one-way deps, D28).

### 6.6 Recents (D115, SPEC §29)

Sidebar section listing the last 3 files opened, each with the params they last
had. Backend `shell/recents.py` (beside bookmarks/prefs): `~/.fused-render/recents.json`
holds `{collapsed, entries: [{url, openedAt, title?}]}` — urls verbatim incl. query
(D20 posture), newest first, deduped by target fs path, capped at 20; `GET
/api/recents` filters entries whose file no longer exists (without deleting
them), `POST /api/recents/open {url, title?}` records (file-view `/view/` urls only —
directories and `_`-sentinels no-op), `PUT /api/recents/collapsed` persists the
fold with the data (D44 posture). Frontend `lib/recents.ts` mirrors
`bookmarks.ts` (sync cache, serial queue, `notifyRecentsChanged` signal); its
`useRecentsTracking(fsPath, isDir, title)` is mounted in `App.tsx`'s StatView
beside the session hooks — records the open once the stat confirms a file,
then re-records the current url (and the page's own `<title>`, once known)
on every `fused:urlchange`/`popstate` (500 ms debounce) or title change, so
the entry tracks live param and title changes. Display order is
**stable-slot**, not raw MRU (RC-11): `displayRecents()` keeps session-scoped
slots — a displayed file's row updates in place (rows keyed by fs path; the
store notifies on visible changes only, urls included so hrefs stay fresh,
and param churn moves nothing); only a not-displayed file entering at
the top shifts rows, and a vanished file's slot fills from the bottom.
Sidebar rows prefer the recorded
`title` (the page's own `<title>`) and fall back to the basename otherwise
(D22, extended); click = `navigateUrl` (query-preserving), arms nothing; the
heading toggles the fold (count pill as the collapsed signal, no chevron — D44
visual language); the section is hidden while empty.

---

## 7. Template contract

- Built-in bindings ship as data — **`fused_render/templates/registry.json`** (D73), exactly the user-registry format: **suffix-pattern key → ordered list of template names, first = default** (M8). A name is a folder name, never a filename:
```json
{
  ".parquet": ["table"],
  ".csv": ["csv", "code"], ".tsv": ["csv", "code"],
  ".json": ["tree", "code"],
  ".py": ["code", "api"],
  ".html": ["_render", "code"], ".htm": ["_render", "code"],
  ".zarr/": ["zarr", "_listing"], "/": ["_listing"],
  "…": ["etc"]
}
```
(Full mapping + per-row rationale: SPEC PT-7 table.) `_templates_for(path, is_dir)` matches `os.path.basename(os.path.normpath(path))` against both registries with **one matcher** (`_match_registry`, SPEC CT-3): keys are dot-anchored suffix patterns — compound (`.tar.gz`), `*` wildcard = exactly one whole non-empty segment, trailing `/` = **directory key** (a `.zarr` store matches `".zarr/"`; dir keys match only directories, file keys only files). Specificity: more segments > fewer, ties broken rightmost-first with literal > `*`; a match needs a non-empty stem. Both registries are read per resolution by one loader (`_load_registry`); a built-in parse failure surfaces as `template_error`, and a test pins the shipped file (parses, every name resolves).
- **Name resolution — one rule everywhere (SPEC PT-6):** `<name>` → `~/.fused-render/templates/<name>/template.html` if it exists, else `fused_render/templates/<name>/template.html`, else unresolvable. Applies identically to built-in and user registry entries; a user folder shadows a built-in of the same name. **Sentinel special case (SPEC PT-12):** a name in `KNOWN_SENTINELS` (`{"_render", "_listing"}`, D81) never resolves through the filesystem — the resolver emits `{"mode": "_<name>", "path": null, "icon": null}` directly, and it is referenceable from either registry's lists (D73); any other `_`-prefixed name is invalid (dropped + `template_error` — the rest of the sentinel namespace is shell-owned). `_listing` (the built-in directory listing) is the default of the universal `/` directory key; the shell renders its own `Listing` component for it rather than an iframe (D81). `icon` = the `icon.svg` beside the resolved `template.html`, or `null`. `templates/vendor/` has no `template.html` so it can never resolve; the `/template-assets` mount is unchanged.
- **User overrides (M7 + M8, SPEC §16):** the resolver consults `~/.fused-render/templates/registry.json`, and any user match beats the built-in registry — including for `.html`/`.htm` (the old CT-4 exemption is dropped, D73) and for directory keys (D65's package-only restriction is dropped, D73). Keys follow the same CT-3 grammar above; values are `list | string | null`: a **list** is the full ordered mode list (replace semantics; the `"..."` entry splices the built-in list in place — dedup against explicit names, more than one `"..."` invalidates the entry, splice with no built-ins expands to nothing); a **string** is a single-mode list of that name (unchanged D50 meaning); **`null`** = no template at all, shell fallback (plain listing for a directory key). Names must be a single safe path segment (no `/`, `\`, `.`, `..`) since they're joined into a path — correctness guard, not auth (D3). Validation is per entry: an unresolvable entry is dropped and `template_error` on the stat payload names the first problem; a user value resolving to nothing falls back to the built-in list. Registries are read on every resolution (no restart, no cache); missing dir/registry is a clean no-op. Constants `BUILTIN_REGISTRY`/`USER_TEMPLATES_DIR`/`USER_REGISTRY` in `server.py`; runtime untouched — the shell obeys `templates` (§3, §6), and M4 auto-reload already live-reloads previews when the user edits their template or readers (registry edits apply on next stat, open previews don't watch it).
- Template receives target file as read-only param `_file`. Templates are ordinary renderable HTML: same runtime, same powers. Templates reach the filesystem through the runtime IO helpers (`fused.rawUrl`/`stat`/`readFile`/`writeFile`), never by fetching `/api/fs/*` URLs directly — one code path, and the write guard/lock come for free. Helper files sit inside the folder as `reader.py` etc.; relative `runPython('./reader.py', …)` just works because the `html` path sent to `/api/run` is the template's real path. Each built-in folder also ships a **monochrome `icon.svg`** (single fill — `currentColor` or plain black, only alpha matters since the shell masks it; square viewBox, 24×24 suggested, legible at 16px).
- Vendored JS libraries (marked, CodeMirror; and the sci decoders `geotiff.bundle.mjs`, `netcdfjs.bundle.mjs`, `zarrita.bundle.mjs`) live in `fused_render/templates/vendor/` and are served from a dedicated absolute mount `GET /template-assets/*` (a relative `<script src>`/`import` in a template would resolve against `/render`, not the templates dir). All committed local files — no CDN/network at runtime (D3). Regenerate the CodeMirror bundle via `scripts/vendor-codemirror/build.sh`, and the sci bundles via `scripts/vendor-sci/build.sh` (both Node 22; each emits a single self-contained ESM module).
- The sciViz core shared by the `geotiff/`, `netcdf/`, and `zarr/` templates (colormap LUTs, stretch/stats/histogram, canvas draw, and the plain-DOM UI kit) is first-party, not vendored — it lives in `fused_render/templates/shared/sciviz.mjs` and is served from its own absolute mount `GET /template-shared/*` (kept separate from `/template-assets` so `vendor/` stays third-party-only; like `vendor/`, `shared/` has no `template.html` so it can never resolve as a template name).
- `geotiff/`, `netcdf/`, `map/`, and `zarr_aoi/` each spin up a persistent localhost tile daemon (`tile_server.py` / `grid_tile_server.py` / `vector_tile_server.py`, bound to `127.0.0.1` on a random port) so MapLibre pan/zoom isn't capped by `runPython`'s ~700ms per-call subprocess cost; the daemon serves tiles/metadata straight to the template's iframe instead of through `/api/run`. Endpoints are read-only GETs (plus `map/`'s async `/open`) and answer `Access-Control-Allow-Origin: *` — the opposite CORS posture from the main server's D36 guard, and deliberately so (D122): the daemon's only client is a cross-port iframe that needs to read the response, unlike `/api/run`'s same-origin caller. Access is gated by a **per-daemon token** (not CORS, and not the loopback bind — a same-browser page can fetch loopback cross-origin): each daemon mints `secrets.token_urlsafe(32)` at startup, stores it in its state file, and returns it from `main("ensure")`; every endpoint except `/ping` requires `?t=<token>` (403 otherwise), and the templates thread it into their daemon URLs (the sci-viz `dURL()` chokepoint, `pyramid/`'s `/ltile` URL, and `map_render.py`'s tile/status/meta URLs).

**M1 templates** (folder names per M8 renames — `table/`, `image/`, `text/`):

- `table/` (`template.html` + `reader.py`):
  - reader `main(file: str, offset: int = 0, limit: int = 100)` → `{"columns": [...], "rows": [...], "total_rows": N}` via pyarrow (`pq.read_table(file).slice(offset, limit).to_pylist()`); cell values must be JSON-safe — stringify non-JSON scalars (timestamps, bytes, decimals) in the reader.
  - UI: table, row-count line ("rows 0–99 of 12,345"), Prev/Next buttons paging via `offset` param → `fused.params.set('offset', …)` → onChange → refetch; the call site passes `offset`/`limit` as **numbers** (params are URL strings — `Number()` where read). Loading + error states.
- `image/`: `<img src="/api/fs/raw?path=" + encodeURIComponent(fused.params.get('_file'))>`, centered, `max-width/height: 100%`, filename caption. No runPython needed.
- `text/`: `fetch('/api/fs/raw?path=…')` → text → `<pre>`. Guard: file > 2 MB → show "too large" note with raw link instead. Monospace, preserved whitespace.

**M2 templates** (added alongside M1; same runtime, same `_file` contract, same dark palette):

- `markdown/`: `fetch` raw → render with vendored `marked` (`/template-assets/marked.min.js`). GitHub-ish readable column (~46rem, centered). No sanitizer by design — local trust model (D3). Guard: file > 2 MB → "too large" note + raw link.
- `csv/` (`template.html` + `reader.py`): same UX as table (table, "rows X–Y of N", Prev/Next via `offset` param, typed call-site params). Reader `main(file, offset=0, limit=100)` via pandas; `.tsv` → tab sep, else comma. Reads the full file once for an honest `total_rows`, returns only the page. Same JSON-safe cell stringifying as `table/reader.py` (NaN → null, timestamps/bytes/decimals coerced).
- `tree/`: `fetch` raw → `JSON.parse` → collapsible tree in pure JS (no library). Objects/arrays fold (▾/▸), keys/primitives type-colored, arrays/objects show count, nodes deeper than depth 2 start collapsed. Parse failure → error + first 2 KB raw. Guard: file > 5 MB → "too large" note + raw link. Also serves `.geojson`.
- `xlsx/` (`template.html` + `reader.py`): openpyxl `read_only=True`, first row is header. Reader `main(file, sheet="", offset=0, limit=100)` → `{sheets, sheet, columns, rows, total_rows}`. Template adds a sheet `<select>` (shown when >1 sheet) wired to a `sheet` param (resets `offset` on change); paging like csv, typed call-site params (`sheet` stays a string). JSON-safe cells (datetimes → isoformat, None → null).
- `pdf/`: thin filename header + full-height `<embed type="application/pdf">` of the raw endpoint.
- `media/`: branches on extension — `<video>` for mp4/mov/m4v/webm, `<audio>` for mp3/wav/m4a/ogg/flac. `controls`, centered, filename caption, video constrained to viewport.
- `code/`: **editable** CodeMirror 6 (vendored `/template-assets/codemirror.bundle.js`, global `CM`), `CM.oneDark` theme to match the shell. `basicSetup` line numbers; language chosen by extension (py/js/ts/json/yaml/html/css + StreamLanguage shell/toml; unknown → plain). Guard: file > 2 MB → "too large" note + raw link (no editor). Top bar (matches other templates' `#bar`): filename + Saved/Modified status + Save button (disabled when clean). Save flow: `fused.stat` arms the mtime on load → `fused.writeFile(file, doc, {expectedMtime})` on save; Cmd/Ctrl+S bound at the window (CM's `keymap` isn't in the bundle); dirty tracked via `EditorView.updateListener` (docChanged); `beforeunload` warns when dirty. On a 409 conflict a bar banner offers **Reload** (refetch + re-arm, discard local) or **Overwrite** (write with no lock, re-arm).

---

## 8. Examples

- `examples_seed/sine/sine.py` — `main(n: int = 80, freq: float = 1.0)` → `{"points": [[x, y], …]}` (math.sin, stdlib only).
- `examples_seed/sine/sine.html` — range slider bound to `freq` param, SVG polyline chart (hand-rolled, no deps), wiring pattern:
  slider input → `fused.params.set('freq', value)`; `fused.params.onChange(draw)`; initial `draw()` reads param-or-default. Demonstrates: URL sync, refresh restores state, runPython round-trip, python print → browser console.

---

## 9. Verification checklist (M1 done =)

Automatable (curl / CLI):
1. `python -c "import fused_render.server"` etc. — all modules import.
2. Start `fused-render --no-browser --port <test>`; then:
   - `/api/config` → start_dir
   - `/api/fs/list?path=/tmp`-equivalent → entries
   - `/api/fs/stat` on a `.parquet` → `templates[0]` is `{"mode": "table", …}` pointing at templates/table/template.html
   - `/api/fs/raw` on a text file → bytes + MIME
   - `/render?path=<examples_seed/sine/sine.html>` → contains `runtime.js` script tag
   - `POST /api/run` `{py: <abs examples_seed/sine/sine.py>, params: {freq: "2"}}` → `ok: true`, points array
   - `POST /api/run` with missing main / raising main / non-JSON return → `ok: false`, structured error
   - executor timeout: `main` sleeping past a short timeout → TimeoutError dict
3. Parquet reader: generate small parquet via pyarrow in a temp dir, `POST /api/run` the reader with offset/limit → correct slice + total.

Manual (browser, after build): browse dirs, click parquet → paged table, click png → image, click sine.html → slider updates URL live, refresh restores, back/forward navigates dirs.

---

## 10. Style constraints

- Python: stdlib + fastapi + uvicorn + pyarrow only. Type hints on public functions. No classes where a function does.
- Shell: React 18 + Vite + strict TypeScript (D52/D53), function components + hooks only; no state library, no router library (the URL model is bespoke — `_layout` cannot ride a stock router). Small files > clever files.
- Template/runtime JS: no dependencies, no build. `const`/`let`, template literals, async/await.
- Shell CSS: system font stack, no framework. Dark theme is the product look — single palette in shell.css `:root` vars (bg #131417, panel #1b1d21, border #2a2d33, text #e8eaed, accent #5b9dff), `color-scheme: dark`; templates and examples match it.
- Error messages: always actionable — say what was wrong AND what shape was expected.

---

## 11. Panel mode (M5) — contracts

Split-pane grid of `/embed` iframes; the whole arrangement + per-pane locations + all params live in one bookmarkable URL. Full requirements in SPEC §14 (LM-1..LM-12), decisions D45/D46.

**Route sentinel.** `/view/_panel` (and `/embed/_panel`) is a sentinel pathname, not a file. `main.js` `route()` intercepts it under both prefixes **before** the `statPath` call, rendering the layout view + the layout-mode breadcrumb (sidebar only outside embed). The pane tree lives in the reserved `_layout` query param. Zero server changes — the server already serves the shell for any `/view/*` and `/embed/*`.

**`_layout` codec** (`views/layout-codec.ts`, shared with tab mode §12). The pane tree lives in the reserved query param `_layout` (`_` prefix → invisible to `fused.params`, PR-6). `,` = row (side by side), `;` = column (stacked), `(…)` groups for nesting; a leaf = the pane's fs path + optional pane-local query. Within a segment the structural chars `, ; ( ) %` (and `?` inside the path, so the first `?` always separates path from query) are percent-encoded (`%25 %2C %3B %28 %29 %3F`) so the delimiters stay unambiguous; one left-to-right decode pass reverses it (`%25` → `%` and scanning continues, so literal escaped chars survive). URL grammar (D51): the whole value is **parenthesized and emitted last** — `?global=1&_layout=(…)` — and `&` is **literal inside the parens**, so the codec string keeps `, ; ( ) / ? & =` literal for a readable address bar; only `% #`/space are escaped when placing it inside the parens (one `decodeURIComponent` pass reverses that). Because `&` is literal, plain `URLSearchParams` cannot parse a layout URL: every shell-query read goes through the codec's `splitShellSearch` (balanced-paren scan — safe because literal parens inside segments are codec-escaped, so the only literal parens in the span are structural and balanced; returns the decoded codec string + the remaining params, excluding the span even when it is broken). Strict read: an unwrapped `_layout` value is not this grammar and reads as absent; an unbalanced span (paste-truncated trailing `)`, accepted breakage) is invalid → the mode's missing-layout fallback. The runtime (injected standalone, imports nothing) duplicates the scan as `splitSearch`: `fused.params` get/getAll parse only the non-layout remainder, and `set()` rebuilds the query with the raw `_layout=(…)` span untouched and last — layout URLs stay readable across param writes.

**Pane-local params (D72).** The panel shell sets `window._fusedParamBoundary = true` (set at render, cleared on unmount — the shell window survives SPA navigation, a stale flag would corrupt the next view), so every pane's pages read/write their **own pane's `/embed` URL**; the ordinary URL-sync captures the full pane query — user params included — segment-local inside `_layout`. The layout URL's top-level query carries only hand-typed globals (never promoted by the shell; readable from every pane via the runtime's ancestor-read fallback, §5, and passed through untouched by the sync). The Split entry (`Breadcrumb.tsx`) puts the current view's **whole** query into each pane segment — no partitioning.

**Panes.** Each pane is an `/embed/<path>` iframe (D39) with a bar: clickable path crumbs (click navigates that pane's iframe), split-right, split-down (new pane duplicates the pane's live location), maximize (transient — a `.maximized` class, `position:absolute inset:6px` inside the `position:relative` `.layout-root`, never encoded in the URL), close. Closing collapses single-child splits; closing the last pane exits to `/view/<that pane's path><query>`.

**URL sync up.** The panel view observes each pane's live location on the iframe `load` event **and** the pane window's `fused:urlchange` event (attached via the codec's shared `attachEmbedUrlChange` — a window-expando marker `_fusedUrlHooked` re-attached after each load, since the embed shell dispatches the event on client-side SPA navigation that fires no `load`). On either, it reads the pane's same-origin `contentWindow.location` (pathname under `/embed/`), updates that leaf, and re-encodes `_layout` via `history.replaceState` — guarded to only write when the encoded value changed. That replaceState fires the shell's own `fused:urlchange` (main.js wraps both `replaceState` and `pushState`), so the update-bookmark button reacts (D38). `stopPanel()` (parallel to `stopListingWatch()`) detaches the pane listeners when navigating away; `main.js` calls it at the top of `route()`.

## 12. Tab mode (M6) — contracts

Tabbed set of `/embed` iframes, one visible at a time; same URL-is-state model as §11. Full requirements SPEC §15 (TM-1..TM-10), decisions D47/D48.

**Route sentinel.** `/view/_tab` and `/embed/_tab`, intercepted in `route()` exactly like `_panel`. The tab list is a **flat top-level `,` row** of the shared `_layout` codec (§11); nested `;`/`()` structure is defensively flattened to leaves on parse. Missing/unparseable `_layout` → single tab of the start dir.

**Param independence.** Same contract as panel mode (§11, D72): the tab shell sets `window._fusedParamBoundary = true` (cleared on teardown — the shell window survives SPA navigation, a stale flag would corrupt the next view), and the runtime's ancestor climb stops below it (§5). Each tab's pages therefore read/write their **own pane's `/embed` URL**; the ordinary URL-sync captures the full pane query — user params included — **segment-local** inside `_layout`. The tab URL's top-level query carries only hand-typed globals. A nested `_panel` inside a tab stays pane-local among its own panes (its own boundary) and isolated from other tabs.

**Tabs** (`views/tabs.js`). Iframes are **lazy-mounted on first activation and kept alive** (`display:none` when inactive) — state survives switching; iframes are never re-parented (that would reload them), only the bar is rebuilt. Tab label = basename of the tab's live path (sentinels label as `Panel`/`Tabs`); per-tab close `×`; trailing `+` opens a new tab at the start dir. The **active tab is not encoded in the URL** (refresh/bookmark restores the first tab — deliberate, avoids update-bookmark churn). Closing the last tab exits to a plain view of its live location in the active prefix. URL sync + `fused:urlchange` attachment (the codec's shared `attachEmbedUrlChange`/`detachEmbedUrlChange`, expando `_fusedUrlHooked`) and `stopTabs()` teardown mirror §11.

**Folder entry** (`sidebar.js` → `composeFolderTabsUrl`, the documented acyclic import). Clicking a folder's name/row expands the folder (if collapsed) and opens `/view/_tab?_layout=(<children>)` — each child bookmark's pathname becomes the segment path and its **entire saved query stays segment-local** (no hoisting, no collisions; a `_panel`/`_tab` child just works since a segment path may be a sentinel). Only the folder glyph toggles collapse without opening. Folder click arms nothing; ★ Bookmark on the tab view saves the composed URL as a normal bookmark with the full D38 update flow.
