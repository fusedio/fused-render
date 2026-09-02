# Refactor playbook — bringing an existing app onto the Flow design language

Follow these phases in order. Each phase leaves the app shippable; don't mix
phases in one PR. Read `SKILL.md` first — it holds the rules; this file holds
the procedure.

## Phase 0 — install the foundation

1. If the app isn't on Tailwind v4 + shadcn, decide: adopt the stack (Layer 2)
   or apply Layer 1 rules only in the existing stack. Prefer adopting.
   Dependencies the assets require: `tailwindcss@4`, `@tailwindcss/typography`
   (or delete the `@plugin` line in `tokens.css`), `class-variance-authority`,
   `clsx`, `tailwind-merge`, `radix-ui`, `lucide-react`.
2. Copy `assets/tokens.css` in as the root stylesheet; add `@source` lines for
   the app's source trees. Copy `assets/components.json` settings into the
   app's `components.json` (style new-york, baseColor neutral, cssVariables
   true, iconLibrary lucide).
3. Copy `assets/status-colors.ts` into the shared UI kit; trim status keys to
   the app's actual lifecycle vocabulary but keep the color buckets and the
   neutral fallback.
4. Put `.dark` on `<html>` by default (or wire the app's theme toggle to it).
5. Verify: page renders with dark neutral background, square cards, and the
   app still builds.

## Phase 1 — color audit

1. Sweep for raw colors: `grep -rnE '#[0-9a-fA-F]{3,8}|rgb\(|rgba\(|hsl\(' src/`
   plus Tailwind palette classes used for chrome (e.g. `bg-gray-*`,
   `text-slate-*`, `bg-zinc-*`).
2. Map each hit to the nearest semantic token: page bg → `bg-background`,
   surfaces → `bg-card`/`bg-popover`, text → `text-foreground` /
   `text-muted-foreground`, hovers → `bg-accent`, borders → `border-border`,
   danger → `destructive`. Chromatic color survives only if it is a status,
   priority, or chart color — and then only through `status-colors.ts`.
3. Leave brand/marketing pages out of scope unless asked.

## Phase 2 — radius and shadow sweep

1. `grep -rnE 'rounded-(2xl|3xl|\[)' src/` — replace with `rounded-lg` (square)
   for surfaces or `rounded-md` for controls; keep `rounded-full` for pills,
   avatars, dots.
2. `grep -rnE 'shadow-(md|lg|xl|2xl)' src/` — downgrade to `shadow-sm` (cards)
   or `shadow-xs` (outline controls) or delete; add a border if separation is
   lost.

## Phase 3 — primitive adoption

1. Install/replace primitives via shadcn using the copied `components.json`
   (button, card, input, textarea, select, dialog, popover, dropdown-menu,
   tooltip, tabs, command, avatar, badge, separator, skeleton, checkbox,
   label, scroll-area).
2. Bring each primitive to the `assets/button.tsx` shape: `data-slot`,
   `data-variant`/`data-size`, `cn()` merge, trailing `className`, `asChild`
   where useful.
3. Replace hand-rolled buttons/inputs/modals page by page. Don't add domain
   logic to primitives — wrap them in composites.

## Phase 4 — composite convergence

1. Build the app's `EntityRow`-equivalent and converge every list surface onto
   it (rule: same visual pattern in 2+ places becomes a component).
2. Build `StatusIcon` (+ `PriorityIcon` if the app has priorities) reading
   from `status-colors.ts`; delete per-page status styling.
3. Apply the typography scale (SKILL.md Layer 1 §5) to headings, rows,
   metadata, identifiers.
4. Convert detail-view modals into a right-side `w-80` properties panel built
   from property rows; convert edit dialogs into inline click-to-edit where
   reasonable.

## Phase 5 — interaction layer

1. Add the Cmd/Ctrl+K command palette (cmdk) navigating the app's main nouns;
   navigation only, no mutations.
2. Add the global shortcut hook (palette, composer submit, panel toggles);
   suppress single-key shortcuts while a text field is focused.
3. Verify the coarse-pointer 44px floor works (the `data-size` stamps make the
   dense exemptions apply).

## Phase 6 — motion & polish

1. Sweep all animations: cap entries around 300–520ms, prefer the house easing
   `cubic-bezier(0.16, 1, 0.3, 1)`, and add a `prefers-reduced-motion` guard
   to every one.
2. Apply `scrollbar-auto-hide` to long internal scroll regions.

## Definition of done

- No raw color outside `tokens.css` / `status-colors.ts`.
- Cards/dialogs square; controls `rounded-md`; pills round; nothing rounder.
- All list surfaces on the shared row composite; all statuses through the map.
- Cmd+K works; every animation guarded.
- `MAINTENANCE.md` checklist adopted in the repo's review process.
