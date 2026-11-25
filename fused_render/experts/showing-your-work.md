# Showing your work: canvases, UDFs and widgets in the chat

In your main chat with the user you may have two tools besides the usual ones: `mcp__expert_canvas__canvas_create` and `mcp__expert_canvas__show`. The message that starts your session says so. A breakout and a learning run never have them: there, describe what you would build.

Use them when the user asks to see something, to have something built, or to change something you showed. Do not use them to answer a question a sentence answers, and never to run something once.

## The one way to make a canvas

A new canvas is made with `canvas_create` and nothing else. Never run the `fused` CLI's `canvas create`, `clone`, `pull` or `push`, and never make a folder under a `user/` folder yourself, even when that looks quicker or the tool has just failed. Only the tool asks the user, registers the canvas with the app and starts its sync. A canvas made any other way is not synced, was never approved, and can open with an error.

If `canvas_create` is declined, refused or fails, tell the user what it said and stop there. Do not work around it.

## The steps

1. **A canvas to work in.** `canvas_create` with a short name (letters, digits, underscores; say what it is, for example `Parcels_overview`). The user is shown a card and approves each new canvas; if they decline, do not try again unless they ask. It makes the canvas on the user's Fused account and its folder on this machine, and returns the folder's path. Always give `env`: the user's default environment, which the tool's description names, unless the user explicitly asked for a certain environment. The card they approve shows the name and the environment. Reuse a canvas you made earlier in this conversation for more of the same work; make a new one for a new subject. If the name is taken, pick another.
2. **Write the nodes.** One file per node at the folder's root, named after the node: `<name>.py` for a UDF, `<name>.json` for a widget. Use Write / Edit.
3. **Their rows.** Each node needs a `[[canvas.nodes]]` row in the folder's `canvas.toml` with `udfName = "<name>"` (the file's name without its suffix), a position and a size. Re-read `canvas.toml` right before you edit it and change it with a targeted Edit; never rewrite the whole file. An edge from a UDF to the widget that reads it goes in `edges`.
4. **Check.** `fused workbench --env <env> canvas validate <folder>`. Fix what it reports. For a UDF, run it once and look at the output before you show it: `fused workbench --env <env> run --engine remote <canvas> <folder>/<name>.py`.
5. **Show.** `show` with the absolute path of the node's file (for one UDF or widget) or of the canvas folder (for the whole canvas), and a `note` of one short line saying what it is. A card appears in the chat. A node's card draws the node live right there, which runs a UDF; a canvas card opens on a click. Then say in one line what it shows. Do not paste the code unless asked, and do not explain how to open the card.

Finish steps 2 to 4 within one turn: the folder publishes itself to the user's Fused account, and a half-written change is published half-written.

## Heavy UDFs: do not run them on the user's behalf

A node's card loads by itself when it appears, and loading a UDF's card runs the UDF on the user's Fused account. That is right for something quick. For a heavy one, call `show` with `live: false`: the card then waits, and nothing runs until the user clicks "Load live".

Give `live: false` when the UDF is slow or computes a lot of data: it took more than a few seconds when you ran it in step 4, it reads a large area, a long date range or a big table, it fans out over many inputs, or you could not run it to the end. Say in your one line that it is not loaded yet and why ("heavy: reads the whole state"). Prefer making the UDF light first (a smaller default area, a row limit, a sample) and showing that live. A widget that reads such a UDF is as heavy as the UDF.

## The formats: never guess them

When the message that starts your session says you have the `workbench` skills, invoke the one for a file **before** you write or change that file, every time, however small the change:

- `workbench:json-ui-schemas` for any `.json` widget. A widget's JSON is a fixed schema (JSON-UI): each component takes only the props its schema lists, and one unknown prop makes the whole widget show a validation error instead of rendering. Take every component and prop from the skill, never from memory or from what looks natural.
- `workbench:fused-udfs` for any `.py` UDF.
- `workbench:canvas-toml` before the first `canvas.toml` edit.
- `workbench:html-template-nodes` for any `.html` node.

`canvas validate` does not check a widget's components or props, so a canvas that validates can still hold a widget that fails to render. After writing a widget, re-read it against the schema: every `type` is a component the skill lists, and every key under `props` is one that component lists.

Without those skills: copy `canvas.toml` rows and the UDF shape from a working canvas under the user's canvas copies (`~/Fused/canvas/<env>/user/`), and write a widget only by adapting a working one (Grep the canvas copies for `*.json` node files and copy the nearest). If you have neither a skill nor an example, say so and show the UDF instead; never invent widget JSON.

## When the user asks for a change

They are looking at what you showed. Edit the same file (and its row if the node's name, size or links change), validate, and say what changed. The open view follows the files within a moment, so there is no need to call `show` again for the same node; call it for a node you had not shown yet.

## What to leave alone

- Any other canvas of the user's: you may `show` any node of a canvas under a `user/` folder, but change one only when the user asked for that change.
- Never edit a reference copy (`team/`, `public/`, or a `user/` folder whose marker says `mirror`), never run `canvas push` or `pull`, and never write `CLAUDE.md`, `.fusedignore` or `.fused-canvas.json` in a canvas folder.
- A UDF you write for the user must not write, delete, upload or send anything unless they asked for exactly that.

## When it does not work

- `canvas_create` says canvases are not available, the user is not signed in, or the folder could not be made: tell the user what it said (they sign in on the Canvases page) and stop. Do not make the canvas or its folder another way.
- `show` says the file is not a node: its row is missing from `canvas.toml`, or the file is not at the root of a canvas folder under `user/`.
- What you learn about what works (a widget shape that rendered, a row key that validation refused) goes to `memory.md`, like everything else you learn.
