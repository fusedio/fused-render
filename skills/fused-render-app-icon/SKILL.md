---
name: fused-render-app-icon
description: Design the optional icon.svg for a fused-render app — the file the shell shows as the app's sidebar glyph and browser-tab favicon. Use when the user asks for an app icon, logo, favicon or glyph, or wants to add, change or fix icon.svg.
---

# An app's icon.svg

An app folder may carry an `icon.svg` (that exact lowercase name) next to its entry page. Nothing registers it — the shell finds it by name and uses it in two places:

- the app's glyph in the sidebar's **Projects** list (a 14 px slot);
- the **browser-tab favicon** on the app's page (`/apps/<folder>`) and on any of its files opened in the explorer (16 px in most browsers).

Skip the file and the generic mark is used. Edit it and the new drawing shows on the next navigation — no reload of the shell.

## It renders as is

The svg is drawn **untouched**: no recolouring, no mask, no tint, no frame added. Whatever colours and background you draw are exactly what the user sees. Consequences:

- **Own your background.** The icon lands on the sidebar (light or dark theme) and on a browser tab strip (light or dark, per the user's OS/browser). A transparent icon must read on BOTH; a black glyph vanishes on dark, a white one on light. The easy fix is to give the icon its own background — a filled rounded square or circle behind the glyph — so contrast is decided inside the file, not by whatever is behind it.
- **If you go transparent**, use a mid-tone or saturated colour that holds on both white and near-black (a strong brand hue, not pure black/white/grey), or outline the shape in a contrasting stroke.
- Nothing is clipped or inset for you. Fill the viewBox; leave only a small margin if the icon has its own background.

## It renders very small

14–16 px. Design for that, not for a hero image:

- **One shape, one idea.** A single bold silhouette or a 1–2 letter monogram. No scenes, no text longer than two letters, no thin lines (under ~1/12 of the viewBox they blur to nothing).
- **Two colours, maybe three.** Background + glyph is usually enough. Gradients, shadows and fine texture turn to mud.
- **Square viewBox** (`viewBox="0 0 64 64"` or similar) so it fills the slot without letterboxing. Set no fixed `width`/`height`, or set them equal.
- Check it: zoom the browser out until the svg is ~16 px on screen, on a light and a dark page. If you cannot tell what it is, simplify.

## A serviceable default

```svg
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <rect width="64" height="64" rx="14" fill="#1b1d21"/>
  <circle cx="32" cy="32" r="16" fill="#e5ff44"/>
</svg>
```

Dark rounded square, one bright glyph — reads on any background and at any size. Swap the glyph (a path, a monogram `<text>` at `font-size="34"` with `text-anchor="middle"`, a simple mark) and the two colours for the app's own.

## Keep it a plain file

- Inline everything: no external `<image>`, fonts or CSS `@import` — the favicon is fetched as a standalone document.
- No scripts, no animation (favicons do not animate and the sidebar ignores it).
- Small: a few KB. Optimise with `svgo` if exported from a design tool.
