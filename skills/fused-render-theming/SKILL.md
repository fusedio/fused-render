---
name: fused-render-theming
description: Use when a fused-render view must follow the app's light/dark appearance, or looks wrong in the opposite theme.
---

# Theming a view

The iframe is a blank canvas (nothing injected by default), but the shell follows OS light/dark with a Preferences override. Pick ONE strategy; the failure mode is half-following.

1. **Fixed palette** — fine for a strong own look (dark map, photo grid). Don't pretend to follow.
2. **Follow the app** (the usual right answer): `data-fused-theme="shell"` on `<html>`. The runtime writes `data-theme="light"|"dark"` there before your stylesheet parses and keeps it in step (pin, OS flip, other-window change). Author two `:root` token blocks against `[data-theme]` + `color-scheme`. What built-in templates use — see SPEC.md §30 (AP-8/AP-9) and any `fused_render/templates/*/template.html`.
3. **Follow the desktop only**: `@media (prefers-color-scheme)` around the second token block, no attribute. Doesn't see an in-app pin.
4. **Own switcher**: choice in a param (`fused.params.set("theme", …)`), drive `data-theme` yourself. INSTEAD of option 2, never alongside — the runtime re-applies and your button loses.

Two rules that make a second palette work:

- **Every colour from a token.** Same token set in both blocks; zero colour literals elsewhere — a stray hex is a smear the other mode can't repaint.
- **Colours handed to JS don't follow.** Canvas/chart/maplibre values: read via `getComputedStyle` at draw time, redraw on a `MutationObserver` over `data-theme`.

Never read the app's own localStorage key — private, drifts. Options 2/3 give the answer without it.

Rest of page authoring → `fused-render-authoring`.
