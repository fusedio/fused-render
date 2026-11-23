# Connecting nodes: edges and folders
trigger: edge, edges, connect, connection, upstream, downstream, link nodes, udf-folder, folder, group nodes, childUdfOrder, folderName

## Edges

- Edges live in `[canvas]` as `edges`, a list of two-name pairs: `edges = [["a", "b"], ["b", "c"]]`.
- Each pair is `[source, target]`, both given by the node's `udfName` (not by a server id or file name).
- Both ends must be rows in `canvas.toml`. A dangling edge (a name with no row) is rejected by the push's validation. An entry that is not a pair is skipped by readers.
- When a node is deleted, its edges go with it. When a node is renamed, every edge endpoint naming it must be renamed in the same change set.
- Sessions in a clone are told to run `fused workbench canvas validate .` after adding or removing nodes or edges.

## What the repo does not cover

How a UDF's Python code reads the output of the node an edge comes from is not described anywhere in this specialist's sources. Do not answer it from memory. Search the canvas copies for a pair of connected nodes (find an `edges` entry in a `canvas.toml`, then read both nodes' `.py` files) and show what that canvas does, citing both paths. If no copy shows it, say you are unsure. Sessions in a clone are also handed the `workbench:fused-udfs` skill for writing UDF code.

## Folders (node groups on the canvas)

A canvas folder is a `[[canvas.nodes]]` row, not a directory on disk:

```toml
[[canvas.nodes]]
udfName = "grp"
type = "udf-folder"
folderName = "Group"
folderColor = "#11223344"
childUdfOrder = ["a", "b"]
isLocked = true
x = -20.0
y = -20.0
width = 540.0
height = 180.0
```

Rules:

- A folder is named by its `folderName`. Give the row `udfName` equal to its `folderName`.
- Membership lives ONLY in the folder's `childUdfOrder`: the ordered `udfName`s of the nodes inside it. A child row has no pointer back to its folder.
- One folder per child. A folder never lists another folder (no nesting).
- A child's `x`/`y` are RELATIVE to its folder's `x`/`y`. A node in no folder has absolute coordinates. Moving a node into or out of a folder means converting its coordinates.
- A folder has no source file. Its child nodes are still root-level files in the canvas folder.
- Deleting or renaming a node means removing or renaming it in its folder's `childUdfOrder` too. A stale name there is tolerated by the server but is a bug to fix.
- Problems fused-render reports in folder rows: a folder row with neither `folderName` nor `udfName`, a `childUdfOrder` that is not a list, an entry that is not a name or that names a folder, a name listed twice by one folder or by two folders.
- After re-arranging folders, sessions in a clone are told to take one `canvas_screenshot` with `what: "canvas"` and fix anything missing, misplaced or overlapping.

## Finding an example

Search the canvas copies' `canvas.toml` files for `udf-folder` to find canvases that use folders, and read the `edges` line of a `canvas.toml` to see which of its nodes are connected (an empty `edges = []` means none).
