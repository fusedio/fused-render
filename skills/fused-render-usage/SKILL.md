---
name: fused-render-usage
description: How to run and use a fused-render project — start the local server, browse the filesystem, and open views/files in the browser. Use this whenever the user wants to open, run, launch, start, serve, or explore a fused-render project or its views/files; asks to "open this in fused-render", "run fused-render", "show me this view", "look at this file in the explorer"; or is orienting inside a fused-render repo without yet authoring code. For creating/editing/debugging an .html view or .py data file, use fused-render-authoring instead; for registering custom preview templates, use fused-render-custom-templates.
---

# Using a fused-render project

fused-render is a **local file explorer** that runs entirely on `127.0.0.1` — no accounts, no cloud. It browses any directory in the browser, previews files, and live-renders `.html` views that call local Python. This skill covers **running and using** an existing project; it does not cover writing views (that's `fused-render-authoring`).

## Running the server

```
fused-render                                    # opens a tab at http://127.0.0.1:1777/, starting in ~/Documents/Fused
fused-render --start-dir ~/data --port 9000     # different start dir + port
fused-render --port 1777 --no-browser           # don't steal focus (best when driving it yourself)
```

`--start-dir` only sets the initial location (default `~/Documents/Fused`, override with `$FUSED_RENDER_DIR`); the whole filesystem stays browsable from there. On first run the server seeds that Fused directory with example content and opens a landing/showcase page rather than a bare listing. When you launch it to open something for the user or to test a change, prefer `--no-browser` and open the specific URL yourself (see below) rather than landing them on that page.

### macOS desktop app

On macOS, fused-render also ships as a standalone **`FusedRender.app`** (packaged as `dist/FusedRender-<version>.dmg` via `bash scripts/build_dmg.sh`). It bundles Python and a prebuilt shell — no `pip install`, no Node — so a non-developer just double-clicks it to launch the same local server and browser tab. The `.app` also ships an offline wheelhouse (numpy, pandas, pyarrow, duckdb, polars, requests, matplotlib, scipy, pillow, openpyxl, shapely, geopandas, rasterio, zarr, pymupdf, and more — see the `[bundled]` extra in `pyproject.toml` for the full set), so views built on those packages work with no network. Everything below — URLs, modes, params — is identical whether the server was started by the CLI or the app.

Source checkout only: the React shell must be built once before the server starts (`cd frontend && npm install && npm run build`, Node 22), or use `scripts/dev.sh` for the watch+server dev loop. Wheels and the `.app` ship the shell prebuilt.

## Opening files and views by URL

The filesystem path rides in the URL after a mode prefix, with its **leading slash dropped** and each segment URL-encoded (space → `%20`). `/Users/me/proj/dash.html` → `.../embed/Users/me/proj/dash.html`.

| Path | Renders |
|---|---|
| `/` | The explorer at `start_dir` — click to browse. |
| `/embed/<path>` | **Embed mode** — the page chrome-free (no sidebar/breadcrumb/header). **Default for opening a specific view.** |
| `/view/<path>` | **Full-shell mode** — the same page wrapped in the explorer chrome (sidebar, breadcrumb, preview header). |

### Default to embed mode

**When you open a link to a specific view/file for the user or to show a change, use `/embed/` by default.** Embed shows only the view itself — the thing the user actually cares about — without the explorer chrome around it. Reach for `/view/` only when the user is *browsing* (wants the sidebar/breadcrumb to navigate) or explicitly asks to see the file inside the full explorer shell.

View vs embed is a fixed page-load mode set by the prefix — it cannot toggle without a full navigation. Both serve the same page, route identically, and sync URL params the same way; embed just hides the chrome.

**Preview templates** (parquet/image/text viewers, etc.) open at the *target file's* path — `/embed/<abs path to the data file>` — and the shell resolves the template by extension, handing it the file via a read-only `_file` param. You don't point at the template html; you point at the data file.

## Where things live in a project

- A **view** is usually a sibling pair: an `.html` page (UI) + a `.py` file (data it fetches via `fused.runPython`).
- Built-in preview templates live under `fused_render/templates/<name>/`; user-owned ones under `~/.fused-render/` (see `fused-render-custom-templates`).
- View state lives in **URL params**, so any view is refresh-proof and bookmarkable — copy the URL to share the exact state.

## When to switch skills

- Creating, editing, or debugging an `.html` view or `.py` data file, or a blank/errored view → **`fused-render-authoring`**.
- Registering a custom preview template for a file extension → **`fused-render-custom-templates`**.
