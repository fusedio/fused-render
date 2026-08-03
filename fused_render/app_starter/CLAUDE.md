# fused-render app

This folder is a **fused-render app** — a self-contained folder rendered by
the fused-render explorer. `index.html` is the app's entry view; it was
scaffolded from the starter kit, so edit it in place (don't create a second
top-level `.html` next to it — one entry file is what makes the folder open
as an app).

The page runs inside the explorer, which injects a `fused` runtime bridge:
`fused.params` (URL-synced view state), `fused.runPython("./file.py", args)`
(compute in Python files beside this one), `fused.readFile` / `fused.rawUrl`,
and more. There is no network at runtime and no build step.

Before non-trivial changes, invoke the **`fused-render-authoring`** skill —
the full contract for `.html` views and `.py` data files: the `fused` bridge,
params-as-state wiring, file IO, theming, and debugging blank views /
traceback overlays.

## Version control

This folder is a local git repository (initialised at creation with the
starter as its first commit). **Commit as you work, in small chunks** — after
every coherent change, even tiny ones (a copy tweak, a single style fix, one
function). Never batch a whole task into one commit, and never leave the tree
dirty at the end of a turn: finish every turn with `git add -A` and a commit.
Use short imperative subjects ("Add dark theme toggle", "Fix param sync").
Don't push, don't add remotes, don't rewrite history — this repo is purely
local undo history for the app.

fused-render installs that skill (and its siblings, `fused-render-usage` and
`fused-render-custom-templates`) into Claude Code's user-level skills
directory and keeps them up to date, so it is available here by name. If the
skill isn't listed, start (or restart) fused-render once — the server
re-installs it on startup.
