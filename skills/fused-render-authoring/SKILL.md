---
name: fused-render-authoring
description: Use when creating, editing or debugging a fused-render .html view or .py data file — fused.runPython, params, readFile/writeFile, preview mode, blank views, traceback overlays.
---

# Authoring fused-render views

A view = sibling pair: `.html` (UI) + `.py` (data). The explorer renders the html in an iframe, injects `window.fused` (never `<script src>` a runtime), and the page calls local Python via `fused.runPython`. UI state lives in URL params → refresh-proof, bookmarkable. Plain HTML/CSS/JS, no framework, no build step.

## App markers (entry page `<head>`, first 4 KiB)

```html
<meta name="fused-app" />
<meta name="fused-api-version" content="N" />
```

- `fused-app` is the ONLY thing that makes a folder an app. No marker = never in /apps, never registered.
- `fused-api-version`: copy N from `fused_render/app_starter/index.html`. Missing tag = version 0. Migration → `fused-render-api-migration`.
- Optional `icon.svg` beside entry page = sidebar glyph + favicon → `fused-render-app-icon`.

## Python side: `main()`

One plain function `main(**params)`. Rules:

- **Annotate params** — they arrive as URL strings; annotations coerce (`limit: int`, `bool` accepts "true/1/yes/on"). Unannotated = raw string.
- **Default every param** unless truly required.
- **Return JSON-native only** (dict/list/str/int/float/bool/None). DataFrame → `df.to_dict("records")`; datetime/Decimal/numpy → cast.
- Relative paths resolve beside the `.py`.
- **Fresh subprocess per call.** No globals survive. Import cost repaid every call (pandas ≈ 1 s). Killed at **60 s** (`DEFAULT_TIMEOUT`, `fused_render/executor.py`) — no override. Longer work → `fused-render-jobs`.
- `print()` → browser console as `[python]`.

### Available Python libraries

No `pyproject.toml` in folder → app interpreter: stdlib plus exactly this bundled set (authoritative: `[bundled]` extra in repo `pyproject.toml`). Prefer it — zero install.

- **Data:** `numpy` `pandas` `pyarrow` `duckdb` `openpyxl` `msgpack`
- **Images:** `pillow`
- **Documents:** `python-pptx` `fpdf2` (import name *fpdf*)
- **Network & cloud:** `requests` `httpx` `botocore` `google-auth`
- **Logs:** `drain3`

Anything else needs a folder `pyproject.toml` (project root only; add `[tool.uv] package = false`). Facts:

- The dependency list is **complete** — bundled set is NOT unioned in. Import numpy → declare numpy.
- All-or-nothing per folder; only the topmost `pyproject.toml` counts.
- First render installs (loader, auto-retry). Non-PyPI sources / custom indexes → consent prompt. No-wheel deps stop with "install anyway". Commit `uv.lock`.
- Never run `uv sync` by hand — it skips the readiness marker and consent (`fused_render/projectenv.py`).
- `# /// script` headers are IGNORED. Delete them; move deps into `pyproject.toml`.
- Version question? Probe the live env via `POST /api/run` with a throwaway `main()` returning `importlib.metadata.version(...)` — don't guess.

## App state: `.fused/`

Created automatically at app root. Convention, no helper API — build paths yourself off `os.path.dirname(os.path.abspath(__file__))`:

- `.fused/data/` — state the app owns, cannot rebuild. NOT beside index.html, NOT `~/.myapp`, NOT tmp.
- `.fused/cache/` — rebuildable derived bytes. Deleting it must cost only time. No auto-sweep — cap it yourself.
- `.fused/meta.json` — `{version, app_dir, created_at}`. `app_dir` ≠ actual path → folder was moved; distrust absolute paths stored in state.
- Machine-local: gitignored, excluded from export. Nothing required-on-fresh-machine goes here.

**Cache aggressively.** Fresh subprocess + 60 s kill means: memoize fetches/parses/aggregations to `.fused/cache/`. Key = hash of all inputs **with a version segment** (bump on shape change). Write atomically (`os.replace` a temp file — calls overlap). Treat unreadable entry as miss. Cache the expensive step, not `main()` wholesale. Bytes (PNG/parquet) → cache file, hand page `fused.rawUrl(path)`.

## HTML side: `window.fused`

| Call | Notes |
|---|---|
| `await fused.runPython(py, params, opts?)` | Runs `main(**params)`; path relative to the html. Rejects with Error carrying `.type/.message/.traceback/.stdout`. **Stale-cancel by default**, keyed per pyPath: new call aborts prior in-flight one, superseded promise never settles. `opts.key` regroups; `opts.key: null` = full concurrency; `opts.signal` = your AbortSignal. |
| `fused.params.get/getAll/set/onChange` | Strings only — `set("n", 5)` THROWS, do `String(n)`. `set` uses replaceState then fires `onChange`. `_`-prefixed keys are the shell's: `set` throws, `get` returns undefined (`_file` readable exception). |
| `await fused.readFile(path)` | UTF-8 text. |
| `await fused.stat(path)` | `{path, name, is_dir, size, mtime, writable, remote, templates}` (+ `template_error`). Size-guard before big reads; capture `mtime` before edits; check `remote`. |
| `await fused.writeFile(path, text, opts?)` | Atomic. `opts.expectedMtime` → rejects `.type==="conflict"` on stale disk; `opts.create` → rejects `.type==="exists"` (race-free create); readonly → `.type==="readonly"`. Resolves with fresh stat — keep its mtime. |
| `fused.rawUrl(path)` | Sync URL for raw bytes — img/video/embed/download. Also resolves relative sibling assets (see pitfall below). |
| `fused.ai.*` | → `fused-render-ai`. |
| `fused.fileIndex.search/query` | Machine-wide file index — use instead of walking the fs → `fused-render-index`. |
| `fused.capture.*` | Native screen/mic/screenshot → `fused-render-capture`. |
| `fused.trackJob(spec)` | Report long work to download manager; never rejects → `fused-render-jobs`. |
| `fused.daemon.*` | Folder's warm worker / resident daemon → `fused-render-background-apps`. |
| `fused.env` | `"local"` vs `"hosted"` (exported). |
| `fused.autoReload(false)` | Disable reload-on-file-change (in-page editors). |

