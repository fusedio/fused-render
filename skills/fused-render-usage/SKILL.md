---
name: fused-render-usage
description: Use when opening, running or showing a fused-render project, file or view (not authoring one) — launching the desktop app, URL modes, browsing.
---

# Using fused-render

Local file-explorer desktop app, single instance per user, serves on `127.0.0.1` — no accounts, no cloud. Browses directories, previews files, live-renders `.html` views that call local Python. Authoring → `fused-render-authoring`.

## Open a path via the app

macOS `open -a FusedRender <path>` · Linux `gtk-launch fused-render <path>` (or the AppImage binary) · Windows `FusedRender.exe <path>`. Directory → explorer there; file → preview; `.html` → rendered view.

## Open by URL

Reuse the running instance's `http://127.0.0.1:<port>`. Path rides after the prefix, leading slash dropped, segments URL-encoded:

- `/` → `/apps` hub; `/explorer` = file-explorer homepage.
- `/explorer/view/<path>` — full shell chrome (what the app opens).
- `/explorer/embed/<path>` — chrome-free; best for a single view or a screenshot.

Mode is fixed by prefix (no toggle without navigation); params sync the same in both. Old `/view/`/`/embed/` links redirect; write the `/explorer/` forms.

**Preview templates** (parquet/image/etc.): open the TARGET file's path; the shell resolves the template by extension and passes `_file`. `?_mode=<name>` picks a specific mode.

**URL params are the shareable state** — copy the URL to reproduce the exact view.

## Switch skills

Authoring/debugging views → `fused-render-authoring` · template registration → `fused-render-custom-templates` · AI → `fused-render-ai` · file index/search → `fused-render-index` · resident daemons → `fused-render-background-apps`.
