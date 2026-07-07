---
name: fused-render-custom-templates
description: How to create and register a custom preview template for fused-render — a user-owned template that overrides, reorders, or extends the built-in extension → template mode list, so opening a file of that extension renders the user's template (or offers it as a switchable mode) instead. Use this whenever the user wants a custom/own preview for a file extension (e.g. "my own parquet viewer", "add a mode for .xyz files", "render .xyz files with my template"), wants to override, add, reorder, or disable template modes, mentions ~/.fused-render, registry.json, or _mode, or asks to "register a template". For actually writing the template's html and py files, this skill delegates to the fused-render-authoring skill — read that one too.
---

# Custom templates for fused-render

fused-render resolves an **ordered list of preview templates — modes** — for each file by extension; the first mode is the default, and when a file has more than one the shell shows an icon-only switcher to flip between them. Built-ins ship inside the package; a user can override, reorder, or extend the list with templates under `~/.fused-render/`. A custom template is an **ordinary renderable-HTML page** — same injected `window.fused` runtime, same `_file` param, same sibling-`.py` pattern as every built-in, optionally with an `icon.svg` for the switcher. Nothing about authoring is special; only *registration* is.

**Division of labor between skills (do not duplicate):**

- **This skill:** where files go, how the registry binds extensions, how to test registration.
- **`skills/fused-render-authoring/SKILL.md`:** how to write the `template.html` and reader `.py` themselves — the `fused` API, the `@fused.udf` contract, the params-are-state wiring pattern, the `_file` handling, the pitfalls. **Read it before writing any html/py.** In particular its "Preview templates" section is exactly what a custom template is.

## Layout on disk

One folder per template, self-contained:

```
~/.fused-render/
├── registry.json          ← bindings: extension → mode list (names)
├── geo/                   ← a template — the folder name IS its name
│   ├── template.html      ← required, this is what renders
│   ├── reader.py          ← optional siblings: readers, css, assets
│   └── icon.svg           ← optional, shown by the mode switcher
└── wip-thing/             ← no registry entry → inert draft
```

- **The folder name is the template's public name** — it's what a registry list references, the `_mode=<name>` URL value, and the switcher's tooltip label. One name-resolution rule everywhere: a name resolves to `~/.fused-render/<name>/template.html` if that exists, else the built-in `fused_render/templates/<name>/template.html`, else it's unusable. **Naming your folder after a built-in shadows it** — `table`, `csv`, `xlsx`, `tree`, `markdown`, `image`, `media`, `pdf`, `code`, `text`, `geotiff`, `netcdf` are the built-in names; reuse one deliberately to replace that built-in everywhere it's referenced, including inside a `"..."` splice (below).
- `template.html` is the fixed entry-point filename.
- Sibling files are referenced relatively, e.g. `fused.runPython("./reader.py", {...})` — the template renders from its real path, so relative resolution just works.
- `icon.svg` is optional: a monochrome single-fill SVG (`currentColor` or plain black — the shell tints it via a CSS mask, so only alpha matters), square viewBox (24×24 suggested), simple enough to read at 16px. No icon → the switcher shows a first-letter placeholder for that mode instead.

## registry.json

Flat JSON object. Keys are **dotted extensions**, values are a **list of names**, a single **name** (string), or **`null`**:

```json
{
  ".parquet": ["geo", "..."],
  ".geojson": "geo",
  ".tar.gz": "archive",
  ".png": null
}
```

Rules:

