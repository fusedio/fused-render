# fused-render — Requirements Specification

**Status:** Draft v0.1
**Scope:** Fully local system. Never deployed to cloud. Single user, single machine.

---

## 1. Overview

fused-render is a local file explorer consisting of:

1. **A local server** that runs on the user's machine, exposes the file system, executes user-authored Python, and serves the browser UI.
2. **A browser UI** where the user browses their computer's files and views rich previews of them.

The differentiating feature is the **renderable HTML** system: HTML files can call Python functions inline for data, and can sync their internal state to the browser URL via **params**. Built-in **preview templates** for known file formats (parquet, CSV, images, …) are themselves just renderable HTML files that ship with the application — the same primitives (params + Python execution) are the entire rendering power of the system.

### Goals

- Browse the local file system in the browser with fast navigation.
- Preview any supported file format with a rich, format-appropriate UI.
- Let users author their own interactive HTML views backed by local Python code.
- Make preview URLs shareable-with-self / bookmarkable: URL fully reconstructs the view state.

### Non-Goals

- Cloud or remote deployment, multi-user access, authentication/user accounts.
- File editing (v1 is read/preview oriented; editing is a possible v2).
- Sandboxing Python for safety against the *user's own* code — the user's code is trusted. (Protecting against *other websites* driving the server is in scope; see §9.)

---

## 2. Architecture

```
┌────────────────────────────── Browser ──────────────────────────────┐
│  Explorer Shell (app UI)                                            │
│  ├── File tree / directory listing                                  │
│  ├── URL routing  (/view/<path>?params…)                            │
│  └── Preview pane                                                   │
│        └── plain same-origin <iframe> ← renderable HTML runs here   │
│              • injected runtime JS: runPython(), params API         │
│              • talks to server directly via fetch                   │
└───────────────┬─────────────────────────────────────────────────────┘
                │ plain HTTP (localhost)
┌───────────────┴─────────────────────────────────────────────────────┐
│  Local Server (Python)                                              │
│  ├── Static: explorer shell app, injected runtime JS                │
│  ├── FS API: list / stat / raw file streaming                       │
│  ├── Python Executor: runs main() of a .py file in worker proc  │
│  └── Template Registry: extension → preview template HTML           │
└──────────────────────────────────────────────────────────────────────┘
```

- **Server language:** Python (natural fit — it must import and execute user Python). Suggested: FastAPI + uvicorn.
- **Binding:** `127.0.0.1` on a configurable port (default e.g. 8765). Never `0.0.0.0`.
- **Startup:** single CLI command, `fused-render [--start-dir DIR] [--port N]`, opens the browser. Start dir is a UI convenience only — the whole filesystem is accessible.

---

## 3. File Explorer (Shell UI)

### Requirements

- **FS-1** Directory listing with name, size, modified time, type; sortable columns (sort state in URL params `sort`/`order`, dirs always grouped first).
- **FS-2** Breadcrumb navigation. *(tree pane, keyboard nav: follow-up)*
- **FS-3** **DECIDED:** the explorer browses the **entire computer** — there is no root-scoping concept. The CLI may take a *start directory* (`--start-dir`, default home) but it is only the initial UI location, not a restriction.
- **FS-4** v1 shows all files including dotfiles. *(hide/toggle: follow-up)*
- **FS-5** Selecting a file opens its preview (§5). Selecting a directory navigates into it.
- **FS-6** The current directory/file is reflected in the URL path so browser back/forward and refresh work: `http://localhost:8765/view/<url-encoded-path>`.
- **FS-7** **DONE (M14):** in-folder filename search over a streamed recursive walk — see §22.
- **FS-8** "Open raw" escape hatch for any file: streams bytes with correct MIME type (used for download and by templates for images/video/pdf).

### Sidebar & Bookmarks (M2 — next)

Left sidebar in the shell, always visible:

- **SB-1** Fixed left column. Top entry **Home**: navigates to `/view/<home dir>` (the user's real `~`, independent of `--start-dir`). `GET /api/config` gains a `home` field.
- **SB-2** **Bookmarks section** below Home. A bookmark captures *whatever the right side currently shows* — directory listing or any preview — as the **exact current URL verbatim** (`/view/…?freq=2.4&_file=…`). Clicking a bookmark is a plain browser redirect (`location.href = url`); the sidebar never interprets bookmark contents, so bookmarks survive future param/dispatch changes.
- **SB-3** Capture UI: a bookmark button in the shell header area, one click, no prompt. Default name = basename of the viewed path (file or dir name).
- **SB-4** Bookmarks are renamable inline (edit affordance on hover → input → Enter/blur commits) and deletable. No confirm on delete (re-bookmarking is one click).
- **SB-5** **DECIDED: persistence = server-side file** `~/.fused-render/bookmarks.json` (D75; superseded the original localStorage store). JSON array `{id, name, url, created_at}` (+ folders, D44); `id = crypto.randomUUID()`. Served by `GET /api/bookmarks` → `{exists, bookmarks}` and `PUT /api/bookmarks` (whole-tree, atomic, last-write-wins); server code lives in `fused_render/shell/`. Frontend reads a synchronous in-memory cache hydrated at boot; mutations await the PUT (no optimistic update); a 30 s poll re-reads the server so another tab's edits converge (D77, eventual ≤30 s, still last-write-wins). Legacy `localStorage["fused.bookmarks"]` is imported once (gated on the file not yet existing), then left dormant.
- **SB-6** Duplicate URLs allowed; **names are globally unique, case-insensitive** (D97 — names become `<name>.bookmark` filenames): a colliding create/rename auto-suffixes `-1`, `-2`, ... instead of rejecting, existing duplicates migrate once on GET (oldest by `created_at` keeps its name). Folder names are a separate namespace. List ordered by creation time. *(drag reorder, active-bookmark highlight: polish, later)*
- **SB-7** **DECIDED: bookmark create/update is mirrored into the target file's `.html.json` sidecar** (D83) as `bookmarkHistory` — the same per-file sidecar the `claude` chat template owns via `claudeSessions` (§7). `POST /api/bookmarks/history` upserts an entry by bookmark `id`; the frontend calls it fire-and-forget right after `addBookmark`/`updateBookmarkUrl` commit. A bookmark targeting a layout/tab sentinel or a path no longer on disk records nothing. **Delete never touches the sidecar** — history is permanent, independent of the bookmark's current lifetime.
- **SB-8** **Save to disk**: a per-bookmark button writes a portable `<name>.bookmark` JSON file (format v1: `{version, name, icon?, kind: single|panel|tab, path?, search}`, D98) next to the file(s) the bookmark points at — a single bookmark into its target's own directory (`path` relative to it), a panel/tab bookmark into the deepest common ancestor directory of all `_layout` leaves, each leaf path rewritten relative to that dir (grammar, nesting, per-leaf queries and global params untouched). The button's hover title shows the exact destination path before the click; it is disabled (greyed, explanatory title) when no save target exists — a leaf without an absolute fs path, or no common root. Frontend computes `{dir, filename, content}` (`lib/bookmark-file.ts`); `POST /api/bookmarks/export` validates and writes, overwrite allowed (a re-save refreshes the snapshot).
- **SB-9** **Double-click open** (macOS): the packaged app registers `.bookmark` as an Owner document type (D99); Finder-opening one routes to the `/view/_bookmark?file=<abs path>` sentinel, which reads the file (`GET /api/bookmark-file`), resolves its relative paths against the file's own directory (`lib/bookmark-file.ts` `bookmarkOpenUrl`, the inverse of SB-8's relativize) and `location.replace()`s to the described view — single, panel or tab. Browsing to a `.bookmark` file in the explorer opens it the same way (never a preview). Malformed / unsupported-version files render a readable error, no redirect.

### Server FS API (shape, not final contract)

| Endpoint | Purpose |
|---|---|
| `GET /api/fs/list?path=` | entries with metadata |
| `GET /api/fs/stat?path=` | single-entry metadata |
| `GET /api/fs/raw?path=` | streamed bytes, `Range` support (video/audio seek), correct `Content-Type` |

---

## 4. Renderable HTML

Any `.html` file on disk, when previewed, is **rendered live** (not shown as source) inside a sandboxed iframe. A "view source" toggle shows the raw text instead.

### 4.1 Runtime injection

The server serves the HTML with a small runtime `<script>` injected (or the iframe loads a bootstrap that provides it). The runtime exposes a global API (working name `fused`):

```js
// Execute main() of a Python file
const result = await fused.runPython(pathToPy, paramsObject);

// Params (see §6)
fused.params.get(name)
fused.params.set(name, value)          // strings only; always replaceState
fused.params.getAll()
fused.params.onChange(callback)   // fires whenever params change; author re-runs Python here
```

### 4.2 `runPython(path, params)`

- **RH-1** **DECIDED:** `path` may be **relative to the HTML file's own location** or **absolute** (anywhere on the machine — whole filesystem is in scope, consistent with FS-3).
- **RH-2** `params` is a flat JSON object; keys map to the Python function's keyword arguments (§5.2).
- **RH-3** Returns a Promise. Resolves with the deserialized return value; rejects with a structured error `{ type, message, traceback }` on Python exception, missing file, missing `main` function, or timeout.
- **RH-4** Concurrent calls are allowed (e.g. a page fires 3 data fetches on load). Server may queue or parallelize; ordering is not guaranteed.
- **RH-5** Calls have a configurable timeout (default e.g. 30 s), after which the worker is killed and the promise rejects.

### 4.3 Isolation — DESCOPED (v1)

- **RH-6** v1 uses a **plain same-origin iframe**; the injected runtime calls the server API directly with `fetch`. No sandbox, no postMessage bridge, no token. Previewed HTML is fully trusted.
- **RH-7** *(follow-up)* Sandboxed iframe + postMessage bridge if/when untrusted-HTML protection is wanted.
- **RH-8** Network access from inside the iframe to the outside internet: allowed.

---

## 5. Python Execution

### 5.1 Authoring model

**Convention over annotation:** a user Python file exposes a function named **`main`**. No decorator, no import required — a plain `.py` file works as-is:

```python
def main(city: str = "oslo", limit: int = 100):
    import pandas as pd
    df = pd.read_parquet(f"./data/{city}.parquet").head(limit)
    return df
```

- **PY-1** When called from HTML, the executor imports the module and calls its `main` with the params. Missing or non-callable `main` → structured error.
- **PY-2** Module top-level code runs on import (normal Python semantics); side effects there are the user's responsibility.

### 5.2 Parameter binding

- **PY-3** The JS `params` object maps to keyword arguments by name.
- **PY-4** Values arrive as JSON types. If the function has type annotations, the executor coerces (`"100"` → `int 100`, `"true"` → `bool`) since URL-derived params are strings. Unannotated args receive the raw JSON value.
- **PY-5** Extra params not in the signature: ignored unless the function has `**kwargs`. Missing required args → structured error naming the missing arg.

### 5.3 Execution environment

- **PY-6** **DECIDED (v1):** **user** code executes in a **fresh subprocess per call** — always-fresh code, zero stale state, trivial timeout/kill; a crash or `sys.exit` cannot take down the server. Cost: interpreter + import time on every call. A warm worker pool is the designated v2 upgrade if interactivity demands it (API unchanged). **Exception (D72):** an explicit allowlist of first-party helpers (`executor.INPROCESS_HELPERS` — the `table`/`csv`/`xlsx` readers and the `api` inspector) run **in the server process**, not a subprocess — they are trusted, fast, bounded, and never import/exec user code, and running them in-process means the protected-folder file access they perform reuses the app's macOS TCC grant instead of re-prompting on every call. Everything else stays subprocess-isolated: user code (the `api` Run button, user-authored template readers) **and every other shipped `templates/` helper** (e.g. the `claude/` chat agent, the geo tile servers/browsers), which can be slow/long-running and so must keep the subprocess timeout.
- **PY-7** The worker's Python interpreter/venv is configurable; default is the environment the server was launched from. (User installs pandas etc. there.)
- **PY-8** Working directory of execution = the Python file's directory, so relative data paths in user code behave intuitively.
- **PY-9** Module reload: automatic — every call is a fresh process, so edits to the .py file take effect on the next call.

### 5.4 Return value serialization

**DECIDED (v1): JSON only.** `main` must return JSON-native values (dict / list / str / num / bool / None). Anything else — including DataFrames and bytes — is a structured "return type not serializable" error; the user converts himself (e.g. `df.to_dict("records")`).

Deferred to later milestones (needed for data templates):

| Return type | Wire encoding (future) |
|---|---|
| `pandas.DataFrame` / Arrow table | Arrow IPC or `{columns, records}` JSON |
| `bytes` | binary response with declared content type |

- **PY-10** Large results: responses stream; a configurable size cap (default e.g. 100 MB) protects the browser.

### 5.5 Caching — follow-up, not in v1

- **PY-11** Optional per-call cache keyed by `(resolved py path, file mtime, params)`. Opt-in via config (per-directory or global). Keeps re-renders during param tweaking snappy.

### 5.6 Optional fused engine (D69)

- **PY-12** `/api/run` executes the built-in executor **by default**, regardless of whether the `fused` package is importable. `FUSED_RENDER_ENGINE=auto` opts in to running code through its local compute backend (`engine.py`) instead — fresh subprocess per call in a temp exec dir (PY-6 semantics preserved), PEP 723 `# /// script` inline requirements resolved into a cached venv (plus a default data-stack set mirroring the `bundled` extra), params delivered via `_params.json` — falling back to the built-in executor if `fused` isn't importable; `FUSED_RENDER_ENGINE=fused` requires it (startup error if missing); `=builtin` (or unset) always uses the built-in executor (D70). The active engine is reported in `GET /api/config` (`engine`) and logged at startup — the choice changes the code contract, so it is never silent.
- **PY-13** **Code contract under the fused engine:** a function decorated with **`@fused.udf`** — any name, the last decorated one is the entrypoint — receiving params as **raw JSON values** (no annotation coercion; the calling JS owns types); or a plain script assigning **`result = ...`**. A bare **`main()`** remains supported as a compat bridge with PY-4 coercion and PY-8 cwd semantics, so pages and the built-in templates behave identically under either engine. A file with none of the three → the PY-1 structured error, extended to name the alternatives.
- **PY-14** Both engines return **one wire shape** — `{ok, result, error: {type, message, traceback}, stdout}` (the fused engine adds `stderr`/`duration_ms`) — so `runtime.js` and templates never see which ran. Tracebacks under the fused engine point at the user's real file (the source is compiled as its own unit under its own filename); backend/wrapper plumbing frames are stripped.

