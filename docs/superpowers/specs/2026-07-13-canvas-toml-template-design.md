# Canvas.toml conditional preview template — design

Date: 2026-07-13. Status: approved. First consumer of the CT-12 conditional-template
mechanism (PR #100).

## Goal

Opening a Fused canvas definition (`canvas.toml`) in fused-render shows a read-only
**layout viewer** as the default mode: nodes drawn as positioned boxes, folder groups,
edges, pan/zoom honoring the stored viewport. Non-canvas `.toml` files are untouched
(condition gate filters the mode per file).

## Files (all new, `fused_render/templates/canvas/`)

- `condition.py` — `def method(target_path)`: True iff file basename is `canvas.toml`
  (cheap pre-check) AND tomllib-parses with `type == "canvas"`. Any exception → False
  (CT-12 fail-closed does this anyway; return False explicitly on parse errors).
  Size guard: skip files > 2 MB.
- `reader.py` — `@fused.udf def main(file: str) -> dict`: tomllib parse → JSON dict
  `{name, nodes, folders, edges, viewport, viewportBounds, siblings}`. `siblings` maps
  each node's `udfName` → list of sibling file extensions present next to the toml
  (`.py`, `.json`, `.md`, `.html`) — one os.listdir of the toml's dir. Self-contained
  main (engine isolation rule), stdlib only.
- `template.html` — the viewer (see below).
- `icon.svg` — simple nodes-and-edges glyph for the mode switcher.

Registry: `".toml": ["canvas", "code", "annotate"]` (canvas first = default when the
condition passes; filtered out otherwise so `code` stays default for plain toml).

## Viewer (template.html)

- `_file` → `runPython("./reader.py", {file})` once; render from the returned JSON.
- Single full-viewport `<canvas>` (2D). World space = toml coordinates.
- Render order: folder nodes (rounded rects, `folderColor` fill w/ alpha, name label)
  → edges (lines between node rect borders, subtle arrowhead) → UDF nodes (rounded
  rect, title, udfName subtitle, sibling-extension badges, `visible=false` shown
  ghosted at 40% alpha).
- Camera: start from `[canvas.viewport]` x/y/zoom when present, else fit-to-bounds of
  all nodes with 10% margin. Wheel = zoom to cursor, drag = pan. "Fit" button resets.
- URL params (canonical wiring): `cx`, `cy`, `z` — camera state, written on interaction
  (150 ms debounce), read on load (they override the toml viewport) → refresh/share
  preserves the exact camera. Params are strings; parse at boundary.
- Hit-test on click: node → footer panel shows title, description, size, sibling files
  as links opening `/view/<abs sibling path>` in a new tab. Folder → name + child list.
  Click empty space clears.
- Header strip: canvas name (toml `name` or folder name), node/edge counts.
- Empty/broken states: no nodes → centered "empty canvas" note; reader error → let the
  traceback overlay show (default), plus the header still renders.
- Dark theme matching explorer; no external assets; ES2020; no runtime.js script tag.

## Docs

- SPEC.md: new numbered section following house style (model on §24 history view):
  template inventory line + behaviors (condition gate, camera params, sibling links).
- DECISIONS.md entry only if a nontrivial call needs recording (expected: default-mode
  ordering for .toml + condition content-sniff) — follow repo convention.

## Tests

Follow PR #100's test conventions (tests/ dir, FastAPI TestClient):
- condition: canvas.toml fixture → `canvas` mode present & first; plain .toml →
  absent; malformed toml → absent (fail-closed); file named other-than-canvas.toml
  with canvas content → per condition.py rule (basename check → absent).
- reader: golden parse of a fixture canvas folder (nodes/edges/siblings shape).

## Out of scope (v0)

Editing/writing the toml, rendering widget file contents inside nodes, executing UDFs,
in-shell navigation to siblings (new-tab links only), non-v2 canvas versions.
