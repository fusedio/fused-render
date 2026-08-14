# fused-render preview template

This folder is one **fused-render preview template** — a self-contained folder
that renders a file (or directory) in the preview pane. It was scaffolded from
the starter kit; edit it in place. Two skills carry the full contract — invoke
them before non-trivial changes:

- **`fused-render-authoring`** — writing `template.html` and the optional
  `reader.py`, plus the injected `fused` runtime bridge (`params`, `runPython`,
  `readFile`, `rawUrl`, …).
- **`fused-render-custom-templates`** — registering the template: registry keys,
  binding rules, `condition.py`, and `icon.svg`.

fused-render supplies both (and `fused-render-usage` / `fused-render-index`,
the latter for reading the machine-wide file index) to every chat it
launches, as a plugin loaded for that session, so they are available here by
name with no install step. It also keeps a copy in Claude Code's user-level
skills directory for sessions fused-render didn't start — a plain `claude` in
this folder, say. If a skill isn't listed in one of those, start (or restart)
fused-render once; it refreshes both on startup.
