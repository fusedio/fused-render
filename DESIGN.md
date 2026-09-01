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
  popover-graphite: "#14161a"
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
    fontSize: "14px"
    fontWeight: 500
  control-legacy:
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
  xl: "16px"
  pill: "999px"
components:
  button-default:
    backgroundColor: "{colors.fused-lime}"
    textColor: "{colors.on-lime}"
    typography: "{typography.control}"
    rounded: "{rounded.lg}"
    height: "32px"
    padding: "0 10px"
  button-outline:
    backgroundColor: "{colors.near-black-ground}"
    textColor: "{colors.off-white-ink}"
    typography: "{typography.control}"
    rounded: "{rounded.lg}"
    height: "32px"
    padding: "0 10px"
  button-outline-hover:
    backgroundColor: "{colors.raised-graphite}"
    textColor: "{colors.off-white-ink}"
  button-secondary:
    backgroundColor: "{colors.raised-graphite}"
    textColor: "{colors.off-white-ink}"
    rounded: "{rounded.lg}"
    height: "32px"
    padding: "0 10px"
  button-ghost:
    backgroundColor: "transparent"
    textColor: "{colors.off-white-ink}"
    rounded: "{rounded.lg}"
    height: "32px"
    padding: "0 10px"
  button-ghost-hover:
    backgroundColor: "{colors.raised-graphite}"
  button-link:
    backgroundColor: "transparent"
    textColor: "{colors.fused-lime}"
  button-legacy-base:
    backgroundColor: "{colors.near-black-ground}"
    textColor: "{colors.off-white-ink}"
    typography: "{typography.control-legacy}"
    rounded: "{rounded.md}"
    height: "32px"
    padding: "0 14px"
  button-legacy-primary:
    backgroundColor: "{colors.fused-lime}"
    textColor: "{colors.on-lime}"
    typography: "{typography.control-legacy}"
    rounded: "{rounded.md}"
    height: "32px"
    padding: "0 14px"
  button-legacy-primary-hover:
    backgroundColor: "{colors.lime-hover}"
    textColor: "{colors.on-lime}"
  input:
    backgroundColor: "transparent"
    textColor: "{colors.off-white-ink}"
    typography: "{typography.body}"
    rounded: "{rounded.lg}"
    height: "32px"
    padding: "4px 10px"
  badge-secondary:
    backgroundColor: "{colors.raised-graphite}"
    textColor: "{colors.off-white-ink}"
    typography: "{typography.label}"
    rounded: "{rounded.pill}"
    height: "20px"
    padding: "2px 8px"
  badge-outline:
    backgroundColor: "transparent"
    textColor: "{colors.off-white-ink}"
    rounded: "{rounded.pill}"
    height: "20px"
    padding: "2px 8px"
  card:
    backgroundColor: "{colors.raised-graphite}"
    textColor: "{colors.off-white-ink}"
    rounded: "{rounded.xl}"
    padding: "16px"
  app-card:
    backgroundColor: "{colors.raised-graphite}"
    textColor: "{colors.off-white-ink}"
    rounded: "{rounded.lg}"
    padding: "10px 12px"
  menu-panel:
    backgroundColor: "{colors.popover-graphite}"
    textColor: "{colors.off-white-ink}"
    rounded: "{rounded.lg}"
    padding: "4px"
  menu-item:
    backgroundColor: "transparent"
    textColor: "{colors.off-white-ink}"
    rounded: "{rounded.md}"
    padding: "4px 6px"
  menu-item-hover:
    backgroundColor: "{colors.hover-row-wash}"
  sidebar-item:
    backgroundColor: "transparent"
    textColor: "{colors.off-white-ink}"
    rounded: "{rounded.md}"
    padding: "7px 10px"
  listing-row-selected:
    backgroundColor: "{colors.selected-row-wash}"
    textColor: "{colors.off-white-ink}"
  tooltip:
    backgroundColor: "{colors.off-white-ink}"
    textColor: "{colors.near-black-ground}"
    rounded: "{rounded.md}"
    padding: "6px 12px"
---

# Design System: fused-render

