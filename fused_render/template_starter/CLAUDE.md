# fused-render preview template

Folder = one **fused-render preview template** — renders a file (or dir) in preview pane. Scaffolded from starter kit; edit in place. Two skills carry full contract — invoke before non-trivial changes:

- **`fused-render-authoring`** — writing `template.html` + optional `reader.py`, injected `fused` bridge (`params`, `runPython`, `readFile`, `rawUrl`, …).
- **`fused-render-custom-templates`** — registering: registry keys, binding rules, `condition.py`, `icon.svg`.

fused-render supplies both (plus `fused-render-usage` / `fused-render-index` — latter for machine-wide file index) to every chat it launches as session plugin — available by name, no install. Also keeps copy in Claude Code user-level skills dir for sessions fused-render didn't start (plain `claude` here). Skill missing from both → start/restart fused-render once; refreshes both on startup.
