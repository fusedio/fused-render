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

- **PY-6** **DECIDED (v1):** execution is a **fresh subprocess per call** — always-fresh code, zero stale state, trivial timeout/kill; a crash or `sys.exit` cannot take down the server. Cost: interpreter + import time on every call. A warm worker pool is the designated v2 upgrade if interactivity demands it (API unchanged).
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

Built-in renderable-HTML files that ship **inside the application code**, one per supported format. They are ordinary renderable HTML — same runtime, same `runPython`, same params — proving the primitive is sufficient.

### 7.1 Dispatch

- **PT-1** **DECIDED: the registry is server-side** — single source of truth. Extension → template path mapping lives in the server; `GET /api/fs/stat` response includes `template: <abs path>|null`, and the shell simply obeys.
- **PT-2** When the user opens `data/trips.parquet`, the shell renders the returned template in the preview iframe and passes the target file as `_file=<path>` **on the iframe's own URL** (not the shell URL — its pathname already names the file, so no duplication like `/view/x.parquet?_file=/x.parquet`). Reserved `_` params are readable by the template, not settable by page code.
- **PT-3** Templates are html + py pairs living side by side in a real directory inside the package (`fused_render/templates/`), so plain **relative** `runPython` paths work unchanged — no virtual-path mechanism needed:

```js
const page = await fused.runPython("./parquet_reader.py",
                                   { file: fused.params.get("_file"),
                                     offset: "0", limit: "500" });
```

- **PT-4** Template UI state (current page, selected columns, sort) uses normal params → survives refresh, e.g. `?_file=…&offset=500&sort=fare`.

### 7.2 Template set — **M1 ships parquet, image, and text templates**; rest are follow-ups

**Shell dispatch is exactly three-way: template > html > fallback.** No file-type special-casing in the shell — image and text handling are templates like any other.

| Extension(s) | Template | M1? | Notes |
|---|---|---|---|
| `.parquet` | parquet_template.html | **M1** | paged table via pyarrow, row count |
| `.png .jpg .gif .webp .svg` | image_template.html | **M1** | `<img>` via raw endpoint |
| text/code (`.txt .py .js .ts .json .md .csv .log .yaml .toml …`) | text_template.html | **M1** | fetches raw, `<pre>`; syntax highlight later |
| `.csv`, `.tsv` | csv_template.html | later | paged table, delimiter sniffing (M1: text template) |
| `.md` | markdown_template.html | later | rendered markdown (M1: text template) |
| `.mp4 .mov .mp3 .wav` | media_template.html | later | raw endpoint w/ Range |
| `.pdf` | pdf_template.html | later | browser-native embed |
| `.html` | — | M1 | rendered live (§4); "Source" toggle shows text |
| unknown | shell fallback | M1 | metadata + raw/download link (built into shell, not a template) |

- **PT-5** **User overrides (v1.5):** a user templates directory (e.g. `~/.fused-render/templates/`) checked before built-ins, so users can replace or add format handlers using the exact same mechanism.

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
- **Follow-ups (unordered):** remaining preview templates (csv/json/markdown/media/pdf/syntax-highlighted code); user template overrides (`~/.fused-render/templates/` checked first); warm worker pool; DataFrame/Arrow returns; security layer (token, origin checks, sandboxed bridge); exec console; search/sort/tree/keyboard nav; caching; user template overrides; editing.

## 13. Live Editing — Autosave & Auto-Reload (M4)

Goal: a live-preview loop. Edit a file (in our editor or externally) → it saves itself → every open view of it reacts. Combined with embed mode (D39) this gives "source in one tab, rendered output in another, updates as you type".

### 13.1 Autosave (code editor)

Applies to `code_template.html`, the only editable surface (D37).

