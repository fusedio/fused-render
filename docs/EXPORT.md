# Exporting a page for hosted serving

fused-render is local-only: the server binds `127.0.0.1` and hosts nothing (SPEC
§1). Exporting does not change that. It is a **local `POST /api/export` call on
the already-running server** that packs a renderable page and its dependencies
into a portable *bundle* directory. A separate hosting layer — the `fused`
wheel's `build_html_artifact` — turns that bundle into a served app. Export
touches no network — it only writes files to a local directory.

```
curl -X POST http://127.0.0.1:1777/api/export \
  -H 'Content-Type: application/json' -H 'X-Fused: 1' \
  -d '{"page": "/abs/path/to/page.html", "out": "/abs/path/to/bundle"}'
```

You rarely need to call this yourself: the shell's **Deploy** button (SPEC §19,
`fused_render/deploy.py`) runs the same export into a temporary bundle and hands
it straight to `fused share create --public` on a hosted environment, returning
the minted URL. Manual export remains the path for driving the hosting layer
yourself.

`page` and `out` must both be absolute filesystem paths (same convention as
every other endpoint). Two optional fields tune the file set (see "Choosing which
files are bundled" below): `include` (extra page-relative files to bundle as
assets) and `exclude` (files to drop from the bundle) — both arrays of relative
paths, defaulting to empty. On success the response is
`{"out", "entrypoints": [...], "assets": [...], "warnings": [...]}` — the same
shape written into `manifest.json` below, plus the resolved `out` directory and
any advisory warnings. On a blocking export problem (see "Rules the exporter
enforces" below) the response is a `400` `{"error": "..."}`; the `X-Fused` header
is required on every call, like any other mutating endpoint (a missing/invalid
header is a `403`).

## What a bundle contains

```
bundle/
  page.html         # the page, copied verbatim
  manifest.json     # the contract the hosting layer reads
  code/<name>.py    # one file per fused.runPython() target
  assets/<key>      # one file per fused.rawUrl()/readFile() target
  resources/<key>   # one file per first-party module a bundled entrypoint imports
```

`manifest.json`:

```json
{
  "fused_render_bundle": 1,
  "page": "page.html",
  "entrypoints": [{ "path": "./sine.py", "name": "sine", "file": "code/sine.py" }],
  "assets": [{ "path": "./logo.png", "name": "logo.png", "file": "assets/logo.png" }],
  "resources": [{ "key": "helpers.py", "file": "resources/helpers.py" }]
}
```

- **entrypoints** map each `runPython` literal path to a served route name. When
  hosted, `fused.runPython("./sine.py", params)` becomes a `POST` to that route.
- **assets** map each `rawUrl`/`readFile` literal path to an asset key served by a
  read-only `_asset` route. That route honours **HTTP Range** requests (`206` +
  `Content-Range`, with `Accept-Ranges: bytes` on a full `200`), so a browser client can
  stream a large bundled file — e.g. geotiff.js reading a Cloud-Optimized GeoTIFF
  byte-range by byte-range — directly from a hosted page, with no local range daemon.
- **resources** are sibling `.py` modules a bundled entrypoint `import`s, found by a
  static scan of the entrypoint sources (transitively). They are shipped so the served
  entrypoint's `import helpers` resolves, but — unlike an asset — a page never fetches
  them, so they are **not** web-served. Only absolute imports resolving to a `<name>.py`
  beside the page are bundled; stdlib/third-party imports and subpackages are left alone.
  A relative import (`from . import x`) is skipped — a hosted entrypoint runs without
  package context.

The hosting layer uses the manifest to wire the served page's runtime — which
literal path posts to which route — without re-parsing the HTML.

## The portable subset of `window.fused`

A hosted page has no local filesystem behind it, so only part of the local
runtime API is portable:

| API | Hosted? | Notes |
|---|---|---|
| `fused.runPython(pyPath, params, opts?)` | ✅ | `pyPath` is bundled and served as a route the page posts to. Default stale-request cancellation (keyed by `pyPath`) and the `opts.key`/`opts.signal` controls (SPEC RH-9) work identically on the hosted page. |
| `fused.rawUrl(path)` | ✅ | `path` is bundled as a read-only asset. |
| `fused.readFile(path)` | ✅ | same bundling as `rawUrl`. |
| `fused.params.*` | ✅ | pure client-side URL state — unchanged. |
| `fused.env` | ✅ | runtime identity — `"local"` in the fused-render app, `"hosted"` here. Branch on it to gate local-only paths when deployed. |
| `fused.writeFile(...)` | ❌ | a hosted artifact is immutable. |
| `fused.stat(...)` | ❌ | no filesystem to stat. |
| SSE live-reload | ❌ | the artifact does not change under the page. |

`fused.env` is the recommended way to tell the two environments apart: it is a
**positive** signal present in both runtimes (`"local"` vs `"hosted"`), not the
absence of a method — `writeFile`/`stat` exist in the hosted runtime too (they
throw), so sniffing for them misidentifies a hosted page as local.

## Rules the exporter enforces

Export **fails loudly** (nothing is written) when a page cannot be hosted
faithfully, rather than shipping a page whose data calls 404 at request time:

- **Literal `runPython` paths only.** A `runPython` path must be a quoted literal:
  a hosted entrypoint's served route name is derived from that literal, so a
  computed target (a variable, a template string) cannot be routed and is an error.
- **No unsupported API.** `writeFile`/`stat` in the page are errors.
- **In-bundle paths only.** Absolute paths and paths escaping the page directory
  (`..`) are rejected — a hosted page can only reach files inside its bundle.
- **Targets must exist.** A referenced `.py`/asset (or an `include` file) that
  isn't on disk is an error.

Some conditions are **warnings**, not errors — they don't block export:

- **Computed `rawUrl`/`readFile` paths.** The exporter can't discover the target from
  the HTML, but once you bundle it — via the page's bundle manifest (below) or an
  explicit `include` — the served `_asset` route looks the file up by its bundle key and
  the hosted runtime resolves the computed path to that key, so `fused.rawUrl("data/" +
  name)` resolves fine. (A call like that is a string *prefix* plus an expression, so it
  is treated as computed — it is **not** mis-bundled as a literal `data/` target.)
- **Excluding a referenced file.** Dropping a file the page literally references is
  honored, but the page's call to it will 404 when hosted.

Route names are derived from the `.py` filename stem (`sine.py` → `sine`),
prefixed with `run-` if they would collide with a reserved serve route (`data`,
`health`, …) and suffixed `-2`, `-3`, … on duplicate stems.

## Choosing which files are bundled

By default the bundle is exactly the auto-detected set — the page plus every file
reached by a literal `runPython`/`rawUrl`/`readFile` call. `include` and `exclude`
layer a user selection on top:

- **`include`** — extra page-relative files bundled as read-only assets, beyond the
  scan. Use it for files reached by a computed path, or data a bundled `.py` reads
  at runtime, which the HTML scan can't see. An included file that duplicates an
  auto-detected asset is bundled once.
- **`exclude`** — page-relative paths (or their bundle key) dropped from the final
  set. Dropping an auto-detected target warns (its call 404s when hosted); dropping
  a file you only added via `include` is silent.

The Deploy modal (SPEC §19) drives these from its editable "Will publish" list —
add files from the page's folder, add everything, remove a file, or reset to the
auto-detected default — and persists the selection on the deployment record so a
reopened modal reloads it. `/api/export` exposes the same two fields for driving a
bundle by hand.

### The page's own bundle manifest (checked in, reproducible)

`include`/`exclude` above are the per-deployment selection (kept on the deployment
record). To declare the bundle set **in the repo** — reviewable, reproducible, and
travelling inside the single HTML file — add one embedded manifest block to the page:

```html
<script type="application/fused-bundle">
{ "include": ["data/*.json", "boundaries/**/*.geojson"] }
</script>
```

- **`include`** takes page-relative **globs** (`*`, `?`, `**` for recursion) and/or
  literal paths — an entry with no `*`/`?` is a literal, so a real filename with brackets
  (e.g. a `file[1].json` browser download) is taken as-is, not a character class. Globs are
  expanded against the page dir at export time and each match runs
  the same safety checks as any asset (`..`/absolute/symlink escapes are rejected). A glob
  that matches nothing is a **warning**; a literal that isn't on disk is an **error**. The
  manifest set is folded in **beneath** any `/api/export` `include`.
- The block carries **no version** — the `type` attribute identifies it, and unknown keys
  are ignored, so new directives can be added later without breaking older exports. It is
  stripped from the HTML before the dependency scan, so its JSON body is never misread as a
  `fused.*` call.
- **`exclude` is not honored here** (it would publish the withheld file names in the served
  page source) — it is warned about; drop files via the Deploy modal / `/api/export`
  `exclude` instead.

This is the clean way to back a **computed** asset call: declare `"include": ["data/*.json"]`
and fetch with `fused.rawUrl("data/" + name)` — the glob bundles the files and the hosted
`_asset` route resolves the computed name by key, so there is no `RAW_URLS`-style table to
hand-maintain.

### Reading a bundled file / importing a module from a `runPython` entrypoint

The hosting layer materializes every bundled file at its **real page-relative path**
under the entrypoint's working directory — the runtime's cwd **and** `sys.path[0]`. So
from entrypoint Python your code reads and imports exactly as it did locally, with no
rewriting:

```python
import helpers                      # bundled automatically (a "resource") — resolves

def main():
    data = open("data.csv").read()  # <root>/data.csv — a bare relative open() works
    return helpers.process(data)
```

- **Data files** reached by `fused.rawUrl`/`readFile`, or added under "Include files"
  (below), land at their key, so `open("data.csv")` / `open("tiles/0.png")` resolve.
- **Modules** a bundled entrypoint imports are discovered and shipped automatically, so
  `import helpers` works. Only absolute imports of a sibling `<name>.py` are bundled; a
  relative import (`from . import x`) is not — a hosted entrypoint runs without package
  context.

Use a bare relative path — it is the form that matches both local and hosted. Do **not**
use `openfused.asset_path("data.csv")` here: that helper anchors under `<root>/assets/`
(the resource scheme the project/widget deploy path uses), whereas a hosted fused-render
page's files sit at the project root, so `asset_path` would point at a file that isn't
there. If you need an absolute path, anchor it yourself at the working directory, e.g.
`os.path.join(os.getcwd(), "data.csv")`.