---

## 6. Params & URL Sync

The core state-sharing mechanism between an HTML view and the browser URL.

- **PR-1** The **shell URL** is the single source of truth: `http://localhost:8765/view/path/to/sample.html?city=oslo&limit=50`.
- **PR-2** On load, the runtime hydrates `fused.params` from the shell URL's query string.
- **PR-3** **DECIDED (v1):** `fused.params.set(k, v)` updates iframe-local state and messages the shell, which updates the URL via `history.replaceState` — always. Param changes never create history entries; refresh/bookmark still reproduce state. (`pushState` opt-in is a possible later addition; API shape allows it without breakage.)
- **PR-4** Views must treat params as reactive inputs: `onChange` fires on every applied change (today: `set()` and shell-initiated updates; back/forward too if pushState ever lands).
- **PR-5** **DECIDED (v1): strings only.** Param values are strings, period — `set()` rejects non-strings, `get()` returns strings. Users JSON-encode themselves if they need structure. Zero magic.
- **PR-6** **Reserved namespace:** param keys beginning with `_` belong to the app shell (e.g. `_file`, `_raw`). User HTML cannot set them; the runtime rejects the call.
- **PR-7** Full page refresh reproduces the exact view: same file, same params, same rendered state (assuming user code is deterministic in its params).
- **PR-8** History writes are coalesced (D99): a `set()` takes effect immediately for all readers via a pending-search overlay, but the underlying `replaceState` lands at most once per 400 ms (trailing flush; flushed on pagehide). WebKit throttles history writes to 100/30 s and throws past the cap — scrub-speed param churn in the popover's WKWebView (§25) must never hit it, and a throttle error is caught, never propagated into the calling view.

---

## 7. Preview Templates

Built-in renderable-HTML files that ship **inside the application code**. They are ordinary renderable HTML — same runtime, same `runPython`, same params — proving the primitive is sufficient. Since M8 (template modes) an extension maps to an **ordered list** of templates; each list entry is a **mode** the user can switch between.

### 7.1 Dispatch

- **PT-1** **DECIDED: the registry is server-side** — single source of truth. The extension → template mapping lives in the server; `GET /api/fs/stat` carries the resolved result and the shell simply obeys. *(Originally a single `template: <abs path>|null` field; since M8 the field is the `templates` array of PT-8 — clean break, no compat alias, shell is same repo.)*
- **PT-2** When the user opens `data/trips.parquet`, the shell renders the returned template in the preview iframe and passes the target file as `_file=<path>` **on the iframe's own URL** (not the shell URL — its pathname already names the file, so no duplication like `/view/x.parquet?_file=/x.parquet`). Reserved `_` params are readable by the template, not settable by page code.
- **PT-3** Every template — built-in or user — is a **self-contained folder** named after the template: `fused_render/templates/<name>/` (built-ins) or `~/.fused-render/templates/<name>/` (user, §16), holding `template.html` (required), any sibling helper files (`reader.py`, css, assets), and optionally `icon.svg` (PT-11). Templates render from their real path, so plain **relative** `runPython` paths work unchanged — no virtual-path mechanism needed:

```js
const page = await fused.runPython("./reader.py",
                                   { file: fused.params.get("_file"),
                                     offset: "0", limit: "500" });
```

- **PT-4** Template UI state (current page, selected columns, sort) uses normal params → survives refresh, e.g. `?_file=…&offset=500&sort=fare`.
- **PT-6** **One name-resolution rule everywhere:** a template name resolves to `~/.fused-render/templates/<name>/template.html` if that exists, else `fused_render/templates/<name>/template.html`, else it is unusable (error). A user folder **shadows** a built-in of the same name — the deliberate override channel. The template **name is public stable API**: it is the registry reference, the `_mode` URL value, and the switcher tooltip label. (`fused_render/templates/vendor/` has no `template.html`, so it can never resolve as a template name — the `/template-assets` mount is unchanged.)

### 7.2 Template set — modes per extension

**Shell dispatch is exactly two-way: `templates` non-empty > fallback.** No file-type special-casing in the shell — image, text, and (via the `_render` sentinel, PT-12) HTML handling all arrive through the `templates` list like any other mode. Directories dispatch the same way: every directory resolves through the registry too, so the built-in listing is itself a mode — the `_listing` sentinel (PT-12), default of the universal `/` directory key (D81). A `.zarr` store previews via its `templates` (`["zarr", "_listing"]`) with the listing as a peer mode reachable through `_mode` (PT-13).

- **PT-7** The built-in bindings live in **`fused_render/templates/registry.json`** (D73) — data, not code, in **exactly the user-registry format** (§16): dot-anchored suffix-pattern keys (compound `.xyz.json`, wildcard `.*.json`, trailing-`/` directory keys — CT-3) mapping to an **ordered list of template names**. Each entry is a **mode**; the **first entry is the default**. One matcher and one value grammar serve both registries; the only asymmetry is precedence (user match wins, CT-3). Rule of thumb: `code` (the editable CodeMirror buffer) appears as a secondary mode only for text formats where raw text is meaningful — never for binary formats (a code view of `.parquet` is garbage).

| Extension(s) | Modes (first = default) | Notes |
|---|---|---|
| `.parquet` | `table` | paged table via pyarrow; binary — no `code` mode |
| `.csv .tsv` | `csv`, `code` | paged table, delimiter sniffing |
| `.xlsx` | `xlsx` | sheet select + paged table |
| `.json .geojson` | `tree`, `code` | collapsible tree |
| `.md` | `markdown`, `code` | rendered markdown |
| `.svg` | `image`, `code` | `<img>` via raw endpoint; svg source is text |
| `.png .jpg .jpeg .gif .webp` | `image` | `<img>` via raw endpoint |
| `.pdf` | `pdf` | browser-native embed |
| `.mp4 .mov .m4v .webm .mp3 .wav .m4a .ogg .flac` | `media` | raw endpoint w/ Range |
| `.py` | `code`, `api` | editable CodeMirror; `api` = swagger-style run form over the `main()` entry point (D63) |
| `.js .ts .sh .yaml .yml .toml .css` | `code` | editable CodeMirror |
| `.txt .log` | `text`, `code` | `<pre>` |
| `.tif .tiff` | `geotiff` | GeoTIFF/COG via vendored geotiff (in-browser decode, no reader.py); full metadata + dump, photometric routing (RGB/palette/YCbCr), band select + RGB stretch + colormaps, histogram, hover. Small files full-fetched; >32 MiB range-request `fromUrl` |
| `.nc .nc4 .cdf` | `netcdf` | NetCDF-3 via vendored netcdfjs (HDF5/NetCDF-4 → graceful card); leading-dim sliders, colormaps + stretch, histogram, hover |
| `.zarr/` (directory) | `zarr`, `_listing` | Zarr v2/v3 store — a *directory*, bound by the trailing-`/` directory key (PT-13); vendored zarrita (in-browser decode, members fetched per-key); group tree + array select, colormaps + stretch, histogram, hover. `_listing` (PT-12) rides as the secondary mode — the raw member listing, replacing the old "Browse contents" escape hatch (D81) |
| `/` (any directory) | `_listing` | The **universal directory key** (CT-3) — the built-in default for *every* folder. `_listing` is a sentinel (PT-12), not a template folder: the shell's built-in directory listing (sortable columns, in-folder search, FS-1). Zero segments, so any dot-anchored directory key (`.zarr/`) beats it (D81) |
| `.html .htm` | `_render`, `code` | defaults shipped in the built-in registry like any other key — user-rebindable since D73 (CT-4 revised); `_render` is a shell sentinel (PT-12) rendering the file itself live (§4) |
| unknown | shell fallback | metadata + raw/download link (built into shell, not a template) |

- **PT-8** `GET /api/fs/stat` carries the resolved mode list as **`templates`**: an array of `{"mode": <name>, "path": <abs template.html>, "icon": <abs icon.svg|null>}`, in order, first = default. `templates: []` when nothing applies — an unmapped file extension or a `null` binding. A **directory** always resolves at least the universal `/` key's `["_listing"]` (PT-13, D81), so it is empty only when a `null` binding disables it, whereupon the shell falls back to the built-in listing anyway (a folder must always render something). The old singular `template` field is **removed**.
- **PT-9** **`_mode` param (shell URL):** non-default modes are selected via reserved param `_mode=<template name>` on the **shell URL** (bookmarkable, same URL-is-state pattern D40 established for the old HTML `_mode=render|source` toggle — that toggle itself is now the ordinary `["_render", "code"]` mode list, PT-12; old `_mode=source` bookmarks fall to the default, accepted break). Absent `_mode` = default = `templates[0]`; selecting the default **deletes** the param (clean URLs); an unknown/stale value falls back to the default with no error. Switching swaps the iframe src to the selected template's `/render?path=<template>&_file=<file>` with a fresh document per switch. A sentinel mode may render a **shell view instead of an iframe**: `_listing` (PT-12) mounts the shell's built-in listing component (no iframe, no `_file`) in place of the preview body, selected by `_mode=_listing` like any other mode (D81). Known accepted quirk: template params (e.g. `offset`) persist on the shell URL across mode switches; a param name used differently by two modes collides — documented, not prevented.
- **PT-10** **Mode switcher (shell, preview header):** rendered only when `templates.length > 1`, right side of the preview header bar. **Icon-only buttons**, mode name via native `title` tooltip, active mode in accent color. When an entry's `icon` is `null`, the shell renders a placeholder: the first letter of the mode name in a small rounded box. The `.html` Rendered|Source pair is **not a special case**: it is the ordinary mode list `["_render", "code"]` (PT-12) riding this same switcher — `_render` gets a shell-baked eye icon (sentinels have no folder to ship `icon.svg`); `code` gets its real folder icon. The `_listing` sentinel likewise gets a shell-baked list icon (D81).
- **PT-11** **Icons:** a template folder may ship `icon.svg` — **monochrome** (single fill; the shell tints it via CSS `mask-image` + `currentColor`, so only alpha matters), square viewBox (24×24 suggested), legible at 16px. `icon` in the stat entry is the abs path of the `icon.svg` sitting next to the *resolved* `template.html` (the user folder's icon when a user template resolved), or `null`. The shell loads it through the existing `/api/fs/raw` endpoint — no new routes. Every built-in folder ships one. Sentinel modes (`_render`, `_listing`) have no folder, so the shell bakes their icons in (PT-12).
- **PT-12** **Sentinel modes:** a mode name starting with `_` is a **shell sentinel** — no template folder backs it; the shell knows what it means. Server resolution special-cases sentinels: the stat entry is emitted as `{"mode": "_<name>", "path": null, "icon": null}` without touching the filesystem. The `_` prefix matches the reserved-param convention (`_mode`, `_file`). The sentinel namespace is **shell-owned**; since D73 the server keeps a **known-sentinel set** (`KNOWN_SENTINELS = {"_render", "_listing"}`, D81) and a name in that set is referenceable from **any** registry list, built-in or user — any other `_`-prefixed name is invalid (dropped + `template_error`, CT-6). Two sentinels exist:
  - **`_render`** — "render the file itself" — the default mode of the built-in `.html`/`.htm` list `["_render", "code"]`. Shell handling: iframe src `/render?path=<the file itself>` (no `_file`), shell-baked eye icon.
  - **`_listing`** — "the shell's built-in directory listing" (sortable columns + in-folder search, FS-1/§13.4) — the default of the universal `/` directory key (PT-13, D81), and a peer mode of `.zarr/`'s `["zarr", "_listing"]`. It backs no folder and takes no `_file`: when it is the active mode the shell **mounts its Listing component in place of the preview iframe** (no iframe at all). Shell-baked list icon.

  Users **can** rebind any registry key — including `.html`/`.htm` (CT-4 revised, D73) and the directory keys (D81) — dropping a sentinel, then listing it explicitly brings it back. Unknown sentinel entries (path `null`, mode not in the set) are filtered out defensively. Non-sentinel entries in the same list (e.g. `code`, `zarr`) work exactly like any template mode. Future modes are added to the server-side registry and flow through the framework normally.
- **PT-13** **Directory views (D65, revised by D73 and D81):** a preview target may be a **directory**. Directories resolve through the **same registry** as files (PT-7, CT-3): a key with a **trailing `/`** binds a directory's basename, and the **universal `/` key** (zero segments, CT-3) matches *every* directory at lowest specificity. The built-in registry ships `"/": ["_listing"]` and `".zarr/": ["zarr", "_listing"]` — so **every** directory carries a non-empty `templates` list (≥ `["_listing"]`), and dispatch is uniform: a directory previews its default mode exactly like a file. The built-in **listing is itself a mode** — the `_listing` sentinel (PT-12) — so it rides the ordinary mode switcher (PT-10) and `_mode` selection (PT-9): a plain folder's single-mode `["_listing"]` shows the listing with no switcher; a `.zarr` store shows the zarr map by default with `_listing` as a switchable peer (`_mode=_listing`). This replaces D65's one-way `?listing=1` "Browse contents" escape hatch, which is **removed** (D81) — the only way to the listing is now the `_listing` mode. In **embed** (the preview header, hence the switcher, is hidden), a corner chip toggles the `_listing` mode (writing/deleting `_mode`) so an embedded directory preview can still reach its members. Annotate (§17) is not offered for `_listing` (no iframe to overlay). A directory resolves to an **empty** list only when a `null` binding disables it (CT-2); the shell then falls back to the built-in listing regardless (a folder must always render something). Users bind directory views like any other key — `"/": ["_listing", "gallery"]` lists the built-in listing plus a gallery mode for every folder (built-in names are listed explicitly — there is no splice, D94); dropping `_listing` from a list forgoes the file listing for those directories (owner call, same "user can shoot themselves" posture as D73's `.html` rebind). Accepted break: old `?listing=1` bookmarks ignore the dropped param — a plain folder still lists (its default), a `.zarr` bookmark now opens the zarr map instead of members.
- **PT-5** **User overrides:** DECIDED and specced as §16 (M7, extended by M8) — user template folders under `~/.fused-render/templates/` bound to extensions by `~/.fused-render/templates/registry.json`, replacing or extending the built-in mode list, using the exact same mechanism.