- **AS-1** The editor autosaves **1 s after the last edit** (debounced). Manual Save / Cmd+S remain and save immediately, cancelling any pending autosave timer.
- **AS-2** Autosave uses the same optimistic lock as manual save (`expected_mtime`). On 409 the existing conflict banner shows and **autosave suspends** until the user resolves via Reload or Overwrite. Autosave must never auto-overwrite a conflict — that would reduce the lock to decoration.
- **AS-3** Status text is the save lifecycle: `Modified → Saving… → Saved`. A non-conflict save failure shows the error; the next edit re-arms autosave (transient failures self-heal).
- **AS-4** Always-on. No toggle, no setting. Consequence accepted: half-typed code reaches disk and triggers reloads of watching views (that is the point of a live-preview loop; the D17 traceback overlay makes broken intermediate states self-explanatory).
- **AS-5** The `beforeunload` dirty guard stays — it covers the sub-second window between last keystroke and autosave completion.

### 13.2 Change feed (server)

- **WF-1** New endpoint `GET /api/fs/events?path=A&path=B&…` — an **SSE** stream (`text/event-stream`). Watched paths arrive as **repeated `path` query params** (paths may contain commas; repetition avoids a delimiter).
- **WF-2** v1 implementation: async loop stats every watched path every **500 ms**; baseline mtimes captured at connect. When a path's mtime differs from the last seen value (or the file appears/disappears) emit one event: `data: {"path": "<abs path>", "mtime": <float|null>}` — `null` means deleted. No event replay: changes that happen while disconnected are missed by design (the client reloads on reconnect-relevant changes anyway).
- **WF-3** A comment line (`: keepalive`) every 15 s keeps intermediaries and buffers honest. The endpoint must be `async def` (a sync def would pin a threadpool thread per open view for the lifetime of the page).
- **WF-4** No filesystem-watcher dependency (watchdog/fsevents) in v1 — polling stat is cheap and dependency-free at local scale. A later upgrade to real FS events is internal to this endpoint; the client contract (SSE, same event shape) does not change.
- **WF-5** Read-only GET — no `X-Fused` guard, consistent with the other read endpoints (D36 covers only mutating/executing POSTs).

### 13.3 Auto-reload (runtime)

The reload logic lives **entirely in the injected runtime** — the shell needs no per-view watching, and every rendered page (view mode, embed mode, standalone `/render`) gets the behavior for free.

