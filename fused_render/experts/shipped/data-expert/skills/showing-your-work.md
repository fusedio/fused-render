# Showing your work: canvases, UDFs and widgets in the chat
trigger: show, show me, visualize, chart, plot, map, table, widget, dashboard, build a canvas, make a canvas, new canvas, canvas_create, card, open it, improve the widget, change the chart, json widget, write a udf for me

In your chat with the user you may have two tools besides the usual ones: `canvas_create` and `show` (their full names start with `mcp__bot__`). The message that starts your session says so. A task session's question never has them, and neither does a routine's turn: there, describe what you would build.

Use them when the user asks to see something, to have something built, or to change something you showed. Do not use them to answer a question a sentence answers, and never to run something once (that is your scratch folder, see `running-udfs.md`).

## The steps

1. **A canvas to work in.** `canvas_create` with a short name (letters, digits, underscores; say what it is, for example `Parcels_overview`). It makes the canvas on the user's Fused account and its folder on this machine, and returns the folder's path. Reuse a canvas you made earlier in this conversation for more of the same work; make a new one for a new subject. If the name is taken, pick another.
2. **Write the nodes.** One file per node at the folder's root, named after the node: `<name>.py` for a UDF, `<name>.json` for a widget. Use Write / Edit. In a canvas you made these writes need no approval.
3. **Their rows.** Each node needs a `[[canvas.nodes]]` row in the folder's `canvas.toml` with `udfName = "<name>"` (the file's name without its suffix), a position and a size. Re-read `canvas.toml` right before you edit it and change it with a targeted Edit; never rewrite the whole file. An edge from a UDF to the widget that reads it goes in `edges`.
4. **Check.** `fused workbench --env <env> canvas validate <folder>`. Fix what it reports. For a UDF, run it once and look at the output before you show it: `fused workbench --env <env> run --engine remote <canvas> <folder>/<name>.py`.
5. **Show.** `show` with the absolute path of the node's file (for one UDF or widget) or of the canvas folder (for the whole canvas). A card appears in the chat; the user opens it and sees the node live. Then say in one line what it shows. Do not paste the code unless asked, and do not explain how to open the card.

Finish steps 2 to 4 within one turn: the folder publishes itself, and a half-written change is published half-written.

## The formats: never guess them

- `canvas.toml` and the row keys: your skill on the canvas folder layout if you have one, and the format references the message that starts your session points to (a folder of `workbench` skills: `canvas-toml`, `fused-udfs`, `json-ui-schemas`). Read the one for the kind of file you are about to write.
- A widget's JSON is a fixed schema (JSON-UI). Write one only from the `json-ui-schemas` reference or by adapting a working widget: Grep the canvas copies you may read for `*.json` node files and copy the nearest one. If you have neither a reference nor an example, say so and show the UDF instead; never invent widget JSON.
- A UDF is a `.py` file with one `@fused.udf` function. Copy the shape from a UDF in the canvas copies.

## When the user asks for a change

They are looking at what you showed. Edit the same file (and its row if the node's name, size or links change), validate, and say what changed. The open view follows the files within a moment, so there is no need to call `show` again for the same node; call it for a node you had not shown yet.

## What you may write, and what asks first

- Without asking: node files (`.py`, `.json`, `.html`, `.md`) and `canvas.toml` at the root of a canvas you made with `canvas_create`.
- Any other canvas of the user's: you may `show` any node of a canvas under a `user/` folder, but every edit there raises an Approve / Deny card for the user. Call the edit when the user asked for the change; do not ask them to approve in words first.
- Never edit a reference copy (`team/`, `public/`, or a `user/` folder whose marker says `mirror`), never run `canvas push` or `pull`, and never write `CLAUDE.md`, `.fusedignore` or `.fused-canvas.json` in a canvas folder.
- A UDF you write for the user must not write, delete, upload or send anything unless they asked for exactly that.

## When it does not work

- `canvas_create` says canvases are not available, or the user is not signed in: tell the user (they sign in on the Canvases page). Do not try the `fused` CLI's own create.
- `show` says the file is not a node: its row is missing from `canvas.toml`, or the file is not at the root of a canvas folder under `user/`.
- What you learn about what works (a widget shape that rendered, a row key that validation refused) goes to `memory.md`, like everything else you learn.
