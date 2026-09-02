---
name: fused-render-app-icon
description: Use when adding, changing or fixing an app's icon.svg — the sidebar glyph and browser-tab favicon.
---

# icon.svg

`icon.svg` (exact lowercase name) beside the entry page. Nothing registers it — found by name; edits show on next navigation. Used at 14 px (sidebar Projects) and 16 px (favicon). Skip it → generic mark.

Rules:

- **Rendered as is** — no tinting, masking or framing. It lands on light AND dark surfaces, so own your background: filled rounded square/circle behind the glyph. Going transparent → mid-tone/saturated colour or contrasting outline, never pure black/white/grey.
- **Design for 16 px**: one bold shape or 1–2 letter monogram; no scenes, no thin lines (< ~1/12 viewBox blurs away); 2–3 colours, no gradients/shadows/texture. Verify by zooming out to ~16 px on a light and a dark page.
- **Square viewBox**, fill it (small margin only if it has its own background); no fixed width/height (or equal).
- **Plain standalone file**: everything inline — no external images/fonts/CSS imports (favicon is fetched standalone), no scripts/animation. A few KB; `svgo` if exported from a tool.

Serviceable default: dark rounded `<rect rx>` + one bright glyph (path, or centred `<text>` monogram); swap glyph and colours for the app's own.
