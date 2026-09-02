---
name: fused-render-theming
description: Use when fused-render view must follow app light/dark, or looks wrong in opposite theme.
---

# Theming a view

Iframe = blank canvas (nothing injected by default), but shell follows OS light/dark with Preferences override. Pick ONE strategy; failure mode = half-following.

1. **Fixed palette** — fine for strong own look (dark map, photo grid). Don't pretend to follow.
2. **Follow the app** (usual right answer): `data-fused-theme="shell"` on `<html>`. Runtime writes `data-theme="light"|"dark"` there before stylesheet parses, keeps it in step (pin, OS flip, other-window change). Author two `:root` token blocks against `[data-theme]` + `color-scheme`. Built-in templates do this — SPEC.md §30 (AP-8/AP-9), any `fused_render/templates/*/template.html`.
3. **Follow desktop only**: `@media (prefers-color-scheme)` around second token block, no attribute. Blind to in-app pin.
4. **Own switcher**: choice in param (`fused.params.set("theme", …)`), drive `data-theme` yourself. INSTEAD of option 2, never alongside — runtime re-applies, your button loses.

Two rules making second palette work:

- **Every colour from a token.** Same token set both blocks; zero colour literals elsewhere — stray hex = smear other mode can't repaint.
- **Colours handed to JS don't follow.** Canvas/chart/maplibre values: read via `getComputedStyle` at draw time, redraw on `MutationObserver` over `data-theme`.

Never read app's own localStorage key — private, drifts. Options 2/3 give answer without it.

Rest of page authoring → `fused-render-authoring`.
