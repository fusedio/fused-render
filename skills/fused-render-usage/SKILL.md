---
name: fused-render-usage
description: Use when opening/running/showing fused-render project or view (not authoring) — launch app, URL modes, browsing.
---

# Using fused-render

Local file-explorer desktop app, single instance per user, serves on `127.0.0.1` — no accounts, no cloud. Browses dirs, previews files, live-renders `.html` views calling local Python (`.py` sibling convention → `fused-render-authoring`).

## Open path via app

macOS `open -a FusedRender <path>` · Linux `gtk-launch fused-render <path>` (or AppImage binary) · Windows `FusedRenderPy.exe <path>`. Dir → explorer; file → preview; `.html` → rendered view.

## Open by URL

Reuse running instance's `http://127.0.0.1:<port>`. Path after prefix, leading slash dropped, segments URL-encoded:

- `/` → `/home`; `/explorer` = file-explorer homepage.
- `/explorer/view/<path>` — full shell chrome (what app opens).
- `/explorer/embed/<path>` — chrome-free; single view or screenshot.

Mode fixed by prefix (no toggle without navigation); params sync same in both. Old `/view/`/`/embed/` links rewritten client-side; write `/explorer/` forms.

**Preview templates** (parquet/image/etc.): open TARGET file's path; shell resolves template by extension, passes `_file`. `?_mode=<name>` picks specific mode.

**URL params = shareable state** — copy URL, reproduce exact view.

## Switch skills

Authoring/debugging → `fused-render-authoring` · template registration → `fused-render-custom-templates` · AI → `fused-render-ai` · file index/search → `fused-render-index` · resident daemons → `fused-render-background-apps`.
