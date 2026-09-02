---
name: fused-render-custom-templates
description: Use when registering a custom preview template for a file extension, or overriding/reordering built-in template modes — ~/.fused-render layout, registry.json, condition.py.
---

# Custom preview templates

Each extension resolves to an ordered list of modes (first = default; >1 shows a switcher). This skill = registration only. Writing the `template.html`/reader `.py` → `fused-render-authoring` (its "Preview templates" section IS a custom template). A "template" that's really a long-running server → `fused-render-background-apps`.

## Layout

```
~/.fused-render/templates/
├── registry.json      extension → mode list
└── <name>/            folder name IS the public name (registry ref, _mode value)
    ├── template.html  required entry point
    ├── reader.py      optional siblings, referenced relatively
    ├── condition.py   optional per-file gate
    └── icon.svg       optional switcher icon (monochrome, currentColor, square, legible 16px)
```

Name resolution: user folder wins, else built-in `fused_render/templates/<name>/`. Naming after a built-in shadows it deliberately (built-in names = every folder there with a template.html; also Templates → Library). Folder names: single segment, no dots, no leading `_`. Folder without a registry entry = inert draft.

## registry.json

Flat object; same key grammar as built-in `fused_render/templates/registry.json`. Values: list of names (full ordered mode list — **replace semantics**, no merge; re-list built-in modes you want to keep), a string (single-mode shorthand), or `null`/`[]` (disable templating for the extension).

- Keys: dot-anchored suffix patterns, case-insensitive, most-specific wins (`.tar.gz` > `.gz`; literal > `*` at equal length). `*` = exactly one whole segment. Trailing `/` binds a directory (`.zarr/`). Any extension allowed, `.html` included (rebind `["code", "_render"]` to make source the default).
- `_`-prefixed names are shell sentinels; referenceable: `_render`, `_listing`. Any other `_name` is invalid → dropped, `template_error` set, rest of list works.
- Any registry key beats the built-in table. Re-read on every file open — no restart.

## condition.py

`def main(path) -> bool` beside template.html — show this template for this specific file? No file = always shown. Runs after registry resolution, evaluated in the background (`GET /api/fs/conditions?path=…`), verdicts cached ~60 s. May read the file but keep reads bounded (headers/footers, remote mounts exist). Broken gate = template dropped, reason in `error` on the conditions response. Gated template is never the default while an ungated one exists. Shows a "conditional" badge in Templates → Library.

## Guardrail

Templates run on every open with server privileges inside the packaged app. In readers/conditions: no `subprocess`/`os.system`/system binaries — bundled libraries in-process only (list in `fused-render-authoring`; more needs a folder pyproject). In html: filesystem only via `window.fused` helpers, never raw `/api/fs/*` fetches or external script/data hosts.

## Workflow

1. `mkdir -p ~/.fused-render/templates/<name>` (or scaffold via Templates → Library → New).
2. Author per `fused-render-authoring` (`_file` param = target; UI state in normal params).
3. Iterate before registering: open `…/explorer/view/<abs template.html>?_file=<abs sample>` — drafts are plain fused pages. Saves live-reload.
4. Register: add the extension line to registry.json (create with `{}` if absent).
5. Test: open a file of that extension; switcher appears only when >1 mode; `?_mode=<name>` opens a specific one.

## Troubleshooting

- One mode missing, rest fine → bad name dropped silently; check `template_error` on `fused.stat(path)`.
- Whole extension falls back to built-ins → shape-level registry error (invalid JSON / wrong value type). `null`/`[]` are not errors.
- Mode missing for SOME files → condition.py verdict; check `error` on `/api/fs/conditions`.
- Blank/erroring render → authoring problem, not registration → `fused-render-authoring`.
- Registry edits not applying to an open preview → previews watch files, not the registry; re-navigate.
