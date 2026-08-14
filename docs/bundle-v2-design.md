# Export bundle v2 — single payload dir + role-tagging manifest — design

Date: 2026-07-17. Branch `claude/bundle-v2-payload-dir` (follow-up to the Layer A/B change
in fusedio/fused-render#180 + fusedio/fused#335).

Status: **implemented** (option (a) — curated set under a single payload dir). Export emits
v2; `load_html_bundle` reads both v1 and v2. The "what's in the payload" sub-options below
are kept for context — v2 shipped with the curated set (page + entrypoints + assets +
discovered modules) and still enumerates each role in the manifest, rather than the
whole-folder ship of option (b). This doc remains the format reference and rationale.

## Problem

The v1 bundle (SPEC §18, `docs/EXPORT.md`) splits files into category directories:

```
bundle/
  page.html
  manifest.json
  code/<route>.py     # runPython targets, renamed to route slug
  assets/<key>        # rawUrl/readFile targets
  resources/<key>     # modules a bundled entrypoint imports
```

Those dirs made sense in the **original** scheme, where assets were served from
`<root>/assets/<key>` at runtime (via `openfused.asset_path`) — bundle storage
mirrored runtime layout. The Layer A change (#335) made the runtime key the file's
**real page-relative path** (`<root>/data.csv`, not `<root>/assets/data.csv`), so a
page's own `open("data.csv")` / `import helpers` resolve with no rewriting. That
decoupled bundle storage from runtime layout and left the category dirs **vestigial**:

- The manifest already carries each file's **role** (in `entrypoints`/`assets`/`resources`)
  and its **runtime key** (`name`/`key`). The category dir is a second, redundant encoding
  of the role, and the `file` field exists *only* to point at the relocated copy.
- Bundle layout no longer matches the runtime layout (which is flat, at the project root),
  so a reader has three layouts to reconcile instead of one.

Goal: **bundle layout == author's folder == runtime tree.** One layout, zero rearranging —
the same north star as Layer A.

## v2 layout

`manifest.json` at the bundle root; one **payload directory** holding the author's tree
verbatim (name: `files/`, deliberately *not* `resources/` — that word is already the
Layer B module role):

```
bundle/
  manifest.json
  files/                 # the author's folder, verbatim
    index.html
    sine.py
    helpers.py
    data.csv
    tiles/0.png
```

```json
{
  "fused_render_bundle": 2,
  "root": "files",
  "page": "index.html",
  "entrypoints": [{ "path": "sine.py", "name": "sine" }],
  "assets": ["data.csv", "tiles/0.png"]
}
```

Every path is **relative to the payload dir**, and is also its runtime key. The `file`
field is gone (it equals the path). The three category dirs collapse to one.

### The load-bearing rule

The payload prefix (`files/`) is a **bundle-only convention**. At build/serve time it is
**stripped**: `files/helpers.py` → runtime key `helpers.py` → lands at `<root>/helpers.py`.
The **runtime tree stays flat** (cwd + `sys.path[0]` = the project root), so Layer A keeps
working with **no handler change**. Do *not* set the runtime project root to the payload
subdir — that would require changing the per-call cwd/`sys.path` in every backend's handler
(`backends/aws/handler/handler.py` and the local equivalent), which is out of scope and
affects far more than fused-render pages.

## Role classification stays in the manifest

Layout is not role — a file's role is **semantic**, so the manifest still classifies:

| Manifest field | Role | At build | At runtime |
|---|---|---|---|
| `page` | the `_shell` HTML | embedded into the `_shell` route code | served as the page |
| `entrypoints[]` (`path` + `name`) | a `runPython` route | source embedded into the `<name>` route code | runs as `user_code.py` at the root |
| `assets[]` | web-served file | shipped as a `resource_file` | at `<root>/<path>`; **allow-listed** for the `_asset` route |
| *(every other payload file)* | runtime-only | shipped as a `resource_file` | at `<root>/<path>`; **not** web-served |

Two consequences:

- **Entrypoints are still embedded, not shipped as runtime files.** So the manifest must
  still tag which `.py` are routes (and their route names) — the payload dir alone can't
  say it. (This is also why route-name slugging/`-2` deduping stays a manifest concern; in
  v2 two entrypoints `a/run.py` and `b/run.py` sit at distinct real paths and only their
  *route names* need deduping — the storage collision the `code/` flattening created goes
  away.)
- **The explicit `resources`/modules list can disappear** — see the next section.

## What's in the payload: two sub-options

**(a) Curated set (conservative).** The payload dir holds only the selected set — page +
entrypoints + their `rawUrl`/`readFile` assets + the transitively-imported modules that
Layer B discovers — stored at real paths. Keeps Layer B's AST import scan. Smallest bundle;
no risk of shipping unrelated files.

**(b) Whole-folder ship (simplest).** The payload dir *is* the author's folder, and the
whole tree ships to the runtime root. Then `import helpers` resolves because `helpers.py`
is simply present — **Layer B's import discovery is no longer needed at all** (discovery
only existed because v1 ships a *selective* set). Cost: **over-bundling** — a `.env`, a
`.git/`, a large dataset, or a scratch file next to the page would all ship. Needs
guardrails: sensible ignore defaults (dotfiles, `__pycache__`, `node_modules`, `.git`) plus
the Deploy modal's existing `include`/`exclude` "Will publish" controls (SPEC §19, EX-6).

Recommendation: **(b) with ignore defaults + the existing include/exclude surface.** It is
the true "copy the folder, run from the folder" model and removes a whole subsystem (the
AST scanner). Fall back to (a) if conservative-by-default bundling is preferred over
zero-discovery simplicity. Either way the *layout* (single payload dir) is unchanged; this
axis only decides *what goes in it*.

## What still needs care

- **Reserved runtime-root names.** `_params.json`, `fused.py`, `openfused.py`,
  `_openfused.py`, `_runner.py`, `user_code.py`, `_result.json`/`_result_body` are written
  by the handler at the execution root. A payload file whose stripped key collides with one
  (at the root) must be rejected at build time — the guard already added in
  `project_deploy._reject_unsafe_asset_key` (#335) covers this and is reused unchanged.
  (Note this is a *runtime-root* concern, independent of bundle layout — v2 does not make it
  better or worse.)
- **Bundle-root names are no longer a hazard.** A payload file literally named
  `manifest.json` or `index.html`/`page.html` is safely inside `files/`, so it cannot
  collide with the bundle's own metadata — v2 *removes* the v1 need to special-case those.
- **Escape/symlink validation** within the payload dir (`..`, absolute, a symlink pointing
  outside the tree) — the existing `_within_page_dir` / `_read_bundled` checks apply per
  payload file, unchanged.
- **Manifest v2 + back-compat.** Bump the discriminator to `fused_render_bundle: 2`.
  `load_html_bundle` accepts **both**: v1 (category dirs, `file` fields) for already-exported
  bundles, v2 (payload dir, no `file`) going forward. Export emits v2 only. The Fused control
  plane rebuilds via its own pinned `load_html_bundle` + `build_html_artifact`, so a v2
  bundle needs a control plane new enough to parse v2 — the same service-version coordination
  point v1 already is (SPEC/`fused` spec §Limitations), not a new class of problem.

## Cross-repo implementation sketch

- **fused-render `export.py`** — emit `files/<tree>` + v2 manifest (`root`, `page`,
  `entrypoints`, `assets`), drop `file`. For option (b): copy the (filtered) folder wholesale
  and drop `_discover_modules`; for (a): keep it. `include`/`exclude` operate on payload-
  relative paths.
- **fused `project_deploy.load_html_bundle`** — parse v2 (strip `root`, read roles), keep the
  v1 branch. `build_html_artifact` is largely unchanged: it already keys `resource_files` by
  the real relative path (Layer A) and allow-lists only assets — v2 just feeds it
  payload-relative keys and a page/entrypoint/asset split with no `resources` list (option b)
  or the same split plus modules (option a).
- **fused `cli._read_html_bundle_source`** (Fused inline upload) — ship `manifest.json` + every
  payload file under its bundle-relative path (`files/<...>`); the control plane's
  `load_html_bundle` reads them back.
- **Docs/specs** — `fused` `spec/serve/fused-render.md` (bundle boundary), this repo's
  SPEC §18 + `docs/EXPORT.md`, and a `DECISIONS.md` entry.

## Non-goals

- **Layer C** (running each entrypoint as its real file via `runpy` so `__file__` and
  relative-package imports resolve) — orthogonal to bundle layout; tracked separately.
- Changing the runtime handler, the per-call project-root semantics, or the flat-runtime
  invariant. v2 is purely a **bundle-format** change; the runtime is untouched.
