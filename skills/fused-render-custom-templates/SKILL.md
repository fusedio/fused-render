---
name: fused-render-custom-templates
description: Use when registering custom preview template for file extension, or overriding/reordering built-in template modes.
---

# Custom preview templates

Each extension resolves to ordered mode list (first = default; >1 shows switcher). This skill = registration only. Writing `template.html`/reader `.py` → `fused-render-authoring` (its "Preview templates" section IS a custom template). "Template" that's really long-running server → `fused-render-background-apps`.

## Layout

```
~/.fused-render/templates/
├── registry.json      extension → mode list
└── <name>/            folder name IS public name (registry ref, _mode value)
    ├── template.html  required entry point
    ├── reader.py      optional siblings, referenced relatively
    ├── condition.py   optional per-file gate
    ├── pyproject.toml optional: [tool.fused-render.template] stream = [".parquet"] — template reads `_file=s3://…` itself
    └── icon.svg       optional switcher icon (monochrome, currentColor, square, legible 16px)
```

Name resolution: user folder wins, else built-in — resolved from staged copy `~/.fused-render/.core-templates/<name>/` (packaged `fused_render/templates/` tree copied there at server startup; repo tree is source only). Naming after built-in shadows it deliberately (built-in names = every staged folder with template.html; also Templates → Library). Folder names: single segment, no dots, no leading `_`. Folder without registry entry = inert draft.

## registry.json

Flat object; same key grammar as built-in `fused_render/templates/registry.json`. Values: list of names (full ordered mode list — **replace semantics**, no merge; re-list built-in modes you keep), string (single-mode shorthand), or `null`/`[]` (disable templating for extension).

- Keys: dot-anchored suffix patterns, case-insensitive, most-specific wins (`.tar.gz` > `.gz`; literal > `*` at equal length). `*` = exactly one whole segment. Trailing `/` binds directory (`.zarr/`). Any extension allowed, `.html` included (rebind `["code", "_render"]` → source becomes default).
- `_`-prefixed names = shell sentinels; referenceable: `_render`, `_listing`. Other `_name` invalid → dropped, `template_error` set, rest of list works.
- Any registry key beats built-in table. Re-read every file open — no restart.

## condition.py

`def main(path) -> bool` beside template.html — show this template for this file? No file = always shown. Runs after registry resolution, evaluated in background (`GET /api/fs/conditions?path=…`), verdicts cached ~60 s; multiple gates for one extension run concurrently (cost = slowest gate, all still run). May read file, keep reads bounded (headers/footers). Broken gate = template dropped, reason in `error` on conditions response. Gated template never default while ungated one exists. Shows "conditional" badge in Templates → Library.

## Streaming from S3

`pyproject.toml` beside template.html, `[tool.fused-render.template] stream = [".parquet"]` = template opens bucket objects of those extensions WITHOUT download (duckdb: `.parquet`; geotiff: `.tif`, `.tiff`). Explorer passes `_file=s3://bucket/key` to it for an uncached object; every other mode bound to the file shows a download card in its pane and opens on the downloaded copy. No key = never sees `s3://` (download fallback is automatic). Template must take BOTH forms of `_file`: an `s3://` address (streamed) and a local path (an object already downloaded opens from its cached copy, like any local file). Read it itself: `fused.s3.sign(addr)` (JS) / `fused_s3.sign(addr)` (Python, `templates/shared`) → `{url, expires_at}` HTTPS URL, Range works. Keep address as identity (`fused.params`, cache keys); sign in the page and pass the URL to `runPython`/daemon (duckdb, geotiff), or let a long-lived process sign itself (`fused_s3`). Read only what the view needs — refuse/point to Download when a view would read most of a big file (duckdb: `STREAM_SCAN_BUDGET`; geotiff: zoom-in only without overviews); the explorer's "Streaming from S3" bar has the Download button. Not honoured for native templates. A conditional template that streams gets the signed `https://` URL as its gate's `main(path)` (read it or return False); other gated modes show only once the object is cached. Examples: `duckdb/` (DuckDB `httpfs` on the signed URL), `geotiff/` (GDAL `/vsicurl/` on the signed URL, in the tile daemon).

## Guardrail

Templates run on every open, server privileges, inside packaged app. Readers/conditions: no `subprocess`/`os.system`/system binaries — bundled libraries in-process only (list in `fused-render-authoring`; more → folder pyproject). Html: filesystem only via `window.fused` helpers, never raw `/api/fs/*` or external script/data hosts.

## Workflow

1. `mkdir -p ~/.fused-render/templates/<name>` (or Templates → Library → New).
2. Author per `fused-render-authoring` (`_file` param = target; UI state in normal params).
3. Iterate before registering: open `…/explorer/view/<abs template.html>?_file=<abs sample>` — drafts are plain fused pages. Saves live-reload.
4. Register: add extension line to registry.json (create with `{}` if absent).
5. Test: open file of that extension; switcher only when >1 mode; `?_mode=<name>` opens specific one.

## Troubleshooting

- One mode missing, rest fine → bad name dropped silently; check `template_error` on `fused.stat(path)`.
- Whole extension falls to built-ins → shape-level registry error (invalid JSON / wrong value type). `null`/`[]` not errors.
- Mode missing for SOME files → condition.py verdict; check `error` on `/api/fs/conditions`.
- Blank/erroring render → authoring problem, not registration → `fused-render-authoring`.
- Registry edits not applying to open preview → previews watch files, not registry; re-navigate.
