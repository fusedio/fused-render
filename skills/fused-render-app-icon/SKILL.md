---
name: fused-render-app-icon
description: Use when adding/changing/fixing app's icon.svg — sidebar glyph, browser-tab favicon.
---

# icon.svg

`icon.svg` (exact lowercase name) beside entry page. Nothing registers it — found by name; edits show next navigation. Used 14 px (sidebar Projects), 16 px (favicon — also for plain files opened in explorer). Skip → generic mark.

Rules:

- **Rendered as is** — no tinting/masking/framing. Lands on light AND dark surfaces → own your background: filled rounded square/circle behind glyph. Transparent → mid-tone/saturated colour or contrasting outline, never pure black/white/grey.
- **Design for 16 px**: one bold shape or 1–2 letter monogram; no scenes, no thin lines (< ~1/12 viewBox blurs); 2–3 colours, no gradients/shadows/texture. Verify: zoom to ~16 px on light + dark page.
- **Square viewBox**, fill it (small margin only if own background); no fixed width/height (or equal).
- **Plain standalone file**: everything inline — no external images/fonts/CSS imports (favicon fetched standalone), no scripts/animation. Few KB; `svgo` if tool-exported.

Serviceable default: dark rounded `<rect rx>` + one bright glyph (path, or centred `<text>` monogram); swap glyph + colours for app's own.
