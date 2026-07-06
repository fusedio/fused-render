---
name: fused-render-custom-templates
description: How to create and register a custom preview template for fused-render — a user-owned template that overrides (or adds to) the built-in extension handlers, so opening a file of that extension renders the user's template instead. Use this whenever the user wants a custom/own preview for a file extension (e.g. "my own parquet viewer", "render .xyz files with my template"), wants to override or disable a built-in template, mentions ~/.fused-render or registry.json, or asks to "register a template". For actually writing the template's html and py files, this skill delegates to the fused-render-authoring skill — read that one too.
---

# Custom templates for fused-render

fused-render resolves a preview template for each file by extension. Built-ins ship inside the package; a user can override or extend them with templates under `~/.fused-render/`. A custom template is an **ordinary renderable-HTML page** — same injected `window.fused` runtime, same `_file` param, same sibling-`.py` pattern as every built-in. Nothing about authoring is special; only *registration* is.

**Division of labor between skills (do not duplicate):**

- **This skill:** where files go, how the registry binds extensions, how to test registration.
- **`skills/fused-render-authoring/SKILL.md`:** how to write the `template.html` and reader `.py` themselves — the `fused` API, the `main()` contract, the params-are-state wiring pattern, the `_file` handling, the pitfalls. **Read it before writing any html/py.** In particular its "Preview templates" section is exactly what a custom template is.

## Layout on disk

One folder per template, self-contained:

```
~/.fused-render/
├── registry.json          ← bindings: extension → folder name
├── geo/                   ← a template ("geo" is just a label)
│   ├── template.html      ← required, this is what renders
│   └── reader.py          ← optional siblings: readers, css, assets
└── wip-thing/             ← no registry entry → inert draft
```

- The folder name carries **no meaning** — binding happens only in `registry.json`.
- `template.html` is the fixed entry-point filename.
- Sibling files are referenced relatively, e.g. `fused.runPython("./reader.py", {...})` — the template renders from its real path, so relative resolution just works.

## registry.json

Flat JSON object. Keys are **dotted extensions**, values are a **folder name** or **`null`**:

```json
{
  ".parquet": "geo",
  ".geojson": "geo",
  ".tar.gz": "archive",
  ".png": null
}
```

Rules:

- **Dotted keys, longest suffix wins, case-insensitive.** `.tar.gz` beats `.gz` for `backup.tar.gz`. Compound extensions work only through the registry — always include the leading dot.
- **Many-to-one is normal** — several keys may name the same folder.
- **`null` disables the built-in** for that extension: no template renders; the explorer shows its plain metadata/raw-download fallback.
- **Any extension is allowed**, including ones fused-render has no built-in for.
- **`.html`/`.htm` cannot be bound** — renderable HTML is the product's core behavior and never goes through a template.
- Registry beats built-ins; no registry entry (or no `registry.json` at all) means built-in behavior.
- A folder without a registry entry does nothing — that's the draft state. Registering = adding the line; unregistering = deleting it. No restart needed: the registry is re-read on every file open, so the next navigation/refresh picks changes up.

## Workflow: create and register a template

1. **Make the folder:** `mkdir -p ~/.fused-render/<name>` — pick a descriptive label.
2. **Author `template.html` (+ readers) following `fused-render-authoring`.** The template reads its target from the read-only `_file` param and keeps UI state (paging, sort) in normal params. A reader `.py` is only needed where Python adds value — text formats can `fused.readFile(file)` directly, media can point at `fused.rawUrl(file)`.
3. **Develop before registering (optional):** the draft folder is invisible to dispatch, but the template is a plain fused page — open `http://127.0.0.1:8765/view/<abs path to template.html>?_file=<abs path to a sample file>` to iterate on it directly. Saving the html or a reader auto-reloads the open view.
4. **Register:** add the extension line(s) to `~/.fused-render/registry.json` (create the file with `{}` around the first entry if it doesn't exist).
5. **Test dispatch:** open a file of that extension in the explorer (navigate to it, or right-click → open with fused-render from Finder). Your template should render with `_file` pointing at the file. Editing `template.html` afterwards live-reloads any open preview.

## Troubleshooting

- **Built-in still renders / nothing changed:** typo in the registry key (missing dot?), folder name mismatch, `template.html` missing, or invalid JSON — all of these fall back to the built-in. The file's stat response (`fused.stat(path)` or `GET /api/fs/stat?path=…`) carries a `template_error` field naming the problem.
- **Folder name rejected:** names must be a single path segment — no `/`, no `..`, not empty.
- **Template renders but is blank / errors:** that's an authoring problem, not registration — debug with the `fused-render-authoring` skill (red traceback overlay, `print()` → browser console).
- **Registry edits not applying to an already-open preview:** open previews watch their files, not the registry — refresh or re-navigate.
