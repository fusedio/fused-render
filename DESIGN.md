---
name: fused-render
description: Local file explorer with renderable HTML views — a quiet, dark-first instrument panel for your own machine.
colors:
  fg: "#e8eaed"
  fg-muted: "#9aa0a6"
  border: "#2a2d33"
  bg: "#131417"
  bg-alt: "#1b1d21"
  bg-panel: "#202329"
  bg-popover: "#1c1e24"
  sel: "#2b3a52"
  accent: "#E5FF44"
  accent-soft: "#c9d95e"
  on-accent: "#10131a"
  on-fg: "#ffffff"
  error: "#ff6b6b"
  success: "#3fb950"
  warning: "#d29922"
  activity: "#60a5fa"
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
  mono:
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"
    fontSize: "13px"
    fontWeight: 400
rounded:
  sm: "4px"
  md: "6px"
  lg: "8px"
  xl: "10px"
  pill: "999px"
components:
  button-base:
    backgroundColor: "{colors.bg}"
    textColor: "{colors.fg}"
    rounded: "{rounded.md}"
    padding: "0 14px"
    height: "32px"
  button-primary:
    backgroundColor: "{colors.fg}"
    textColor: "{colors.bg}"
    rounded: "{rounded.md}"
    padding: "0 14px"
    height: "32px"
  button-primary-hover:
    backgroundColor: "{colors.on-fg}"
    textColor: "{colors.bg}"
  field-control:
    backgroundColor: "{colors.bg}"
    textColor: "{colors.fg}"
    rounded: "{rounded.md}"
    padding: "7px 10px"
    height: "32px"
  sidebar-item:
    textColor: "{colors.fg}"
    rounded: "{rounded.lg}"
    padding: "7px 10px"
---

# Design System: fused-render

## Overview

**Creative North Star: "The Instrument Panel"**

fused-render is a dark cockpit for your own machine: quiet chrome, hairline seams, utility density, and one lime signal. The interface is confident and dark-first — the dark palette IS the app's identity, and light mode is a faithful, per-token translation of the same relationships, never a restyle. Every colour in the app comes from the token file; nothing is hardcoded in a rule (`tests/test_theme.py` enforces it).

The system runs on semantic colour discipline: every hue means something. Status hues (`success`/`warning`/`error`) say what a thing means; `activity` blue says something is in flight; the file-icon and series palettes distinguish, never decorate. The Fused Lime accent is rare and earned — focus rings, selection, the occasional highlight — never a button fill or a large surface. Hierarchy is carried by contrast, spacing, and hairline borders, not by colour weight.

**Key Characteristics:**
- Dark-first; light is a byte-faithful translation, not a second design
- Quiet, precise, dense — 13–14px UI type, 32px controls, hairline borders
- One accent (Fused Lime), used sparingly; neutral-filled primary buttons
- Every colour is a named token with a defined role; semantic hues form a learned vocabulary
- Flat-first: borders carry separation, shadows stay minimal and ambient

## Colors

A near-black neutral ramp with one electric signal and a disciplined semantic vocabulary. Dark values are normative (`:root` default); every token has a light counterpart.

