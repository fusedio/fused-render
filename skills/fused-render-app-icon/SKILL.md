---
name: fused-render-app-icon
description: Use when adding/changing/fixing app's icon.svg or icon.png — sidebar glyph, app card mark, browser-tab favicon.
---

# icon.svg / icon.png

`icon.svg` (exact lowercase name) beside entry page; `icon.png` (exact lowercase) accepted as a lower-priority fallback — svg wins when both exist. Nothing registers either — found by name; edits show next navigation. Used tiny: sidebar Projects glyph + app card mark + favicon (favicon also for plain files opened in explorer). Skip both → generic mark.

## icon.png

- **Shell fits it to a rounded square** (`object-fit: cover` + corner radius) — ship a plain square image, edge to edge; do NOT bake your own rounding or padding (double-rounded corners, shrunken glyph).
- Square, ≥ 256×256 px, opaque background (transparent corners show the host surface through the clip). Same 16 px legibility rules as svg below.
- Prefer svg: the icon picker writes `icon.svg`, and a picker-written svg hides the png; "Remove" in the picker deletes BOTH files.

## icon.svg

Rules:

- **Rendered as is** — no tinting/masking/framing. Lands on light AND dark surfaces → own your background: filled rounded square/circle behind glyph. Transparent → mid-tone/saturated colour or contrasting outline, never pure black/white/grey.
- **Design for 16 px**: one bold shape or 1–2 letter monogram; no scenes, no thin lines (< ~1/12 viewBox blurs); 2–3 colours, no gradients/shadows/texture. Verify: zoom to ~16 px on light + dark page.
- **Square viewBox**, fill it (small margin only if own background); no fixed width/height (or equal).
- **Plain standalone file**: everything inline — no external images/fonts/CSS imports (favicon fetched standalone), no scripts/animation. Few KB; `svgo` if tool-exported.

Serviceable default: dark rounded `<rect rx>` + one bright glyph (path, or centred `<text>` monogram); swap glyph + colours for app's own.
