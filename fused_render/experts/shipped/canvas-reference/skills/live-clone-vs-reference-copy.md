# Editing a live clone vs a reference copy
trigger: live clone, clone, reference copy, read-only, edit canvas, sync, publish, trash, hard pull, adopt, open canvas, user edits

## Telling them apart

- A live clone is the user's editable copy of a canvas, at `~/Fused/canvas/<env>/user/<name>/`, two-way synced by fused-render. Its `.fused-canvas.json` says `"sync": "live"`, or it has a `canvas.toml` and no readable marker.
- A reference copy is a read-only copy written by the mirror command. Its marker says `"sync": "mirror"`. Everything under `team/` and `public/` is a reference copy; under `user/` a folder is one when its marker says so.
- A marker that says `mirror` always wins: such a folder is never treated as a clone.
- While a reference copy under `user/` is being turned into a clone (the user opened it from the Canvases page), it is still read-only until the first pull has landed in it.

## Rules for a reference copy

- Read it freely. Never edit, create or delete files in it. Edits there are refused for agent sessions.
- Never run `fused workbench canvas push` or `pull` in it; both are refused.
- Nothing in it is synced or published, and the folder is overwritten when the canvas changes.
- To change that canvas, the user opens it from fused-render's Canvases page, which gives it an editable folder of its own.
- Copy patterns from it into the clone you are working on. Never point at its files from another canvas.

## Rules for a live clone

- The session only edits files. It never runs a sync command: the folder publishes itself when the turn ends, and server changes (edits in a hosted workbench tab) are merged in per file. While a turn runs a server change is held and merged when the turn ends.
- A change set left half-done at the end of a turn is published half-done. Finish multi-file changes (a rename: the `.py` file AND its `canvas.toml` row) within the turn.
- The user edits the same folder live from the canvas view beside the chat: adding, deleting, renaming, moving or resizing nodes, grouping them into folders and editing code all land in `canvas.toml` and the source files within a fraction of a second.
- Re-read `canvas.toml` right before every edit to it, and edit it with targeted edits only. Never rewrite the whole file: that throws away what the user did since it was read.
- When the session and the user change the same property of the same node at the same moment, the user's change is kept.
- A node file that vanished was deleted by the user. A copy is in `<env>/.sync/trash/<canvas>/<timestamp>/` (from the clone: `../../.sync/trash/<canvas>/<timestamp>/`). Do not recreate it unless the user asks. A recreated file with no row stays off the canvas.
- A Hard pull replaces the clone with the server's copy, after a backup into the same trash folder. Restore from the trash only what the user asks for.
- Verify work with the `canvas_screenshot` tool (`what: "canvas"` for the whole graph, `what: "udf"` for one node's rendered output) and `fused workbench canvas validate .`. If a publish failed, the canvas view shows why; fixing the files lets the next publish ship the fix.

## Files fused-render seeds into a clone

`CLAUDE.md` (the session's instructions for this clone, naming the `workbench:` skills to load first), `.fusedignore` (lists `CLAUDE.md`, `.fusedignore` and `.fused-canvas.json` so a push never ships them) and the `.fused-canvas.json` marker. They are rewritten after every pull. Do not treat them as nodes.
