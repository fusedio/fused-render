---
name: fused-render-authoring
description: Use when writing or debugging fused-render .html view or .py data file — runPython, params, preview mode, blank views, traceback overlays.
---

# Authoring fused-render views

View = sibling pair: `.html` (UI) + `.py` (data). Explorer renders html in iframe, injects `window.fused` (never `<script src>` runtime), page calls local Python via `fused.runPython`. UI state lives in URL params → refresh-proof, bookmarkable. Plain HTML/CSS/JS. No framework, no build step.

## App markers (entry page `<head>`, first 4 KiB)

```html
<meta name="fused-app" />
<meta name="fused-api-version" content="N" />
```

- `fused-app` = ONLY thing making folder an app. No marker → never in /apps, never registered.
- `fused-api-version`: copy N from `fused_render/app_starter/index.html`. Missing tag = version 0. Migration → `fused-render-api-migration`.
- Optional `icon.svg` beside entry page = sidebar glyph + favicon → `fused-render-app-icon`.

## Python side: `main()`

One plain fn `main(**params)`. Rules:

- **Annotate params** — arrive as URL strings; annotations coerce (`limit: int`, `bool` accepts "true/1/yes/on"). Unannotated = raw string.
- **Default every param** unless truly required.
- **Return JSON-native only** (dict/list/str/int/float/bool/None). DataFrame → `df.to_dict("records")`; datetime/Decimal/numpy → cast.
- Relative paths resolve beside `.py`.
- **Fresh subprocess per call.** No globals survive. Import cost paid every call (pandas ≈ 1 s). Killed at **60 s** (`DEFAULT_TIMEOUT`, `fused_render/executor.py`), no override. Longer work → `fused-render-jobs`.
- `print()` → browser console as `[python]`.

### Available Python libraries

No `pyproject.toml` in folder → app interpreter: stdlib plus exactly this bundled set (authoritative: `[bundled]` extra in repo `pyproject.toml`). Prefer it — zero install.

- **Data:** `numpy` `pandas` `pyarrow` `duckdb` `openpyxl` `msgpack`
- **Images:** `pillow`
- **Documents:** `python-pptx` `fpdf2` (import name *fpdf*)
- **Network & cloud:** `requests` `httpx` `botocore` `google-auth`
- **Logs:** `drain3`

Anything else needs folder `pyproject.toml` (project root only; add `[tool.uv] package = false`). Facts:

- Dependency list **complete** — bundled set NOT unioned in. Import numpy → declare numpy.
- All-or-nothing per folder; only topmost `pyproject.toml` counts.
- First render installs (loader, auto-retry). Non-PyPI sources / custom indexes → consent prompt. No-wheel deps stop with "install anyway". Commit `uv.lock`.
- Never run `uv sync` by hand — skips readiness marker + consent (`fused_render/projectenv.py`).
- `# /// script` headers IGNORED. Delete; move deps into `pyproject.toml`.
- Version question? Probe live env: `POST /api/run` with throwaway `main()` returning `importlib.metadata.version(...)`. Don't guess.

## App state: `.fused/`

Auto-created at app root. Convention, no helper API — build paths off `os.path.dirname(os.path.abspath(__file__))`:

- `.fused/data/` — state app owns, cannot rebuild. NOT beside index.html, NOT `~/.myapp`, NOT tmp.
- `.fused/cache/` — rebuildable derived bytes. Deleting must cost only time. No auto-sweep — cap yourself.
- `.fused/meta.json` — `{version, app_dir, created_at}`. `app_dir` ≠ actual path → folder moved; distrust stored absolute paths.
- Machine-local: gitignored, excluded from export. Nothing required-on-fresh-machine here.

**Cache aggressively.** Fresh subprocess + 60 s kill → memoize fetches/parses/aggregations to `.fused/cache/`. Key = hash of all inputs **with version segment** (bump on shape change). Write atomic (`os.replace` temp file — calls overlap). Unreadable entry = miss. Cache expensive step, not `main()` wholesale. Bytes (PNG/parquet) → cache file, hand page `fused.rawUrl(path)`.

## HTML side: `window.fused`

| Call | Notes |
|---|---|
| `await fused.runPython(py, params, opts?)` | Runs `main(**params)`; path relative to html. Rejects with Error carrying `.type/.message/.traceback/.stdout`. **Stale-cancel by default**, keyed per pyPath: new call aborts prior in-flight one, superseded promise never settles. `opts.key` regroups; `opts.key: null` = full concurrency; `opts.signal` = own AbortSignal. |
| `fused.params.get/getAll/set/onChange` | Strings only — `set("n", 5)` THROWS, do `String(n)`. `set` = replaceState then `onChange`. `_`-prefixed keys are shell's: `set` throws, `get` undefined (`_file` readable exception). |
| `await fused.readFile(path)` | UTF-8 text. |
| `await fused.stat(path)` | `{path, name, is_dir, size, mtime, writable, remote, templates}` (+ `template_error`). Size-guard big reads; grab `mtime` before edits; check `remote`. |
| `await fused.writeFile(path, text, opts?)` | Atomic. `opts.expectedMtime` → rejects `.type==="conflict"` on stale disk; `opts.create` → rejects `.type==="exists"` (race-free create); readonly → `.type==="readonly"`. Resolves with fresh stat — keep its mtime. |
| `fused.rawUrl(path)` | Sync URL for raw bytes — img/video/embed/download. Also resolves relative sibling assets (pitfall below). |
| `fused.ai.*` | → `fused-render-ai`. |
| `fused.fileIndex.search/query` | Machine-wide file index — use instead of walking fs → `fused-render-index`. |
| `fused.capture.*` | Native screen/mic/screenshot → `fused-render-capture`. |
| `fused.trackJob(spec)` | Report long work to download manager; never rejects → `fused-render-jobs`. |
| `fused.daemon.*` | Folder's warm worker / resident daemon → `fused-render-background-apps`. |
| `fused.env` | `"local"` vs `"hosted"` (exported). |
| `fused.autoReload(false)` | Kill reload-on-file-change (in-page editors). |