### Primary
- **Fused Lime** (#E5FF44): the brand's one voice. Focus rings (`outline: 2px solid`), selection tints, required-field marks, rare highlights. In light mode it becomes a deep olive-lime (#5f7300) that clears 4.5:1 as text, border, and fill from one token.
- **Lime Soft** (#c9d95e): the accent as readable body text on dark; hover-state text on cards.

### Neutral
- **Ink** (#e8eaed): primary text.
- **Ink Muted** (#9aa0a6): secondary text, labels, hints, metadata chips.
- **Page** (#131417): the app ground.
- **Raised** (#1b1d21): bars, sidebars, alternate surfaces.
- **Panel** (#202329) / **Popover** (#1c1e24): floating surfaces above Raised.
- **Hairline** (#2a2d33): the app's border; separation is drawn, not shadowed.
- **Selection** (#2b3a52): selected rows and ranges.

### Semantic
- **Error** (#ff6b6b), **Success** (#3fb950), **Warning** (#d29922): meaning, not decoration.
- **Activity** (#60a5fa): things in flight — live pings, unread dots, run affordances. Deliberately not a status: it marks motion, not outcome.
- Task-status vocabulary (`--status-*`), categorical chart series (`--series-1..6`), per-project calendar hues (`--task-c0..7`), and file-type icon hues (`--icon-*`) are fixed, hand-picked sets in `frontend/src/styles/tokens.css`; reuse them, never generate new hues.

### Named Rules
**The One Signal Rule.** Fused Lime never fills a button or a large surface; it marks focus, selection, and the rare highlight. Primary actions read strongest through contrast (ink-filled), not accent.
**The Token Rule.** Every colour comes from `tokens.css`. A hardcoded colour is a colour light mode cannot repaint.
**The Scrim Exception.** Controls sitting on arbitrary pixels (images, maps, video) use the `--scrim-*` set — identical in both themes, white ink on dark wash — because the surface underneath is not ours.

## Typography

**Body Font:** system sans (-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial)
**Mono Font:** ui-monospace, SFMono-Regular, Menlo, Consolas

**Character:** native, invisible, dense. Type never performs; paths, code, and data get the mono voice, everything else recedes into the OS.

### Hierarchy
- **Body** (400, 14px): the page default.
- **Control** (400, 13px): buttons, fields, rows; sidebar items sit at 13.5px.
- **Label** (500, 12px): field labels, hints, metadata — always in Ink Muted.
- **Mono** (400, ~13px): file paths, code, sizes, technical values.

### Named Rules
**The No-Display Rule.** There is no display face. Headings differentiate by weight and spacing within the system stack; nothing is decorative.

## Layout

Utility density on an app-shell grid: a collapsible global sidebar rail, content panes, and full-height listings. Controls stand 32px tall; row padding runs 7px 10px; gaps step through 6/10/14px. Separation is drawn with 1px Hairline borders on surfaces, not with whitespace luxury — the app is an instrument, information-dense by intent. Motion uses three durations and one easing (`--dur-fast` 80ms hover, `--dur-med` 150ms overlays, `--dur-slow` 200ms panels; ease-out), all collapsing under `prefers-reduced-motion`.

## Elevation & Depth

Flat-first. Surfaces are flat at rest; borders carry separation. Depth exists only as quiet surface ordering (dark: page < lane < card via slightly lighter fills; light: white surfaces over a dimmed ground) plus minimal, ambient shadows from the `--shadow-*` alpha tokens — deeper alphas in dark (0.25–0.5) because a shadow on a dark ground must work harder, faint in light (0.1–0.18). Shadows never structure a screen.

### Named Rules
**The Drawn-Edge Rule.** If two surfaces must read as separate, give them a Hairline border or an ordered fill — never a heavier shadow.

## Shapes

Small-radius rectangles throughout: 6px is the workhorse (buttons, fields, thumbnails), 8px for sidebar rows and larger containers, 10–12px for panels and modals, 4px for chips and small elements, 999px pills for counts and status dots. No sharp-corner or super-ellipse experiments; the form language is uniform and quiet.

## Components

### Buttons
- **Shape:** gently rounded (6px), 32px tall, 0 14px padding, 13px type.
- **Base:** Page fill, Hairline border, Ink text.
- **Primary:** ink-filled (background `--fg`, text `--bg`, weight 600) — strongest through contrast, not accent. Hover steps to `--on-fg` (pure white in dark).
- **Focus:** 2px Fused Lime outline, 2px offset — the one place the accent is guaranteed.
- **Disabled:** 0.5 opacity. **Press:** shared 1px translateY nudge on icon-shaped buttons.

### Inputs / Fields
- **Style:** Page fill, Hairline border, 6px radius, 7px 10px padding, 32px tall, 13px type.
- **Label:** 12px/500 Ink Muted above, 6px gap; required mark in accent.
- **Focus:** border shifts to Ink Muted — quiet, no glow.
- **Select:** de-nativized with an inline muted-chevron SVG.

### Cards / Containers
- **Corner Style:** 6px thumbnails, 10–12px panels.
- **Background:** Raised or ordered board fills (`--tasks-lane-bg` / `--tasks-card-bg`).
- **Border:** Hairline always; hover tints the border toward accent (`color-mix` ~55%).
- **Shadow Strategy:** minimal, ambient only (see Elevation).

### Navigation (Global Sidebar)
- **Rows:** 8px radius, 7px 10px padding, 13.5px type, 10px icon gap.
- **States:** colour-only transitions at `--dur-fast`; active rows use pre-composited row fills (`--row-bg-hover` / `--row-bg-active`), never translucent washes on stretched-link rows.

### Signature: The Listing Row
The app's atom: full-width rows in tables and lists, hover repaint at 80ms, selection in `--sel`, file-type icon in its fixed hue, metadata in Ink Muted mono. A plain press's release opens; a modified press selects (FS-5/D460) — design never adds a second press model.

## Do's and Don'ts

### Do:
- **Do** take every colour from `tokens.css`; add a token (with a light counterpart) before adding a colour.
- **Do** keep controls at 32px, radius at 6px, and UI type at 13–14px.
- **Do** use `--activity` for in-flight/unread signals and the status hues for outcomes; keep hue meanings stable.
- **Do** carry hierarchy with contrast and Hairline borders; keep primary buttons ink-filled.
- **Do** pick one of the three motion durations; never invent a new number.

### Don't:
- **Don't** fill buttons or large surfaces with Fused Lime — accent everywhere is the confirmed anti-reference; accent is for focus and selection.
- **Don't** import generic SaaS decoration: gradient heroes, glassmorphism, decorative card shadows.
- **Don't** play terminal cosplay: no scanlines, phosphor glow, or faux-CRT styling — developer-native means quiet, not themed.
- **Don't** hardcode a colour, restyle dark mode, or give a dark token no light counterpart.
- **Don't** use heavier shadows to separate surfaces; draw the edge instead.