- Uncaught `runPython` rejection → red traceback overlay (good default). Catch for custom UI.
- Filesystem ONLY via these helpers — never fetch `/api/fs/*` yourself (writes rejected, unstable contract).
- Page is served at `/render?path=...`, NOT its own dir: bare `<script src="./app.js">` 404s **silently** (no overlay, page inert). Use `fused.rawUrl("app.js")`. Blank view doing nothing → check console for 404 first.
- Editing pattern (stat → readFile → writeFile with expectedMtime, handle conflict/readonly): see the built-in code editor template.

## Canonical wiring

Params ARE the state. Controls write params; `onChange` is the single re-render path; `draw()` reads params (never the control), so refresh/bookmark reproduces the view. Values passed to `runPython` stay strings — annotations coerce. Worked example: `fused_render/templates/fusedapp/template.html`.

## Preview mode (`_preview=1`)

Shell renders pages nobody opened: /apps cards, listing peeks, hovers — stamped `_preview=1` (+`_nofocus=1`), many boot at once. Ungated boot = N cold starts on the home page.

- Read the flag AND climb parent frames (it's inherited; only an ancestor URL may carry it). Copy `isPreview()` from `fused_render/templates/fusedapp/template.html`.
- Under preview: render cheap static placeholder. NO runPython, network, model loads, daemon starts, capture, writeFile, trackJob, timers, websockets, state mutation, dependency installs.
- Forward `?_preview=1&_nofocus=1` to iframes you mount yourself.
- A preview may SHOW a cached artifact, never compute one.
- `_noopen=1` ≠ preview — it's a real interactive surface that just shouldn't count as an "open". Ignore it in `isPreview()`.
- Runtime covers only `fused.daemon.*` rejection + URL writes; everything else is yours to gate.

## Theming

Iframe is a blank canvas, but the shell follows OS/pref light-dark. Quick answer: `data-fused-theme="shell"` on `<html>`, author two `:root` token blocks against `data-theme`. Full rules → `fused-render-theming`.

## Preview templates

Same html, opened FOR a target file: read-only `_file` param carries the path. Reader `.py` only when Python adds value; text → `readFile`, media → `rawUrl`. Built-ins: `fused_render/templates/<name>/` (see `xlsx/`). Extension → mode list: `fused_render/templates/registry.json`. User overrides → `fused-render-custom-templates`.

## Testing

Open in a real browser against the running server (`fused-render --port 1777 --no-browser`):

- `/explorer/embed/<abs path, leading slash dropped, segments URL-encoded>` — chrome-free. **Default for testing.**
- `/explorer/view/<path>` — full shell chrome.
- Templates: open the TARGET file's path; or template html directly with `?_file=<abs target>`.

Loop: render → interact → URL updates → hard refresh → identical view.

## Verifying: the call log

You cannot execute the page's JS from a terminal. After the user opens it:

```
fused-render calls --page <abs html> --since 15m   # --failed, --json, --follow
```

Read the digest. Zero records + visible placeholder = preview-gated, fine. Zero records + blank page = JS died pre-boot (look for `page error` = window.onerror). `error` = Python raised (traceback + params in record). Way more calls than interactions = onChange/set render loop. High `stale` = normal for slider drags only. Raw store: `~/.fused-render/logs/<app>/*.calls.jsonl`.

## Pitfalls

- `params.set` with non-string → throws. `_`-prefixed page param → throws/collides.
- Own `history.replaceState` → drops shell `_` keys, breaks the pane.
- Ungated boot under `_preview` / not climbing ancestors / not forwarding to own iframes.
- Non-JSON return, unannotated numeric param, expecting state across calls.
- Plain `open(...,"w")` cache write; unversioned cache key; irreplaceable bytes in `cache/`.
- Import outside bundled set with no `pyproject.toml`.
- Slider + heavy import without ~150 ms debounce = subprocess per tick.
- `writeFile` on existing file without `expectedMtime` = silent clobber; create-if-absent = `{create: true}`, not stat-then-write.
- `readFile` for media → use `rawUrl`.
- Walking the fs for counts/sizes → `fused.fileIndex.query` (`fused-render-index`).
- `fused.ai` in an exportable page → exporter rejects textually; env guard doesn't help (`fused-render-ai`).
- Claiming "done" without `fused-render calls` — blank-JS and failing-Python look identical without the log.
