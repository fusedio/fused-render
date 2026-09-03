---
name: flow-design-language
description: The Flow (openfused) UI design language — OKLCH neutral tokens, squared cards, dense keyboard-first surfaces. Load whenever writing, refactoring, or reviewing UI in any app adopting this language so every screen follows the same rules.
---

# Flow design language

Extracted from `fusedio/flow` @ commit `9e93d88`. Exact values live in the
adjacent `assets/` files — copy them, don't retype them:

- `assets/tokens.css` — the full token sheet (colors, radius, base layer, touch targets, scrollbars, motion).
- `assets/status-colors.ts` — the single-source status/priority → color maps.
- `assets/button.tsx` — the exemplar CVA primitive; every primitive follows its shape.
- `assets/cn.ts` — the `cn()` class-merge helper `button.tsx` imports.
- `assets/components.json` — shadcn config (new-york, neutral, cssVariables, lucide).

`PLAYBOOK.md` (same folder) is the step-by-step refactor procedure for bringing
an existing app onto this language. `MAINTENANCE.md` is the per-PR enforcement
checklist. Read those when doing that work; the rules below apply always.

## Layer 1 — rules that hold in any stack

1. **Semantic tokens only, never raw color.** Every color in a component is a
   semantic token (`background`, `foreground`, `card`, `popover`, `primary`,
   `secondary`, `muted`, `accent`, `destructive`, `border`, `input`, `ring`,
   `sidebar-*`, `chart-1..5`, each with a `-foreground` pair where relevant).
   No hex, no rgb, no ad-hoc oklch at a call site. Both themes are neutral
   OKLCH grays (chroma 0); chromatic color is reserved for status/priority
   and charts.
2. **Dark theme is the default posture.** Light theme exists; both are defined
   as full token sets. Dark is class-driven (`.dark` on `<html>`), with
   `color-scheme` set per theme and dark-styled scrollbars.
3. **Squared cards, curved controls.** Base radius is 0. Large surfaces
   (cards, dialogs) have square corners. Interactive controls (buttons,
   inputs) use a small radius (0.5rem). Pills, avatars, and status dots are
   fully round. Nothing rounder than that anywhere.
4. **Status color is single-sourced.** One central map binds status → color;
   no call site invents a status color. The semantic buckets: green =
   done/published/fresh/active; yellow = in progress/draft/resolving; blue =
   todo; orange = paused/stale/waiting-on-user; red = failed/error/broken;
   neutral = cancelled/archived/unknown (also the fallback — an unknown
   status renders neutral, it never throws). Priority: critical = red
   triangle, high = orange up-arrow, medium = yellow minus, low = blue
   down-arrow.
5. **Fixed typography scale, never invented.** Page title `xl/bold`; section
   title `lg/semibold`; section heading `sm/semibold muted uppercase
   tracking-wide`; row title `sm/medium`; body `sm`; muted `sm muted`; tiny
   metadata `xs muted`; data identifiers `xs mono muted`; large stat
   `2xl/bold`; code/logs `mono xs`.
6. **Shadows minimal.** Extra-small on outline controls, small on cards,
   nothing heavier. Borders and background shifts do the separating.
7. **Dense but scannable.** Maximum information without a click to reveal;
   whitespace separates, it does not pad. Lists are bordered rows
   (`px-4 py-2`-scale), not spaced-out cards.
8. **Contextual, not modal.** Inline click-to-edit over dialogs; a right-side
   properties panel over a modal; dropdowns over page navigations. A new
   dialog is a last resort.
9. **Keyboard-first.** Cmd/Ctrl+K command palette for global navigation;
   Cmd/Ctrl+Enter submits the focused composer; single-key shortcuts
   suppressed while a text field is focused.
10. **Motion is subtle and guarded.** Short (≤520ms) ease-out entries, the
    house easing `cubic-bezier(0.16, 1, 0.3, 1)`; every animation has a
    `prefers-reduced-motion: reduce` guard that disables it.
11. **Touch floor with dense exemption.** 44px minimum interactive height on
    coarse pointers, except small inline widgets (checkboxes, icon-xs/sm
    buttons, chips) whose surrounding row provides the touch area.
12. **Component-driven.** A visual pattern used in 2+ places, or carrying
    interactive behavior or domain logic, becomes a shared component. A
    one-off layout or thin class combo does not.

## Layer 2 — the implementation (Tailwind v4 + shadcn)

- **Stack**: Tailwind v4 (`@theme inline` token bridge), shadcn/ui new-york
  style, neutral base, `cssVariables: true`, Radix-backed primitives, Lucide
  icons (16px nav, 14px inline), `cva` for variants, `cn()` =
  `twMerge(clsx(...))` for class merging.
- **Tokens**: install `assets/tokens.css` as the app's root stylesheet (add
  the app's own `@source` lines). `rounded-lg`/`rounded-xl` resolve to 0px by
  design; only `rounded-sm`/`rounded-md`/`rounded-full` curve. Never use
  `rounded-2xl`+ or `shadow-md`+.
- **Status colors**: install `assets/status-colors.ts` in the shared UI kit
  and import it everywhere a status renders; extend the map there, never
  inline. Use Tailwind `*-600 dark:*-400` pairs as it does.
- **Primitives** follow `assets/button.tsx`: a `cva` definition, a
  `data-slot` attribute, `data-variant`/`data-size` stamps (the coarse-pointer
  CSS targets these), `cn()` merging, a forwarded trailing `className`, and
  `asChild` via Radix Slot where links need to be buttons. Focus ring:
  `focus-visible:border-ring focus-visible:ring-ring/50
  focus-visible:ring-[3px]`; disabled: `disabled:pointer-events-none
  disabled:opacity-50`; icons `[&_svg:not([class*='size-'])]:size-4`.
- **Composite patterns** (compose primitives, don't fork them):
  - **Entity row** — the universal list row: `flex items-center gap-3 px-4
    py-2 text-sm border-b border-border last:border-b-0`; clickable adds
    `cursor-pointer hover:bg-accent/50`; selected adds `bg-accent/30`; group
    wrapped in `border border-border rounded-md`. Slots: leading status icon
    first, mono identifier, title, optional meta, trailing badge/timestamp
    pinned right.
  - **Property row** — `text-xs text-muted-foreground` label left, value
    right, `py-1.5`, container `space-y-1`; stacked in a `w-80` right-side
    properties panel on detail views.
  - **Metric cards** — `grid md:grid-cols-2 xl:grid-cols-4 gap-4`.
  - **Progress/meter bar** — green <60%, yellow 60–85%, red >85%.
  - **Log viewer** — `bg-neutral-950 rounded-lg p-3 font-mono text-xs`;
    WARN yellow-400, ERROR red-400, SYS blue-300; a live dot when streaming.
  - **Status dot** — `size-1.5 rounded-full` in the status color.
  - **Empty state** — icon + one-line message + optional single CTA.

## Enforced negatives (reject in review)

Raw hex/rgb instead of a token · ad-hoc typography outside the scale ·
status colors invented at a call site · `shadow-md`+ · `rounded-2xl`+ ·
forgetting `rounded-lg`/`xl` are square · an animation without a
reduced-motion guard · domain logic added inside a primitive (extend by
composition) · a dialog where inline editing or a panel would do.
