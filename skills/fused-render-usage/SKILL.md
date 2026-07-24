---
name: fused-render-usage
description: How to run and use a fused-render project — open files and views by driving the FusedRender desktop app (which launches or reuses a single local instance) and browse the filesystem. Use this whenever the user wants to open, run, launch, start, show, or explore a fused-render project or its views/files; asks to "open this in fused-render", "run fused-render", "show me this view", "look at this file in the explorer"; or is orienting inside a fused-render repo without yet authoring code. For creating/editing/debugging an .html view or .py data file, use fused-render-authoring instead; for registering custom preview templates, use fused-render-custom-templates.
---

# Using a fused-render project

fused-render is a **local file-explorer desktop app** that runs entirely on `127.0.0.1` — no accounts, no cloud. It browses any directory in the browser, previews files, and live-renders `.html` views that call local Python. This skill covers **running and using** the app; it does not cover writing views (that's `fused-render-authoring`).

## Drive the desktop app — never a raw server

There is **one** FusedRender instance per user. Launching the app again does **not** start a second server: it elects a single instance (an `flock` on Linux, a named mutex on Windows, LaunchServices on macOS) and **forwards** your request to the instance already running — or starts one if none is. Launch-if-necessary-else-reuse is built into the app's own entrypoint; you never orchestrate it, probe for it, or reproduce it.

To open a file, a view, or a directory, **hand its path to the FusedRender app**. It opens that path as a full-shell view (`/view/<path>`) in a browser tab, reusing the running instance.

| Platform | Open a path (launch-or-reuse) | Open home |
|---|---|---|
| macOS | `open -a FusedRender <path>` | `open -a FusedRender` |
| Linux | `gtk-launch fused-render <path>`, the AppImage binary `FusedRender-<ver>.AppImage <path>`, or right-click → *Open With → FusedRender* | same without a path |
| Windows | `FusedRender.exe <path>`, or right-click → *Open with → FusedRender* | `FusedRender.exe` |

Under the hood the Linux/Windows launcher execs `python -I -m fused_render.supervisor <path>` (the AppImage's `AppRun`); the macOS `.app` does the same job natively. Invoke the app, not this module.

### Guardrail: internal entrypoint only — never raw OS commands

Driving the app through its own entrypoint is a **security and performance boundary**, not a style preference.

**Do NOT:**
- ❌ Start the server yourself — no `uvicorn`, no `fused-render serve`, no `python -m fused_render.cli`, no `scripts/dev.sh`.
- ❌ Kill / `pkill` / `taskkill` the app or its server, and do not `POST /api/desktop/shutdown` (a token-gated internal endpoint — the tray owns shutdown).
- ❌ Scan ports, read or write the pidfile, or otherwise hunt for or free a port.
- ❌ Hand-roll a second instance, or point a raw `webbrowser` / `xdg-open` / `start` at a server you spawned.

**DO:** open files and views by handing the path to the FusedRender app, and let the app own the server lifecycle.

**Why (security):** the app runs its Python server in a sandboxed child environment — a per-launch 256-bit instance token, a token-gated shutdown endpoint, and an isolated tools dir that wires the bundled `rclone` / `uv` / `duckdb`. A hand-spawned bare server bypasses all of that isolation, and killing processes by hand can corrupt the single-instance lock and leave `rclone` mounts dangling.

**Why (performance):** reusing the elected instance avoids duplicate servers fighting over ports and re-paying cold startup on every open — `rclone` remounts and the `uv` / `duckdb` / warm VFS caches survive across opens only in the one long-lived instance.

## How the app addresses pages (URLs)

The filesystem path rides in the URL after a mode prefix, with its **leading slash dropped** and each segment URL-encoded (space → `%20`). `/Users/me/proj/dash.html` → `.../view/Users/me/proj/dash.html`.

| Path | Renders |
|---|---|
| `/` | The explorer at `start_dir` — click to browse. |
| `/view/<path>` | **Full-shell mode** — the page wrapped in explorer chrome (sidebar, breadcrumb, preview header). **This is what the app opens when you hand it a path** — the natural desktop experience. |
| `/embed/<path>` | **Embed mode** — the page chrome-free (no sidebar/breadcrumb/header). Useful for a bare view or a screenshot. |

**Preview templates** (parquet/image/text viewers, etc.) open at the *target file's* path — `/view/<abs path to the data file>` — and the shell resolves the template by extension, handing it the file via a read-only `_file` param. You don't point at the template html; you point at the data file.

View state lives in **URL params**, so any view is refresh-proof and bookmarkable — copy the URL to share the exact state.

### Opening a specific URL (embed mode, preset params)

Handing the app a path always opens full-shell `/view/` at default params. To open **embed mode** or a **preset param state**, open that URL in the browser against the **already-running** app server — that is navigation *within* the running instance, not lifecycle management, so it still starts no new server. You need the instance's port:

- **macOS:** the port is persisted at `~/Library/Application Support/fused-render/server.port`.
- **Linux/Windows:** the port is ephemeral (OS-assigned) and is **not** persisted — prefer handing the app the path and toggling to embed / adjusting params from within the open view.

## Where things live in a project

- A **view** is usually a sibling pair: an `.html` page (UI) + a `.py` file (data it fetches via `fused.runPython`).
- Built-in preview templates live under `fused_render/templates/<name>/`; user-owned ones under `~/.fused-render/` (see `fused-render-custom-templates`).
- View state lives in **URL params**, so any view is refresh-proof and bookmarkable — copy the URL to share the exact state.

## When to switch skills

- Creating, editing, or debugging an `.html` view or `.py` data file, or a blank/errored view → **`fused-render-authoring`**.
- Registering a custom preview template for a file extension → **`fused-render-custom-templates`**.
