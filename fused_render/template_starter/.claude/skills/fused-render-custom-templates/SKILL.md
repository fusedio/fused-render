---
name: fused-render-custom-templates
description: How to create and register a custom preview template for fused-render — a user-owned template that overrides, reorders, or extends the built-in extension → template mode list, so opening a file of that extension renders the user's template (or offers it as a switchable mode) instead. Use this whenever the user wants a custom/own preview for a file extension (e.g. "my own parquet viewer", "add a mode for .xyz files", "render .xyz files with my template"), wants to override, add, reorder, or disable template modes, mentions ~/.fused-render, registry.json, or _mode, or asks to "register a template". For actually writing the template's html and py files, this skill delegates to the fused-render-authoring skill — read that one too.
---

# Custom templates for fused-render

fused-render resolves an **ordered list of preview templates — modes** — for each file by extension; the first mode is the default, and when a file has more than one the shell shows an icon-only switcher to flip between them. Built-ins ship inside the package; a user can override, reorder, or extend the list with templates under `~/.fused-render/templates/`. A custom template is an **ordinary renderable-HTML page** — same injected `window.fused` runtime, same `_file` param, same sibling-`.py` pattern as every built-in, optionally with an `icon.svg` for the switcher. Nothing about authoring is special; only *registration* is.

**Division of labor between skills (do not duplicate):**

- **This skill:** where files go, how the registry binds extensions, how to test registration.
- **`skills/fused-render-authoring/SKILL.md`:** how to write the `template.html` and reader `.py` themselves — the `fused` API, the `main()` contract, the params-are-state wiring pattern, the `_file` handling, the pitfalls. **Read it before writing any html/py.** In particular its "Preview templates" section is exactly what a custom template is.

## Guardrail: use fused's internal APIs, never raw OS/shell commands

A custom template runs on every open of its extension, with the local server's privileges, inside the packaged app — which bundles **only** a fixed Python library set plus a few tools (`rclone`/`uv`/`duckdb`), not a general shell. Reach for fused's own APIs, not raw OS or network commands. This is a security and performance boundary, not a style preference.

**In the reader `.py` / `condition.py`:**
- ❌ Don't shell out — no `subprocess`, `os.system`, `os.popen`, or invoking system binaries (`gdalinfo`, `curl`, `ffmpeg`, …). They may not exist in the packaged app, run with the server's privileges, and pay a process-spawn cost on every preview.
- ✅ Do the work in-process with the **bundled libraries** (pandas, polars, pyarrow, duckdb, rasterio, pillow, pymupdf, requests/httpx, … — the authoritative set is in `fused-render-authoring`). Reading local files with `open()` / `os.path` relative to your script is fine and idiomatic; spawning commands is not.
- ✅ In `condition.py`, **keep reads bounded** — sniff a footer/header/prefix, never `read()` a whole file, and never shell out. The gate runs for every file of the extension, some on remote mounts.

**In `template.html`:**
- ❌ Don't `fetch()` the server's `/api/fs/*` endpoints directly, and don't `<script src>` a runtime or pull data from arbitrary external hosts.
- ✅ Reach the filesystem only through the injected `window.fused` helpers — `fused.readFile` / `fused.rawUrl` / `fused.stat` / `fused.writeFile`, and `fused.runPython` for anything Python adds value to. They're the stable contract and carry the headers writes require.

**Why:** the internal APIs keep the template working in the sandboxed packaged app (no system dependencies), avoid the security surface of running shell commands with server privileges, and stay fast — no per-open subprocess spawns, and remote-mounted files are streamed through the runtime instead of pulled whole.

## Layout on disk

One folder per template, self-contained:

```
~/.fused-render/templates/
├── registry.json          ← bindings: extension → mode list (names)
├── geo/                   ← a template — the folder name IS its name
│   ├── template.html      ← required, this is what renders
│   ├── reader.py          ← optional siblings: readers, css, assets
│   ├── condition.py       ← optional gate: def main(path) -> bool
│   └── icon.svg           ← optional, shown by the mode switcher
└── wip-thing/             ← no registry entry → inert draft
```

