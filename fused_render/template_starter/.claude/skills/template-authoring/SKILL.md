---
name: template-authoring
description: Author a fused-render preview template — the folder anatomy (template.html, reader.py, icon.svg, condition.py), the registry key grammar and binding rules, the injected `fused` runtime API, and how to preview your work. Use when creating or editing a template in this folder.
---

# Authoring a fused-render preview template

A **template** is a self-contained folder that renders a file (or directory) in
the fused-render preview pane. It is ordinary renderable HTML — same runtime,
same powers as any page the app renders — plus optional sibling files. This
folder is one such template; the guidance below is everything needed to shape it.

## Anatomy

```
<template-name>/
  template.html      # required — the page
  reader.py          # optional — server-side, read-only data prep
  icon.svg           # optional — monochrome switcher icon
  condition.py       # optional — per-file gate (add only when needed)
  <assets…>          # optional — css, images, more .py, anything the page loads
```

The **folder name is the template's identity**: it is what you bind in the
registry, the value of the `_mode` URL param, and the switcher tooltip label. It
must be a single safe path segment — no `/`, `\`, or `.` anywhere, and no leading
`_` (that prefix is reserved for shell sentinel modes like `_render` /
`_listing`). Renaming the folder renames the template; rebind afterwards.

### template.html (required)

An ordinary HTML document. The shell injects a global `fused` runtime before your
scripts run — **do not** add `<script src>` to CDNs or fetch over the network
(offline by design); inline what you need or vendor it into the folder and load
it with a relative path (it resolves against this folder because the page renders
from its real path).

The target file arrives as the reserved URL param **`_file`**:

```js
const file = fused.params.get("_file");
```

Reserved `_`-prefixed params (`_file`, `_mode`) are readable but not settable by
page code. Your own UI state (paging, selected column, sort) uses ordinary
params — they sync to the shell URL, so they survive refresh and are
bookmarkable.

### reader.py (optional)

Server-side data prep, run with full Python on the host machine. The page calls:

```js
const data = await fused.runPython("./reader.py", { file, offset: "0" });
```

which invokes `def main(...)` with those params as keyword args. **Params arrive
as strings** — annotate `main` (`def main(file: str, offset: int = 0)`) so they
coerce. Return anything JSON-serialisable; it becomes the resolved value in the
page. Keep it **read-only** (inspect the file, never mutate it) and cheap (return
only what the current view needs — page large files, don't ship the whole thing).
Delete `reader.py` if the template renders purely client-side.

Errors thrown in `main` surface to the page as the rejected promise
(`err.type` / `err.message`) — catch and render them.

### The `fused` runtime API (used from template.html)

- `fused.params.get(name)` / `fused.params.set(name, value)` — read/write URL
  params. `set` re-runs `onChange`.
- `fused.params.onChange(fn)` — register a callback fired when any param changes.
- `fused.runPython(relPath, paramsObj)` → Promise — run a sibling `.py`'s
  `main`, resolve to its return value.
- `fused.rawUrl(file)` — a URL that serves the file's raw bytes (for `<img>`,
  `<a href>`, media `src`, Range requests).
- `fused.stat(file)` → Promise of `{ size, … }` — cheap metadata; gate huge files
  before reading them inline.
- `fused.readFile(file)` → Promise of the file's text.
- `fused.writeFile(file, contents)` → Promise — write (only for templates that
  edit; the write guard/lock come for free through the runtime).

Always reach the filesystem through these helpers, never by fetching `/api/fs/*`
directly — one code path, and the guards apply automatically.

### icon.svg (optional)

**Monochrome**: single fill, only the alpha channel matters — the shell tints it
via CSS `mask-image` + `currentColor`. Square viewBox (24×24 suggested), legible
at 16px. Shown in the mode switcher when an extension has more than one mode. No
icon → the shell draws a letter placeholder.

### condition.py (optional — add only when needed)

A per-file gate so one extension binding can offer different templates for
different files (gate on contents, a path prefix, a naming convention):

```python
def method(path):  # -> bool
    return path.endswith(".special.json")
```

Returning falsy hides this template for that file. **No `condition.py` = always
shown** (the common case — don't scaffold one you don't need). Gates may do real
I/O; they are evaluated in the background (not at stat time) and **fail closed**
— a gate that raises, or defines no callable `method`, hides the template. A
template whose whole list is conditional holds the preview until a verdict lands.

## Registry & bindings

A template is inert until an extension is bound to it. Bindings live in the
**user registry**, `~/.fused-render/templates/registry.json` — a flat JSON object
mapping keys to an ordered list of template names (first entry = the default
mode):

```json
{
  ".myext": ["<template-name>"],
  ".csv": ["<template-name>", "csv", "code"],
  ".zarr/": ["zarr", "_listing"],
  "/": ["_listing"]
}
```

### Key grammar

A key is a **dot-anchored suffix pattern** — one or more dot-led segments, each a
literal (`json`, `tar`) or the wildcard `*` (matches exactly one whole non-empty
segment; partial wildcards like `.geo*` are invalid):

- **simple** — `.csv`
- **compound** — `.tar.gz`, `.xyz.json`
- **wildcard** — `.*.json`
- **directory** — trailing slash binds a directory's basename: `.zarr/`
- **universal** — the bare `/` matches *any* directory (lowest specificity)

Matching is case-insensitive and needs a non-empty stem (a file literally named
`.json` does not match `.json`; `.hidden.json` does). Specificity: more segments
win; at equal length, compare rightmost-first, literal beats `*` — so for
`data.xyz.json`: `.xyz.json` > `.*.json` > `.json`.

### Values

- A **list** of template names = the full ordered mode list (replace semantics,
  first = default). A bare string is shorthand for a single-mode list.
- **`null` or `[]`** disables previews for that type entirely (no built-in
  fallback) — the file falls through to the shell's metadata/download view.
- A name may reference a **shell sentinel** — `_render` (render an HTML file
  itself) or `_listing` (the built-in directory listing). Any other `_`-prefixed
  name is invalid.
- An unknown/not-yet-created name is saved as a **dangling ref** — surfaced as
  broken in the UI and dropped at render, not rejected — so you can bind ahead of
  creating the folder.

### How bindings are written

You do **not** hand-edit `registry.json`. Bindings are managed one key at a time:
the "New template" flow seeds the initial bindings, and the Templates view
(`/view/_templates`) edits each key via a read-modify-write of that key only
(never a whole-file rewrite), so concurrent edits to other keys are never lost. A
user-registry key always beats a built-in one, so binding `.csv` here overrides
the shipped `.csv` template.

## Preview & dev loop

1. Bind the extension (New-template flow, or the Templates view).
2. Open a matching file in the explorer — or hit `/render?path=<template-name>&_file=<abs-file-path>`
   directly in the browser to render this template against a file.
3. Editing `template.html` or any `runPython` target **live-reloads** open
   previews (auto-reload watches the html and its readers).
4. Registry edits apply on the **next stat** — navigate or refresh; open previews
   do not watch `registry.json`.

Editing template *file contents* happens here in the file explorer — the
Templates view manages bindings and inventory only, not the files themselves.