---

## 8. Server Requirements (cross-cutting)

- **SV-1** Single process, no external services, no database. State = file system + in-memory.
- **SV-2** *(follow-up)* WebSocket/SSE push channel (progress, file-change notifications). v1: plain request/response only.
- **SV-3** *(follow-up)* Structured execution logging + dev console panel. v1: server stdout logs only; Python print() output is returned to the calling page and logged to the browser console.
- **SV-4** Graceful shutdown; per-call subprocesses die with their call.

---

## 9. Security Model — DESCOPED (v1)

**Decision: no security layer in v1.** Base layer simplicity wins; everything below is a recorded follow-up, not a requirement.

- v1 keeps only: bind `127.0.0.1` (one line, free).
- **Follow-ups (documented, not built):** session token auth; `Origin`/`Host` validation (DNS-rebinding defense); sandboxed iframe + bridge for untrusted HTML. Note for later: a localhost server that executes Python and reads the whole disk is an RCE/exfiltration primitive for any website open in the same browser — revisit before this ever runs on a shared/edge machine.


---

## 10. Tech Stack (proposed)

| Layer | Choice | Rationale |
|---|---|---|
| Server | Python 3.11+, FastAPI, uvicorn | must run user Python; async + WS built in |
| Exec workers | `multiprocessing` pool or subprocess-per-call | isolation, kill-on-timeout |
| Shell UI | Vite + React + TypeScript | fast to build tree/table UI; any SPA framework acceptable |
| Data tables in templates | Arrow JS + a virtualized grid | large parquet/csv without choking |
| Packaging | `pipx install fused-render` → `fused-render` CLI | single-command local install |

---

## 11. Open Questions

1. ~~Whole-disk vs scoped root~~ — **RESOLVED: whole computer, no root concept (FS-3).**
2. ~~Python env~~ — **RESOLVED: the env the server was launched from. Good enough.**
3. ~~Streaming/partial results~~ — **RESOLVED: not required.**
4. **Editing** — is write support (rename/delete/save) wanted in v1 or strictly v2?
5. **Multiple entry functions per file** addressed by name (`runPython("f.py#chart")`) — needed, or keep `main`-only?
6. ~~Param change → auto re-run?~~ — **RESOLVED: manual — author wires `runPython` inside `params.onChange`. Declarative binding possible later, layered on top.**

---

## 12. macOS Distribution (DMG) — M3

Distribute as a DMG containing a menu-bar app; all UI stays in the browser.

- **DM-1** **DECIDED (v2, D33):** the `.app` is built by **py2app** from a framework-build python (Homebrew `python@3.12`, bootstrapped by the build script). py2app ships a real re-invokable interpreter in-bundle (`Contents/MacOS/python`) — `sys.executable` subprocess executor works unchanged — and its compiled stub gives proper LaunchServices/AppKit process identity (the earlier hand-rolled bash-shim caused flaky NSStatusItem behavior under Finder launches).
- **DM-2** **DECIDED:** user `runPython` code executes on the **bundled interpreter only**. The `[bundled]` extra ships preinstalled (numpy, pandas, requests, duckdb, polars, matplotlib, scipy, pillow, openpyxl, shapely, geopandas + core pyarrow). py2app note: these are force-copied via `packages` — the executor imports them only in child processes, so import tracing can't see them. Known gap: `mpl_toolkits` (3D axes) excluded (namespace-package vs py2app limitation).
- **DM-3** **DECIDED (v2, D34):** regular app — **Dock icon AND menu bar ✦** (Open in browser / Copy URL / Quit). No LSUIElement. Dock right-click → Quit is the discoverable lifecycle path.
- **DM-4** **DECIDED (v2, D73):** signing is credential-driven in `scripts/build_dmg.sh` — a **Developer ID** identity in the keychain (auto-detected or via `FUSED_RENDER_CODESIGN_IDENTITY`) triggers hardened-runtime, inside-out signing + optional notarization (`FUSED_RENDER_NOTARY_PROFILE`); with no identity it **ad-hoc signs** (local testing, unchanged). Developer-ID signing is also the general fix for the repeated Downloads/Desktop/Documents prompt (one Team ID unifies the app + its executor subprocess, complementing the D72 in-process reader split). Details: `docs/signing.md`. Supersedes the earlier "Briefcase external-app" plan (D35 — Briefcase's template breaks `sys.executable`).
- **DM-5** Launch flow: pidfile+portfile in `~/Library/Application Support/fused-render/`; liveness probe = GET `/` (file-backed, catches zombies); already running ⇒ open browser only; else start (8765, fall forward to 8775), write pidfile, open browser.
- **DM-6** **DECIDED (v2, D35):** DMG built by **dmgbuild** (app + Applications symlink, UDZO) orchestrated by `scripts/build_dmg.sh`; ~270 MB compressed.
- **DM-7** `fused_render/app.py`: menu-bar entry point (uvicorn on a daemon thread); py2app entry = `scripts/app_entry.py`; build spec = `scripts/setup_py2app.py`. CLI (`fused-render`) remains for dev.
- **DM-8** **Finder integration:** `CFBundleDocumentTypes` — `.parquet` rank Default, html + all template extensions rank Alternate (never steals user defaults, appears in Open With). Double-clicked files reach the app via the delegate's `application:openFiles:` (implemented by adding the method to rumps's delegate class); each file opens a browser tab at `/view/<path>`. Startup ordering: AppKit run loop starts first, server boots in the background after — the home-vs-file decision happens at server-ready, long after any launch document event has arrived, so a file double-click cold launch opens exactly the file view (no stray home tab).

## 12b. Milestones

- **M1 — Base layer (current focus):** server + shell, whole-disk browsing, raw streaming, live-rendered HTML in plain iframe, `runPython` → `main()` subprocess execution, params ↔ URL sync (strings, replaceState), server-side template registry (dispatch: template > html > fallback) + **parquet, image, text templates**. No security, no WS, no caching.
- **M2 — Sidebar & bookmarks:** SHIPPED.
- **M3 — DMG distribution:** menu-bar app + bundled CPython + build script (§12).
- **M4 — Live editing:** autosave + SSE change feed + auto-reloading views (§13).
- **M5 — Layout mode:** split-pane grid of embed views, layout + merged params in one bookmarkable URL (§14).
- **M6 — Tab mode:** tabbed set of embed views on the §14 URL model; bookmark folders open as tab layouts (§15).
- **M7 — Custom templates:** user template folders in `~/.fused-render/templates/` + `registry.json` extension bindings, overriding built-ins (§16).
- **M8 — Template modes:** 1:n extension→template mapping — folder-per-template built-ins (renamed to public names), ordered mode lists (first = default), registry `list|string|null` grammar (the `"..."` splice shipped here was later removed, D94), `_mode` shell param + icon-only mode switcher, stat `templates` array replacing `template`, html folded in as the hardcoded `["_render", "code"]` sentinel list (§7, §16 / PT-6..PT-12, CT-10..CT-11).
- **M9 — Annotation mode:** annotate toggle over any preview mode, element/selection-anchored comment threads stored in the URL (§17).
- **M13 — Directory views:** directories resolve through the registry like files — the built-in listing becomes the `_listing` sentinel (PT-12), the universal `/` directory key (CT-3) makes it every folder's default mode, custom directory-view templates ride the same mode list + switcher, and `?listing=1` is removed in favor of `_mode=_listing` (§7, §16 / PT-12/PT-13, CT-3 / D81).
- **M14 — Explorer search:** the in-folder search's recursive walk goes breadth-first and streams NDJSON batches; client-side incremental fuzzy scoring, scroll-paged results, honest truncation, machine-noise pruning (§22 / SR-1..SR-11 / D85).
- **M16 — Pinned view:** the status item's only surface — any click drops an NSPopover whose native header row carries all app actions (menu removed, D98) above a live WKWebView of the pinned file's `/embed` view; detaches into a floating always-on-top window (§25 / PV-1..PV-8 / D97/D98).
- **Follow-ups (unordered):** remaining preview templates (csv/json/markdown/media/pdf/syntax-highlighted code); warm worker pool; DataFrame/Arrow returns; security layer (token, origin checks, sandboxed bridge); exec console; search/sort/tree/keyboard nav; caching; editing.

## 13. Live Editing — Autosave & Auto-Reload (M4)

Goal: a live-preview loop. Edit a file (in our editor or externally) → it saves itself → every open view of it reacts. Combined with embed mode (D39) this gives "source in one tab, rendered output in another, updates as you type".

### 13.1 Autosave (code editor)

Applies to the `code` template (`templates/code/`), the only editable surface (D37).

- **AS-1** The editor autosaves **250 ms after the last edit** (debounced). Manual Save / Cmd+S remain and save immediately, cancelling any pending autosave timer.
- **AS-2** Autosave uses the same optimistic lock as manual save (`expected_mtime`). On 409 the existing conflict banner shows and **autosave suspends** until the user resolves via Reload or Overwrite. Autosave must never auto-overwrite a conflict — that would reduce the lock to decoration.
- **AS-3** Status text is the save lifecycle: `Modified → Saving… → Saved`. A non-conflict save failure shows the error; the next edit re-arms autosave (transient failures self-heal).
- **AS-4** Always-on. No toggle, no setting. Consequence accepted: half-typed code reaches disk and triggers reloads of watching views (that is the point of a live-preview loop; the D17 traceback overlay makes broken intermediate states self-explanatory).
- **AS-5** The `beforeunload` dirty guard stays — it covers the sub-second window between last keystroke and autosave completion.

### 13.2 Change feed (server)

- **WF-1** Endpoint `/api/fs/events?path=A&path=B&…` — a **WebSocket** (D74; was SSE until the Chrome 6-connections-per-origin HTTP/1.1 cap starved every other fetch once ≥6 panes held streams open). Watched paths arrive as **repeated `path` query params** (paths may contain commas; repetition avoids a delimiter).
- **WF-2** v1 implementation: async loop stats every watched path every **200 ms**; baseline mtimes captured at connect. When a path's mtime differs from the last seen value (or the file appears/disappears) send one JSON text message: `{"path": "<abs path>", "mtime": <float|null>}` — `null` means deleted. No event replay: changes that happen while disconnected are missed by design (the client reloads on reconnect-relevant changes anyway).
- **WF-3** A `{"keepalive": true}` message every 15 s keeps intermediaries and buffers honest; clients ignore it.
- **WF-4** No filesystem-watcher dependency (watchdog/fsevents) in v1 — polling stat is cheap and dependency-free at local scale. A later upgrade to real FS events is internal to this endpoint; the client contract (WebSocket, same message shape) does not change.
- **WF-5** Read-only GET — no `X-Fused` guard, consistent with the other read endpoints (D36 covers only mutating/executing POSTs).

### 13.3 Auto-reload (runtime)

The reload logic lives **entirely in the injected runtime** — the shell needs no per-view watching, and every rendered page (view mode, embed mode, standalone `/render`) gets the behavior for free.

