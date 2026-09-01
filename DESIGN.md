---
name: fused-render
description: Local file explorer for the whole machine — dev-tool canon played straight at Linear/Vercel/Raycast craft.
colors:
  fused-lime: "#E5FF44"
  lime-hover: "#eeff70"
  on-lime: "#10131a"
  near-black-ground: "#0b0d10"
  rail-black: "#08090b"
  raised-graphite: "#121417"
  panel-graphite: "#16181d"
  hairline: "#26292e"
  off-white-ink: "#f2f3f5"
  cool-muted: "#8b9096"
  selected-row-wash: "#262a15"
  hover-row-wash: "#1a1d22"
  signal-red: "#ff6b6b"
  signal-green: "#3fb950"
  signal-amber: "#d29922"
typography:
  body:
    fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif"
    fontSize: "14px"
    fontWeight: 400
  control:
    fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif"
    fontSize: "13px"
    fontWeight: 400
  label:
    fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif"
    fontSize: "12px"
    fontWeight: 500
  mono-metadata:
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace"
    fontSize: "12px"
    fontWeight: 400
rounded:
  sm: "4px"
  md: "8px"
  lg: "12px"
  pill: "999px"
components:
  button-base:
    backgroundColor: "{colors.near-black-ground}"
    textColor: "{colors.off-white-ink}"
    rounded: "{rounded.md}"
    height: "32px"
    padding: "0 14px"
  button-primary:
    backgroundColor: "{colors.fused-lime}"
    textColor: "{colors.on-lime}"
    rounded: "{rounded.md}"
    height: "32px"
    padding: "0 14px"
  button-primary-hover:
    backgroundColor: "{colors.lime-hover}"
    textColor: "{colors.on-lime}"
  field-control:
    backgroundColor: "{colors.near-black-ground}"
    textColor: "{colors.off-white-ink}"
    rounded: "{rounded.md}"
    padding: "7px 10px"
  sidebar-item:
    backgroundColor: "transparent"
    textColor: "{colors.off-white-ink}"
    rounded: "{rounded.md}"
    padding: "7px 10px"
  listing-row-selected:
    backgroundColor: "{colors.selected-row-wash}"
    textColor: "{colors.off-white-ink}"
---

# Design System: fused-render

