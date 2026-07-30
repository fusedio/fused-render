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

Before non-trivial changes, read the skill under `.claude/skills/`:

- **`fused-render-authoring`** — the full contract for `.html` views and
  `.py` data files: the `fused` bridge, params-as-state wiring, file IO,
  theming, and debugging blank views / traceback overlays.
