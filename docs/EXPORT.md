# Exporting a page for hosted serving

fused-render is local-only: the server binds `127.0.0.1` and hosts nothing (SPEC
§1). `fused-render export` does not change that. It is an offline **build step**
that packs a renderable page and its dependencies into a portable *bundle*
directory. A separate hosting layer — the `fused` wheel's `build_html_artifact`
— turns that bundle into a served app. Export opens no socket and touches no
network.

```
fused-render export path/to/page.html --out ./bundle
```

## What a bundle contains

```
bundle/
  page.html         # the page, copied verbatim
  manifest.json     # the contract the hosting layer reads
  code/<name>.py    # one file per fused.runPython() target
  assets/<key>      # one file per fused.rawUrl()/readFile() target
```

`manifest.json`:

```json
{
  "fused_render_bundle": 1,
  "page": "page.html",
  "entrypoints": [{ "path": "./sine.py", "name": "sine", "file": "code/sine.py" }],
  "assets": [{ "path": "./logo.png", "name": "logo.png", "file": "assets/logo.png" }]
}
```

- **entrypoints** map each `runPython` literal path to a served route name. When
  hosted, `fused.runPython("./sine.py", params)` becomes a `POST` to that route.
- **assets** map each `rawUrl`/`readFile` literal path to an asset key served by a
  read-only `_asset` route.

The hosting layer uses the manifest to wire the served page's runtime — which
literal path posts to which route — without re-parsing the HTML.

## The portable subset of `window.fused`

A hosted page has no local filesystem behind it, so only part of the local
runtime API is portable:

| API | Hosted? | Notes |
|---|---|---|
| `fused.runPython(pyPath, params)` | ✅ | `pyPath` is bundled and served as a route the page posts to. |
| `fused.rawUrl(path)` | ✅ | `path` is bundled as a read-only asset. |
| `fused.readFile(path)` | ✅ | same bundling as `rawUrl`. |
| `fused.params.*` | ✅ | pure client-side URL state — unchanged. |
| `fused.writeFile(...)` | ❌ | a hosted artifact is immutable. |
| `fused.stat(...)` | ❌ | no filesystem to stat. |
| SSE live-reload | ❌ | the artifact does not change under the page. |

## Rules the exporter enforces

Export **fails loudly** (nothing is written) when a page cannot be hosted
faithfully, rather than shipping a page whose data calls 404 at request time:

- **String-literal paths only.** Every `runPython`/`rawUrl`/`readFile` path must be
  a quoted literal. A computed path (a variable, a template string) cannot be
  resolved at build time and is an error.
- **No unsupported API.** `writeFile`/`stat` in the page are errors.
- **In-bundle paths only.** Absolute paths and paths escaping the page directory
  (`..`) are rejected — a hosted page can only reach files inside its bundle.
- **Targets must exist.** A referenced `.py`/asset that isn't on disk is an error.

Route names are derived from the `.py` filename stem (`sine.py` → `sine`),
prefixed with `run-` if they would collide with a reserved serve route (`data`,
`health`, …) and suffixed `-2`, `-3`, … on duplicate stems.