<!-- Recorded 2026-09-01 from the built canon retheme (frontend/src/styles/*).
     Supersedes the pre-redesign DESIGN.md, which documented the retired
     warm-graphite world. Dark `:root` is the normative palette; light values
     live in tokens.css and the sidecar. -->

## Overview

**Creative North Star: "The Dev-Tool Canon, Played Straight"**

fused-render dresses the whole machine as a first-class developer product: the modern dev-tool canon (benchmarked against Linear, Vercel dashboard, Raycast — user-chosen, PRODUCT.md Brand Commitments) executed at full craft, with no irony and no smuggled quirk. The world is a deep near-black ground with a sidebar rail that recedes darker than the pane it borders, off-white ink, hairline borders, and exactly one loud voice: Fused Lime (#E5FF44), reserved for "you are here" and "do this".

Density is high and calm — dense tables, quiet chrome, translucent tint-washes instead of new grays. Every color on every surface is a CSS custom property defined in `frontend/src/styles/tokens.css`, dark-first with a full light counterpart; `tests/test_theme.py` enforces both halves of that contract.

**Key Characteristics:**
- One lime signal per grammar: active nav marker, selection edge, focus ring, primary fill — nothing else.
- Rail (#08090b) darker than page (#0b0d10) darker-side of raised (#121417): the pane is the lit surface.
- Hairline #26292e borders carry structure; shadows are reserved for floating surfaces.
- Measurements speak monospace, 12px, muted.
- Three motion durations (80/150/200ms), one ease-out, shell-wide.

## Colors

A near-black neutral ladder with a single electric accent; light mode restates every token with the same relationships on white.

### Primary
- **Fused Lime** (#E5FF44): the binding brand accent (PRODUCT.md, user-confirmed). Active-nav edge marker, selected-row edge + wash, focus rings, and the one primary-button fill per screen. Hover step **Lime Hover** (#eeff70); ink on a lime fill is **On-Lime** (#10131a). In the light palette the same token becomes a deep olive-lime (#5f7300, hover #4d5e00) because raw lime is unreadable as text/border on white — same role, contrast-corrected value.

### Tertiary
- **Signal set**: error #ff6b6b, success #3fb950, warning #d29922 (`--error/--success/--warning`), each with an `--*-rgb` triple for translucent washes. Six categorical series hues, file-type icon hues, and task-chip hues are all tokenized with light counterparts.

### Neutral
- **Near-Black Ground** (#0b0d10): the page (`--bg`). Light: #ffffff.
- **Rail Black** (#08090b): the global sidebar's own ground (`--bg-rail`), one step darker than the page. Light: #eff0f3.
- **Raised Graphite** (#121417): raised surfaces, row hover ancestry (`--bg-alt`). Light: #f5f6f8.
- **Panel Graphite** (#16181d / #14161a): floating pickers, tooltips, popovers (`--bg-panel`/`--bg-popover`). Light: white, lifted by border + shadow instead.
- **Hairline** (#26292e): every structural border (`--border`). Light: #e2e4e8.
- **Off-White Ink** (#f2f3f5): body text (`--fg`). Light: #17181a.
- **Cool Muted** (#8b9096): secondary text, labels, metadata (`--fg-muted`). Light: #676c73.
- **Row Washes**: pre-composited opaque row fills — hover #1a1d22 (`--row-bg-hover`), selected #262a15 (`--row-bg-active`, the accent 0.13 wash over raised graphite).

### Named Rules
**The One-Lime-Signal Rule.** Fused Lime appears only as: the 2px active-nav inset marker, the selected-row wash + 2px first-cell inset, the focus treatment (accent border/outline + faint accent wash), and the single primary-button fill. It never decorates, never fills large areas, never colors prose.

**The Token Rule.** No color literal exists outside the two palette blocks in `tokens.css`. Every rule paints with `var(--token)`; every dark token has a light counterpart (`tests/test_theme.py` enforces both).

**The Rail-Recedes Rule.** The sidebar rail is always a step darker than the pane it borders (`--bg-rail` < `--bg` < `--bg-alt`), in both themes — the pane is the lit surface; later tuning must not invert the ordering.

## Typography

**Body Font:** system sans (-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial)
**Label/Mono Font:** ui-monospace (SFMono-Regular, Menlo fallback) — metadata only

**Character:** invisible, OS-native, dense. The product's voice is the system's voice; only measurements switch registers into mono.

### Hierarchy
- **Body** (400, 14px): default document size, set on `<body>`.
- **Control** (400, 13px): buttons (`.btn`) and field controls (`.field-control`).
- **Label** (500, 12px): field labels, hints, secondary rows — muted color.
- **Mono metadata** (400, 12px, ui-monospace, `--fg-muted`, tabular-nums): file sizes and modified times (`td.size`, `td.mtime`).

### Named Rules
**The Mono-for-Measurement Rule.** Anything measured — bytes, timestamps — renders in ui-monospace, one size down (12px), muted, with tabular numerals. Prose and names never take mono; measurements never take the sans voice.

## Layout

A fixed 100vh flex shell (`#app`): sidebar rail left, breadcrumb + search header, dense file table center, folder-scoped split preview right; the document itself never scrolls (`overscroll-behavior-y: none`). Rhythm is un-tokenized but consistent: 32px control height, 7-10px control padding (`.btn` 0 14px, `.field-control` 7px 10px, `.sidebar-item` 7px 10px), 9px 16px table cells, 12px 16px modal footers, 6-8px gaps. Narrow listings drop the mtime column below 460px and the size column below 300px via container queries rather than wrapping.

## Elevation & Depth

Depth is tonal and hairline-first: surfaces separate by ground step (`--bg-rail` < `--bg` < `--bg-alt` < `--bg-panel`) plus a #26292e border. Shadows exist only for genuinely floating surfaces (tooltips, popovers, lifted cards) and are tokenized (`--shadow-sm/md/lg`, `--shadow-ink*`), dark-tuned to deeper alphas, lighter in the light palette. The modal scrim (`--scrim`) stays dark in both modes.

### Shadow Vocabulary
- **Popover lift** (`0 4px 14px var(--shadow-sm)`): tooltips, hint panels.
- **Card hover lift** (`0 4px 18px var(--shadow-sm)` + 1px rise): app cards on hover.

### Named Rules
**The Tonal-First Rule.** Resting surfaces separate by ground step and hairline, never by shadow; a shadow means the surface floats above the page.

## Shapes

Quiet, small radii. The working scale: **4px** (inline code chips, small badges), **8px** (the default — buttons, fields, rows, cards, popovers), **12px** (large cards/panels), **999px/50%** (pills and dots). Signals are drawn as 2px *inset box-shadows*, not borders, so text never shifts between states; colored borders stay 1px (error cards, focus borders).

## Components

### Buttons
- **Shape:** gently rounded (8px), 32px tall, 13px text, 0 14px padding.
- **Base (`.btn`):** page ground, hairline border, ink text.
- **Primary (`.btn-primary`):** the one lime fill — Fused Lime background, transparent border, On-Lime (#10131a) 600-weight text; hover steps to #eeff70. One per screen.
- **Secondary:** transparent, hairline border; hover brightens border to `--fg-muted`, no fill change.
- **Danger / Danger-text:** error-tinted wash + error border / borderless error text.
- **Focus:** `outline: 2px solid var(--accent); outline-offset: 2px` on `:focus-visible`. Disabled: opacity 0.5 only.

### Inputs / Fields
- **Style:** page-ground fill, hairline border, 8px radius, 13px text, 32px tall; 12px muted 500-weight label above with 6px gap; custom muted-gray SVG chevron on selects.
- **Focus:** the lime signal — border takes `--accent` plus a `0 0 0 3px rgba(var(--accent-rgb), 0.15)` wash ring; no outline.
- **Error:** full 1px `--error` border + 0.07 error wash card (`.error-banner`).

### Navigation (sidebar)
- **Style:** rows on the Rail Black ground, 8px radius, 7px 10px padding, 10px icon gap; icons at 0.85 opacity.
- **Hover / Active:** a 0.06 tint wash; the active row additionally wears the lime marker — `inset 2px 0 0 var(--accent)` — the canon "you are here".

### Listing rows (signature)
- **Rest:** 9px 16px cells, hairline bottom border, no text selection.
- **Hover:** `--bg-alt`; **selected:** opaque `--row-bg-active` wash plus a 2px lime inset on the first cell — the same one-signal grammar as the nav marker. Drop targets extend the lime insets to top/bottom edges. New rows flash a 1.5s accent-wash fade.
- **Metadata cells:** the mono-for-measurement voice, right-aligned tabular size column.

### Cards (app preview)
- **Thumb well:** 16/10 box on a designed ground — a faint dot grid (`radial-gradient(rgba(var(--tint),0.1) 1px, transparent 1px)` at 14px pitch over `--bg`) so an empty card reads as an intentional canvas, not a void; hairline top border; skeleton shimmer while loading.
- **Hover:** border warms toward `--fg-muted`, soft shadow, 1px rise.

### Motion (applies to all of the above)
Three durations, one ease, defined once in base.css: `--dur-fast` 80ms (hover/press color), `--dur-med` 150ms (overlays, toasts, menus), `--dur-slow` 200ms (panels, sidebar collapse, FLIP reorders); `--ease-out`. Press feedback is one shared 1px translateY nudge on button-shaped controls only. `prefers-reduced-motion` collapses all of it.

## Do's and Don'ts

### Do:
- **Do** paint every color with a `var(--token)` from tokens.css and give any new dark token a light counterpart.
- **Do** draw selection/active edges as 2px inset box-shadows in Fused Lime, never as borders that shift text.
- **Do** keep the rail darker than the pane in any new surface ordering (`--bg-rail` < `--bg` < `--bg-alt`).
- **Do** set measurements (sizes, timestamps, counts) in ui-monospace 12px muted with tabular-nums.
- **Do** pick transitions from the three tokens (80/150/200ms, ease-out); one primary lime fill per screen.

### Don't:
- **Don't** hardcode a color literal outside the two palette blocks in tokens.css (test-enforced).
- **Don't** use Fused Lime for decoration, prose, large fills, or more than the four sanctioned signals.
- **Don't** use raw #E5FF44 as text or border in the light theme — the light `--accent` is the contrast-corrected olive-lime.
- **Don't** add shadows to resting surfaces; hairline + ground step carry structure.
- **Don't** invent new grays — layer `rgba(var(--tint), …)` washes over existing grounds instead.
