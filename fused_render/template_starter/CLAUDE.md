# Authoring a fused-render preview template

This folder is one **preview template** — a self-contained folder that renders a
file (or directory) inside the fused-render preview pane. It was scaffolded from
the starter kit; edit it in place. Full contract and examples live in the
`template-authoring` skill under `.claude/skills/template-authoring/` — read it
before making non-trivial changes.

## Folder contract

- **`template.html`** (required) — an ordinary HTML page. The shell injects the
  `fused` runtime (no `<script src>`, no network). The target file arrives as
  the reserved URL param `_file`: `fused.params.get("_file")`. Render however
  you like.
- **`reader.py`** (optional) — server-side, **read-only** data prep. The page
  calls `fused.runPython("./reader.py", { file, ... })`; params arrive as
  keyword args (strings — annotate `main` for coercion) and the JSON-serialisable
  return value comes back to the page. Full Python is available; inspect the
  file, never mutate it. Delete this file if the template needs no backend.
- **`icon.svg`** (optional) — **monochrome** (single fill; only alpha matters —
  the shell tints it via CSS `mask-image` + `currentColor`). Square viewBox
  (24×24 suggested), legible at 16px. Shown in the mode switcher.
- **`condition.py`** (optional, **not scaffolded** — add it only when needed) —
  a per-file gate: `def method(path) -> bool`. Returning `False` hides this
  template for that file, so one extension binding can offer different templates
  for different files. No `condition.py` = always shown (the common case). Fails
  closed (a broken gate hides the template).

The **folder name is the template's identity** — it is the name you bind in the
registry and the label in the switcher. It must be one plain path segment: no
`/`, `\`, or `.`, and no leading `_` (reserved for shell sentinels).

## How bindings work

A template does nothing until an extension is bound to it in the **user
registry**, `~/.fused-render/templates/registry.json` — a flat JSON object
mapping dot-anchored suffix-pattern keys to an ordered list of template names
(first = default):

```json
{ ".myext": ["<this-folder-name>"] }
```

Key shapes: simple `.csv`, compound `.tar.gz`, wildcard `.*.json` (one whole
segment), directory `.zarr/` (trailing slash), and the universal `/` (any
directory). A user key beats a built-in one. `null` or `[]` disables previews
for that type.

You do not hand-edit this file: the "New template" flow created the initial
bindings, and the Templates view (`/view/_templates`) edits them per key
(read-modify-write of one key at a time). Each edit only rewrites its own key.

## Testing

Bind the extension (above), then open a matching file in the explorer, or hit
`/render?path=<this-folder-name>&_file=<abs-file-path>` directly. Editing
`template.html` or `reader.py` live-reloads any open preview (M4 auto-reload).
Registry edits apply on the next stat — navigate or refresh.
