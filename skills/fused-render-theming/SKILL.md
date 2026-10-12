---
name: fused-render-theming
description: Use when fused-render view must follow app light/dark, or looks wrong in opposite theme.
---

# Theming a view

Iframe = blank canvas (nothing injected by default), but shell follows OS light/dark with Preferences override. Pick ONE strategy; failure mode = half-following.

1. **Fixed palette** — fine for strong own look (dark map, photo grid). Don't pretend to follow.
2. **Follow the app** (DEFAULT for simple apps — forms, dashboards, tools, lists, anything without a strong own look): `data-fused-theme="shell"` on `<html>`. Runtime writes `data-theme="light"|"dark"` there before stylesheet parses, keeps it in step (pin, OS flip, other-window change), AND copies the shell's live palette (colour preset: Default / High contrast / Midnight) in as `--fused-*` tokens. Built-in templates do this — SPEC.md §30 (AP-8/AP-9), any `fused_render/templates/*/template.html`.
3. **Follow desktop only**: `@media (prefers-color-scheme)` around second token block, no attribute. Blind to in-app pin.
4. **Own switcher**: choice in param (`fused.params.set("theme", …)`), drive `data-theme` yourself. INSTEAD of option 2, never alongside — runtime re-applies, your button loses.

## `--fused-*` tokens (opted-in views only)

`--fused-bg` page, `-bg-alt` inset/zebra, `-bg-panel` card/panel, `-fg` text, `-fg-muted`, `-fg-faint`, `-border`, `-sel` selection/hover fill, `-accent`, `-on-accent` text on accent, `-error`, `-success`, `-warning`. Follow light/dark AND preset, live, no reload.

Pattern — keep both `:root` blocks, define the app's own tokens as `var(--fused-x, fallback)`: dark fallbacks in `:root`, light fallbacks in `:root[data-theme="light"]`:

```css
:root { color-scheme: dark; --bg: var(--fused-bg, #101318); --text: var(--fused-fg, #dce2ea); }
:root[data-theme="light"] { color-scheme: light; --bg: var(--fused-bg, #f7f8fa); --text: var(--fused-fg, #1a1f27); }
```

Standalone (no shell above: top-level `/render?path=…`) tokens are unset → only fallbacks apply, still correct light/dark. Always give a fallback. Rare preset-specific tweak: `html[data-fused-theme-name="midnight"]` (absent on Default). Simple app: don't hard-code a palette.

Two rules making second palette work:

- **Every colour from a token.** Same token set both blocks; zero colour literals elsewhere — stray hex = smear other mode can't repaint.
- **Colours handed to JS don't follow.** Canvas/chart/maplibre values: read via `getComputedStyle` at draw time, redraw on `MutationObserver` over `data-theme`, `data-fused-theme-name`, `style` on `<html>` (tokens land as inline style, so a preset change is a `style` mutation).

Never read app's own localStorage key — private, drifts. Options 2/3 give answer without it.

Native controls/scrollbars across engines (`color-scheme` vs `appearance: none`) → `fused-render-cross-browser`. Rest of page authoring → `fused-render-authoring`.