- **The folder name is the template's public name** — it's what a registry list references, the `_mode=<name>` URL value, and the switcher's tooltip label. One name-resolution rule everywhere: a name resolves to `~/.fused-render/templates/<name>/template.html` if that exists, else the built-in `fused_render/templates/<name>/template.html`, else it's unusable. **Naming your folder after a built-in shadows it** — the built-in names include `code`, `csv`, `xlsx`, `duckdb` (the tabular viewer), `sqlite`, `structure`, `tree`, `markdown`, `image`, `media`, `pdf`, `text`, `geotiff`, `netcdf`, `zarr_aoi`, `map`, `vector`, `pmtiles`, `h3`, `api`, plus many more (the authoritative set is every folder under `fused_render/templates/` that contains a `template.html`, also visible in the app's Templates → Library). Reuse one deliberately to replace that built-in everywhere it's referenced.
- `template.html` is the fixed entry-point filename.
- Sibling files are referenced relatively, e.g. `fused.runPython("./reader.py", {...})` — the template renders from its real path, so relative resolution just works.
- `icon.svg` is optional: a monochrome single-fill SVG (`currentColor` or plain black — the shell tints it via a CSS mask, so only alpha matters), square viewBox (24×24 suggested), simple enough to read at 16px. No icon → the switcher shows a first-letter placeholder for that mode instead.

## registry.json

Flat JSON object. Keys are **dotted extension patterns** (compound extensions, `*` wildcard segments, trailing `/` for directories — same grammar as the built-in `fused_render/templates/registry.json`), values are a **list of names**, a single **name** (string), or **`null`**:

```json
{
  ".parquet": ["geo", "duckdb"],
  ".geojson": "geo",
  ".tar.gz": "archive",
  ".*.json": "config-view",
  ".obt/": "bundle",
  ".png": null
}
```

Rules:

- **List = the full ordered mode list for that extension — replace semantics.** Order matters: the first entry is the default mode, later entries only show up (as a switcher) when the file has more than one mode. Each entry is a name resolved by the rule above. A user list **replaces** the built-in list for that extension outright — there is no splice/merge with the built-ins, so if you want a built-in mode alongside yours, name it explicitly in your list (e.g. `["geo", "duckdb"]` to keep the tabular viewer as a second mode).
- **String = shorthand for a single-mode list of that one name** — exactly `["geo"]`. This is the pre-modes registry shape; existing registries keep working unchanged.
- **`null` (or an empty list `[]`) disables templating** for that extension entirely: no template renders; the explorer shows its plain metadata/raw-download fallback.
- **Dotted keys, most-specific wins, case-insensitive.** A key is a dot-anchored suffix pattern of one or more segments. More segments beats fewer (`.tar.gz` beats `.gz` for `backup.tar.gz`); at equal length, comparing from the rightmost segment, a literal beats a wildcard (`.xyz.json` > `.*.json` > `.json` for `data.xyz.json`). Always include the leading dot. A match needs something before the suffix — a file literally named `.json` doesn't match the `.json` key (but `.hidden.json` does).
- **`*` = exactly one whole segment.** `.*.json` matches `data.tiles.json` but not `data.json` (nothing for the `*`) and not partially (`.geo*.json` is invalid — a key like that never matches anything).
- **Trailing `/` binds a directory.** `".obt/"` matches a *directory* named `data.obt` — the way built-in `.zarr` stores are bound (`".zarr/": ["zarr_aoi", "_listing"]`). Directory keys never match files and vice versa. A `null` on a directory key gives the plain listing view.
- **Many-to-one is normal** — several extension keys may reference the same template name.
- **Any extension is allowed**, including ones fused-render has no built-in for — and including `.html`/`.htm`: the rendered-page-first default (built-in `".html": ["_render", "code", "claude", "annotate", "history"]`) is just a built-in registry entry, and you can rebind or reorder it (e.g. `["code", "_render"]` to make the source editor the default). Remember a user list replaces the built-in one, so re-list any built-in modes you want to keep.
- **Names starting with `_` are shell sentinels** — modes the shell itself implements, not template folders. The referenceable ones are **`_render`** ("render the HTML file itself") and **`_listing`** (the plain directory listing, used on directory keys); any *other* `_`-prefixed name in a registry list is invalid: dropped, `template_error` set, rest of the list still works. Sentinels can't be shadowed by a folder (`.` and leading `_` are banned in folder names). Same reservation as `_mode`/`_file` params.
- Registry (any matching key) beats the built-in table — even a plain user `.json` key beats a more specific built-in `.xyz.json` one; no registry entry (or no `registry.json` at all) means built-in behavior.
- A folder without a registry entry does nothing — that's the draft state. Registering = adding the line; unregistering = deleting it. No restart needed: the registry is re-read on every file open, so the next navigation/refresh picks changes up.

## Conditional templates (`condition.py`)

The registry decides *which* templates apply to an extension. A template folder can additionally decide *whether* it shows for a **specific file** by dropping a **`condition.py`** beside its `template.html`:

```python
# ~/.fused-render/templates/reports-view/condition.py
def main(path):
    # path is the absolute path of the file being previewed.
    # Return True to show this template for this file, False to hide it.
    return "/reports/" in path and path.endswith("_final.csv")
```

- **Signature:** `def main(path): bool`. `path` is the file being previewed; return truthy to keep the template in the list, falsy to drop it.
- **No `condition.py` = always shown** — the common case. Only add one when a template should apply to *some* files of an extension, not all.
- **Runs after registry resolution**, for both user and built-in folders (whichever `template.html` resolves). So `".csv": ["reports-view", "csv", "code"]` offers `reports-view` only for files where its `main` returns True; the others always show.
- **Evaluated in the background, not at stat time.** Stat only marks the entry `"conditional": true`; the shell renders the first *unconditional* template immediately and resolves the gates via `GET /api/fs/conditions?path=<file>` while a pending spinner shows in the switcher. This means a gate MAY read the file's contents (e.g. sniff a parquet footer) without slowing every preview — but keep reads bounded (metadata/footers/prefixes, not whole files), especially for files on remote mounts.
- **Never the default while a normal template exists:** a gated template can only be the default mode when every template in the list is gated.
- **Re-evaluated when its verdict isn't cached** — the `condition.py` module is re-loaded fresh each time it runs (edit it, no restart needed), but the conditions endpoint keeps a short **~60 s TTL cache** per file, so a re-navigation within that window returns the cached verdict rather than re-running the gate. (The registry itself, by contrast, is re-read with no cache on every open.)
- **Concurrent:** when an extension has several gated templates they're evaluated concurrently, so the cost is the slowest gate, not the sum — but every gate still runs on every file of that extension (a gate that returns False still had to run to decide that).
- **A broken condition drops that template** (no callable `main`, an exception, etc.) and reports the reason as `error` on the conditions response — same fail-closed posture as a bad registry name. It's never silently shown.
- **Sentinel modes** (`_render`, `_listing`) have no folder and can't be gated.
- **Visible in the UI:** a template with a `condition.py` shows a **"conditional"** badge in the templates management page (Templates → Library), so you can tell at a glance which templates are gated.

## Workflow: create and register a template

1. **Make the folder:** `mkdir -p ~/.fused-render/templates/<name>` — the name is public: it's the registry reference, the `_mode` URL value, and the switcher tooltip. Pick a real name, or reuse a built-in name on purpose to shadow it. (Or let the app scaffold it for you — Templates → Library → New — which creates the folder from a starter kit and binds it; see the note after this list.)
2. **Author `template.html` (+ readers) following `fused-render-authoring`.** The template reads its target from the read-only `_file` param and keeps UI state (paging, sort) in normal params. A reader `.py` is only needed where Python adds value — text formats can `fused.readFile(file)` directly, media can point at `fused.rawUrl(file)`.
3. **Optionally add `icon.svg`** — monochrome, square, simple at 16px (see above). Skip it and the mode shows a first-letter placeholder in the switcher.
4. **Develop before registering (optional):** the draft folder is invisible to dispatch, but the template is a plain fused page — open `http://127.0.0.1:1777/view/<abs path to template.html>?_file=<abs path to a sample file>` to iterate on it directly. Saving the html or a reader auto-reloads the open view.
5. **Register:** add the extension line to `~/.fused-render/templates/registry.json` — a bare name string for a single-mode override, or a list to set the full ordered mode list (re-listing any built-in modes you want to keep, since a user list replaces the built-in one). Create the file with `{}` around the first entry if it doesn't exist.
6. **Test dispatch:** open a file of that extension in the explorer. A single-mode list renders directly; with more than one mode, the preview header shows an icon-only switcher — click your mode's icon (or its first-letter placeholder), or navigate straight to `…?_mode=<name>`. Editing `template.html` afterwards live-reloads any open preview.

You don't have to hand-edit JSON at all: the app ships a **Templates → Library** page (backed by `/api/templates/*`) to inspect the merged inventory, edit registry bindings per-extension, scaffold a new template, and export/import — the same operations this workflow does by hand.

## Troubleshooting

- **One mode missing, rest of the list fine:** a single bad entry (typo, folder name mismatch, `template.html` missing in both locations) is dropped silently — everything else in the list still renders. Check `template_error` on the stat response (`fused.stat(path)` or `GET /api/fs/stat?path=…`) for the first bad name.
- **Whole extension falls back to built-ins:** happens when the registry value is a shape-level error — invalid JSON, or a value that isn't a list/string/null. (An empty list `[]` and `null` are *not* errors — they deliberately disable previews.)
- **Folder name rejected:** names must be a single path segment — no `/`, no `\`, no `.` at all (dots are banned to keep names unambiguous against dotted registry keys), no leading `_` (reserved for shell sentinels), not empty.
- **Template renders but is blank / errors:** that's an authoring problem, not registration — debug with the `fused-render-authoring` skill (red traceback overlay, `print()` → browser console).
- **Registry edits not applying to an already-open preview:** open previews watch their files, not the registry — refresh or re-navigate.
- **Mode switcher doesn't show up:** it only renders when a file has more than one mode (`templates.length > 1`) — a single-mode extension, or a `null` override, never shows it.
- **A mode is missing only for some files:** that's a `condition.py` doing its job (or misfiring). If it's unexpectedly hidden, check `error` on `GET /api/fs/conditions?path=…` for a condition exception, and confirm `main(path)` returns True for that file's path.