- **List = the full ordered mode list for that extension — replace semantics.** Order matters: the first entry is the default mode, later entries only show up (as a switcher) when the file has more than one mode. Each entry is a name resolved by the rule above.
- **`"..."` splices in the built-in list, in place** — add modes without knowing (or hand-maintaining) the built-in names, and future built-in additions flow in automatically. `["code", "..."]` promotes `code` to the default and keeps everything the built-in table had, without duplicating it. Rules: names already listed explicitly are skipped when the splice expands (no duplicates); **at most one `"..."` per list** — a second one makes the whole entry invalid (falls back to the built-in list, `template_error` set); splicing an extension with no built-in list expands to nothing (harmless).
- **String = shorthand for a single-mode list of that one name** — exactly `["geo"]`. This is the pre-modes registry shape; existing registries keep working unchanged.
- **`null` disables templating** for that extension entirely: no template renders; the explorer shows its plain metadata/raw-download fallback.
- **Dotted keys, longest suffix wins, case-insensitive.** `.tar.gz` beats `.gz` for `backup.tar.gz`. Compound extensions work only through the registry — always include the leading dot.
- **Many-to-one is normal** — several extension keys may reference the same template name.
- **Any extension is allowed**, including ones fused-render has no built-in for.
- **Names starting with `_` are reserved** — they're shell sentinels (modes the shell itself implements, like `_render`, the "render this HTML file" mode), not template folders. A `_`-prefixed name in a registry list is invalid: it's dropped and `template_error` is set, the rest of the list still works. Same reservation as `_mode`/`_file` params.
- **`.html`/`.htm` cannot be bound** — renderable HTML is the product's core behavior. HTML has a hardcoded server-side mode list (`["_render", "code"]`: rendered page first, source editor second) that the registry cannot touch.
- Registry (longest matching key) beats the built-in table; no registry entry (or no `registry.json` at all) means built-in behavior.
- A folder without a registry entry does nothing — that's the draft state. Registering = adding the line; unregistering = deleting it. No restart needed: the registry is re-read on every file open, so the next navigation/refresh picks changes up.

## Workflow: create and register a template

1. **Make the folder:** `mkdir -p ~/.fused-render/<name>` — the name is public: it's the registry reference, the `_mode` URL value, and the switcher tooltip. Pick a real name, or reuse a built-in name on purpose to shadow it.
2. **Author `template.html` (+ readers) following `fused-render-authoring`.** The template reads its target from the read-only `_file` param and keeps UI state (paging, sort) in normal params. A reader `.py` is only needed where Python adds value — text formats can `fused.readFile(file)` directly, media can point at `fused.rawUrl(file)`.
3. **Optionally add `icon.svg`** — monochrome, square, simple at 16px (see above). Skip it and the mode shows a first-letter placeholder in the switcher.
4. **Develop before registering (optional):** the draft folder is invisible to dispatch, but the template is a plain fused page — open `http://127.0.0.1:8765/view/<abs path to template.html>?_file=<abs path to a sample file>` to iterate on it directly. Saving the html or a reader auto-reloads the open view.
5. **Register:** add the extension line to `~/.fused-render/registry.json` — a bare name string for a single-mode override, or a list (optionally with `"..."`) to add it alongside the built-ins (create the file with `{}` around the first entry if it doesn't exist).
6. **Test dispatch:** open a file of that extension in the explorer. A single-mode list renders directly; with more than one mode, the preview header shows an icon-only switcher — click your mode's icon (or its first-letter placeholder), or navigate straight to `…?_mode=<name>`. Editing `template.html` afterwards live-reloads any open preview.

## Troubleshooting

- **One mode missing, rest of the list fine:** a single bad entry (typo, folder name mismatch, `template.html` missing in both locations) is dropped silently — everything else in the list still renders. Check `template_error` on the stat response (`fused.stat(path)` or `GET /api/fs/stat?path=…`) for the first bad name.
- **Whole extension falls back to built-ins:** happens when the registry value resolves to nothing at all — invalid JSON, an empty list, or two `"..."` in one list (only one splice per list is allowed).
- **Folder name rejected:** names must be a single path segment — no `/`, no `..`, no `.` (dots are reserved for the `"..."` splice token), no leading `_` (reserved for shell sentinels), not empty.
- **Template renders but is blank / errors:** that's an authoring problem, not registration — debug with the `fused-render-authoring` skill (red traceback overlay, `print()` → browser console).
- **Registry edits not applying to an already-open preview:** open previews watch their files, not the registry — refresh or re-navigate.
- **Mode switcher doesn't show up:** it only renders when a file has more than one mode (`templates.length > 1`) — a single-mode extension, or a `null` override, never shows it.
