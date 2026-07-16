# fused-render preview template

This folder is one **fused-render preview template** — a self-contained folder
that renders a file (or directory) in the preview pane. It was scaffolded from
the starter kit; edit it in place. Two skills under `.claude/skills/` carry the
full contract — read them before non-trivial changes:

- **`fused-render-authoring`** — writing `template.html` and the optional
  `reader.py`, plus the injected `fused` runtime bridge (`params`, `runPython`,
  `readFile`, `rawUrl`, …).
- **`fused-render-custom-templates`** — registering the template: registry keys,
  binding rules, `condition.py`, and `icon.svg`.
