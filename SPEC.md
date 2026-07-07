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
- **FS-7** *(follow-up)* Filename search/filter.
- **FS-8** "Open raw" escape hatch for any file: streams bytes with correct MIME type (used for download and by templates for images/video/pdf).

### Sidebar & Bookmarks (M2 — next)

Left sidebar in the shell, always visible:

- **SB-1** Fixed left column. Top entry **Home**: navigates to `/view/<home dir>` (the user's real `~`, independent of `--start-dir`). `GET /api/config` gains a `home` field.
- **SB-2** **Bookmarks section** below Home. A bookmark captures *whatever the right side currently shows* — directory listing or any preview — as the **exact current URL verbatim** (`/view/…?freq=2.4&_file=…`). Clicking a bookmark is a plain browser redirect (`location.href = url`); the sidebar never interprets bookmark contents, so bookmarks survive future param/dispatch changes.
- **SB-3** Capture UI: a bookmark button in the shell header area, one click, no prompt. Default name = basename of the viewed path (file or dir name).
- **SB-4** Bookmarks are renamable inline (edit affordance on hover → input → Enter/blur commits) and deletable. No confirm on delete (re-bookmarking is one click).
- **SB-5** **DECIDED: persistence = browser localStorage** (key `fused.bookmarks`, JSON array `{id, name, url, created_at}`; `id = crypto.randomUUID()`). Zero server code. Known trade-off: bookmarks are per-browser-profile; migration to a server-side file later is a trivial export.
- **SB-6** Duplicate URLs allowed; list ordered by creation time. *(drag reorder, active-bookmark highlight: polish, later)*

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
- **RH-3** Returns a Promise. Resolves with the deserialized return value; rejects with an `Error` whose `.message` is the **final line of the traceback** (reads like `"ZeroDivisionError: division by zero"`), `.traceback` is the full cleaned traceback text (frames point at the script's real path and line), and `.stdout` carries any print output — on Python exception, missing file, bad dependency header, or timeout.
- **RH-4** Concurrent calls are allowed (e.g. a page fires 3 data fetches on load). Server may queue or parallelize; ordering is not guaranteed.
- **RH-5** Calls time out at 30 s (backend-enforced), after which the worker is killed and the promise rejects.

### 4.3 Isolation — DESCOPED (v1)

- **RH-6** v1 uses a **plain same-origin iframe**; the injected runtime calls the server API directly with `fetch`. No sandbox, no postMessage bridge, no token. Previewed HTML is fully trusted.
- **RH-7** *(follow-up)* Sandboxed iframe + postMessage bridge if/when untrusted-HTML protection is wanted.
- **RH-8** Network access from inside the iframe to the outside internet: allowed.

---

## 5. Python Execution

### 5.1 Authoring model

A user Python file registers its entry point with **`@fused.udf`** (the `fused` module is provided inside the execution sandbox — no install needed in the script's env):

```python
import fused

@fused.udf
def main(city: str = "oslo", limit: int = 100):
    return {"city": city, "rows": list(range(limit))}
```

Third-party dependencies are declared per-script with a **PEP 723** inline header; the engine resolves them into a cached venv (D56):

```python
# /// script
# dependencies = ["pandas", "pyarrow"]
# ///
import fused
import pandas as pd

@fused.udf
def main(city: str = "oslo", limit: int = 100):
    df = pd.read_parquet(f"./data/{city}.parquet").head(limit)
    return df.to_dict("records")
```

- **PY-1** The engine runs the file through openfused; a registered `@fused.udf` entry point is called with the params. Scripts with no udf may set a module-level `result` variable instead (parameterless style). A plain `def main` without the decorator is **not called**: the module runs top-to-bottom and any params sent are silently unused — no error.
- **PY-2** Module top-level code runs on every call (the file is compiled and exec'd fresh, not imported); side effects there are the user's responsibility.

### 5.2 Parameter binding

- **PY-3** The JS `params` object maps to keyword arguments by name.
- **PY-4** Values arrive as **raw JSON types — no coercion**. The calling JS owns types: pass numbers as numbers, booleans as booleans (URL params are strings, so convert where you read them — `Number(fused.params.get("offset"))`). Annotations on `main` are documentation, not a coercion table. *(Inverts the original string-coercion model, D56/D57.)*
- **PY-5** Extra or missing params surface as an ordinary Python `TypeError` traceback from the call (no special structured error).

### 5.3 Execution environment

- **PY-6** **DECIDED (v1):** execution is a **fresh subprocess per call** — always-fresh code, zero stale state, trivial timeout/kill; a crash or `sys.exit` cannot take down the server. Cost: interpreter + import time on every call. A warm worker pool is the designated v2 upgrade if interactivity demands it (API unchanged).
- **PY-7** Scripts run in an **openfused-managed venv**, never the server's own environment: bare stdlib by default; a PEP 723 `# /// script` header's `dependencies` are resolved into a **cached per-requirements venv** (uv, pip fallback; first call per requirement set builds it — seconds — then it's reused). The server's site-packages are not importable from scripts. *(Supersedes "the environment the server was launched from", D56.)*
- **PY-8** Working directory during `main()` = the Python file's directory, so relative data paths in user code behave intuitively. (At module-import time the cwd is the engine's exec dir — the backend must read its params file from there before the udf runs; only the `main()` call itself is re-homed.)
- **PY-9** Module reload: automatic — every call is a fresh process, so edits to the .py file take effect on the next call.

### 5.4 Return value serialization

**DECIDED (v1): JSON only.** `main` must return JSON-native values (dict / list / str / num / bool / None). Anything else — including DataFrames and bytes — fails serialization and rejects with the resulting traceback; the user converts himself (e.g. `df.to_dict("records")`).

Deferred to later milestones (needed for data templates):

| Return type | Wire encoding (future) |
|---|---|
| `pandas.DataFrame` / Arrow table | Arrow IPC or `{columns, records}` JSON |
| `bytes` | binary response with declared content type |

- **PY-10** Large results: responses stream; a configurable size cap (default e.g. 100 MB) protects the browser.

### 5.5 Caching — follow-up, not in v1

- **PY-11** Optional per-call cache keyed by `(resolved py path, file mtime, params)`. Opt-in via config (per-directory or global). Keeps re-renders during param tweaking snappy.

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

---

## 7. Preview Templates

Built-in renderable-HTML files that ship **inside the application code**. They are ordinary renderable HTML — same runtime, same `runPython`, same params — proving the primitive is sufficient. Since M8 (template modes) an extension maps to an **ordered list** of templates; each list entry is a **mode** the user can switch between.

### 7.1 Dispatch

- **PT-1** **DECIDED: the registry is server-side** — single source of truth. The extension → template mapping lives in the server; `GET /api/fs/stat` carries the resolved result and the shell simply obeys. *(Originally a single `template: <abs path>|null` field; since M8 the field is the `templates` array of PT-8 — clean break, no compat alias, shell is same repo.)*
- **PT-2** When the user opens `data/trips.parquet`, the shell renders the returned template in the preview iframe and passes the target file as `_file=<path>` **on the iframe's own URL** (not the shell URL — its pathname already names the file, so no duplication like `/view/x.parquet?_file=/x.parquet`). Reserved `_` params are readable by the template, not settable by page code.
- **PT-3** Every template — built-in or user — is a **self-contained folder** named after the template: `fused_render/templates/<name>/` (built-ins) or `~/.fused-render/<name>/` (user, §16), holding `template.html` (required), any sibling helper files (`reader.py`, css, assets), and optionally `icon.svg` (PT-11). Templates render from their real path, so plain **relative** `runPython` paths work unchanged — no virtual-path mechanism needed:

```js
const page = await fused.runPython("./reader.py",
                                   { file: fused.params.get("_file"),
                                     offset: 0, limit: 500 });
```

- **PT-4** Template UI state (current page, selected columns, sort) uses normal params → survives refresh, e.g. `?_file=…&offset=500&sort=fare`.
- **PT-6** **One name-resolution rule everywhere:** a template name resolves to `~/.fused-render/<name>/template.html` if that exists, else `fused_render/templates/<name>/template.html`, else it is unusable (error). A user folder **shadows** a built-in of the same name — the deliberate override channel. The template **name is public stable API**: it is the registry reference, the `_mode` URL value, and the switcher tooltip label. (`fused_render/templates/vendor/` has no `template.html`, so it can never resolve as a template name — the `/template-assets` mount is unchanged.)

### 7.2 Template set — modes per extension

**Shell dispatch is exactly two-way: `templates` non-empty > fallback.** No file-type special-casing in the shell — image, text, and (via the `_render` sentinel, PT-12) HTML handling all arrive through the `templates` list like any other mode. Directories dispatch the same way (a `.zarr` store previews via its `templates`), with `?listing=1` as the one shell-owned escape hatch to the listing view (PT-13).

- **PT-7** The built-in table maps each extension to an **ordered list of template names**. Each entry is a **mode**; the **first entry is the default**. Rule of thumb: `code` (the editable CodeMirror buffer) appears as a secondary mode only for text formats where raw text is meaningful — never for binary formats (a code view of `.parquet` is garbage).

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
| `.py` | `code`, `api` | editable CodeMirror; `api` = swagger-style run form over the `@fused.udf` entry point (D63) |
| `.js .ts .sh .yaml .yml .toml .css` | `code` | editable CodeMirror |
| `.txt .log` | `text`, `code` | `<pre>` |
| `.tif .tiff` | `geotiff` | GeoTIFF/COG via vendored geotiff (in-browser decode, no reader.py); full metadata + dump, photometric routing (RGB/palette/YCbCr), band select + RGB stretch + colormaps, histogram, hover. Small files full-fetched; >32 MiB range-request `fromUrl` |
| `.nc .nc4 .cdf` | `netcdf` | NetCDF-3 via vendored netcdfjs (HDF5/NetCDF-4 → graceful card); leading-dim sliders, colormaps + stretch, histogram, hover |
| `.zarr` (directory) | `zarr` | Zarr v2/v3 store — a *directory*, routed via `DIR_TEMPLATES` (PT-13), not `TEMPLATES`; vendored zarrita (in-browser decode, members fetched per-key); group tree + array select, colormaps + stretch, histogram, hover |
| `.html .htm` | `_render`, `code` | list **hardcoded server-side**, registry-exempt (CT-4); `_render` is a shell sentinel (PT-12) rendering the file itself live (§4) |
| unknown | shell fallback | metadata + raw/download link (built into shell, not a template) |

- **PT-8** `GET /api/fs/stat` carries the resolved mode list as **`templates`**: an array of `{"mode": <name>, "path": <abs template.html>, "icon": <abs icon.svg|null>}`, in order, first = default. `templates: []` when nothing applies (a directory with no `DIR_TEMPLATES` match — PT-13, unmapped extension, `null` binding). The old singular `template` field is **removed**.
- **PT-9** **`_mode` param (shell URL):** non-default modes are selected via reserved param `_mode=<template name>` on the **shell URL** (bookmarkable, same URL-is-state pattern D40 established for the old HTML `_mode=render|source` toggle — that toggle itself is now the ordinary `["_render", "code"]` mode list, PT-12; old `_mode=source` bookmarks fall to the default, accepted break). Absent `_mode` = default = `templates[0]`; selecting the default **deletes** the param (clean URLs); an unknown/stale value falls back to the default with no error. Switching swaps the iframe src to the selected template's `/render?path=<template>&_file=<file>` with a fresh document per switch. Known accepted quirk: template params (e.g. `offset`) persist on the shell URL across mode switches; a param name used differently by two modes collides — documented, not prevented.
- **PT-10** **Mode switcher (shell, preview header):** rendered only when `templates.length > 1`, right side of the preview header bar. **Icon-only buttons**, mode name via native `title` tooltip, active mode in accent color. When an entry's `icon` is `null`, the shell renders a placeholder: the first letter of the mode name in a small rounded box. The `.html` Rendered|Source pair is **not a special case**: it is the ordinary mode list `["_render", "code"]` (PT-12) riding this same switcher — `_render` gets a shell-baked eye icon (sentinels have no folder to ship `icon.svg`); `code` gets its real folder icon. `.html` stays registry-exempt (CT-4).
- **PT-11** **Icons:** a template folder may ship `icon.svg` — **monochrome** (single fill; the shell tints it via CSS `mask-image` + `currentColor`, so only alpha matters), square viewBox (24×24 suggested), legible at 16px. `icon` in the stat entry is the abs path of the `icon.svg` sitting next to the *resolved* `template.html` (the user folder's icon when a user template resolved), or `null`. The shell loads it through the existing `/api/fs/raw` endpoint — no new routes. Every built-in folder ships one.
- **PT-12** **Sentinel modes:** a mode name starting with `_` is a **shell sentinel** — no template folder backs it; the shell knows what it means. Server resolution special-cases sentinels: the stat entry is emitted as `{"mode": "_<name>", "path": null, "icon": null}` without touching the filesystem. The `_` prefix matches the reserved-param convention (`_mode`, `_file`). The sentinel namespace is **shell-owned, not user-addressable**: a `_`-prefixed name in a registry list is invalid (dropped + `template_error`, CT-6). Only one sentinel exists today: **`_render`** — "render the file itself" — the default mode of the hardcoded `.html`/`.htm` list `["_render", "code"]` (users cannot remove `_render`; renderable HTML stays the core semantic, §4). Shell handling: `_render` → iframe src `/render?path=<the file itself>` (no `_file`), shell-baked eye icon; unknown sentinel entries (path `null`, mode not recognized) are filtered out defensively. Non-sentinel entries in the same list (e.g. `code`) work exactly like any template mode. Future html modes are added to the server-side list and flow through the framework normally.
- **PT-13** **Directory templates (D65):** a preview target may be a **directory** — a Zarr store is one logical dataset spread across many chunk files. A built-in **`DIR_TEMPLATES`** table maps a directory's basename extension to an ordered mode list, mirroring `TEMPLATES`'s shape and resolving through the **same** name-resolution (PT-6) so `stat.templates` entries come out identically shaped (PT-8). Dispatch keys on `templates`, not `is_dir`: a directory with a non-empty `templates` list **previews** (like a file) rather than listing — **unless** the shell-owned `?listing=1` query param is present, which forces the plain listing view (a directory with no `DIR_TEMPLATES` match lists as before). Directory previews render a **"Browse contents"** header action that navigates to `?listing=1`; because embed mode hides the whole preview header, the same action also rides as an unobtrusive corner chip pinned over the iframe, revealed only in embed. `listing` never reaches a template: it only takes effect on a directory, and when set the shell renders the listing (no template iframe is mounted). Directory templates are **package-only** — the user registry (§16) is a per-file *suffix* match with no coherent meaning for a directory, so it does not apply. Only `.zarr → ["zarr"]` today.
- **PT-5** **User overrides:** DECIDED and specced as §16 (M7, extended by M8) — user template folders under `~/.fused-render/` bound to extensions by `~/.fused-render/registry.json`, replacing or extending the built-in mode list, using the exact same mechanism.

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
2. ~~Python env~~ — ~~RESOLVED: the env the server was launched from~~ — **re-resolved 2026-07-07: openfused-managed venvs + PEP 723 headers (PY-7, D56).**
3. ~~Streaming/partial results~~ — **RESOLVED: not required.**
4. **Editing** — is write support (rename/delete/save) wanted in v1 or strictly v2?
5. **Multiple entry functions per file** addressed by name (`runPython("f.py#chart")`) — needed, or keep `main`-only? *(2026-07-07 note: openfused calls the **last-registered** `@fused.udf` in a file — one effective entry point per file is the current engine constraint.)*
6. ~~Param change → auto re-run?~~ — **RESOLVED: manual — author wires `runPython` inside `params.onChange`. Declarative binding possible later, layered on top.**

---

## 12. macOS Distribution (DMG) — M3

Distribute as a DMG containing a menu-bar app; all UI stays in the browser.

- **DM-1** **DECIDED (v2, D33):** the `.app` is built by **py2app** from a framework-build python (Homebrew `python@3.12`, bootstrapped by the build script). py2app ships a real re-invokable interpreter in-bundle (`Contents/MacOS/python`) — `sys.executable` subprocess executor works unchanged — and its compiled stub gives proper LaunchServices/AppKit process identity (the earlier hand-rolled bash-shim caused flaky NSStatusItem behavior under Finder launches).
- **DM-2** **DECIDED:** user `runPython` code executes on the **bundled interpreter only**. The `[bundled]` extra ships preinstalled (numpy, pandas, requests, duckdb, polars, matplotlib, scipy, pillow, openpyxl, shapely, geopandas + core pyarrow). py2app note: these are force-copied via `packages` — the engine imports them only in child processes, so import tracing can't see them. Known gap: `mpl_toolkits` (3D axes) excluded (namespace-package vs py2app limitation). **Wheelhouse (D58):** the .app also ships an in-bundle wheelhouse (`Contents/Resources/wheels/`, the `[bundled]` list + pyarrow); scripts' PEP 723 dependency installs resolve **offline from it first** (`PIP_FIND_LINKS`/`UV_FIND_LINKS`), PyPI fallback when online — since D56 user code imports from openfused venvs, not the interpreter's site-packages, this is what preserves offline first-use.
- **DM-3** **DECIDED (v2, D34):** regular app — **Dock icon AND menu bar ✦** (Open in browser / Copy URL / Quit). No LSUIElement. Dock right-click → Quit is the discoverable lifecycle path.
- **DM-4** **DECIDED:** ad-hoc signed for now; future signing/notarization path = **Briefcase external-app mode** (D35), hook noted in build script.
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
- **M7 — Custom templates:** user template folders in `~/.fused-render/` + `registry.json` extension bindings, overriding built-ins (§16).
- **M8 — Template modes:** 1:n extension→template mapping — folder-per-template built-ins (renamed to public names), ordered mode lists (first = default), registry `list|string|null` grammar with the `"..."` splice, `_mode` shell param + icon-only mode switcher, stat `templates` array replacing `template`, html folded in as the hardcoded `["_render", "code"]` sentinel list (§7, §16 / PT-6..PT-12, CT-10..CT-11).
- **M9 — Annotation mode:** annotate toggle over any preview mode, element/selection-anchored comment threads stored in the URL (§17).
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

- **WF-1** New endpoint `GET /api/fs/events?path=A&path=B&…` — an **SSE** stream (`text/event-stream`). Watched paths arrive as **repeated `path` query params** (paths may contain commas; repetition avoids a delimiter).
- **WF-2** v1 implementation: async loop stats every watched path every **200 ms**; baseline mtimes captured at connect. When a path's mtime differs from the last seen value (or the file appears/disappears) emit one event: `data: {"path": "<abs path>", "mtime": <float|null>}` — `null` means deleted. No event replay: changes that happen while disconnected are missed by design (the client reloads on reconnect-relevant changes anyway).
- **WF-3** A comment line (`: keepalive`) every 15 s keeps intermediaries and buffers honest. The endpoint must be `async def` (a sync def would pin a threadpool thread per open view for the lifetime of the page).
- **WF-4** No filesystem-watcher dependency (watchdog/fsevents) in v1 — polling stat is cheap and dependency-free at local scale. A later upgrade to real FS events is internal to this endpoint; the client contract (SSE, same event shape) does not change.
- **WF-5** Read-only GET — no `X-Fused` guard, consistent with the other read endpoints (D36 covers only mutating/executing POSTs).

### 13.3 Auto-reload (runtime)

The reload logic lives **entirely in the injected runtime** — the shell needs no per-view watching, and every rendered page (view mode, embed mode, standalone `/render`) gets the behavior for free.

- **LR-1** Each rendered page watches the union of: **its own rendered file** (the `path` param of its `/render` URL), **`_file`** if present (templates watching their target), and **every Python file executed via `runPython` this page-life**.
- **LR-2** `POST /api/run` response gains a `resolved_py` field — the absolute resolved path of the executed file — so the runtime learns dependency paths authoritatively instead of re-implementing the server's relative-path resolution. Recorded for failed runs too (a broken py that gets fixed must still trigger reload).
- **LR-3** On any change event: debounce **300 ms** (coalesce bursts), then `location.reload()` on the iframe itself. Full reload is the honest re-execution — the runtime cannot replay what the page did with a python result. State survives because view state lives in URL params (D8/D20/D25).
- **LR-4** When the watch set grows (a new py runs), the runtime closes and reopens its `EventSource` with the full set. Resubscribe is debounced so a page firing several `runPython` calls on load reconnects once.
- **LR-5** Opt-out: `fused.autoReload(false)` disables watching/reloading for that page. The `code` template calls it — the editor must not reload out from under the cursor (its own autosave changes the mtime; external changes are the conflict lock's job). To make the opt-out race-free, the runtime starts watching on `DOMContentLoaded`, after inline page scripts have run.
- **LR-6** Deletion (`mtime: null`) reloads too — the resulting 404/error view is the truthful state.
- **LR-7** Reload works identically for standalone `/render?path=…` pages (runtime is the same code).

### 13.4 Listing refresh (shell)

- **LS-1** The directory listing view watches the directory path via the same endpoint; on change it re-fetches `/api/fs/list` and re-renders, preserving sort params.
- **LS-2** Known limitation, accepted: a directory's mtime changes on create/delete/rename of entries — not when a child file's content or size changes. Stale sizes in an open listing are fine.
- **LS-3** The shell closes the listing's `EventSource` when navigating away (to a preview or another directory).

## 14. Layout Mode — Split Panes (M5)

Goal: view several files/directories side by side in a resizable grid of panes, with the **entire state — pane arrangement, each pane's location, and all view params — captured in one bookmarkable URL**. Combined with bookmarks (D20) this makes a saved layout a one-click dashboard.

### 14.1 URL & route

- **LM-1** Route: `/view/_panel?...` and `/embed/_panel?...`. `_panel` is a **sentinel pathname**, not a real file: the shell's `route()` intercepts it (under both prefixes) before calling `stat`. Zero server changes (the server already serves the shell for any `/view/*` and `/embed/*`). The pane tree lives in the reserved `_layout` query param (LM-2).
- **LM-2** The pane tree lives in the reserved query param **`_layout`** (underscore prefix → already invisible to `fused.params`, PR-6). Codec (borrowed from the reference grid-viewer):
  - `,` separates panes in a **row** (side by side), `;` separates **columns** (stacked), `(…)` groups for nesting. Single pane = bare path.
  - Each pane segment is the pane's **fs path plus optional pane-local query** (`/data/a.parquet?_mode=source&sort=name`). Within a segment, the characters `, ; ( ) % ?` occurring *inside* path components or the query are percent-encoded so the codec's delimiters stay unambiguous.
  - **URL grammar (D51): the entire `_layout` value is parenthesized and emitted last** — `?city=sf&_layout=(/data/a.parquet?_mode=source&sort=name,/notes.md)`. The parens delimit scope both visually (inside = iframe-local, outside = global) and structurally: **`&` is literal inside them**, so segment queries read exactly as they appear. Every read of a shell query goes through the codec's `splitShellSearch` (balanced-paren scan; the runtime carries a small standalone duplicate) — plain `URLSearchParams` cannot parse a layout URL. Strict read, no lenient fallback: an unwrapped `_layout` value is treated as absent (the key is dropped on the next sync); an unbalanced span (paste-truncated URL — auto-linkers may eat the trailing `)`, accepted breakage) is invalid and falls back per LM-2's missing-layout rule. Params appearing *after* the `)` are ordinary globals — position is convention, the parens are the boundary.
  - Example: `?_layout=(/data/a.parquet,/data/b.parquet;/notes.md)` → a and b side by side on top, notes below.
- **LM-3** All **non-underscore params on the layout URL form one merged pool shared by every pane** (see LM-6). Same key = same value in all panes, by construction. Pane-local shell state (listing `sort`/`order`, `_mode`) stays on the pane's own embed URL and is captured per-pane inside the `_layout` segment (LM-2), not merged.

### 14.2 Panes

- **LM-4** A pane is an **`/embed/<path>` iframe** (D39): a full navigable chrome-free shell — panes can browse directories, open previews, use templates, all existing behavior for free.
- **LM-5** Pane bar (top of each pane): clickable **path crumbs** (segment click navigates that pane), then buttons: **split right**, **split down** (new pane duplicates the current pane's location), **maximize** (transient — fills the layout area, not encoded in the URL), **close**. Closing collapses single-child splits; closing the **last** pane exits layout mode by navigating to plain `/view/<that pane's path>`.
- **LM-6** Pane navigation syncs up: the layout view observes each pane's URL (iframe `load` + the pane window's `fused:urlchange`, LM-8) and re-encodes `_layout` on the shell URL via `history.replaceState` — refresh/bookmark always reproduce the current arrangement.

### 14.3 Params — merge & sync (runtime change)

- **LM-7** The injected runtime's param target becomes the **topmost same-origin ancestor window** (was: direct parent), stopping **below** any ancestor marked as a param boundary (`_fusedParamBoundary` — only tab mode sets one, TM-3). In normal view/embed mode this is the same window as before (parent = top), so behavior is unchanged; inside layout mode every rendered page in every pane reads/writes params **directly on the layout shell URL**. Merging and cross-pane sync are structural, not a synchronization mechanism.
- **LM-8** Change notification: the shell wraps **both** `history.replaceState` and `history.pushState` to dispatch `fused:urlchange` (today: only replaceState). The runtime listens for `fused:urlchange` on its target window and re-notifies `params.onChange` listeners — but only when the **visible (non-reserved) param snapshot actually changed** (snapshot diff). The diff guard prevents notification loops and duplicate fires (a `set()` would otherwise notify twice: once directly, once via the event; direct notify is removed in favor of the event path).
- **LM-9** Consequence, intended: two panes rendering pages that use the same param key (e.g. `city`) are automatically linked — either pane's `set()` updates the shared URL and fires `onChange` in both. Pages wanting pane-private state must namespace their keys themselves (documented, not enforced).

### 14.4 Entry & chrome

- **LM-10** Entry: a **"Split" button** in the breadcrumb's crumb-actions (next to ★ Bookmark). Click → navigate to `<prefix>/_panel?<current user params>&_layout=(<seg>,<seg>)` (D51 grammar) where `<seg>` is the current fs path + pane-local query — two side-by-side panes, both the current view with its params carried over (a single pane on entry looked like nothing happened).
- **LM-11** In layout mode the sidebar stays visible (bookmarks reachable, ★ button works on the layout URL — bookmarking a layout needs zero bookmark-layer changes, D20). Breadcrumb shows a static "Panel" label. The armed-bookmark "Update bookmark" flow (D38) works unchanged: pane/param drift rewrites the shell URL via replaceState → `fused:urlchange` → `syncUpdateButton`.
- **LM-12** Module: **`views/panel.js`** — tree codec, tree ops (split/close/collapse), pane DOM + bar, URL sync. Imports `router.js` only (one-way deps, ARCHITECTURE §6). `main.js` gains one sentinel branch; `shell.css` a `.layout-*` section; sidebar/bookmarks/api untouched.

## 15. Tab Mode — Tabbed Views (M6)

Goal: the same URL-is-state model as §14, but as **tabs instead of a grid**: one page visible at a time, a tab bar to switch. Primary use: a **bookmark folder rendered as one view** — click the folder, get its bookmarks as tabs, bookmark the result as a dashboard.

### 15.1 URL & route

- **TM-1** Route: `/view/_tab?...` and `/embed/_tab?...` — a sentinel pathname exactly like `_panel` (LM-1), intercepted by `route()` under both prefixes. Zero server changes.
- **TM-2** The tab list lives in the same reserved **`_layout`** param, as a **flat top-level `,` row** of the §14 codec — a tab segment is a fs path + optional segment-local query, same escaping (LM-2). Produced URLs are always a flat list; on parse, any nested structure (`;`, `()`) is defensively **flattened to its leaves in document order**, each leaf becoming a tab.
- **TM-3** Params are **tab-independent — no merged pool** (deliberate inversion of LM-3). The tab shell marks its window as a **param boundary** (`window._fusedParamBoundary = true`, set on render, cleared on teardown); the runtime's ancestor climb (LM-7) stops **below** a boundary-marked ancestor, so a page rendered inside a tab targets its own pane's `/embed/...` URL. Each tab's full query — user params included — is therefore captured **segment-local** inside `_layout` by the ordinary sync (TM-7); the tab URL's own top-level query carries no user params.
- **TM-4** A tab segment's path may itself be a sentinel (`_panel`, `_tab`): the iframe src is just `/embed/<segment path>` + segment query, so a panel layout nests inside a tab through the ordinary pipeline (D45 embed support), its `_layout` riding inside the segment query. A nested panel keeps its LM-7 merged-pool semantics **among its own panes** (the climb stops at the panel shell, just below the tab boundary) while staying isolated from every other tab.

### 15.2 Tabs

- **TM-5** A tab is an **`/embed/<path>` iframe**, mounted **lazily on first activation** and kept alive afterwards (`display:none` when inactive) — scroll/editor state survives switching, and hidden tabs keep receiving `fused:urlchange` (the runtime listens on the top window, LM-8), so param sync is live while hidden.
- **TM-6** Tab bar (top of the layout area): one button per tab — label = basename of the tab's **current** path (sentinel paths label as `Panel` / `Tabs`) — plus a close `×` per tab and a trailing `+` that opens a new tab at the configured start dir. Click activates. The **active tab index is NOT encoded in the URL**: refresh/bookmark restores the first tab (avoids "Update bookmark" churn on every switch).
- **TM-7** URL sync up, same machinery as LM-6: iframe `load` + tab-window `fused:urlchange` → read the tab's live location → re-encode `_layout` via guarded `replaceState`. Closing a tab removes its segment; closing the **last** tab exits to a plain view of its location (active prefix, like LM-5).

### 15.3 Entry — bookmark folders

- **TM-8** Clicking a bookmark **folder's name or row** opens the folder as a tab layout: each child bookmark's pathname becomes the segment path and its **entire saved query stays segment-local** (TM-3 — no hoisting, no cross-child key collisions; every bookmark keeps exactly its own params). A child that is itself a `_panel`/`_tab` bookmark just works (TM-4). Opening also **expands the folder** if it was collapsed (the sidebar should show what the tabs now show); the **folder glyph** keeps the plain collapse/expand toggle.
- **TM-9** A folder is not a bookmark: opening it arms nothing. ★ Bookmark on the tab view saves the composed URL as a normal bookmark; a tab layout opened *from* such a bookmark gets the full armed/update flow (D38) unchanged. Breadcrumb shows a static "Tabs" label; no breadcrumb entry button (folder-only entry).

### 15.4 Module

- **TM-10** The §14 codec (escape/parse/encode/segment helpers) moves to a shared **`views/layout-codec.js`**; `views/panel.js`, the new **`views/tabs.js`**, and `breadcrumb.js` import it. `tabs.js` owns the tab bar DOM, lazy iframes, and URL sync; `main.js` gains the `_tab` sentinel branch; `shell.css` a `.tabs-*` section; `sidebar.js` changes only the folder-row click wiring.

## 16. Custom Templates — User Overrides (M7)

Goal: users replace or add preview templates using the **exact same mechanism** as the built-ins (§7). A user template is an ordinary renderable-HTML page (plus optional sibling `.py` readers) that receives the target file as `_file` — nothing new is exposed; only the server's extension → template resolution gains a user-controlled layer. The resolution layer is server-only: the shell obeys whatever `templates` list the stat response carries (PT-8), and `/render` already renders any absolute path with the runtime injected.

### 16.1 Layout on disk

- **CT-1** A user template is a **self-contained folder** `~/.fused-render/<name>/` holding `template.html` plus any sibling files it needs (reader `.py` files, css, assets) and optionally `icon.svg` (PT-11) — identical in shape to a built-in folder (PT-3). `<name>` carries **no** binding-by-convention semantics (CT-7), but it is the template's public name: it resolves by the single rule of PT-6, so a user folder named like a built-in **shadows** it. Relative `fused.runPython("./reader.py")` works unchanged because the template renders from its real path (PT-3).
- **CT-2** Bindings live in **`~/.fused-render/registry.json`** — a flat JSON object mapping **dotted extension keys** to a template name, or to `null`:

```json
{
  ".parquet": "geo",
  ".geojson": "geo",
  ".tar.gz": "archive",
  ".png": null
}
```

  A name binds the extension to a single-mode list of that template, resolved by the PT-6 rule. **`null` disables** templating for that extension entirely: the file gets no template at all and falls through to the shell's metadata/raw-download fallback (§7.2).
- **CT-10** **Mode lists (M8):** a registry value may also be a **JSON list of template names** — the full ordered mode list for that extension, **replace semantics**, first = default (PT-7). The string form of CT-2 is exactly a single-mode list; existing registries keep working unchanged.
- **CT-11** **`"..."` splice token:** inside a list value, the entry `"..."` expands, in place, to **the built-in mode list for that extension** — users add modes without knowing built-in names, and future built-in additions flow in automatically. `.` is forbidden in folder names (CT-6), so `"..."` can never collide with a real name. Rules: names already listed explicitly are skipped when the splice expands (`["code", "..."]` promotes `code` to default without duplication); more than one `"..."` in a list = invalid entry (built-in fallback + `template_error`, CT-6); a splice on an extension with no built-ins expands to nothing (harmless).

```json
{
  ".parquet": ["geo-view", "..."],
  ".md": "my-markdown",
  ".csv": null
}
```

### 16.2 Resolution

- **CT-3** Matching is **longest-suffix, case-insensitive**: the registry key must be a suffix of the lowercased filename beginning at a dot (`report.TAR.GZ` matches `.tar.gz` before `.gz`). Dotted keys are what make compound extensions expressible — the built-in table stays single-extension (`splitext`). Precedence: **registry (longest matching key) > built-in table**. Any extension may be bound, including ones no built-in handles.
- **CT-4** `.html`/`.htm` stay exempt from the registry — renderable HTML is the product's core semantic (§4). Their mode list `["_render", "code"]` is **hardcoded server-side** (PT-12): users cannot rebind the extension or remove `_render`. Relatedly, `_`-prefixed names (the sentinel namespace, PT-12) are invalid anywhere in a registry list — dropped per CT-6 with `template_error`.
- **CT-5** The registry is read **per stat/render resolution** (tiny local file — no restart, no cache invalidation problem). Missing `~/.fused-render/` or `registry.json` = clean no-op, built-in behavior; first run creates nothing.
- **CT-6** **Validation and fallback — per entry:** a folder name must be a single safe path segment (no `/`, no `..`, no `.`, not empty) — it is joined into a filesystem path, so a malformed name must not stat arbitrary locations (correctness guard, not auth — §9 stands). Within a mode list, an entry whose name cannot resolve (unsafe name, `template.html` missing in both PT-6 locations) is **dropped** from the list, and the stat response carries a **`template_error`** string naming the first problem, so a typo is visible (via stat / server log) instead of silently ignored. If the user's value resolves to nothing at all (unparseable JSON, empty result, double splice per CT-11), fall back to the **built-in list** for that extension.
- **CT-7** **No convention fallback:** a folder in `~/.fused-render/` without a registry entry is inert — a draft. Registration is only ever the registry line; deleting the line unregisters. One source of truth.

### 16.3 Pipeline & dev loop

- **CT-8** No new pipeline: stat carries the resolved user templates inside the ordinary `templates` list (PT-8); the preview iframe renders the selected mode via `/render` with `_file` exactly like a built-in (PT-2), and the switcher (PT-10) shows user modes indistinguishably from built-ins. M4 auto-reload (§13) covers template development for free — the rendered page watches its own html and every `runPython` file, so editing `template.html` or a reader live-reloads open previews. Registry edits apply on the next stat (navigate/refresh); open previews do not watch `registry.json`.
- **CT-9** **Authoring skill:** a repo skill `skills/fused-render-custom-templates/` covers folder layout, registry format, and registration workflow only; it **delegates all html/py authoring guidance to `skills/fused-render-authoring/`** (no duplicated instruction — one source for the runtime API and template patterns).

## 17. Annotation Mode — URL-Stored Comments (M9)

Goal: comment on rendered output. An annotate overlay on any preview: hovering highlights DOM elements, clicking attaches a comment thread to that element (or a free pin for page-level notes). Comments are **pure state stored in the URL** — no server persistence, no agent involvement. Behavior mirrors the flow/fused canvas-comments UX, adapted from canvas nodes to DOM elements.

### 17.1 Mode & entry

- **AN-1** Annotate is an **orthogonal toggle**, not a `_mode` value — `_mode` belongs to template-mode selection (PT-9, M8). State = reserved **`_annotate=1`** shell param (absent = off); bookmarkable, key deleted when toggled off. Annotate therefore overlays **whichever template mode is active** (rendered html, code editor, parquet table, …).
- **AN-2** The preview header gains a **comment-bubble toggle button** (inline SVG + tooltip) next to the mode switcher, same icon-button family. Shown for every templated preview — even single-mode files, where the mode switcher itself is hidden (PT-10 renders nothing for one entry); the fallback metadata view has none.
- **AN-3** The Annotate icon carries a **count badge** (number of open comments) whenever `_comments` is non-empty — visible whether or not annotate is on.
- **AN-4** With annotate on, the shell renders the active mode's **same iframe** plus `_annotate=1` on the iframe URL; the server injects the overlay script only then, and the overlay activates off the flag on its own window. The overlay lives entirely in the injected layer (same pattern as auto-reload §13.3), so view, embed, panel panes, tabs, and standalone `/render` pages all get identical behavior with zero per-surface wiring.

### 17.2 Data model & storage

- **AN-5** Comments live in the reserved **`_comments`** shell query param: a URL-encoded JSON array of thread objects (flow's schema minus agent fields — single-user, no author):

```json
[{
  "id": "<uuid>",
  "content": "root message",
  "replies": [{ "id": "<uuid>", "content": "…", "createdAt": 0 }],
  "status": "open",
  "createdAt": 0, "updatedAt": 0, "resolvedAt": 0,
  "anchorId": "chart-1",
  "anchorPath": "div:nth-of-type(2)>p:nth-of-type(1)",
  "x": 0, "y": 0
}]
```

- **AN-6** **Anchor forms, mutually exclusive, precedence `anchorId` > `anchorPath` > `x`/`y`:** `anchorId` = the clicked element's `id` attribute (used when present); `anchorPath` = structural path of `tag:nth-of-type(n)` segments from `body` (id-less elements); `x`/`y` = document coordinates for a free/page-level pin (click on empty body area). The source file is **never mutated** — no id injection.
- **AN-7** **Budget:** ~6 KB soft cap on the serialized param. On overflow, drop oldest **resolved** threads first; open threads are never dropped. If all remaining threads are open and the cap is still exceeded, the new write is rejected with a visible message in the overlay. (URL is the *only* store, so dropping = deleting — hence resolved-only.)
- **AN-8** The `_` prefix makes `_comments` invisible to `fused.params` (PR-6) and **segment-local inside `_layout`** (LM-2) — per-pane comments in panel/tab mode work with zero codec changes.
- **AN-9** The runtime writes `_comments` through its **internal** shell-URL replaceState channel (+ `fused:urlchange` dispatch). The public `fused.params.set` guard (PR-6) still rejects `_` keys from page code — user HTML cannot forge or clobber comments. **Target window = the direct parent shell** (the window whose preview iframe this is), NOT the runtime's LM-7 topmost-ancestor climb: `_comments` is pane-local shell state like `_mode` (LM-3), so in panel/tab mode it must land on the pane's own embed URL, where the panel's ordinary sync captures it segment-local into `_layout` (AN-8). In plain view/embed mode parent === top, so behavior is identical; standalone `/render` pages target themselves.

### 17.3 Interaction

- **AN-10** Entering annotate mode: crosshair cursor; hovering **outlines** the hovered element. Click an element → inline draft popover, auto-focused. Enter submits, Shift+Enter newlines, Escape cancels the draft. Click on empty body area → free pin at document coordinates.
- **AN-11** Pins render at the anchor's top-right corner, positioned in document coordinates (they scroll with content). Glyph = reply count; ✓ when resolved.
- **AN-12** Click a pin → **thread popover**: root + chronological replies with relative times, reply input (hidden when resolved), inline edit per message, Resolve/Reopen and Delete in the footer. Click-outside closes the popover. Escape closes popover first, then exits annotate mode.
- **AN-13** Pins and overlay are visible **only in annotate mode** — Rendered stays clean. The badge (AN-3) is the only cross-mode signal.
- **AN-14** **Detached anchors** (an `anchorPath` that no longer resolves, or a vanished `anchorId` after the file was edited): the pin docks into a small tray at the viewport corner listing the thread; it re-attaches automatically on a later render where the anchor resolves again. Comments are never silently lost.

### 17.4 Module

- **AN-15** New **`fused_render/static/annotate.js`**, injected alongside the runtime only when `_annotate=1` (normal pages pay zero cost). `views/preview.js`: icon toggle group + third mode plumbing. `shell.css`: icons, badge, outline/pin/popover styles. Server: the conditional injection line only.

### 17.5 Selection anchors — code editor (AN-16…AN-23)

Element anchoring makes no sense inside a text editor. On editor surfaces, annotate mode anchors comments to **text selections** instead (Cursor-style: click-drag a range, a draft pops up at the selection).

- **AN-16** **Surfaces:** `code_template.html` (every extension it serves). Rendered markdown/text/other templates keep element anchors (§17.2); selection-anchoring for rendered HTML text is a follow-up. The mechanism is a generic adapter (AN-17), not editor-specific code in annotate.js.
- **AN-17** **Adapter API:** when active, annotate.js exposes `window.__fusedAnnotate.registerAdapter(adapter)`. Registering disables the element-mode UI (hover highlight, pointer-sequence suppression, click-capture, element pins and anchor resolution) while keeping the core: data layer + URL channel + budget (AN-5/7/9), draft and thread popovers, tray, toast. Core hands the adapter `{ getComments(), onChange(cb), openDraftAt(clientX, clientY, anchorFields), openThread(id, clientX, clientY), setDetached(ids) }`; the adapter owns anchor rendering and resolution and calls back into the core for all UI/data operations.
- **AN-18** **Anchor form (new, AN-6 extended):** `selFrom`/`selTo` = `{line, ch}` (1-based line, 0-based column) plus `quote` = the selected text, capped at 120 chars. Mutually exclusive with the other forms; precedence `sel > anchorId > anchorPath > x/y`.
- **AN-19** **Interaction:** in annotate mode the editor is **read-only** (annotate is a review mode; keeps coordinates stable and sidesteps autosave interplay). Completing a non-empty selection (mouse release or keyboard) pops the draft near the selection end; Enter saves, Escape cancels; collapsing the selection cancels the pending draft.
- **AN-20** **Rendering:** CM6 range decorations tint each comment's range (muted variant when resolved) — virtual scrolling handled natively by CM. Clicking inside a decorated range opens the thread popover at the click point.
- **AN-21** **Re-resolution:** on load, if the doc slice at the stored range still matches `quote` → attached. Else search the doc for `quote` (first match) → re-anchor in memory (URL rewritten only on the next actual write). No match → detached tray (AN-14 behavior).
- **AN-22** Edits made outside annotate mode may shift ranges; comments re-resolve by quote on the next annotate session. Accepted drift — the quote is the truth, the line/ch pair is a hint.
- **AN-23** The vendored CM bundle (`scripts/vendor-codemirror/entry.js`) gains `Decoration`, `StateField`, `StateEffect`, `RangeSet` exports; rebuilt via `build.sh` (Node 22).
