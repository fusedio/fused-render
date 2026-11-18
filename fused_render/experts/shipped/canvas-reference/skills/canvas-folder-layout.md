# Canvas folder layout and canvas.toml
trigger: canvas.toml, canvas folder, layout, node file, udfName, viewport, canvas.nodes, file layout, folder structure, text note, widget

## Where canvases live

- Every Fused environment has a root `~/Fused/canvas/<env>/`. The environments are `unstable`, `prod` and `custom`.
- The root holds three scope folders: `user/` (the user's own canvases), `team/` (their team's) and `public/` (the public catalog), plus a `.sync/` folder of bookkeeping (locks, merge base, trash). `.sync/` is not a canvas.
- One canvas is one folder: `~/Fused/canvas/<env>/<scope>/<Canvas_Name>/`. Only `user/<name>` can be the user's editable clone; `team/` and `public/` only ever hold read-only reference copies.

## What is in a canvas folder

- `canvas.toml`: defines the canvas (nodes, edges, viewport, comments).
- One source file per node at the folder root, named after the node's `udfName`: `<udfName>.py` for a UDF, `.json` for a JSON-UI widget, `.html` for an HTML node, `.md` for a text note. A node inside a canvas folder (group) is still a root-level file, never a subdirectory.
- `.fused-canvas.json`: the ownership marker (see the reference-canvas skill).
- In a live clone only: `CLAUDE.md` and `.fusedignore`, seeded by fused-render. They are local files, never pushed.
- A `.py` file whose name starts with `_` (for example `_helpers.py`) is not a node source. Files listed in `.fusedignore` are not node sources either.

## canvas.toml shape

The file is recognised as a canvas only when it is named `canvas.toml` and declares `type = "canvas"` at the top level. A real example (from the repo's test fixture):

```toml
type = "canvas"
version = 2
name = "Test canvas"

[canvas]
edges = [["a", "b"], ["b", "c"]]

[canvas.viewport]
x = 12.0
y = 34.0
zoom = 0.5

[[canvas.nodes]]
udfName = "a"
title = "Node A"
description = "first"
x = 0.0
y = 0.0
zIndex = 1
width = 200.0
height = 120.0

[[canvas.nodes]]
udfName = "b"
x = 300.0
y = 0.0
zIndex = 2
width = 200.0
height = 120.0
visible = false
```

`[canvas]` may also carry `comments = []` and `viewportBounds`.

## Node row keys

Each node is one `[[canvas.nodes]]` row, keyed by `udfName`. Keys a row may carry: `udfName`, `x`, `y`, `zIndex`, `width`, `height`, `type`, `title`, `description`, `visible`, `isLocked`; display keys `textBoxColor`, `textBoxGradient`, `textBoxScaleFactor`, `textBoxAlignment`, `nodeDescriptionScaleFactor`, `widgetBackgroundColor`; folder keys `folderName`, `folderColor`, `childUdfOrder`; catalog keys `assetUrl`, `tags`, `filePreview`, `filePreviewExtensions`, `filePreviewFileName`, `filePreviewRegex`, `filePreviewMenuOrder`.

- `type` is omitted for an ordinary UDF node (its type is `simple-udf`). A folder row has `type = "udf-folder"`.
- `title` defaults to the `udfName`.

## Rows and files must agree

- Every node needs its source file next to `canvas.toml`.
- A file with no row gets a default row at push. Write the row yourself when the node's position matters. The one exception: the file of a node the user deleted, written back without a row, stays off the canvas while that node's tombstone is kept (a tombstone whose file stays absent is dropped after seven days; then the file gets a default row like any other; see the live-clone skill).
- To remove a node, delete its file AND its row. A node whose file is missing is removed from the server at the next push.
- A rename is the file AND the row's `udfName` (and every edge and `childUdfOrder` entry naming it), done together in one change set.
- Check the folder with `fused workbench canvas validate .` after structural edits. An invalid canvas is rejected at push time.

## Default rows fused-render writes

When fused-render adds a row for a file that has none, it uses these defaults: `title` = the file's stem, `visible = true`; a `.md` note gets `textBoxColor = "#FFF9C4"`, `textBoxGradient = true`, `textBoxScaleFactor = 1.0`, `textBoxAlignment = "left"` and a 350 x 250 box; a `.json` widget gets `nodeDescriptionScaleFactor = 1.5` and `widgetBackgroundColor = "#2e2e2e"`; `.py` and `.html` get `nodeDescriptionScaleFactor = 1.5`.

## Authoritative format reference

Sessions working in a clone are handed the `workbench:canvas-toml` skill, which the clone's CLAUDE.md names as the authoritative canvas.toml reference. When a caller has it, point them to it for required fields not listed here.