- Uncaught `runPython` rejection → red traceback overlay (good default). Catch for custom UI.
- Filesystem ONLY via these helpers — never fetch `/api/fs/*` yourself (writes rejected, unstable contract).
- Page served at `/render?path=...`, NOT own dir: bare `<script src="./app.js">` 404s **silently** (no overlay, page inert). Use `fused.rawUrl("app.js")`. Blank dead view → check console for 404 first.
- Editing pattern (stat → readFile → writeFile with expectedMtime, handle conflict/readonly): see built-in code editor template.

## Canonical wiring

Params ARE state. Controls write params; `onChange` = single re-render path; `draw()` reads params (never control) → refresh/bookmark reproduces view. Values to `runPython` stay strings — annotations coerce. Worked example: `fused_render/templates/fusedapp/template.html`.

## Preview mode (`_preview=1`)

Shell renders pages nobody opened: /apps cards, listing peeks, hovers — stamped `_preview=1` (+`_nofocus=1`), many boot at once. Ungated boot = N cold starts on home page.

- Read flag AND climb parent frames (inherited; only ancestor URL may carry it). Copy `isPreview()` from `fused_render/templates/fusedapp/template.html`.
- Under preview: cheap static placeholder. NO runPython, network, model loads, daemon starts, capture, writeFile, trackJob, timers, websockets, state mutation, dependency installs.
- Forward `?_preview=1&_nofocus=1` to iframes you mount.
- Param sync stops at each embed shell boundary — nested embeds keep params independent (tabs don't share state).
- Preview may SHOW cached artifact, never compute one.
- `_noopen=1` ≠ preview — real interactive surface, just not counted as "open". Ignore in `isPreview()`.
- Runtime covers only `fused.daemon.*` rejection + URL writes; rest is yours to gate.

## Theming

Iframe = blank canvas; shell follows OS/pref light-dark. Quick answer: `data-fused-theme="shell"` on `<html>`, two `:root` token blocks against `data-theme`. Full rules → `fused-render-theming`.

## Preview templates

Same html, opened FOR target file: read-only `_file` param carries path. Reader `.py` only when Python adds value; text → `readFile`, media → `rawUrl`. Built-ins: `fused_render/templates/<name>/` (see `xlsx/`). Extension → mode list: `fused_render/templates/registry.json`. User overrides → `fused-render-custom-templates`.

## Testing

Real browser against running server (`fused-render --port 1777 --no-browser`):

- `/explorer/embed/<abs path, leading slash dropped, segments URL-encoded>` — chrome-free. **Default for testing.**
- `/explorer/view/<path>` — full shell chrome.
- Templates: open TARGET file's path; or template html directly with `?_file=<abs target>`.

Loop: render → interact → URL updates → hard refresh → identical view.

## Verifying: call log

Cannot run page JS from terminal. After user opens page:

```
fused-render calls --page <abs html> --since 15m   # --failed, --json, --follow
```

Read digest. Zero records + visible placeholder = preview-gated, fine. Zero records + blank page = JS died pre-boot (look for `page error` = window.onerror). `error` = Python raised (traceback + params in record). Way more calls than interactions = onChange/set render loop. High `stale` = normal for slider drags only. Raw store: `~/.fused-render/logs/<app>/*.calls.jsonl`.

## Pitfalls

- `params.set` non-string → throws. `_`-prefixed page param → throws/collides.
- Own `history.replaceState` → drops shell `_` keys, breaks pane.
- Ungated boot under `_preview` / not climbing ancestors / not forwarding to own iframes.
- Non-JSON return, unannotated numeric param, expecting state across calls.
- Plain `open(...,"w")` cache write; unversioned cache key; irreplaceable bytes in `cache/`.
- Import outside bundled set, no `pyproject.toml`.
- Slider + heavy import, no ~150 ms debounce → subprocess per tick.
- `writeFile` on existing file without `expectedMtime` = silent clobber; create-if-absent = `{create: true}`, not stat-then-write.
- `readFile` for media → use `rawUrl`.
- Walking fs for counts/sizes → `fused.fileIndex.query` (`fused-render-index`).
- `fused.ai` in exportable page → exporter rejects textually; env guard no help (`fused-render-ai`).
- Claiming "done" without `fused-render calls` — blank-JS and failing-Python look identical without log.