- **LR-1** Each rendered page watches the union of: **its own rendered file** (the `path` param of its `/render` URL), **`_file`** if present (templates watching their target), and **every Python file executed via `runPython` this page-life**.
- **LR-2** `POST /api/run` response gains a `resolved_py` field — the absolute resolved path of the executed file — so the runtime learns dependency paths authoritatively instead of re-implementing the server's relative-path resolution. Recorded for failed runs too (a broken py that gets fixed must still trigger reload).
- **LR-3** On any change event: debounce **300 ms** (coalesce bursts), then `location.reload()` on the iframe itself. Full reload is the honest re-execution — the runtime cannot replay what the page did with a python result. State survives because view state lives in URL params (D8/D20/D25).
- **LR-4** When the watch set grows (a new py runs), the runtime closes and reopens its `EventSource` with the full set. Resubscribe is debounced so a page firing several `runPython` calls on load reconnects once.
- **LR-5** Opt-out: `fused.autoReload(false)` disables watching/reloading for that page. `code_template.html` calls it — the editor must not reload out from under the cursor (its own autosave changes the mtime; external changes are the conflict lock's job). To make the opt-out race-free, the runtime starts watching on `DOMContentLoaded`, after inline page scripts have run.
- **LR-6** Deletion (`mtime: null`) reloads too — the resulting 404/error view is the truthful state.
- **LR-7** Reload works identically for standalone `/render?path=…` pages (runtime is the same code).

### 13.4 Listing refresh (shell)

- **LS-1** The directory listing view watches the directory path via the same endpoint; on change it re-fetches `/api/fs/list` and re-renders, preserving sort params.
- **LS-2** Known limitation, accepted: a directory's mtime changes on create/delete/rename of entries — not when a child file's content or size changes. Stale sizes in an open listing are fine.
- **LS-3** The shell closes the listing's `EventSource` when navigating away (to a preview or another directory).

## 14. Layout Mode — Split Panes (M5)

Goal: view several files/directories side by side in a resizable grid of panes, with the **entire state — pane arrangement, each pane's location, and all view params — captured in one bookmarkable URL**. Combined with bookmarks (D20) this makes a saved layout a one-click dashboard.

### 14.1 URL & route

- **LM-1** Route: `/view/_layout?...`. `_layout` is a **sentinel pathname**, not a real file: the shell's `route()` intercepts it before calling `stat`. Zero server changes (the server already serves the shell for any `/view/*`).
- **LM-2** The pane tree lives in the reserved query param **`_layout`** (underscore prefix → already invisible to `fused.params`, PR-6). Codec (borrowed from the reference grid-viewer):
  - `,` separates panes in a **row** (side by side), `;` separates **columns** (stacked), `(…)` groups for nesting. Single pane = bare path.
  - Each pane segment is the pane's **fs path plus optional pane-local query** (`/data/a.parquet?_mode=source&sort=name`). Within a segment, the characters `, ; ( ) % ?` occurring *inside* path components or the query are percent-encoded so the codec's delimiters stay unambiguous.
  - Example: `?_layout=/data/a.parquet,/data/b.parquet;/notes.md` → a and b side by side on top, notes below.
- **LM-3** All **non-underscore params on the layout URL form one merged pool shared by every pane** (see LM-6). Same key = same value in all panes, by construction. Pane-local shell state (listing `sort`/`order`, `_mode`) stays on the pane's own embed URL and is captured per-pane inside the `_layout` segment (LM-2), not merged.

### 14.2 Panes

- **LM-4** A pane is an **`/embed/<path>` iframe** (D39): a full navigable chrome-free shell — panes can browse directories, open previews, use templates, all existing behavior for free.
- **LM-5** Pane bar (top of each pane): clickable **path crumbs** (segment click navigates that pane), then buttons: **split right**, **split down** (new pane duplicates the current pane's location), **maximize** (transient — fills the layout area, not encoded in the URL), **close**. Closing collapses single-child splits; closing the **last** pane exits layout mode by navigating to plain `/view/<that pane's path>`.
- **LM-6** Pane navigation syncs up: the layout view observes each pane's URL (iframe `load` + the pane window's `fused:urlchange`, LM-8) and re-encodes `_layout` on the shell URL via `history.replaceState` — refresh/bookmark always reproduce the current arrangement.

### 14.3 Params — merge & sync (runtime change)

- **LM-7** The injected runtime's param target becomes the **topmost same-origin ancestor window** (was: direct parent). In normal view/embed mode this is the same window as before (parent = top), so behavior is unchanged; inside layout mode every rendered page in every pane reads/writes params **directly on the layout shell URL**. Merging and cross-pane sync are structural, not a synchronization mechanism.
- **LM-8** Change notification: the shell wraps **both** `history.replaceState` and `history.pushState` to dispatch `fused:urlchange` (today: only replaceState). The runtime listens for `fused:urlchange` on its target window and re-notifies `params.onChange` listeners — but only when the **visible (non-reserved) param snapshot actually changed** (snapshot diff). The diff guard prevents notification loops and duplicate fires (a `set()` would otherwise notify twice: once directly, once via the event; direct notify is removed in favor of the event path).
- **LM-9** Consequence, intended: two panes rendering pages that use the same param key (e.g. `city`) are automatically linked — either pane's `set()` updates the shared URL and fires `onChange` in both. Pages wanting pane-private state must namespace their keys themselves (documented, not enforced).

### 14.4 Entry & chrome

- **LM-10** Entry: a **"Split" button** in the breadcrumb's crumb-actions (next to ★ Bookmark). Click → navigate to `/view/_layout?_layout=<current fs path + pane-local query>&<current user params>` — the current view becomes the first pane with its params carried over.
- **LM-11** In layout mode the sidebar stays visible (bookmarks reachable, ★ button works on the layout URL — bookmarking a layout needs zero bookmark-layer changes, D20). Breadcrumb shows a static "Layout" label. The armed-bookmark "Update bookmark" flow (D38) works unchanged: pane/param drift rewrites the shell URL via replaceState → `fused:urlchange` → `syncUpdateButton`.
- **LM-12** Module: **`views/layout.js`** — tree codec, tree ops (split/close/collapse), pane DOM + bar, URL sync. Imports `router.js` only (one-way deps, ARCHITECTURE §6). `main.js` gains one sentinel branch; `shell.css` a `.layout-*` section; sidebar/bookmarks/api untouched.
