---
name: fused-render-usage
description: How to run and use a fused-render project — open files and views in the FusedRender desktop app and browse the filesystem. Use this whenever the user wants to open, run, launch, start, show, or explore a fused-render project or its views/files; asks to "open this in fused-render", "run fused-render", "show me this view", "look at this file in the explorer"; or is orienting inside a fused-render repo without yet authoring code. For creating/editing/debugging an .html view or .py data file, use fused-render-authoring instead; for registering custom preview templates, use fused-render-custom-templates.
---

# Using a fused-render project

fused-render is a **local file-explorer desktop app** that runs entirely on `127.0.0.1` — no accounts, no cloud. It browses any directory in the browser, previews files, and live-renders `.html` views that call local Python. This skill covers **running and using** the app; it does not cover writing views (that's `fused-render-authoring`).

This skill assumes **the FusedRender desktop app is already running** — there's a single instance per user, serving on `127.0.0.1`. Everything below is a way to open something into that running instance.

## Opening a file, view, or directory

Hand the path to the app — it opens as a full-shell view (`/view/<path>`) in a browser tab, reusing the running instance:

| Platform | Open a path |
|---|---|
| macOS | `open -a FusedRender <path>` |
| Linux | `gtk-launch fused-render <path>`, right-click → *Open With → FusedRender*, or the AppImage binary `FusedRender-<ver>.AppImage <path>` |
| Windows | `FusedRender.exe <path>`, or right-click → *Open with → FusedRender* |

Opening a directory lands the explorer there; opening a file previews it; opening an `.html` view renders it.

## Opening by URL (view / embed / preview templates)

Everything the app shows is a URL, so any view is bookmarkable and shareable. The running app already has a tab open — reuse its `http://127.0.0.1:<port>` from the address bar and swap the path.

The filesystem path rides in the URL after a **mode prefix**, with its **leading slash dropped** and each segment URL-encoded (space → `%20`). `/Users/me/proj/dash.html` → `…/view/Users/me/proj/dash.html`.

| Path | Renders |
|---|---|
| `/` | The explorer at `start_dir` — click to browse. |
| `/view/<path>` | **Full-shell mode** — the page wrapped in explorer chrome (sidebar, breadcrumb, preview header). What the app opens when you hand it a path. |
| `/embed/<path>` | **Embed mode** — the page chrome-free (no sidebar/breadcrumb/header). Best for opening a specific view on its own or for a screenshot. |

View vs embed is a fixed page-load mode set by the prefix — it can't toggle without a full navigation. Both serve the same page and sync URL params the same way; embed just hides the chrome.

**Preview templates** (parquet/image/text viewers, etc.) open at the *target file's* path — `…/view/<abs path to the data file>` — and the shell resolves the template by extension, handing it the file via a read-only `_file` param. Point at the data file, not the template html. When a file has more than one mode, add `?_mode=<name>` to open a specific one (or use the switcher in the preview header).

## Params are the shareable state

View state (paging, sort, selection) lives in **URL params**, so any view is refresh-proof and bookmarkable — copy the URL to reproduce or share the exact state.

## Where things live in a project

- A **view** is usually a sibling pair: an `.html` page (UI) + a `.py` file (data it fetches via `fused.runPython`).
- Built-in preview templates live under `fused_render/templates/<name>/`; user-owned ones under `~/.fused-render/` (see `fused-render-custom-templates`).

## When to switch skills

- Creating, editing, or debugging an `.html` view or `.py` data file, or a blank/errored view → **`fused-render-authoring`**.
- Registering a custom preview template for a file extension → **`fused-render-custom-templates`**.