<!-- Recorded 2026-09-01 from the built canon retheme (frontend/src/styles/*);
     Components section and frontmatter components re-recorded 2026-09-02
     after the shadcn migration (frontend/src/platform/shadcn/ui/*, bridged in
     frontend/src/styles/tailwind.css). Supersedes the pre-redesign DESIGN.md,
     which documented the retired warm-graphite world. Dark `:root` is the
     normative palette; light values live in tokens.css and the sidecar. -->

## Overview

**Creative North Star: "The Dev-Tool Canon, Played Straight"**

fused-render dresses the whole machine as a first-class developer product: the modern dev-tool canon (benchmarked against Linear, Vercel dashboard, Raycast — user-chosen, PRODUCT.md Brand Commitments) executed at full craft, with no irony and no smuggled quirk. The world is a deep near-black ground with a sidebar rail that recedes darker than the pane it borders, off-white ink, hairline borders, and exactly one loud voice: Fused Lime (#E5FF44), reserved for "you are here" and "do this".

Density is high and calm — dense tables, quiet chrome, translucent tint-washes instead of new grays. Every color on every surface is a CSS custom property defined in `frontend/src/styles/tokens.css`, dark-first with a full light counterpart; `tests/test_theme.py` enforces both halves of that contract. Since the shadcn migration the component vocabulary is shadcn (base-nova style on base-ui primitives, `frontend/src/platform/shadcn/ui/*`), and it reaches those tokens through one bridge file: every shadcn semantic variable in `frontend/src/styles/tailwind.css` is an alias onto a canon token, so the shadcn layer has no palette of its own.

**Key Characteristics:**
- One lime signal per grammar: active nav marker, selection edge, focus ring, primary fill, checked state — nothing else.
- Rail (#08090b) darker than page (#0b0d10) darker-side of raised (#121417): the pane is the lit surface.
- Hairline #26292e borders carry structure on bespoke surfaces; shadcn floating surfaces wear a faint ink ring plus a tokenized shadow.
- Measurements speak monospace, 12px, muted.
- Three motion durations (80/150/200ms), one ease-out, shell-wide; shadcn overlays add a 100ms fade/zoom entry.

## Colors

A near-black neutral ladder with a single electric accent; light mode restates every token with the same relationships on white.

### Primary
- **Fused Lime** (#E5FF44): the binding brand accent (PRODUCT.md, user-confirmed). Active-nav edge marker, selected-row edge + wash, focus rings, the checked fill of Checkbox / Radio / Switch, and the one primary-button fill per screen. In shadcn terms it is `--primary`, `--ring` and `--sidebar-primary`. Hover step **Lime Hover** (#eeff70) on the legacy `.btn-primary`; the shadcn default button hovers to the same lime at 80% alpha. Ink on a lime fill is **On-Lime** (#10131a, `--on-accent` / `--primary-foreground`). In the light palette the same token becomes a deep olive-lime (#5f7300, hover #4d5e00) because raw lime is unreadable as text/border on white — same role, contrast-corrected value.

### Tertiary
- **Signal set**: error #ff6b6b, success #3fb950, warning #d29922 (`--error/--success/--warning`; `--error` is also shadcn's `--destructive`), each with an `--*-rgb` triple for translucent washes. Six categorical series hues (`--series-1..6`, aliased to shadcn `--chart-1..5`), file-type icon hues, and task-chip hues are all tokenized with light counterparts.

### Neutral
- **Near-Black Ground** (#0b0d10): the page (`--bg` / `--background`). Light: #ffffff.
- **Rail Black** (#08090b): the global sidebar's own ground (`--bg-rail` / `--sidebar`), one step darker than the page. Light: #eff0f3.
- **Raised Graphite** (#121417): raised surfaces, row hover ancestry (`--bg-alt`; shadcn's `--card`, `--muted` and `--secondary` all point here). Light: #f5f6f8.
- **Panel Graphite** (#16181d / #14161a): floating pickers, tooltips, popovers, dialogs (`--bg-panel`/`--bg-popover`; `--popover`). Light: white, lifted by ring + shadow instead.
- **Hairline** (#26292e): every structural border (`--border`; shadcn `--sh-border` and `--input`). Light: #e2e4e8.
- **Off-White Ink** (#f2f3f5): body text (`--fg` / `--foreground`). Light: #17181a.
- **Cool Muted** (#8b9096): secondary text, labels, metadata (`--fg-muted` / `--muted-foreground`). Light: #676c73.
- **Row Washes**: pre-composited opaque row fills — hover #1a1d22 (`--row-bg-hover`, which is what shadcn calls `--accent`), selected #262a15 (`--row-bg-active`, the accent 0.13 wash over raised graphite).

### Named Rules
**The One-Lime-Signal Rule.** Fused Lime appears only as: the 2px active-nav inset marker, the selected-row wash + 2px first-cell inset, the focus treatment (lime border plus a 3px half-alpha lime ring), the checked/on fill of Checkbox, Radio and Switch, and the single primary-button fill. It never decorates, never fills large areas, never colors prose.

**The Token Rule.** No color literal exists outside the two palette blocks in `tokens.css`. Every rule paints with `var(--token)`; every dark token has a light counterpart (`tests/test_theme.py` enforces both).

**The Rail-Recedes Rule.** The sidebar rail is always a step darker than the pane it borders (`--bg-rail` < `--bg` < `--bg-alt`), in both themes — the pane is the lit surface; later tuning must not invert the ordering.

## Typography

**Body Font:** system sans (-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial)
**Label/Mono Font:** ui-monospace (SFMono-Regular, Menlo fallback) — metadata only

**Character:** invisible, OS-native, dense. The product's voice is the system's voice; only measurements switch registers into mono.

### Hierarchy
- **Body** (400, 14px): default document size, set on `<body>`; also the shadcn Input, Tabs content, table cells and menu items (`text-sm`).
- **Control** (500, 14px): shadcn Button, TabsTrigger, Card title, toast title — medium weight, 14px.
- **Control (legacy)** (400, 13px): the surviving `.btn` / `.field-control` vocabulary on un-migrated forms; sidebar nav rows are 13.5px.
- **Label** (500, 12px): field labels, hints, Badge and Kbd text, tooltip text — muted color except on the badge.
- **Mono metadata** (400, 12px, ui-monospace, `--fg-muted`, tabular-nums): file sizes and modified times (`td.size`, `td.mtime`).

### Named Rules
**The Mono-for-Measurement Rule.** Anything measured — bytes, timestamps — renders in ui-monospace, one size down (12px), muted, with tabular numerals. Prose and names never take mono; measurements never take the sans voice.

## Layout

A fixed 100vh flex shell (`#app`): sidebar rail left, breadcrumb + search header, dense file table center, folder-scoped split preview right; the document itself never scrolls (`overscroll-behavior-y: none`). Rhythm is un-tokenized but consistent: 32px control height (shadcn sizes xs 24 / sm 28 / default 32 / lg 36, icon buttons square at the same steps), 10px horizontal control padding on shadcn controls (`px-2.5`) and 0 14px on legacy `.btn`, 7px 10px sidebar rows, 9px 16px table cells, 12px 16px modal header/footer, 16px card padding, 6-8px gaps. Narrow listings drop the mtime column below 460px and the size column below 300px via container queries rather than wrapping.

## Elevation & Depth

Depth is tonal and hairline-first on the shell's own surfaces: rail, page, table rows and the Modal's header/footer separate by ground step (`--bg-rail` < `--bg` < `--bg-alt` < `--bg-panel`) plus a #26292e border. Floating shadcn surfaces — Popover, DropdownMenu, Select, Dialog, Card, the ContextMenu panel — separate instead by a 1px ink ring at 10% (`ring-1 ring-foreground/10`) plus a tokenized shadow. Shadow geometry is Tailwind's; shadow ink is the app's: `tailwind.css` re-declares `--shadow-sm/md/lg/xl` on every `[data-slot]` element with `--shadow-ink*` colors from tokens.css, so shadcn shadows deepen on dark and soften on light like everything else. The dialog backdrop is the shadcn Dialog overlay — 10% black with a light backdrop blur (`--scrim` is still defined in tokens.css but no stylesheet consumes it).

### Shadow Vocabulary
- **Popover lift** (`0 4px 14px var(--shadow-sm)`): legacy tooltips, hint panels.
- **Card hover lift** (`0 4px 18px var(--shadow-sm)` + 1px rise): app preview cards on hover.
- **Menu / Select lift** (`shadow-md`: `0 4px 6px -1px var(--shadow-ink), 0 2px 4px -2px var(--shadow-ink)`): DropdownMenu, Select, Popover, ContextMenu panels.
- **Sub-menu / Toast lift** (`shadow-lg`: `0 10px 15px -3px var(--shadow-ink), 0 4px 6px -4px var(--shadow-ink)`): nested menus, toasts.

### Named Rules
**The Tonal-First Rule.** Resting surfaces separate by ground step and hairline, never by shadow; a shadow means the surface floats above the page.

## Shapes

Quiet, small radii, pinned as literals in the bridge (`--radius-sm/md/lg/xl` = 4/6/8/12px — the ladder is shifted one step because shadcn draws controls at `rounded-lg` and panels at `rounded-xl`). One scale across both vocabularies: controls — Button, Input, Select, TabsList, menu panels, sidebar rows, listing rows, legacy `.btn` — sit at **8px**; menu items and tabs at 6px inside them; checkboxes at 4px; Card / Dialog / Empty at **12px**. Badges and switches are pills. Signals are drawn as 2px *inset box-shadows*, not borders, so text never shifts between states; colored borders stay 1px (error cards, focus borders).

## Components

The component layer is shadcn (base-nova style, base-ui primitives) in `frontend/src/platform/shadcn/ui/*`, consumed through `@platform/shadcn/ui/*`. Every component paints with shadcn semantic classes (`bg-primary`, `border-input`, `ring-ring`) that resolve — through the bridge in `frontend/src/styles/tailwind.css` — to canon tokens. Tailwind runs without preflight; the one reset is `font: inherit` on `[data-slot]` form controls, so shadcn controls take the 14px app font instead of the UA's 13.3px. The `dark:` variant is `:root:not([data-theme="light"])`, so shadcn's dark refinements (`dark:bg-input/30` fills, softer destructive rings) ride the app's own theme switch.

### Named Rules
**The Bridge Rule.** A shadcn semantic variable is only ever an alias onto a canon token, never a literal: `--primary`, `--ring`, `--sidebar-primary` = `--accent`; `--primary-foreground` = `--on-accent`; `--sh-accent`, `--sidebar-accent` = `--row-bg-hover`; `--muted`, `--secondary`, `--card` = `--bg-alt`; `--popover` = `--bg-popover`; `--sh-border`, `--input` = `--border`; `--destructive` = `--error`; `--chart-n` = `--series-n`. The bridge has no theme branches — canon tokens already flip with `data-theme`, and a second light block would drift. Mapping is by role, not by name: shadcn's "accent" is the hover wash, not the brand lime; the brand lime is "primary" and "ring". The same principle governs the four names both vocabularies claim (`--shadow-sm/md/lg`, `--ease-out`): re-declared on `[data-slot]` with Tailwind's geometry and the app's `--shadow-ink*`, so shadcn keeps its shapes and the palette keeps its colors.

### Buttons (`Button`)
- **Shape:** 8px radius, 32px tall at `default`, 14px 500-weight text, 10px horizontal padding, 6px icon gap; sizes `xs` 24px / `sm` 28px / `lg` 36px, `icon`, `icon-xs`, `icon-sm`, `icon-lg` square. Smaller sizes clamp the radius (`min(--radius-md, 10-12px)`).
- **default (primary):** the one lime fill — `bg-primary` Fused Lime with On-Lime ink; hover drops to 80% alpha lime. One per screen.
- **outline:** hairline border, page ground (30% border-tint fill in dark), hover to Raised Graphite (dark: 50% border-tint); `aria-expanded` holds the hover fill for menu triggers.
- **secondary:** Raised Graphite fill, ink text; hover mixes 5% foreground in.
- **ghost:** transparent; hover Raised Graphite (50% in dark). The Modal's close ✕ is `ghost` / `icon-sm`.
- **destructive:** error wash at 10% (20% dark) with error text; hover deepens; ring goes error-tinted.
- **link:** lime text, underline on hover — used for inline "show details" affordances (TroubleCard, ClaudeHealthStrip, FilesHome).
- **Focus:** `focus-visible` sets the border to lime and adds a 3px ring of lime at 50% (`ring-3 ring-ring/50`) — no outline. **Press:** 1px translateY on non-menu buttons. **Disabled:** opacity 0.5, pointer-events none. **Invalid:** error border + 20% error ring.
- **Legacy `.btn` / `.btn-primary` / `.btn-secondary`:** still present on un-migrated forms — 8px radius, 13px 400 text, 0 14px padding, hover to Lime Hover (#eeff70) on primary; same 32px height so both generations align in a row.

### Inputs / Fields (`Input`, `Textarea`, `Field*`, `Select`)
- **Input:** 32px tall, 12px radius, hairline `border-input`, transparent fill (30% border-tint fill in dark), 14px text (`text-base md:text-sm`), 4px 10px padding, muted placeholder.
- **Focus:** the lime signal — `border-ring` lime plus the 3px 50% lime ring. **Invalid:** error border + 20% error ring. **Disabled:** opacity 0.5, 50% border-tint fill.
- **Field / FieldLabel / FieldDescription:** `Field` is a `role=group` flex column with 8px gap (`horizontal` and `responsive` orientations exist); `FieldLabel` is the shadcn `Label` — 14px 500 — which, when wrapping a control card, takes the same lime border+ring on inner focus and a 5% lime wash when checked; `FieldDescription` is muted 14px; `FieldError` is error text. Field groups stack at 20px.
- **Select:** trigger shares the Input skin with a muted chevron; the popup is a `bg-popover` panel, 12px radius, ink ring + `shadow-md`, 4px padding; items are 8px-radius rows highlighted with the hover wash.
- **Legacy `.field-control`:** 8px radius, 13px, 7px 10px, focus = lime border + `0 0 0 3px rgba(var(--accent-rgb), 0.15)`.

### Checkbox / RadioGroup / Switch
- **Checkbox:** 16px, 4px radius, hairline border (30% tint fill in dark); **checked** = lime fill, lime border, On-Lime check glyph (14px svg). Radio matches with a circular indicator.
- **Switch:** 32×18.4px pill (`sm` 24×14); track is hairline-tint when off, **Fused Lime when on**; thumb inverts. All three grow a hidden 12px/8px hit-area (`after:` inset) and take the standard lime focus ring; a wrapping `FieldLabel` absorbs the ring instead.

### Tabs (`Tabs`, URL-controlled)
- **Style:** `TabsList` default is a 32px `bg-muted` (Raised Graphite) pill at 12px radius with 3px padding; triggers are 14px 500, 8px radius, 60%-ink text; the **active** trigger lifts to page ground (dark: 30% border-tint fill) with a hairline border and `shadow-sm`. The `line` variant is transparent with a 2px **ink** (`bg-foreground`) underline — tabs deliberately do not take the lime; they are a place, not a signal.
- **Behavior:** tab state never lives in the component. Pages render `<Tabs value={tab}>` with `value` derived from the route and each `TabsTrigger` rendered as a real `<a href>` (AiModelsPage, ClaudeConfig, FilesHome), so a tab is an address — middle-click and copy-link work, left-click is intercepted for client-side navigation.

### ToggleGroup / Toggle
- Same sizes and radii as Button (32/28/36px); pressed state is a Raised Graphite fill with ink text, hover the same wash — a segmented control with no lime, matching the Tabs stance.

### Badges (`Badge`)
- **Shape:** 20px pill, 12px 500 text, 2px 8px padding, 12px icons.
- **Variants as used:** `secondary` (Raised Graphite, ink), `outline` (hairline, ink), `destructive` (10%/20% error wash, error text). `default` is a lime fill with On-Lime ink and `link` is lime text — both exist in the vocabulary but the only default-variant instance in the shell (the sidebar Beta chip) overrides itself to lime text on a 12% lime wash via `.sidebar-beta-chip`.

### Cards (`Card`)
- 16px radius, Raised Graphite fill (`bg-card`), 14px text, 1px ink ring at 10% instead of a border, 16px internal spacing (`--card-spacing`; `size="sm"` 12px); header is a grid with 4px row gap, title 16px 500, description muted.
- **App preview card (bespoke):** 16/10 thumb well on a dot-grid ground (`radial-gradient(rgba(var(--tint),0.1) 1px, transparent 1px)` at 14px pitch over `--bg`); hover warms the border toward `--fg-muted`, adds the card-hover lift and rises 1px.

### Tables (`Table`)
- shadcn `Table` is 14px, rows hairline-bottom with a 50% muted hover and `data-[state=selected]` muted fill; header cells 40px tall, 500 weight; body cells 8px padding, no wrap. Used for generic data grids only — the file listing is not this component (see Listing rows).

### Dialog (via `Modal`)
- Every dialog in the app renders through the shared chassis `frontend/src/platform/ui/modal/Modal.tsx`, which sits on shadcn `Dialog`/`DialogContent` (portal, focus trap/restore, aria) and keeps the app's behavioral contract: a **busy** gate (Esc / backdrop / ✕ do not close while an action must not be abandoned — the ✕ is not rendered at all), a **dirty** guard (first close attempt arms an inline "Unsaved changes — close again to discard" hint and turns the ✕ warning-amber; any real interaction inside the form disarms; the next close discards), and a deferred exit so base-ui's `data-closed` animation plays before unmount.
- **Skin:** `bg-popover` panel, 16px radius, ink ring, centered at `top-1/2 left-1/2`, min-width 384px and `sm:max-w-2xl`; header and footer are 12px 16px rows separated by hairline `border-border`; body 16px padding, keeping the `deploy-body` form vocabulary unless `plainBody`. Backdrop: 10% black with a light blur, 100ms fade; content zooms from 95%.
- `AlertDialog` and `Sheet` share the same primitives and skin.

### Menus (`DropdownMenu`, `ContextMenu`, `Popover`)
- **Panel:** `bg-popover`, 12px radius, 4px padding, 1px ink ring at 10%, `shadow-md` (`shadow-lg` for the context-menu / sub-menu panel), min-width 128-160px, 100ms fade + slide-in from the anchor side.
- **Item:** 8px radius, 4px 6px padding, 14px text, 6px icon gap, 16px icons; **highlighted** (focus / hover) takes the hover wash (`bg-accent` = `--row-bg-hover`) with ink text; `destructive` items are error text on a 10-20% error wash; disabled items 50%. Separators are 1px hairline with 4px margins; labels 12px muted.
- **ContextMenu (bespoke engine):** `frontend/src/platform/ui/ContextMenu.tsx` wears the identical panel/item Tailwind vocabulary so both menu families read as one, but keeps its own imperative engine — cursor anchoring, viewport clamp, hover-intent, lazy one-level submenus, dismiss on scroll/resize/blur — because the declarative base-ui menu tree has no home for an `(x, y, items, onClose)` API. It is also the app's one button-triggered dropdown of choices.

### Tooltip (`Tooltip`)
- Inverted: `bg-foreground` ink panel with `text-background` text, 12px type, 8px radius, 6px 12px padding, 10px rotated-square arrow; embedded `Kbd` chips flip to 20% background tint.

### Empty, Skeleton, Spinner, Kbd, Alert, Toast
- **Empty:** centered column, 16px radius dashed border, 24px padding, 16px gaps; an optional 32px Raised Graphite icon well at 12px radius; description muted 14px with lime link hover.
- **Skeleton:** `animate-pulse` Raised Graphite block at 8px radius (the legacy `Skeleton.tsx` shimmer remains on the app-card thumb).
- **Spinner:** 16px `animate-spin` Loader glyph, inherits color.
- **Kbd:** 20px, min-width 20px, 4px radius, Raised Graphite fill, 12px 500 muted sans.
- **Alert:** 12px radius, hairline border, 10px 8px padding, 14px text, icon in a 16px leading column; `destructive` variant paints error text on the card ground.
- **Toast:** bottom-right stack on `bg-popover`, 20px radius, hairline border, `shadow-lg`, 16px padding; title 14px 500, description muted; swipe-to-dismiss on all four axes with a 250ms ease-out slide; focus takes the lime ring.

### Navigation (sidebar rows — bespoke)
- **Style:** `.sidebar-item` rows on the Rail Black ground, 8px radius, 7px 10px padding, 10px icon gap, 13.5px text, icons at 0.85 opacity; `white-space: nowrap` so labels never wrap mid-glide.
- **Hover / Active:** a 0.06 tint wash; the active row additionally wears the lime marker — `inset 2px 0 0 var(--accent)` — the canon "you are here".
- **Why bespoke:** `SidebarFrame` owns the drag-resize handle (`resizeWidth` / `reopenWidth`, min/max clamps) and the expand/collapse glide between the 44px rail and the settled width, which the shadcn `Sidebar` component does not model.

### Listing rows (signature — bespoke)
- A native `<table class="listing-table">`, not shadcn `Table`: rows are a selection widget with their own press model — selection on mouse-down, `user-select: none`, Shift-range anchoring handled in `Listing.tsx` — plus drag-and-drop targets, cut/ignored dimming, FLIP reorders and the D460 preview-pane contract; none of that fits a declarative table.
- **Rest:** 9px 16px cells, hairline bottom border. **Hover:** `--bg-alt`; **selected:** opaque `--row-bg-active` wash plus a 2px lime inset on the first cell — the same one-signal grammar as the nav marker. Drop targets extend the lime insets to top/bottom edges; new rows flash a 1.5s accent-wash fade; `.ignored` rows sit at 0.45 opacity; `.cut` rows dim with a dashed left edge.
- **Metadata cells:** the mono-for-measurement voice, right-aligned tabular size column.

### Segmented controls (bespoke)
- `.schedule-form-seg`: a hairline-bordered 8px pill whose halves are borderless `.btn`s sharing 1px hairline dividers; the active half is a quiet Raised Graphite fill with no weight change (bolding re-measured the row and jittered the toggle). Kept on the legacy button skin because the fill-relationship with its toolbar ground is what carries state; the container flips to `--bg` when it sits on a `--bg-alt` bar so the active fill stays a shade lighter than its rail.

### Motion (applies to all of the above)
Three durations, one ease, defined once in base.css: `--dur-fast` 80ms (hover/press color), `--dur-med` 150ms (overlays, toasts, menus), `--dur-slow` 200ms (panels, sidebar collapse, FLIP reorders); `--ease-out`. shadcn overlays (menus, popovers, dialogs, selects) enter with a 100ms fade + 95% zoom / 8px slide from the anchor side via tw-animate-css, and exit on `data-closed`. Press feedback is one shared 1px translateY nudge on button-shaped controls only. `prefers-reduced-motion` collapses all of it.

## Do's and Don'ts

### Do:
- **Do** paint every color with a `var(--token)` from tokens.css and give any new dark token a light counterpart.
- **Do** reach for the shadcn component first (`@platform/shadcn/ui/*`) and style it with semantic classes (`bg-primary`, `border-input`, `text-muted-foreground`) — never with a canon token name or a literal in `className`.
- **Do** draw selection/active edges as 2px inset box-shadows in Fused Lime, never as borders that shift text.
- **Do** keep the rail darker than the pane in any new surface ordering (`--bg-rail` < `--bg` < `--bg-alt`).
- **Do** set measurements (sizes, timestamps, counts) in ui-monospace 12px muted with tabular-nums.
- **Do** pick transitions from the three tokens (80/150/200ms, ease-out); one primary lime fill per screen.
- **Do** open every dialog through `Modal` so the busy gate and dirty guard travel with it.

### Don't:
- **Don't** hardcode a color literal outside the two palette blocks in tokens.css (test-enforced) — and never set a shadcn variable in `tailwind.css` to anything but a canon token alias.
- **Don't** read shadcn's `accent` as the brand color: `bg-accent` is the hover wash; the lime is `primary` / `ring`.
- **Don't** use Fused Lime for decoration, prose, large fills, or more than the sanctioned signals — a `default` (lime) Badge or `link` Button is a primary action, not a label.
- **Don't** use raw #E5FF44 as text or border in the light theme — the light `--accent` is the contrast-corrected olive-lime.
- **Don't** add shadows to resting surfaces; hairline + ground step carry structure. Floating shadcn surfaces get the ink ring + tokenized shadow, nothing more.
- **Don't** invent new grays — layer `rgba(var(--tint), …)` washes over existing grounds instead.
- **Don't** give tabs or toggle groups the lime; they mark a place with ground and ink, and the lime stays for "you are here" and "do this".
