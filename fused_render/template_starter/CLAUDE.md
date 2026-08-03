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

fused-render installs both (and `fused-render-usage`) into Claude Code's
user-level skills directory and keeps them up to date, so they are available
here by name. If a skill isn't listed, start (or restart) fused-render once —
the server re-installs them on startup.