- **LR-1** Each rendered page watches the union of: **its own rendered file** (the `path` param of its `/render` URL), **`_file`** if present (templates watching their target), and **every Python file executed via `runPython` this page-life**.
- **LR-2** `POST /api/run` response gains a `resolved_py` field — the absolute resolved path of the executed file — so the runtime learns dependency paths authoritatively instead of re-implementing the server's relative-path resolution. Recorded for failed runs too (a broken py that gets fixed must still trigger reload).
- **LR-3** On any change event: debounce **300 ms** (coalesce bursts), then `location.reload()` on the iframe itself. Full reload is the honest re-execution — the runtime cannot replay what the page did with a python result. State survives because view state lives in URL params (D8/D20/D25).
- **LR-4** When the watch set grows (a new py runs), the runtime closes and reopens its watch `WebSocket` with the full set. Resubscribe is debounced so a page firing several `runPython` calls on load reconnects once. Unlike `EventSource`, a WebSocket does not auto-reconnect — the runtime retries a dropped socket after 1 s.
- **LR-5** Opt-out: `fused.autoReload(false)` disables watching/reloading for that page. The `code` template calls it — the editor must not reload out from under the cursor (its own autosave changes the mtime; external changes are the conflict lock's job). To make the opt-out race-free, the runtime starts watching on `DOMContentLoaded`, after inline page scripts have run.
- **LR-6** Deletion (`mtime: null`) reloads too — the resulting 404/error view is the truthful state.
- **LR-7** Reload works identically for standalone `/render?path=…` pages (runtime is the same code).

### 13.4 Listing refresh (shell)

- **LS-1** The directory listing view watches the directory path via the same endpoint; on change it re-fetches `/api/fs/list` and re-renders, preserving sort params.
- **LS-2** Known limitation, accepted: a directory's mtime changes on create/delete/rename of entries — not when a child file's content or size changes. Stale sizes in an open listing are fine.
- **LS-3** The shell closes the listing's watch `WebSocket` when navigating away (to a preview or another directory).

## 14. Layout Mode — Split Panes (M5)

Goal: view several files/directories side by side in a resizable grid of panes, with the **entire state — pane arrangement, each pane's location, and all view params — captured in one bookmarkable URL**. Combined with bookmarks (D20) this makes a saved layout a one-click dashboard.

### 14.1 URL & route

- **LM-1** Route: `/view/_panel?...` and `/embed/_panel?...`. `_panel` is a **sentinel pathname**, not a real file: the shell's `route()` intercepts it (under both prefixes) before calling `stat`. Zero server changes (the server already serves the shell for any `/view/*` and `/embed/*`). The pane tree lives in the reserved `_layout` query param (LM-2).
- **LM-2** The pane tree lives in the reserved query param **`_layout`** (underscore prefix → already invisible to `fused.params`, PR-6). Codec (borrowed from the reference grid-viewer):
  - `,` separates panes in a **row** (side by side), `;` separates **columns** (stacked), `(…)` groups for nesting. Single pane = bare path.
  - Each pane segment is the pane's **fs path plus optional pane-local query** (`/data/a.parquet?_mode=source&sort=name`). Within a segment, the characters `, ; ( ) % ?` occurring *inside* path components or the query are percent-encoded so the codec's delimiters stay unambiguous.
  - **URL grammar (D51): the entire `_layout` value is parenthesized and emitted last** — `?city=sf&_layout=(/data/a.parquet?_mode=source&sort=name,/notes.md)`. The parens delimit scope both visually (inside = iframe-local, outside = global) and structurally: **`&` is literal inside them**, so segment queries read exactly as they appear. Every read of a shell query goes through the codec's `splitShellSearch` (balanced-paren scan; the runtime carries a small standalone duplicate) — plain `URLSearchParams` cannot parse a layout URL. Strict read, no lenient fallback: an unwrapped `_layout` value is treated as absent (the key is dropped on the next sync); an unbalanced span (paste-truncated URL — auto-linkers may eat the trailing `)`, accepted breakage) is invalid and falls back per LM-2's missing-layout rule. Params appearing *after* the `)` are ordinary globals — position is convention, the parens are the boundary.
  - Example: `?_layout=(/data/a.parquet,/data/b.parquet;/notes.md)` → a and b side by side on top, notes below.
- **LM-3** Params are **pane-local** (D72 — supersedes the original merged-pool design). The panel shell marks its window as a **param boundary** (`window._fusedParamBoundary = true`, same contract as TM-3), so a page rendered inside a pane targets its own pane's `/embed/...` URL; each pane's full query — user params included — is captured **segment-local** inside `_layout` by the ordinary sync (LM-6). The layout URL's top-level query carries **only hand-typed globals**: the shell never promotes params there, but a user may type `?city=sf&_layout=(…)` themselves — such params are readable from every pane (LM-7), read-only.

### 14.2 Panes

- **LM-4** A pane is an **`/embed/<path>` iframe** (D39): a full navigable chrome-free shell — panes can browse directories, open previews, use templates, all existing behavior for free.
- **LM-5** Pane bar (top of each pane): clickable **path crumbs** (segment click navigates that pane), then buttons: **split right**, **split down** (new pane duplicates the current pane's location), **maximize** (transient — fills the layout area, not encoded in the URL), **close**. Closing collapses single-child splits; when a close leaves only **one** pane (including closing the last pane directly), the shell exits layout mode by navigating to plain `/view/<that pane's path>` — a one-pane layout is never left on screen.
- **LM-6** Pane navigation syncs up: the layout view observes each pane's URL (iframe `load` + the pane window's `fused:urlchange`, LM-8) and re-encodes `_layout` on the shell URL via `history.replaceState` — refresh/bookmark always reproduce the current arrangement.

### 14.3 Params — target & sync (runtime change)

- **LM-7** The injected runtime's param target is the **topmost same-origin ancestor window**, stopping **below** any ancestor marked as a param boundary (`_fusedParamBoundary` — both layout shells set one, LM-3/TM-3/D72). In normal view/embed mode this is the same window as before (parent = top), so behavior is unchanged; inside a layout mode the climb stops at each pane's own embed shell, so **writes always land pane-local**. Reads additionally fall back to the same-origin ancestor chain **above** the boundary: hand-typed globals on the layout shell URL are visible in every pane (nearer ancestor wins; pane-local wins over all). `set()` never writes above the boundary — a pane setting a key that also exists globally shadows it locally.
- **LM-8** Change notification: the shell wraps **both** `history.replaceState` and `history.pushState` to dispatch `fused:urlchange` (today: only replaceState). The runtime listens for `fused:urlchange` on its target window and re-notifies `params.onChange` listeners — but only when the **visible (non-reserved) param snapshot actually changed** (snapshot diff). The diff guard prevents notification loops and duplicate fires (a `set()` would otherwise notify twice: once directly, once via the event; direct notify is removed in favor of the event path).
- **LM-9** Consequence, intended (D72): two panes using the same param key are **independent** — each pane's `set()` writes only its own segment-local query. Cross-pane linking is opt-in and manual: the user hand-types the shared key on the layout URL's top level, where every pane reads it (LM-7).

### 14.4 Entry & chrome

- **LM-10** Entry: **split-right and split-down icon buttons** in the breadcrumb's crumb-actions (next to ★ Bookmark, same glyphs as the pane bar's split buttons). Click → navigate to `<prefix>/_panel?_layout=(<seg>,<seg>)` (split right) or `(<seg>;<seg>)` (split down) (D51 grammar) where `<seg>` is the current fs path + its **whole** current query (D72 — nothing is promoted to the top level) — two panes side by side or stacked, both the current view with its params carried over (a single pane on entry looked like nothing happened).
- **LM-11** In layout mode the sidebar stays visible (bookmarks reachable, ★ button works on the layout URL — bookmarking a layout needs zero bookmark-layer changes, D20). Breadcrumb shows a static "Panel" label. The armed-bookmark "Update bookmark" flow (D38) works unchanged: pane/param drift rewrites the shell URL via replaceState → `fused:urlchange` → `syncUpdateButton`.
- **LM-12** Module: **`views/panel.js`** — tree codec, tree ops (split/close/collapse), pane DOM + bar, URL sync. Imports `router.js` only (one-way deps, ARCHITECTURE §6). `main.js` gains one sentinel branch; `shell.css` a `.layout-*` section; sidebar/bookmarks/api untouched.

## 15. Tab Mode — Tabbed Views (M6)

Goal: the same URL-is-state model as §14, but as **tabs instead of a grid**: one page visible at a time, a tab bar to switch. Primary use: a **bookmark folder rendered as one view** — click the folder, get its bookmarks as tabs, bookmark the result as a dashboard.

### 15.1 URL & route

- **TM-1** Route: `/view/_tab?...` and `/embed/_tab?...` — a sentinel pathname exactly like `_panel` (LM-1), intercepted by `route()` under both prefixes. Zero server changes.
- **TM-2** The tab list lives in the same reserved **`_layout`** param, as a **flat top-level `,` row** of the §14 codec — a tab segment is a fs path + optional segment-local query, same escaping (LM-2). Produced URLs are always a flat list; on parse, any nested structure (`;`, `()`) is defensively **flattened to its leaves in document order**, each leaf becoming a tab.
- **TM-3** Params are **tab-independent** (same contract as LM-3 since D72). The tab shell marks its window as a **param boundary** (`window._fusedParamBoundary = true`, set on render, cleared on teardown); the runtime's ancestor climb (LM-7) stops **below** a boundary-marked ancestor, so a page rendered inside a tab targets its own pane's `/embed/...` URL. Each tab's full query — user params included — is therefore captured **segment-local** inside `_layout` by the ordinary sync (TM-7); the tab URL's own top-level query carries only hand-typed globals (readable from every tab, LM-7).
- **TM-4** A tab segment's path may itself be a sentinel (`_panel`, `_tab`): the iframe src is just `/embed/<segment path>` + segment query, so a panel layout nests inside a tab through the ordinary pipeline (D45 embed support), its `_layout` riding inside the segment query. A nested panel's panes stay pane-local too (D72 — its own boundary stops each pane's climb) while staying isolated from every other tab.

### 15.2 Tabs

- **TM-5** A tab is an **`/embed/<path>` iframe**, mounted **lazily on first activation** and kept alive afterwards (`display:none` when inactive) — scroll/editor state survives switching, and hidden tabs keep receiving `fused:urlchange` (the runtime listens on the top window, LM-8), so param sync is live while hidden.
- **TM-6** Tab bar (top of the layout area): one button per tab — label = basename of the tab's **current** path (sentinel paths label as `Panel` / `Tabs`) — plus a close `×` per tab and a trailing `+` that opens a new tab at the configured start dir. Click activates. The **active tab index is NOT encoded in the URL** (avoids "Update bookmark" churn on every switch): bookmarks and fresh loads open the first tab; refresh and Back/Forward restore the last active tab via `history.state` (`fusedActiveTab` — rides the entry, never the URL).
- **TM-7** URL sync up, same machinery as LM-6: iframe `load` + tab-window `fused:urlchange` → read the tab's live location → re-encode `_layout` via guarded `replaceState`. Closing a tab removes its segment; closing the **last** tab exits to a plain view of its location (active prefix, like LM-5).

### 15.3 Entry — bookmark folders

- **TM-8** Clicking a bookmark **folder's name or row** opens the folder as a tab layout: each child bookmark's pathname becomes the segment path and its **entire saved query stays segment-local** (TM-3 — no hoisting, no cross-child key collisions; every bookmark keeps exactly its own params). A child that is itself a `_panel`/`_tab` bookmark just works (TM-4). Opening also **expands the folder** if it was collapsed (the sidebar should show what the tabs now show); the **folder glyph** keeps the plain collapse/expand toggle.
- **TM-9** A folder is not a bookmark: opening it arms nothing. ★ Bookmark on the tab view saves the composed URL as a normal bookmark; a tab layout opened *from* such a bookmark gets the full armed/update flow (D38) unchanged. Breadcrumb shows a static "Tabs" label; no breadcrumb entry button (folder-only entry).

### 15.4 Module

- **TM-10** The §14 codec (escape/parse/encode/segment helpers) moves to a shared **`views/layout-codec.js`**; `views/panel.js`, the new **`views/tabs.js`**, and `breadcrumb.js` import it. `tabs.js` owns the tab bar DOM, lazy iframes, and URL sync; `main.js` gains the `_tab` sentinel branch; `shell.css` a `.tabs-*` section; `sidebar.js` changes only the folder-row click wiring.

## 16. Custom Templates — User Overrides (M7)

Goal: users replace or add preview templates using the **exact same mechanism** as the built-ins (§7). A user template is an ordinary renderable-HTML page (plus optional sibling `.py` readers) that receives the target file as `_file` — nothing new is exposed; only the server's extension → template resolution gains a user-controlled layer. The resolution layer is server-only: the shell obeys whatever `templates` list the stat response carries (PT-8), and `/render` already renders any absolute path with the runtime injected.

### 16.1 Layout on disk

- **CT-1** A user template is a **self-contained folder** `~/.fused-render/templates/<name>/` holding `template.html` plus any sibling files it needs (reader `.py` files, css, assets) and optionally `icon.svg` (PT-11) — identical in shape to a built-in folder (PT-3). `<name>` carries **no** binding-by-convention semantics (CT-7), but it is the template's public name: it resolves by the single rule of PT-6, so a user folder named like a built-in **shadows** it. Relative `fused.runPython("./reader.py")` works unchanged because the template renders from its real path (PT-3).
- **CT-2** Bindings live in **`~/.fused-render/templates/registry.json`** — a flat JSON object mapping **dotted extension keys** to a template name, or to `null`. Keys may be compound (`.tar.gz`), carry `*` wildcard segments (`.*.json`), or end with `/` to bind a **directory** basename (`.obt/`), and the bare `/` key binds **any** directory (the universal key, D81) — the full key grammar is CT-3, and it is the same grammar the built-in `templates/registry.json` uses (PT-7, D73):

```json
{
  ".parquet": "geo",
  ".geojson": "geo",
  ".tar.gz": "archive",
  ".*.json": "config-view",
  ".obt/": "bundle",
  ".png": null
}
```

  A name binds the extension to a single-mode list of that template, resolved by the PT-6 rule. **`null` (or an empty list `[]`) disables** templating for that extension entirely: the file gets no template at all and falls through to the shell's metadata/raw-download fallback (§7.2) — on a directory key, to the plain listing view. `[]` and `null` are exactly equivalent (D94).
- **CT-10** **Mode lists (M8):** a registry value may also be a **JSON list of template names** — the full ordered mode list for that extension, **replace semantics**, first = default (PT-7). The string form of CT-2 is exactly a single-mode list; existing registries keep working unchanged.
- **CT-11** **`"..."` splice — REMOVED (D94, owner 2026-07-09).** The list-splice grammar is gone: a `"..."` entry is no longer expanded to the built-in list. `.` is still forbidden in folder names (CT-6), so `"..."` resolves to no template folder and is treated as an ordinary **dangling name** — dropped from the rendered list with a `template_error` (CT-6), and surfaced as a broken (`exists:false`) ref in the registry view so the user is prompted to remove it (nothing is auto-removed). To include the built-in modes, list them explicitly.

```json
{
  ".parquet": ["geo-view", "geo"],
  ".md": "my-markdown",
  ".csv": null,
  ".log": []
}
```

### 16.2 Resolution

- **CT-3** **Key grammar and matching (revised by D73).** A key is a **dot-anchored suffix pattern**: one or more dot-led segments, optionally ending in `/` to bind directories — plus one special zero-segment key, the bare `/` (the **universal directory key**, D81), which matches *any* directory. A segment is a literal (`json`, `tar`) or the wildcard `*`, which matches **exactly one whole non-empty segment** — partial wildcards (`.geo*`) are invalid, and a malformed key (no leading dot, empty segment) never matches (silently ignored, as keys without a leading dot always were). Matching is **case-insensitive** against the basename and requires a **non-empty stem** before the matched suffix (a file literally named `.json` does not match the `.json` key; `.hidden.json` does — its stem is `.hidden`). Directory keys match only directories, file keys only files. **Specificity:** more segments beats fewer; at equal length, comparing from the **rightmost** segment, a literal beats `*` — so for `data.xyz.json`: `.xyz.json` > `.*.json` > `.json`. The universal `/` key has zero segments, so it ranks **below every** dot-anchored directory key (`.zarr/` > `/`); its stem is the whole basename (D81). **Both registries are matched by this same rule** (the old `splitext` single-extension built-in table is gone, D73); precedence stays **any user-registry match > built-in match** — a user `.json` binding beats a built-in `.xyz.json` one. Any extension may be bound, including ones no built-in handles.
- **CT-4** *(revised by D73 — the exemption is dropped.)* `.html`/`.htm` are **ordinary registry keys**: their default list `["_render", "code"]` ships in the built-in registry (PT-7), and users may rebind or reorder them like any other extension — rendered-HTML-by-default stays the shipped behavior (§4), no longer an enforced one. `_render` (and any future name in `KNOWN_SENTINELS`, PT-12) is referenceable from registry lists; all other `_`-prefixed names remain invalid — dropped per CT-6 with `template_error`.
- **CT-5** Registries are read **per stat/render resolution** (tiny local files — no restart, no cache invalidation problem); the built-in `templates/registry.json` rides the same loader (D73). Missing `~/.fused-render/templates/` or `registry.json` = clean no-op, built-in behavior; first run creates nothing.
- **CT-6** **Validation and fallback — per entry:** a folder name must be a single safe path segment (no `/`, no `..`, no `.`, not empty) — it is joined into a filesystem path, so a malformed name must not stat arbitrary locations (correctness guard, not auth — §9 stands). Within a mode list, an entry whose name cannot resolve (unsafe name, `template.html` missing in both PT-6 locations) is **dropped** from the list, and the stat response carries a **`template_error`** string naming the first problem, so a typo is visible (via stat / server log) instead of silently ignored. If the user's value resolves to nothing at all (unparseable JSON, every listed name dangling), fall back to the **built-in list** for that extension. An explicitly **empty** list `[]` is not this case — it disables (CT-2/D94), no fallback.
- **CT-7** **No convention fallback:** a folder in `~/.fused-render/templates/` without a registry entry is inert — a draft. Registration is only ever the registry line; deleting the line unregisters. One source of truth.
- **CT-12** **Conditional templates (per-folder gate).** A template folder may ship an optional **`condition.py`** beside its `template.html`, defining `def method(path): bool`. It runs during resolution (PT-8) **after** a template's name resolves, for **both** built-in and user folders (whichever `template.html` PT-6 resolves): the resolved file's path is passed to `method`, and the template is kept in the `templates` list only when it returns truthy — so one registry key can offer different templates for different files (e.g. gate on a path prefix or naming convention). No `condition.py` = unconditionally shown (the common case). Like the registries, it is loaded **fresh per resolution** (CT-5) — no restart — and never inserted into `sys.modules`. When an extension has more than one gated template, the gates are **evaluated concurrently** (one worker per gate; the fixed-name, never-`sys.modules`-inserted load keeps parallel evaluation safe), so the per-stat cost is the slowest single gate, not their sum — but every gate still runs on every file of that extension (they *decide* visibility), so gates must stay fast (path checks, not heavy I/O). A broken condition (no callable `method`, an exception, evaluated on the target path) **drops** that template and surfaces the reason as **`template_error`**, the same posture as an unresolvable name (CT-6): a template gated by code that can't decide is not silently shown. Sentinel modes (`_render`, `_listing` — PT-12, `path: null`) have no folder and are never gated. The registry stays the source of truth for *which* templates apply to an extension; `condition.py` only narrows *whether* a listed one shows for a specific file.

### 16.3 Pipeline & dev loop

- **CT-8** No new pipeline: stat carries the resolved user templates inside the ordinary `templates` list (PT-8); the preview iframe renders the selected mode via `/render` with `_file` exactly like a built-in (PT-2), and the switcher (PT-10) shows user modes indistinguishably from built-ins. M4 auto-reload (§13) covers template development for free — the rendered page watches its own html and every `runPython` file, so editing `template.html` or a reader live-reloads open previews. Registry edits apply on the next stat (navigate/refresh); open previews do not watch `registry.json`.
- **CT-9** **Authoring skill:** a repo skill `skills/fused-render-custom-templates/` covers folder layout, registry format, and registration workflow only; it **delegates all html/py authoring guidance to `skills/fused-render-authoring/`** (no duplicated instruction — one source for the runtime API and template patterns).

## 17. Annotation — An Ordinary View Template (M9, superseded)

Annotation shipped first as an app feature — an orthogonal `_annotate=1` overlay
injected into every view (AN-1…AN-23, M9) — and was then **rebuilt as an
ordinary view template**, the same pattern as `templates/claude/`:
`templates/annotate/` is a self-contained template.html, bound in registry.json
as a trailing mode on annotatable extensions, swappable/shadowable like any
template (PT-6). It renders the file's normal view in a same-origin iframe (a
`view` param picks WHICH mode is being annotated) and implements the whole
experience itself — hover highlight, click-to-comment pins, sidebar,
resolve/delete. Comments live in an ordinary `comments` template param (synced
to the shell URL by the runtime — bookmarkable, shareable), stamped with the
view they were made on so anchors never cross-resolve between views.

Rationale: annotation is a review layer, not app chrome — as a template it
needs no shell code, no server injection, and users can replace or extend it
by dropping a folder into `~/.fused-render/annotate/`. The `_annotate` render
param, the header toggle (AN-2/AN-3), the injected `static/annotate.js`, and
the code template's selection adapter are gone.

**Containment invariant:** every line of annotation logic lives inside
`templates/annotate/template.html` — no other view template carries
annotation code, hooks, or references, and nothing is injected into the
framed view (the template attaches its listeners and one highlight-tint
`<style>` to its own nested same-origin iframe at runtime; that code ships in
the annotate file). Paged views (table, xlsx, pdf) render **stable element
ids** encoding an absolute address — `__fr_r<row>_c<col>`,
`__fr_s<sheet>_r<row>_c<col>`, `__fr_page_<n>` — inert, deep-linkable markup
useful independent of annotation. The annotate template owns an
`ID_RESOLVERS` table keyed on those id shapes: a recognized anchor id that
isn't in the mounted DOM is **off-page, not detached** — the sidebar card
gets a navigable chip ("row 5" / "Alpha · row 3" / "page 3") and clicking it
navigates the framed view there by writing the ordinary `offset`/`sheet`
params the view already watches (the same shell-URL params its own pagination
controls write). An earlier iteration had each paged view expose a
`window.__fusedAnnotateAnchorResolver` hook instead; removed (D78) because it
put annotation-aware code inside view templates. Accepted trade-off: annotate
cannot ask a view whether a row is truly gone from the data, so a comment
past the data's end keeps its "row N" chip instead of turning "detached".

**Comment focus deep link:** an ordinary `comment` template param carries an
id-only deep link (the history→annotate contract, HV-8; mirrors the claude
`session_id` resume precedent — the id is the whole contract and is never
cleared after use). At boot, once the framed view is wired, the template reads
`comment`: if the id is in the live URL store it focuses it (jumping to the
comment's own view first when it differs, then lighting the pin/card); if it
isn't, the template does a **one-shot full-state hydration** — a single read of
`<file>.json`'s `comments` log that imports every LIVE entry (those without a
`deleted_at` tombstone; a tombstoned wanted id gets no import and no focus —
deleted stays deleted, owner call 2026-07-10), strips the server stamps
(`recorded_at`/`updated_at`/`deleted_at`), and merges them into the live set
(live entries win by id) — then saves once (re-recording, a harmless upsert
no-op) and focuses. Deletion is an **explicit** signal: the annotate delete
button drops the comment from the URL and sends its id as `deleted_ids` on the
SAME `record` call, so upsert and tombstone land in one atomic sidecar write
(two separate calls could interleave and lose the tombstone); `annotate.py`
stamps `deleted_at` (server `time.time()` SECONDS) on each named log entry.
The tombstone is **permanent** — recording an id never clears it, so a stale
bookmarked URL that still carries the deleted comment (or the hydration merge's
live-wins rule) cannot silently resurrect it in the log. Absence
from a `record` array NEVER deletes — each URL carries only its own review
subset, so a missing id means "not in this review", not "deleted". The live URL
`comments` param stays the sole live store; the sidecar read is one boot-time
hydration for a deep link whose id is absent from the live set, not a live-store
sync back from the sidecar. An unreadable/unparseable sidecar or a missing id
fails silently (no error UI, no focus).

## 18. Export — Portable Bundles for Hosted Serving (M10)

Goal: pack a renderable page into a portable *bundle* that a **separate** hosting
layer (the `fused` wheel's `build_html_artifact`) can serve — without weakening the
local-only invariant (§1). Export is a **local, offline call on the already-running
server** (`POST /api/export {"page", "out"}`, both absolute paths): it uploads
nothing and reaches no network — it writes the bundle to a local directory, the same
as every other filesystem-touching endpoint. fused-render itself still hosts
nothing. Full detail: `docs/EXPORT.md`.

### 18.1 Bundle format

- **EX-1** A bundle is a directory holding `page.html` (the page verbatim),
  `manifest.json` (the hosting contract), `code/<name>.py` (one per `runPython`
  target), and `assets/<key>` (one per `rawUrl`/`readFile` target).
- **EX-2** `manifest.json` (`{"fused_render_bundle": 1, "page", "entrypoints",
  "assets"}`) maps each `runPython` literal path to a served route name + bundled
  file, and each `rawUrl`/`readFile` literal path to an asset key + bundled file. The
  hosting layer wires the served page's runtime from this map — it never re-parses
  the HTML.

### 18.2 Portable subset

- **EX-3** Only the transport-agnostic part of the injected `window.fused` API is
  portable: `runPython` (→ a served route the page posts to), `rawUrl`/`readFile`
  (→ read-only bundled assets), and `params` (pure client-side URL state, unchanged).
  `writeFile`, `stat`, and SSE live-reload are **unsupported** — a hosted artifact is
  immutable and has no filesystem behind it.

### 18.3 Static resolution & fail-loud

- **EX-4** Every `runPython`/`rawUrl`/`readFile` path must be a **string literal**
  resolvable at build time. A computed path, an unsupported API call, an absolute or
  `..`-escaping path, or a missing target is a **blocking error** — export writes
  nothing and reports all problems at once, rather than shipping a page whose calls
  404 when hosted.
- **EX-5** Route names derive from the `.py` stem (`sine.py` → `sine`), are prefixed
  `run-` when they'd collide with a reserved serve route (`data`, `health`, the
  `_`-prefixed control/shell/asset routes), and are suffixed `-2`, `-3`, … on
  duplicate stems — so the map is always valid and injective.

## 19. Deploy — Hosted Publish through the fused CLI (M11)

Goal: close the gap between §18's bundle and a working URL, from the shell. The
local-only invariant (§1) is unchanged in kind: fused-render still binds
127.0.0.1, hosts nothing, and mints no URLs — **deploying is an explicit user
action that delegates to the separately-installed `fused` CLI** (`fused share`,
the fused repo's one URL-minting operation — its spec/serve/share-links.md and
spec/serve/fused-render.md; the same shell-out pattern the flow app uses for
project deploys). The server orchestrates the child process; nothing else in
the product gains network access.

### 19.1 Surface

- **DP-1** Any file preview whose mode list carries the `_render` sentinel
  **and** whose filename is `.html`/`.htm` shows a **Deploy** header action —
  both conditions, because that is exactly the set `/api/export` accepts: a
  registry rebind can put `_render` on any type (D73), but the exporter is
  extension-gated, and the button must never open a modal that cannot deploy.
  Additionally gated on the opt-in `deploy_enabled` pref (PF-8): Deploy is off
  by default, so the button is hidden entirely until enabled from Preferences
  → Deployments (re-read on focus/visibility, so a toggle shows through
  without a remount).
  A green dot marks a page whose stored deployment reads active (a local
  pointer read — opening a preview never spawns the CLI; re-read on tab
  focus/visibility regain, so an out-of-band revoke — e.g. the Preferences
  page in another tab — shows through without a remount). Directories never
  show it. The action opens the Deploy modal.
- **DP-2** The modal handles its states in order: the fused CLI missing → an
  install panel; no hosted env configured → guidance (`fused env create` /
  `fused cloud setup`, naming the envs file); else the form — env picker,
  current-deployment card (status chip, URL with copy/open), a **"Will
  publish" preview** (DP-2a), Deploy/Redeploy, and Revoke. The modal is scoped
  to the current page; the **env-wide** deployment list (DP-13) lives on the
  Preferences page's Deployments section (PF-6), not in the modal.
- **DP-2a** Before the click, the modal shows exactly what a deploy would
  publish (`GET /api/deploy/preview` → `preview_deploy`, the same pure
  `plan_export` scan the real export runs, resolved fresh, no files written):
  the page plus each `runPython` target (and its served route name) and each
  `rawUrl`/`readFile` asset. Export blockers come back in the same response
  and **disable Deploy** with the full list — an unexportable page reads as
  "fix these" up front, never as a failed deploy. A preview *fetch* failure
  (unexportable type, file deleted since the header rendered) degrades to a
  blocker entry the same way — the dialog still renders its form; it never
  dead-ends on the preview call.
- **DP-2b** Login guidance, before and after the click.
  `GET /api/deploy/config` carries `fused_logged_in` — presence of the fused
  CLI's own control-plane credentials file
  (`~/.openfused/fused-cloud-credentials.json`,
  `OPENFUSED_FUSED_CLOUD_CREDENTIALS` honored). Presence-only by design: an
  expired-but-refreshable token still works (the CLI refreshes silently), so
  the CLI stays the authority at action time. With a managed `fused` env
  selected and no credentials on disk, the modal warns **before** the click,
  naming `<setup_cli> cloud login` (a one-time browser sign-in). After a
  failed action, CLI errors that name `fused cloud login` are suffixed with
  the packaged app's real wrapper path (`_cli_error` + `_setup_cli_hint`) —
  plain `fused` doesn't resolve inside the .app, so the instruction must be
  runnable as printed. The no-envs guidance states that `cloud setup` opens
  a browser sign-in first.

### 19.2 The fused CLI seam

- **DP-3** CLI resolution (`deploy.fused_cli`) has **exactly two sources —
  one explicit, one autodetected — and nothing else**: (1)
  `FUSED_RENDER_FUSED_BIN` (verbatim, whitespace-split — compound commands
  work, and it is the test seam); (2) the `fused` package **importable in the
  server's own interpreter**, run as `[sys.executable,
  fused_render/_fused_cli.py]` — a shim that sets `argv[0] = "fused"` and
  calls `fused._cli.main()`, behaviorally identical to the console script.
  There is deliberately **no venv-bin scan, no PATH lookup, and no
  well-known-location guessing**: a CLI the server didn't get from its own
  interpreter runs only because the user explicitly configured it. (The old
  venv-bin step is subsumed — a venv whose bin/ has the script always has the
  package importable.)
- **DP-3a** Child-env hygiene: an **external** CLI (the override) is spawned
  with `PYTHONHOME`/`PYTHONPATH` scrubbed — inside the packaged app those are
  bundle-scoped and would break any other Python (the las template's
  external-spawn precedent); the in-interpreter shim keeps them (they are
  what make `sys.executable` work in the bundle). `OPENFUSED_ENV` targeting
  (DP-7) is unchanged for both.
- **DP-4** When the CLI is missing and installing is possible (Python ≥ 3.11
  per the wheel's marker, and the interpreter has pip), `POST
  /api/deploy/install` pip-installs **the wheel pinned by
  `deploy.PINNED_FUSED_REQUIREMENT`** into the server's interpreter — which
  makes the package importable there, i.e. lands in DP-3's autodetected
  source (finder caches are invalidated after the install so the probe sees
  it without a restart). The constant is the in-code source of the pin;
  pyproject.toml's `[fused]` extra must reference the same wheel and a test
  pins the two together. Reading the pin from installed dist-info metadata is
  rejected: metadata is absent on source-tree runs and stripped app bundles,
  and goes stale on an editable install that predates the extra — all of
  which disabled the button exactly when it mattered, while the constant
  ships in the same file as the code using it. When installing is impossible,
  the modal states why — old Python, or a pip-less embedded interpreter
  (point `FUSED_RENDER_FUSED_BIN` at a fused installed with another Python) —
  plus the manual `pip install "fused-render[fused]"` hint.
- **DP-16** The packaged macOS app **ships the CLI**: `build_dmg.sh` installs
  the `[fused]` extra into the bundle (py2app force-copies `fused` + its
  data-bearing deps — `setup_py2app.py`), so DP-3's autodetected source is
  always present and the install panel never appears in the .app (its sealed,
  notarized bundle could not be pip-installed into anyway). The build also
  ships a terminal wrapper, `Contents/Resources/bin/fused` (bundled python +
  the DP-3 shim), for the one-time interactive setup a modal can't do —
  `fused cloud setup` / `cloud login` / `env create` — and smoke-tests real
  CLI verbs through the shim before signing, so a py2app packaging gap fails
  the build rather than the user's first deploy. The wrapper lives under
  `Resources`, not `MacOS`: everything in a bundle's `MacOS/` is nested code
  to codesign, and a shell script there cannot carry a code signature — the
  bundle seal fails ("code object is not signed at all"); a script under
  `Resources` is sealed by the resource rules instead. `GET /api/deploy/config`
  carries `setup_cli` — the wrapper's absolute path when frozen
  (`sys.frozen == "macosx_app"`), else `"fused"` — and the modal's
  no-envs guidance names it.

### 19.3 Environments

- **DP-5** Eligible deploy targets are the **hosted** environments in the fused
  CLI's own store (`~/.openfused/envs.json`, `OPENFUSED_ENVS_FILE` override):
  backends `fused` (managed) and `aws` (self-provisioned serving plane) —
  never `local`, which has no serving plane. The store is read directly, so the
  picker renders even before the CLI is installed.
- **DP-6** Default pick: `OPENFUSED_ENV` when it names an eligible env, else
  the first `fused`-backend env (preferring the store default when it is one),
  else the store default, else the first eligible.
- **DP-7** The chosen env is targeted by setting `OPENFUSED_ENV` on the child —
  the CLI's own override channel; no config file is edited.

### 19.4 Deploy semantics

- **DP-8** Each deploy re-exports the page (§18) into a fresh temp directory
  and hands that bundle to the CLI; the bundle is deleted afterwards. An export
  error blocks the deploy (400, all problems at once — nothing is uploaded).
- **DP-9** Deploys are **public share links** (`share create --public`, no
  `--token`): an opaque, unguessable capability URL. Rationale: authed mounts
  cannot serve a hosted page's browser asset GETs yet (fused repo,
  spec/serve/fused-render.md § Limitations); gate pickers become an option when
  that lands.
- **DP-10** Redeploy keeps the URL. Same-env pointer + mount active per
  `share list` → `share repoint <token>` (stable URL); revoked tombstone →
  `share recreate --same-token` then repoint (a failed repoint best-effort
  re-revokes, so a deliberately taken-down link never comes back silently live
  with old content; the pointer is then persisted to the TRUE resulting state
  and the raised error names it — compensation succeeded → the link is down →
  pointer `revoked`; compensation ALSO failed → the mount is live with its old
  content → pointer `active` (so the dot matches reality) and the error names
  the token for a manual `fused share revoke`); token absent from the list
  entirely (e.g. after an
  `infra teardown`) → fresh `create`. Deploying to a **different** env always
  creates fresh there and repoints the pointer — the old env's mount stays
  live, and the modal says so inline.
- **DP-11** CLI output is parsed defensively (`token`/`id`/`url`/`status`
  only): the managed backend returns the URL on create/repoint/recreate; an
  AWS env prints token+path only, so `url` may stay null — the last-known URL
  is kept, never regressed to null by a URL-less repoint.
- **DP-15** Version dependency, surfaced not hidden: whether a *bundle* deploy
  succeeds on a given backend is the installed fused CLI's contract, not ours —
  the fused repo's spec/serve/fused-render.md publishes bundles via
  `share create` on AWS envs and lists the managed backend's inline-upload
  bundle classification as a follow-up. fused-render passes the CLI's own
  error through verbatim rather than second-guessing the installed version.

### 19.5 State & truth

- **DP-12** A thin per-page pointer at `~/.fused-render/deployments.json`
  (shell/storage; keyed by absolute page path — env, backend, token, url,
  status, entrypoints, updated_at) lets the shell mark deployed files, re-show
  the URL (`create` returns it exactly once; `share list` never carries one),
  and redeploy to the same token. **`share list` on the env stays the
  authority**: the modal reconciles status against it on open (`--all`, so an
  AWS caller-identity change can't fake a revoke); an unreachable env returns
  the last-known pointer with `reconciled: false` instead of failing the
  dialog. A reconciled response also carries `live` (`active | revoked |
  absent`): absent persists as pointer-status `revoked` (the link *is* down)
  but the modal must not promise a same-URL restore for it — an absent mount
  redeploys as a fresh create with a new link (DP-10), and the stored URL is
  likewise never carried onto a *different* token (DP-11's fallback applies
  only while the token is unchanged). The action label's URL promises
  ("same URL" / "restore URL") render only from a **verified** `live`
  classification: when the reconcile never ran (unreachable env, `live`
  null) the button reads a plain "Redeploy" that promises nothing.
- **DP-12a** Store integrity: the pointer file is rewritten whole on every
  mutation, so two writers must not race and a corrupt file must not be
  clobbered. Writes serialize through one process lock (`_update_store`) —
  closing the lost-update window against the reconcile writer (a focus
  refresh) — and load via `_load_store_for_write`, which raises rather than
  overwrite a file that exists but doesn't parse (overwriting would drop every
  other page's pointer, orphaning live mounts). `deploy_page` validates the
  store before the CLI so a corrupt store fails fast instead of minting a
  mount it then can't record. Reads (`get_deployment`, the status/dot) stay
  lenient — a corrupt store shows as not-deployed rather than erroring a
  preview.
- **DP-12b** The open modal re-reconciles on tab focus/visibility regain (like
  the header dot, DP-1), so a page revoked out-of-band — e.g. from the
  Preferences tab — updates the open dialog instead of contradicting the dot.
  That focus refresh is a **background** load: it updates in place, never
  clearing the form to "Loading…" or replacing it with an error on a failed
  re-fetch (only the initial mount load does that). 
  It preselects the deployment's env only when that env is still configured
  (else falls back to the default and states the old env is gone), so a
  removed env never leaves Deploy silently disabled. The dialog is always
  closeable — even mid-action (the action continues server-side and the dot
  stays correct via `onChange`), so a slow CLI child can't trap the user.
- **DP-13** `GET /api/deploy/shares?env=…` is the "what's deployed on this
  env" view: every mount from `share list --all`, joined back to the local
  page that deployed it via the pointer store (`page: null`, rendered "not
  from this app"), local pages first, live before revoked. Its consumer is the
  **Preferences page's Deployments section** (PF-6) — a single env-wide list
  with Revoke — not the per-page Deploy modal. `share list` returns no URLs on
  either backend; each mount's URL is the pointer's recorded one, else
  **derived from the env's base URL**: every mount on one env serves as
  `<base>/<token>` (share-links.md §6), so any recorded absolute URL whose path
  ends in its own token reveals the base for all the rest (`_serve_base_url`).
  With no recorded link to derive from (e.g. only AWS deploys so far), URLs
  stay null and the cell says why on hover.
- **DP-14** Endpoints (`fused_render/deploy.py`, an APIRouter like
  shell/bookmarks): `GET /api/deploy/config`, `GET /api/deploy/status`,
  `GET /api/deploy/preview`, `GET /api/deploy/shares`, `POST /api/deploy`,
  `POST /api/deploy/revoke`, `POST /api/deploy/install`; the POSTs carry the
  `X-Fused` guard (D36). CLI failures surface their last stderr line verbatim
  (click's `Error: ` prefix stripped) — the fused CLI's messages already name
  the fix (`fused cloud login`, `fused infra serve`, …).

## 20. Preferences — Shell Settings Page (M12)

Goal: one unobtrusive place for the shell's cross-cutting settings and
housekeeping. Entry is a muted gear row pinned to the **sidebar's bottom-left**
edge; it navigates to **`/view/_prefs`** — a shell-owned sentinel pathname like
`_panel`/`_tab` (no `/embed` variant: settings chrome inside a pane makes no
sense). Server state lives in `~/.fused-render/prefs.json` behind
`shell/prefs.py` (the D75 shell-state pattern: storage helpers + an APIRouter;
never imports server).

### 20.1 Store & endpoints

- **PF-1** `GET /api/prefs` → `{engine: {selected, effective, forced_by,
  fused_available}, log: {path, dir}, deploy: {enabled}}`. `PUT /api/prefs`
  (X-Fused) applies a **partial** update — any of `{engine}` and/or
  `{deploy_enabled}` present, so each control PUTs only its own field — and
  returns the same shape. An unknown engine value, a non-boolean
  `deploy_enabled`, or a body naming no known preference → 400; the file merges
  (future prefs are new keys, not new files).
- **PF-1a** The page renders its sections in this order: **Template registry**,
  **Logs**, **Execution engine**, **Deployments** (the spec subsection
  numbering below is organizational, not the visual order).
- **PF-2** The page is a thin client over existing backends everywhere else:
  logs reveal via `POST /api/fs/reveal`, deployments via `GET
  /api/deploy/config` + `GET /api/deploy/shares`, revocation via `POST
  /api/deploy/revoke`, registry via `GET /api/templates/registry`.

### 20.2 Execution engine switch

- **PF-3** The persisted `engine` pref (`builtin` default — D70 stands, the
  pref is the opt-in D69 anticipated; or `fused`) drives `/api/run` dispatch,
  **read per request** so a switch applies to the next run with no restart
  (the registries' CT-5 no-restart discipline). Selecting `fused` is
  *effective* only while the fused local backend is importable
  (`prefs.fused_engine_available`, probed per call — an install mid-session
  shows through); otherwise execution degrades to builtin and the page says
  so. The fused option is disabled with an install hint when unavailable.
  **One resolver, no divergence:** `prefs.effective_engine()` is the single
  function both dispatch (`server.current_engine`) and the page's reported
  "running" engine (`engine_state().effective`) go through, resolving the
  override + pref + availability **live** on every call — so the page can
  never claim a different engine than `/api/run` uses, even for a forced
  `=auto` after a mid-session install (an earlier startup-frozen resolution
  let those drift).
  **Both engines are local**: the fused engine instantiates the package's
  `LocalPythonComputeBackend` directly (engine.py — host venvs under
  `~/.openfused/venvs`), never resolving a named environment; `envs.json`,
  the default env, and `OPENFUSED_ENV` play no part in page execution. Fused
  *environments* are exclusively deploy targets (DP-5) — a separate axis,
  and the page's copy states this so "Fused engine" is never read as "runs
  on my Fused env".
- **PF-4** `FUSED_RENDER_ENGINE` remains the **process-level override**: when
  set it beats the pref entirely. `server._forced_engine()` runs **once at
  startup** purely to validate (raises on a bad value; `=fused` still fails
  loudly when missing) and log the choice — dispatch itself goes through the
  live resolver (PF-3), so the override is re-read per request, not frozen.
  The page shows the switch locked with the variable's value; a PUT still
  persists (applies once the override is removed). `GET /api/config`'s
  `engine` reports the in-effect engine per request.

### 20.3 Logs

- **PF-5** The page names this process's log file (`logs.log_path`, from
  `GET /api/prefs`) and "Open logs location" reveals it in the OS file
  manager through the existing reveal endpoint — the web-UI twin of the
  menu-bar app's "Open logs".

### 20.4 Deployments

- **PF-8** The section leads with an **opt-in toggle** for the Deploy
  affordance: the persisted `deploy_enabled` pref (default **off**), PUT via
  `{deploy_enabled}`. Deploy publishes a page to a public hosted URL through
  the fused CLI, so it is opt-in — the preview-header **Deploy** button (§19,
  DP-1) and its modal stay hidden until this is turned on. The gate is a UI
  affordance only, not a security control (the `/api/deploy*` endpoints keep
  their X-Fused guard); the preview re-reads the pref on focus/visibility so a
  toggle shows through without a reload. Any non-`true` stored value reads as
  off.
- **PF-6** A per-env view of `fused share list` (the same joined
  `/api/deploy/shares` data as the Deploy modal's list, same copy: rows with
  a file name were deployed from this app) with a **Revoke** action per
  non-revoked mount. Revocation is by **env + token** (`POST
  /api/deploy/revoke {env, token}` → `deploy.revoke_mount`), so it also
  covers mounts with no local pointer — the CLI's owner-binding still
  applies and its refusal surfaces verbatim. Any local pointer recording the
  revoked mount flips to revoked, keeping the page's Deploy button honest.

### 20.5 Template registry view

- **PF-7** `GET /api/templates/registry` returns the merged
  extension→templates bindings from both registries (SPEC §16): one row per
  pattern with its resolved mode list (first = default), `disabled`
  for `null` bindings, `source` (`builtin` / `user` / `user-override` — a
  user key identical to a built-in key replaces its row), and per-entry
  shape errors. Override detection is **case-insensitive**, matching how
  resolution actually matches keys (`_key_segments` lowercases): a user
  `.CSV` overrides a built-in `.csv` as one `user-override` row, never two
  mis-sourced rows.
  This is the table of bindings, not a per-file resolver: distinct keys
  coexist and CT-3 specificity decides per file. Read per request like every
  resolution (no restart).

  **Superseded (2026-07-09, owner call):** the read-only registry section was
  removed from the Preferences page when the full Template Management view
  shipped (§22, `/view/_templates`) — a single home for bindings rather than a
  glance in one place and an editor in another. The **`GET /api/templates/registry`
  endpoint stays** (unchanged contract, TV-4); it is now consumed by the
  Templates view instead of Preferences.

---

## 21. Session Restore — Per-File Last Params (D84)

Goal: opening a file the way most opens happen — a listing click, a Finder/DMG
double-click, the root redirect — should not lose whatever params you last had
on it. A **file** (never a directory, never an embed-mode pane) remembers its
last shell query in the same `.html.json` sidecar the `claude` chat template
(§7) and bookmark history (SB-7) already use.

- **LSN-1** A viewed file's last URL params are stored as `lastSession` in its
  `<file>.json` sidecar, sibling to the claude template's `claudeSessions` key
  and SB-7's `bookmarkHistory`.
- **LSN-2** `lastSession = {search, updated_at}` — `search` is the shell query
  string verbatim, no leading `?` (same literal-URL posture as bookmarks, SB-2).
- **LSN-3** Tracking upserts when the shell query has a param **other than
  `_mode`**, or when a `lastSession` already exists for the file (so once a
  session is going, a later `_mode`-only change is remembered too); a query that
  is empty, or `_mode`-only with no prior session, never starts one.
- **LSN-4** Opening a file with an **empty** shell query restores `lastSession`
  (if present) via `history.replaceState` before the preview mounts.
- **LSN-5** Opening a file with a **non-empty** query (bookmark, hand-typed,
  refresh) — those params win, no restore — and, if qualifying (LSN-3), become
  the new `lastSession`.
- **LSN-6** Directories and embed-mode panes (panel/tab, D72) neither track
  nor restore — layout mode already owns pane params.
- **LSN-7** Persistence is `GET`/`PUT /api/session` (`fused_render/server.py`);
  `PUT` carries the `X-Fused` guard (D36), `GET` is unguarded (read-only).
- **LSN-8** Sidecar writes read-merge-write the whole dict, so `claudeSessions`,
  `bookmarkHistory`, and `lastSession` never clobber one another (last-write-wins
  on a true simultaneous write — D3).
- **LSN-9** The preview is held (a brief loading state) until the restore
  decision resolves — no flash of default params before the restored ones apply.
- **LSN-10** Tracking writes are debounced (400 ms) and fire-and-forget; a
  sidecar read/write failure never blocks the view — it just renders bare.
- **LSN-11** Dropping params back to empty/`_mode`-only leaves the stored
  `lastSession` untouched — a later bare open re-applies it. Accepted quirk,
  not a bug.

## 22. Explorer Search — Streamed Recursive Walk (M14)

Goal: an in-folder search (FS-7) whose first results paint in tens of
milliseconds on any tree, whose coverage is never silently starved by one big
subtree, and whose truncation is always visible. The searcher is the shell
(client-side fuzzy scoring, fzf/VS Code Quick-Open model — the corpus is local
and per-keystroke re-ranking must not pay a network round trip); the server's
job is to deliver the corpus fast, shallow-first, and pruned of machine noise.

### 21.1 Walk order & pruning (server)

- **SR-1** `GET /api/fs/walk` traverses **breadth-first** (`_walk_bfs`): every
  depth-N entry is emitted before any depth-N+1 entry; within one parent, dirs
  first then files, each name-sorted. Any early stop (cap, disconnect)
  therefore keeps complete shallow coverage. The old depth-first walk let one
  big sibling eat the whole entry budget — a home dir looked like:

  ```
  depth-first + cap                      breadth-first + cap
  ─────────────────                      ───────────────────
  ├─ Desktop   ✓ dives to bottom,        ├─ level 1: ALL top dirs first ✓
  │    eats 15,926 / 20,000 slots        ├─ level 2: all their children ✓
  ├─ Movies    ✗ CAP DEAD — 0 children   ├─ level 3: …
  └─ Music     ✗ 0 children              └─ cap cuts the DEEPEST level only
  ```

- **SR-2** Machine-noise pruning is **gitignore-driven inside git
  repositories** (D100): entries the containing repo's own gitignore rules
  ignore are never emitted **nor descended** — the generic answer to `dist/`,
  `build/`, `.next/`, `target/` and every other ecosystem's junk, with the
  repo's own file as the authority (negations like `!keep.log` honored).
  Verdicts come from one streaming `git check-ignore --stdin` co-process per
  repo (`_IgnoreOracle`, ~14 µs/query, ≤ `WALK_MAX_ORACLES` open at once, all
  closed when the walk ends); each directory inherits its repo root through
  the BFS queue, a `.git` entry starts a nested repo with its own rules, and
  a walk rooted *below* a repo root resolves it via one `git rev-parse
  --show-toplevel`. A directory with a `.gitignore` but NO repo anywhere in
  scope (an un-inited project, an Obsidian vault) prunes the same way: the
  oracle grafts it onto a shared empty `GIT_DIR` as its `GIT_WORK_TREE`, so
  check-ignore honors standalone `.gitignore` files too (cascading into
  subdirs, negations included). Pruning is an optimization, never a
  dependency: git missing or failing degrades to no gitignore pruning.
  Known miss, accepted: walking a SUBDIRECTORY of a repo-less project looks
  upward for nothing (no work-tree boundary to find), so an ancestor's
  standalone `.gitignore` doesn't apply there.
- **SR-2a** `WALK_IGNORE_DIRS` (`node_modules`, `__pycache__`, `venv`,
  `.venv`, `.git`, `site-packages`) stays as the **universal floor**, checked
  by bare name everywhere: it covers junk outside any repo (a stray
  `node_modules` in `~/Downloads`, `Library/Python/*/site-packages`) and
  `.git` itself, which git never reports as ignored. Both SR-2 and SR-2a
  apply in hidden mode too — those trees are machine noise, not "hidden
  data" (a `.py` extension search must not drown in `.git` object files).
  `.git` *files* (worktree/submodule pointers) are ordinary files and do show.
- **SR-2b** Because the walk excludes gitignored entries outright, walk
  entries carry **no `ignored` dimming flag** — dimming remains a
  `/api/fs/list` (plain listing) concern, where ignored entries are still
  shown. Search excludes; the listing dims. (VS Code's split: explorer shows
  gitignored files, Quick Open doesn't.)
- **SR-3** macOS package directories (`WALK_LEAF_DIR_SUFFIXES`: `.app`,
  `.framework`, `.bundle`, `.photoslibrary`, case-insensitive) are emitted as
  a single dir entry but never descended — Finder semantics; one Electron
  `.app` alone is thousands of internal files nobody searches.
- **SR-4** Symlinks are emitted but never followed; unreadable dirs/entries
  are skipped silently (matches `/api/fs/list`).
- **SR-5** `WALK_MAX_ENTRIES` (200 000) is a **memory/latency safety valve,
  not a coverage budget**: with BFS it only ever cuts the deepest levels of
  pathological trees (mounted volumes, cache farms). The response carries
  `truncated` so the UI can be honest about it (SR-10).

### 21.2 Streaming wire format

- **SR-6** `?stream=1` returns `application/x-ndjson`: zero or more
  `{"entries": [...]}` batch lines (`WALK_BATCH_SIZE` = 500 per line), then
  **exactly one** terminal `{"done": true, "truncated": bool, "total": n}`
  line. Closing the connection cancels the walk server-side (the generator is
  closed on disconnect). Without `stream=1` the original single-JSON shape
  (`{path, entries, truncated}`) is unchanged — same entries, same BFS order.

  ```
  blocking (before)                      streamed (after)
  ─────────────────                      ────────────────
  type ▶ [  spinner ~1s  ] ▶ ALL         type ▶ ~10ms ▶ first results
         nothing until whole walk               ▶ list fills in live
         done + one giant JSON                  ▶ "N matches · M scanned…"
  ```

### 21.3 Shell search behavior

- **SR-7** The listing's search (`?q=`, URL-synced like sort) fetches **one
  hidden-inclusive dataset** (`hidden=1` always) and filters dot-entries at
  display time: a dot-leading query segment (`.py`, `sub/.env`) shows them,
  anything else hides them. One corpus means flipping intent mid-query never
  refetches, and `.py` works as an extension search. The walk starts lazily
  on first focus (warm-up) or a URL-seeded query, is cached until the dir
  watch fires, and the in-flight stream is aborted on refresh/unmount.
- **SR-8** Scoring is incremental and off the critical path: stream flushes
  commit at most every 200 ms (`STREAM_FLUSH_MS`), each flush fuzzy-scores
  **only the entries appended since the last one** and merges them into the
  prior ranked list; a full re-scan happens only when the query or
  hidden-intent changes (and then on React's deferred schedule). Rationale:
  re-scoring the whole grown array per network chunk saturated the main
  thread near the tail of a big walk — stuck stale-dim, queued clicks.
- **SR-9** Results render in pages of 250 rows; a sentinel row +
  IntersectionObserver reveals the next page as the user scrolls. The full
  ranked list stays in memory for the count text; ranking = longest
  consecutive run, then fuzzy score, then shallower path, then name.
- **SR-10** Truncation is always visible: a live `N matches · M scanned…`
  counter while streaming; a `+` suffix and tooltip on the final count when
  the cap hit; and the zero-match message names the covered entry count
  ("No matches in the first 200,000 entries — this folder tree is too large
  to search fully") instead of a bare "No matches".
- **SR-11** The query mirrors into the URL **debounced** (200 ms): Safari
  rate-limits `history.replaceState` (~100 calls/30 s, then throws), so
  per-keystroke sync is a crash, not a nicety. Input state stays immediate;
  only the URL lags.

---

## 23. Template Management — Sources, Bindings & Import/Export (M15)

Goal: a dedicated view that turns the read-only registry glance of §20.5
(PF-7) into a full editing surface for template bindings, plus the ability to
see the whole template inventory across sources and move user templates
between machines as zip files. Same underlying data as §16/§20.5 — this
section adds the write path, the inventory/provenance view, and
import/export; it does not change the resolution engine (PT-6/CT-3), the
registry file format (CT-2/CT-10/CT-11), or PF-7's read-only endpoint
contract (TV-4). The read-only glance itself is retired from Preferences once
this view ships (§20.5); the endpoint it used is now consumed here instead.

### 23.1 Sources model (extensibility)

- **TV-1** **DECIDED (D86):** the builtin/user pair (§7, §16) is generalized
  into an ordered list of **sources** — `Source { id, label, editable,
  precedence }`. Today exactly two ship: `core` (`id:"core"`,
  `editable:false`, `precedence:0`, the `TEMPLATES_DIR`/`BUILTIN_REGISTRY`
  pair) and `user` (`id:"user"`, `editable:true`, `precedence:100`, the
  `USER_TEMPLATES_DIR`/`USER_REGISTRY` pair, D76's paths). The list is
  modeled so a third source (org/project) can be appended later with zero UI
  rework — **not built now** (§23.4).
- **TV-2** Effective binding for a registry key = the value from the
  highest-precedence source that defines it — unchanged from PT-6/CT-3 (user
  beats core); the sources list is a presentation/provenance layer over the
  existing resolution rule, not a new one.

### 23.2 API

New endpoints live in `fused_render/templates_api.py` (a `templates_router`,
mirroring `shell/bookmarks.py`/`shell/prefs.py`), included from `server.py`
alongside the existing bookmarks/prefs/deploy routers. Mutating routes carry
the `X-Fused: 1` guard (D36); all paths resolve under `home_dir()`.

- **TV-3** `GET /api/templates/inventory` — the template pool across sources:
  `{sources, templates:[{name, source, editable, hasIcon, usedBy,
  shadowsCore}]}`, one entry per **resolved** folder (a user folder
  shadowing a core folder of the same name emits one `source:"user",
  shadowsCore:true` entry, not two). `usedBy` = registry keys whose effective
  ordered list contains the name.
- **TV-4** `GET /api/templates/registry` — **extended**, back-compat fields
  kept (`builtin_registry`, `user_registry` paths) so PF-7's Preferences
  section keeps working unchanged. Adds `sources` and, per entry, `keyKind`
  (`simple|compound|wildcard|directory`), the effective `templates` list
  resolved to `{name, source, exists, hasIcon}` (a name with no folder on
  disk resolves `exists:false` and stays in the list — surfaced as broken,
  not dropped), `resolvedSource`, `overridesCore` (true whenever the user
  registry defines the key, regardless of value equality), `disabled`
  (effective value is `null`), `coreTemplates` (what the builtin registry
  alone gives, or `null`; drives reset-preview + the known-keys list), and
  `userValue` (raw user-registry value, included only when a user key
  exists). `entries` covers every builtin key plus every user-only key.
- **TV-5** `PUT /api/templates/registry` **(D87)** — upserts **one** user
  key: body `{key, value}` (`value` = ordered name array, `null`, or `[]`).
  Validates the key against the CT-3 grammar; names need only be **non-empty
  strings** — an unknown name is **not** rejected, it saves as a **dangling
  ref** (surfaced broken in the UI, dropped at render) so a user can bind a
  not-yet-created template without being blocked (D95). Only structurally
  invalid entries (non-string / empty) → 400. Then a **read-modify-write of
  that key only** against `USER_REGISTRY` via the existing atomic
  `read_json`/`write_json` helpers (creates the file/dir if missing) — never a
  whole-file overwrite. Returns the recomputed entry (same shape as one
  `entries[]` item from TV-4).
- **TV-6** `POST /api/templates/registry/reset` **(D87)** — body `{key}`;
  deletes that key from the user registry (no-op if absent), reverting the
  effective value to the core one. Returns the recomputed entry, or
  `{key, removed:true}` if no such key resolves anywhere anymore.
- **TV-7** `GET /api/templates/export?names=a&names=b` **(D89)** — streams a
  zip (`application/zip`, `Content-Disposition: attachment;
  filename="fused-render-templates.zip"`) of the named templates — **core or
  user** (a user folder shadows a core folder of the same name; 400 on a name
  that resolves to neither). Names travel as **repeated `names=` params** (not
  comma-joined) so a folder name containing a comma round-trips. Each
  template's folder contents land at its own top level in the zip. **No
  `registry.json` in the zip** — folders only.
- **TV-8** `POST /api/templates/import` **(D90)** — step 1 of 2, multipart
  (`file` field, the `.zip`), stages without committing: unpacks to
  `home_dir()/.import-staging/<importId>/` (`importId` = `secrets.token_hex`).
  Hardening (rejects the whole upload before anything lands outside
  staging): uncompressed total > 50 MB, entry count > 2000, or any single
  entry > 25 MB (zip-bomb guard); any entry that is absolute, contains `..`,
  normalizes outside the staging root, or is a symlink (zip-slip guard). A
  candidate template = a top-level directory containing `template.html`
  (`valid:true`). Returns `{importId, expiresInSec, items:[{name, valid,
  hasTemplateHtml, conflictsExisting, fileCount}], warnings}` —
  `conflictsExisting` flags a name already present under
  `USER_TEMPLATES_DIR`. Stale staging dirs past the TTL are swept
  opportunistically on every call.
- **TV-9** `POST /api/templates/import/{importId}/commit` **(D90)** — step
  2: body `{resolutions: {name: "overwrite"|"skip"|"keep-both"}}`
  (unresolved items default to `skip`). Per valid item: `skip` drops it;
  `overwrite` atomically replaces the existing folder; `keep-both` lands as
  `<name>-2` (then `-3`…, never clobbering). Moves (not copies) from staging
  into `USER_TEMPLATES_DIR`, then deletes the staging dir. Unknown/expired
  `importId` → 404/410. Returns `{imported, skipped, overwritten, renamed}`.
- **TV-10** Reveal and "open in explorer" add **no new endpoints**:
  inventory's Reveal action reuses `POST /api/fs/reveal`; "open in explorer"
  is a plain shell navigation to `USER_TEMPLATES_DIR/<name>`.
- **TV-19** `POST /api/templates/delete` **(D93)** — body `{name}`, `X-Fused`
  guarded; deletes **one user template folder** under `USER_TEMPLATES_DIR`.
  **Core templates are read-only and never deletable** — a core-only name
  resolves to no user folder and 404s (the core folder is untouched); unsafe
  names (path separators, `.`/`..`) → 400; symlinks are rejected. Registry
  bindings are **not** rewritten — a binding that referenced the name resolves
  broken (`exists:false`) until rebound, matching export/import being
  folder-only. Returns `{deleted: name}`.

### 23.3 Frontend — Templates view (`/view/_templates`)

- **TV-11** **(D92)** New route **`/view/_templates`** — a shell-owned
  sentinel dispatched in `App.tsx` the same way `/view/_prefs` is (§20):
  view-only, no `/embed` variant (a template-management page inside an
  embedded pane has no meaning). New component
  `frontend/src/views/Templates.tsx`. The active tab (bindings / library)
  lives in the URL as **`?tab=library`** (bindings = default, clean URL);
  switching tabs is a `pushState`, so browser back/forward moves between
  tabs (D94). The page is keyed by the nav epoch, so it re-derives the tab
  from the URL on each navigation — no separate tab state.
- **TV-12** Sidebar footer gains a "Templates" button next to the
  Preferences gear (`navigateUrl("/view/_templates")`), an inline SVG icon
  in the same style as the gear.
- **TV-13** `lib/api.ts` additions: `getTemplateInventory()` (TV-3),
  `getTemplateRegistry()` (TV-4, extends the existing type, keeps old
  fields), `putRegistryBinding(key, value)` (TV-5),
  `resetRegistryBinding(key)` (TV-6), `exportTemplatesUrl(names)` (builds
  the TV-7 GET url for an `<a download>` click), `importTemplates(file)`
  (TV-8 — the app's first `FormData` multipart call; `X-Fused: 1` header
  set, `Content-Type` left for the browser to fill in with the multipart
  boundary), `commitImport(importId, resolutions)` (TV-9).
- **TV-14** **Bindings table** (one row per registry key): extension/key,
  ordered template chips (first badged "default"), a source chip
  (Core/User), a "● Modified" marker when `overridesCore`, a "Disabled" pill
  when `disabled`, broken-name chips (`exists:false`) in a warning style.
  Filters: All / Modified only / by source; a search box over key and
  template name. `+ Add extension` opens the row editor in create mode.
- **TV-15** **Row editor modal (D91)** (DeployModal-style: backdrop +
  dialog, Escape to close): in **create** mode, a key **pattern builder**
  covering all four CT-3 shapes — simple `.ext`, compound `.a.b`, wildcard
  `.*.json`, directory `.ext/` — via a segmented control with a
  live-rendered key preview and client-side grammar validation (server
  stays authoritative, TV-5); in **edit** mode the key is shown, not
  editable. Template list: ordered chips, drag to reorder (first =
  default), remove, "Add template" opens a picker sourced from `GET
  /api/templates/inventory` grouped by source, disallowing duplicates.
  Actions: **Save** (TV-5), **Disable for this type** (writes `null`,
  inline confirm), **Reset to core** (TV-6, shown only when
  `overridesCore`, previews the core default from `coreTemplates`),
  **Cancel**.
- **TV-16** **Inventory panel**: templates grouped by source, each group
  with its own search + source/used filters. A source's **editability** (the
  🔒 on core) governs only whether its *bindings/templates can be changed* —
  it does not gate read actions. Every row (core **and** user) renders its
  `icon.svg`, name, `usedBy` chips, a select checkbox, and per-row actions —
  Export (single), Reveal in Finder (TV-10), Open in explorer (TV-10) — since
  **core templates are exportable/inspectable too** (owner call: portable
  folders regardless of source). Toolbar: "Import zip" and "Export selected"
  — checkbox multi-select spans any rows (core or user) and drives the export
  download (`downloadTemplatesExport`, which surfaces server errors rather
  than saving a 400 body as a zip). **User** rows also get a **Delete** action
  (never core — the source is read-only); it opens a confirm modal offering
  "Export & delete" (downloads a recovery zip first via `downloadTemplatesExport`,
  then deletes only if that resolves), "Delete without export", or Cancel,
  calling `deleteTemplate` (TV-19) and refreshing on success.
- **TV-17** **Import wizard modal**, three steps: (1) file chooser
  (`accept=".zip"`) → `importTemplates(file)` (TV-8); (2) manifest — a
  table of staged items with a per-conflicting-item resolution selector
  (Overwrite / Skip / Keep both — Overwrite visually distinct, a short
  inline caution suffices, no per-item confirm dialog), invalid items
  greyed and auto-skipped with their reason shown, warnings listed; (3)
  confirm → `commitImport` (TV-9) → a result summary
  (imported/renamed/skipped) → closing re-fetches inventory + bindings.
- **TV-18** Any mutation (put/reset/import commit) re-fetches inventory +
  registry and re-renders — no stale state between the two sections.
  Header copy states plainly that this view manages **bindings + inventory
  only**: editing a template's own files happens in the file explorer
  (D88).

### 23.4 Non-goals (this feature)

- Editing template file contents (`template.html`, `reader.py`, css, icons)
  in this UI — use the file explorer + the existing `/api/fs/write` (D88).
- A real third source (org/project) — TV-1 only models for it.
- Registry bindings inside export zips, or merging/writing registry entries
  from an import — exports are folders only (D89); imported templates are
  inert (CT-7) until bound via the row editor.
- Persisting a per-file "last selected mode" — unrelated, not part of this
  feature.
## 24. History View — Sidecar Inspector Template (D96)

A `history` view template renders a file's `<ext>.json` sidecar (§21, SB-7, D82–D84)
as a readable, sectioned history — every claude session, bookmark, last-session
snapshot, and review comment the file has accumulated. Reachable from both ends:
opening `sine.html` and switching to the `history` mode, or opening `sine.html.json`
(or `data.parquet.json`, or any other `<name>.<ext>.json` sidecar) directly, where
`history` is the default mode.

- **HV-1** An ordinary view template (`fused_render/templates/history/`) —
  `template.html` + `icon.svg` only, **no `.py`** (JSON is browser-parseable; same
  posture as `tree`). No shell/server code; navigation and validation live inside
  the template.
- **HV-2** Registry bindings: wildcard key `".*.json": ["history", "tree",
  "code"]` matches any compound `<ext>.json` sidecar (more specific than bare
  `.json`, which keeps its own tree-first list unchanged) — **no `annotate`**:
  annotating the sidecar log itself doesn't make sense, comments belong on the
  target file (HV-8). `"history"` is also appended to the target-side keys
  `".html"` and `".parquet"` (defaults stay `_render`/`table`). Only these two
  target extensions for now — others later by adding keys.
- **HV-3** Role resolution from `_file`: basename ends `.json` **and** its stem
  (after stripping `.json`) still has its own extension → the sidecar is the
  file itself, target = the name minus `.json` (matches the `.*.json` wildcard
  — a bare `name.json` is never treated as a sidecar); otherwise `_file` is
  the target and sidecar = `_file + ".json"`. Sidecar read via
  `fused.readFile`; absent sidecar → a friendly "no history yet" empty state,
  never an error.
- **HV-4** Validation is **per-key** against an inline `const SCHEMA` in
  `template.html` (a hand-rolled subset validator: `type`, `required`,
  `properties`, `items` — no vendored library). A key that fails renders a
  warning card **in that section only** (first error + collapsed raw JSON of
  that key); the other sections render normally. Only a whole-file parse
  failure (or non-object root) blocks the full view, showing the raw text.
- **HV-5** Unknown top-level keys are NOT corruption — the sidecar is a shared
  store and future writers may add keys. They render as one collapsed
  "Other keys" raw section.
- **HV-6** Entry schemas require only the fields the view renders; extra fields
  on entries are allowed (writers grow their records additively). Timestamp
  units are mixed by design (D83/D84 code comments): bookmark `created_at` and
  comment `createdAt` are **ms** epoch; `recorded_at`/`updated_at` and claude's
  `created_at`/`last_used` are **seconds**. The formatter picks the unit per
  field, never heuristically.
- **HV-7** Interactivity — plain shell navigation via `window.top.location`
  with the `/view/` codec (router.ts shape), the claude-template precedent:
  a claude session opens the target with `_mode=claude&session_id=<id>` (the
  resume contract); a bookmark-history entry and the `lastSession` card open
  the target with their stored `search` verbatim; a comment row opens the
  target with `_mode=annotate&comment=<id>` (HV-8, §17), the same id-only
  precedent.
- **HV-8** Comments render **read-only** (content, created/updated time,
  resolved badge, annotated view — the view never writes the sidecar, HV-9)
  but are now **navigable**: a comment row with an `id` opens the target with
  `_mode=annotate&comment=<id>` — an id-only deep link mirroring the claude
  `session_id` resume contract (HV-7), where annotate resolves the id against
  its live store or a one-shot sidecar lookup (§17). A tombstoned entry (an
  explicit `deleted_at`, stamped via `record`'s `deleted_ids`) renders dimmed and
  struck-through with a " · deleted" tooltip note and is **inert** — no deep
  link; a deleted comment never comes back (owner call 2026-07-10). Supersedes
  the 2026-07-09 owner call that kept comments non-navigable (owner reversed
  2026-07-10).
- **HV-9** The view never writes the sidecar.

## 25. Pinned View — Menu-Bar Popover (M16)

The status item IS the app's whole surface: any click on the menu-bar icon
drops an NSPopover under it — a native header row carrying every app action
(the old dropdown menu is gone, D98) above a live WKWebView of the pinned
file's rendered view — the same `/embed/<path>` page the shell's panes iframe
(chrome-free, full runtime: `fused.runPython`, params, templates, live
reload). Dragging the popover off the menu bar detaches it into a floating
always-on-top window. macOS app bundle only (rides the rumps entry point,
SPEC DM-7); the CLI/browser experience is unchanged.

- **PV-1** Pin state: a single pinned filesystem path, persisted at
  `APP_SUPPORT_DIR/pin.json` (`{"path": "<abs path>"}`). Survives app restarts.
  Any path the registry can render is pinnable — html, parquet, images,
  directories — because the popover loads `/embed/<path>`, which dispatches
  modes exactly like a shell pane. One pin in v1; no pin list.
- **PV-2** Status-item click routing: every click — left, right, ctrl —
  toggles the popover. No NSMenu on the status item (D98: right-click-for-menu
  is undiscoverable; one icon, one gesture, one surface). The popover opens
  even before the server is ready (the body shows a placeholder) so Quit is
  always reachable.
- **PV-3** Header row (native NSButtons above the webview, in the popover):
  **Open in Browser** (home tab, same pending-queue semantics as before
  readiness), **Copy URL**, **Pin…** (NSOpenPanel; becomes **Change…** when a
  pin is set), **Unpin** (hidden when nothing is pinned), **Logs** (reveal in
  Finder), **Quit**. Native, not web chrome: the header must work when the
  server is dead — a web-based Quit would die with it.
- **PV-4** Popover: `NSPopover`, transient behavior (click-away dismisses),
  default content 420×450 — a square 420×420 webview over the 30 px bar —
  and user-resizable (Resizable added to the popover window's style mask;
  edge-drag). The chosen size is saved on close (pin.json `size`, surviving
  re-pins/unpins) and becomes the new default. One `WKWebView` created with
  the popover and kept alive — view state (params, scroll) survives
  close/reopen. Re-pinning a different file reloads the webview; reopening
  does not. No pin (or server not ready) → the webview shows a built-in
  placeholder page.
- **PV-5** Detach: the popover is detachable (`popoverShouldDetach:` → YES).
  On detach the resulting window is raised to `NSFloatingWindowLevel` — it
  stays above other apps' windows ("pin on top"), is resizable, and closing it
  returns to popover-on-click. Closing/detaching never clears the pin. The
  popover, the detached window, and the open panel all carry
  `CanJoinAllSpaces | FullScreenAuxiliary` so they appear over fullscreen
  apps; the open panel lifts a Prohibited activation policy to Accessory
  (source runs) so it can hold key focus.
- **PV-6** Dependency: `pyobjc-framework-WebKit` joins the `[app]` extra and
  py2app's `packages` list. Like rumps it is macOS-only and imported lazily
  inside the app entry point — core install and CI stay cross-platform.
- **PV-7** New AppKit code lives in `fused_render/menubar_pin.py` (popover +
  click routing controller) and the pure-python pin store in
  `fused_render/pin_store.py` (unit-tested; AppKit code is not CI-testable).
- **PV-8** Fallback: the rumps menu (Open in browser / Copy URL / Open logs /
  Quit) is still built but never attached while the popover controller is
  alive. If `menubar_pin` fails to import or construct (e.g. missing WebKit
  framework), the menu is attached as before — the app is never left
  unquittable.
