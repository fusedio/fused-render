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
every other endpoint). On success the response is
`{"out", "entrypoints": [...], "assets": [...]}` — the same shape written into
`manifest.json` below, plus the resolved `out` directory. On a blocking export
problem (see "Rules the exporter enforces" below) the response is a `400`
`{"error": "..."}`; the `X-Fused` header is required on every call, like any
other mutating endpoint (a missing/invalid header is a `403`).

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
