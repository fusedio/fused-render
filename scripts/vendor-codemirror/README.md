# CodeMirror 6 vendor build

`code/template.html` uses a single self-contained CodeMirror 6 bundle
(`fused_render/templates/vendor/codemirror.bundle.js`) so the product stays
fully local at runtime — no CDN, no module loader in the browser.

This directory is the build workspace that produces that bundle. Only the
built `codemirror.bundle.js` is committed; `node_modules/` here is gitignored.

## Regenerate

Node 22 is required (on the dev machine: `/opt/homebrew/opt/node@22/bin`):

```sh
PATH="/opt/homebrew/opt/node@22/bin:$PATH" ./build.sh
```

`build.sh` runs `npm install` then esbuild, emitting an IIFE that assigns a
global `CM`. `entry.js` lists everything the template consumes (`EditorView`,
`EditorState`, `basicSetup`, the language functions, and the `StreamLanguage`
legacy modes for shell/toml). Anything not re-exported from `entry.js` is
tree-shaken away, so add an export there before using a new CodeMirror API in
the template.

Read-only editing is achieved in the template via `CM.EditorState.readOnly.of(true)`
and `CM.EditorView.editable.of(false)` — both facets ride on the exported
`EditorState`/`EditorView`, so nothing extra needs exporting for that.

## Probes

- `code-editor-probe.mjs` — runs `code/template.html`'s helpers against the
  **committed** bundle (no `node_modules` needed); driven by
  `tests/test_code_template.py` (SPEC §51 CE-8). Its `everyLanguageBuilds`
  case fails if a language the template lists is missing from `entry.js`.
- `live-preview-probe.mjs` / `renumber-probe.mjs` — the markdown template's,
  via `template-harness.mjs`; these need `node_modules`.

## Bundle size

Every view that loads this bundle pays for every grammar in it. Prefer a
`@codemirror/legacy-modes` stream mode (re-exported through `legacyModes`,
wrapped on demand by the code template) over a full Lezer package unless the
language needs what only a syntax tree gives; see SPEC §51 CE-1.
