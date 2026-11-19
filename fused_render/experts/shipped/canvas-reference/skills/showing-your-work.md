# Showing your work: canvases, UDFs and widgets in the chat
trigger: show, show me, visualize, chart, plot, map, table, widget, dashboard, build a canvas, make a canvas, new canvas, canvas_create, card, open it, improve the widget, change the chart, json widget, write a udf for me

In your chat with the user you may have two tools besides the usual ones: `canvas_create` and `show` (their full names start with `mcp__bot__`). The message that starts your session says so. A task session's question never has them, and neither does a routine's turn: there, describe what you would build.

Use them when the user asks to see something, to have something built, or to change something you showed. Do not use them to answer a question a sentence answers, and never to run something once (that is your scratch folder, see `running-udfs.md`).

## The steps

1. **A canvas to work in.** `canvas_create` with a short name (letters, digits, underscores; say what it is, for example `Parcels_overview`). It makes the canvas on the user's Fused account and its folder on this machine, so it first shows the user an Approve / Deny card. Call it when the user asked for something to be built; do not ask them in words first. If they deny it, do not call it again for that: say what you could not do, or answer without a canvas. It returns the folder's path and the paths of the format references (below). Reuse a canvas you made earlier in this conversation for more of the same work; make a new one for a new subject. If the name is taken, pick another.
2. **Write the nodes.** First read the format reference for the kind of file (below). One file per node at the folder's root, named after the node: `<name>.py` for a UDF, `<name>.json` for a widget. Use Write / Edit. In a canvas you made these writes need no approval and raise no card.
3. **Their rows.** Each node needs a `[[canvas.nodes]]` row in the folder's `canvas.toml` with `udfName = "<name>"` (the file's name without its suffix), a position and a size. Re-read `canvas.toml` right before you edit it and change it with a targeted Edit; never rewrite the whole file. An edge from a UDF to the widget that reads it goes in `edges`.
4. **Check.** `fused workbench --env <env> canvas validate <folder>`. Fix what it reports. For a UDF, run it once and look at the output before you show it: `fused workbench --env <env> run --engine remote <canvas> <folder>/<name>.py`.
5. **Show.** `show` with the absolute path of the node's file (for one UDF or widget) or of the canvas folder (for the whole canvas). A card appears in the chat; the user opens it and sees the node live. Then say in one line what it shows. Do not paste the code unless asked, and do not explain how to open the card.

Finish steps 2 to 4 within one turn: the folder publishes itself, and a half-written change is published half-written.

## The formats: never guess them

The formats are the `workbench` skills, fetched to this machine for canvas work. The message that starts your session (and `canvas_create`'s result) gives their absolute paths; when it says they are not there yet, `canvas_create`'s result names them; write nothing before you have them. Read the matching one BEFORE you write each kind of file, every session (they change between releases, and their folder name changes with them):

- A widget (`<name>.json`): `skills/json-ui-schemas/SKILL.md` (the envelope, the rules, which widget does what), then the widget's own schema in `skills/json-ui-schemas/reference.md`. That file is long: Grep it for the heading `## <type>` (for example `## sql-table`) and read that section only. Where `fused workbench --env <env> json-ui schemas <type>` answers, it wins over that file. Check a widget with `fused workbench --env <env> json-ui validate <file>`.
- `canvas.toml` and its rows, edges and the folder layout: `skills/canvas-toml/SKILL.md`.
- A UDF (`<name>.py`): `skills/fused-udfs/SKILL.md`.
- An HTML node (`<name>.html`): `skills/html-template-nodes/SKILL.md`.

These texts are written for a session working in a live canvas clone, which is what a canvas you made is. So what they say about the clone applies to you: the folder publishes itself, never run `canvas push` or `pull`. Where they name a `canvas_screenshot` tool, you have none: `show` the node instead.

If you have no reference (it could not be fetched), never invent widget JSON: adapt a working widget (Grep the canvas copies you may read for `*.json` node files and copy the nearest one), or show the UDF only and say so.

## When the user asks for a change

They are looking at what you showed. Edit the same file (and its row if the node's name, size or links change), validate, and say what changed. The open view follows the files within a moment, so there is no need to call `show` again for the same node; call it for a node you had not shown yet.

## What you may write, and what asks first

- Creating a canvas asks the user (the card of step 1).
- Without asking: node files (`.py`, `.json`, `.html`, `.md`) and `canvas.toml` at the root of a canvas you made with `canvas_create`.
- Any other canvas of the user's: you may `show` any node of a canvas under a `user/` folder, but every edit there raises an Approve / Deny card for the user. Call the edit when the user asked for the change; do not ask them to approve in words first.
- Never edit a reference copy (`team/`, `public/`, or a `user/` folder whose marker says `mirror`), never run `canvas push` or `pull`, and never write `CLAUDE.md`, `.fusedignore` or `.fused-canvas.json` in a canvas folder.
- A UDF you write for the user must not write, delete, upload or send anything unless they asked for exactly that.

## When it does not work

- `canvas_create` says canvases are not available, or the user is not signed in: tell the user (they sign in on the Canvases page). Do not try the `fused` CLI's own create.
- `show` says the file is not a node: its row is missing from `canvas.toml`, or the file is not at the root of a canvas folder under `user/`.
- What you learn about what works (a widget shape that rendered, a row key that validation refused) goes to `memory.md`, like everything else you learn.
