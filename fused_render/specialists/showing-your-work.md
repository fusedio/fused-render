# Showing your work: canvases, UDFs and widgets in the chat

In your main chat with the user you may have two tools besides the usual ones: `mcp__specialist_canvas__canvas_create` and `mcp__specialist_canvas__show`. The message that starts your session says so. A breakout and a learning run never have them: there, describe what you would build.

Use them when the user asks to see something, to have something built, or to change something you showed. Do not use them to answer a question a sentence answers, and never to run something once.

## The steps

1. **A canvas to work in.** `canvas_create` with a short name (letters, digits, underscores; say what it is, for example `Parcels_overview`). The user is shown a card and approves each new canvas; if they decline, do not try again unless they ask. It makes the canvas on the user's Fused account and its folder on this machine, and returns the folder's path. Leave `env` out: the canvas is made in the user's default environment. Give `env` only when the user explicitly asked for a certain environment. Reuse a canvas you made earlier in this conversation for more of the same work; make a new one for a new subject. If the name is taken, pick another.
2. **Write the nodes.** One file per node at the folder's root, named after the node: `<name>.py` for a UDF, `<name>.json` for a widget. Use Write / Edit.
3. **Their rows.** Each node needs a `[[canvas.nodes]]` row in the folder's `canvas.toml` with `udfName = "<name>"` (the file's name without its suffix), a position and a size. Re-read `canvas.toml` right before you edit it and change it with a targeted Edit; never rewrite the whole file. An edge from a UDF to the widget that reads it goes in `edges`.
4. **Check.** `fused workbench --env <env> canvas validate <folder>`. Fix what it reports. For a UDF, run it once and look at the output before you show it: `fused workbench --env <env> run --engine remote <canvas> <folder>/<name>.py`.
5. **Show.** `show` with the absolute path of the node's file (for one UDF or widget) or of the canvas folder (for the whole canvas), and a `note` of one short line saying what it is. A card appears in the chat; the user opens it and sees the node live. Then say in one line what it shows. Do not paste the code unless asked, and do not explain how to open the card.

Finish steps 2 to 4 within one turn: the folder publishes itself to the user's Fused account, and a half-written change is published half-written.

## The formats: never guess them

- `canvas.toml` and the row keys: your skill on the canvas folder layout if you have one, else a canvas under the user's canvas copies (`~/Fused/canvas/<env>/user/`). Read a working one before you write yours.
- A widget's JSON is a fixed schema (JSON-UI). Write one only from a format reference you were given or by adapting a working widget: Grep the canvas copies for `*.json` node files and copy the nearest one. If you have neither a reference nor an example, say so and show the UDF instead; never invent widget JSON.
- A UDF is a `.py` file with one `@fused.udf` function. Copy the shape from a UDF in the canvas copies.

## When the user asks for a change

They are looking at what you showed. Edit the same file (and its row if the node's name, size or links change), validate, and say what changed. The open view follows the files within a moment, so there is no need to call `show` again for the same node; call it for a node you had not shown yet.

## What to leave alone

- Any other canvas of the user's: you may `show` any node of a canvas under a `user/` folder, but change one only when the user asked for that change.
- Never edit a reference copy (`team/`, `public/`, or a `user/` folder whose marker says `mirror`), never run `canvas push` or `pull`, and never write `CLAUDE.md`, `.fusedignore` or `.fused-canvas.json` in a canvas folder.
- A UDF you write for the user must not write, delete, upload or send anything unless they asked for exactly that.

## When it does not work

- `canvas_create` says canvases are not available, or the user is not signed in: tell the user (they sign in on the Canvases page). Do not try the `fused` CLI's own create.
- `show` says the file is not a node: its row is missing from `canvas.toml`, or the file is not at the root of a canvas folder under `user/`.
- What you learn about what works (a widget shape that rendered, a row key that validation refused) goes to `memory.md`, like everything else you learn.
