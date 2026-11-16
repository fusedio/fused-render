## What files are in a canvas folder on disk?
- must: names `canvas.toml` as the file that defines nodes, edges and viewport
- must: says each node has one source file at the folder root named after its `udfName` (`.py`, `.json`, `.html` or `.md`)
- must: mentions the `.fused-canvas.json` marker

## How do I connect node `load` to node `plot` in canvas.toml?
- must: adds a `["load", "plot"]` pair to the `edges` list under `[canvas]`, as `[source, target]` udfNames
- must: says both names must be rows in `canvas.toml` (a dangling edge is rejected at push)

## How do I put two nodes into a folder on the canvas?
- must: describes a `[[canvas.nodes]]` row with `type = "udf-folder"`, a `folderName` and a `childUdfOrder` listing the nodes
- must: says a child's `x`/`y` are relative to the folder's `x`/`y`
- must: says the folder is not a directory on disk and the node files stay at the canvas folder root

## I renamed `clean.py` to `clean_rows.py`. What else do I have to change?
- must: renames the row's `udfName` in `canvas.toml` in the same change set
- must: renames the name in every edge and in any folder's `childUdfOrder`

## Is `_helpers.py` in a canvas folder a node?
- must: says no, a `.py` file whose name starts with `_` is not a node source

## What does `"sync": "mirror"` in `.fused-canvas.json` mean, and how is it different from `"live"`?
- must: says `mirror` is a read-only reference copy written by the mirror command
- must: says `live` is the user's clone that fused-render two-way syncs, only under `user/`

## Where would I look for an example of a canvas that groups nodes into folders?
- must: searches the `canvas.toml` files under `~/Fused/canvas/<env>/` for `udf-folder`
- must: cites the full path of a real canvas copy, or says plainly that none was found

## Where would I look for an example of a UDF that reads a Parquet file?
- must: searches the `.py` files in the canvas copies, `user/` before `team/` before `public/`
- must: cites the full path of a real file it opened, or says plainly that none was found

## How do I check from the command line that my canvas is valid?
- must: gives `fused workbench canvas validate .` (or with the canvas folder path)

## Should I run `fused workbench canvas push` in my canvas clone after editing?
- must: says no: the clone publishes itself when the turn ends
- must: says a push there only prints the sync state and a pull there is refused

## Can I fix a bug in a canvas under `~/Fused/canvas/prod/public/`?
- must: says no, it is a read-only reference copy; edits and push or pull there are refused
- must: says to copy the pattern into the user's own canvas, and that changing that canvas means opening it from the Canvases page

## How should an agent run the fused CLI, and what if it gets an auth error?
- must: says to run the bare `fused ...` command, not `pip install fused` or `python -m fused`
- must: says never to run `fused workbench login` or `fused cloud login`; ask the user to sign in instead

## What is the maximum number of nodes a canvas can hold?
- must: says it is not sure or that this is not covered
- must: does not invent a number or an API

## What rate limit does the Fused server apply to `canvas push` requests?
- must: says it is not sure or that this is not covered
- must: does not invent a limit, flag or API
