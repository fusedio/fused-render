# Long-term maintenance — keeping the design language enforced

## Provenance

Source of truth: `fusedio/flow`, extracted @ commit `9e93d88` from
`packages/ui-kit/src/index.css`, `packages/ui-kit/src/status-colors.ts`,
`packages/ui-kit/src/button.tsx`, `app/components.json`, and
`spec/ui/app-design-system.md`. To re-sync, diff those files against this
folder's `assets/` and update both `assets/` and the commit stamp here and in
`SKILL.md`. Where the spec and shipped code disagree, shipped code wins (known
case: `blocked` is orange in `status-colors.ts` — waiting on the user, not an
error — while the spec still says red).

## Per-PR review checklist (agents: run on every UI diff)

Grep the diff, not the tree — flag only new violations:

1. **Raw color** — `#hex`, `rgb(`, `hsl(`, ad-hoc `oklch(` in components, or
   chrome-colored palette classes (`bg-gray-*`, `text-slate-*`, …). Must use a
   semantic token; chromatic color only via `status-colors.ts` or chart tokens.
2. **Radius** — any `rounded-2xl`/`rounded-3xl`/`rounded-[...]`. Reject.
   Remember `rounded-lg`/`xl` render square; a reviewer seeing "rounded-lg on a
   button" should suggest `rounded-md`.
3. **Shadow** — `shadow-md` or heavier. Max `shadow-sm`.
4. **Status color at a call site** — a status/health/lifecycle color not
   imported from the central map. Move it into the map.
5. **Typography** — a font-size/weight combo outside the SKILL.md scale.
6. **Unguarded motion** — a new `@keyframes`/`animate-*`/`transition` driving
   movement without a `prefers-reduced-motion: reduce` guard (color/opacity
   transitions are fine).
7. **Primitive purity** — domain logic or one-off styling added inside a
   shared primitive instead of a wrapping composite.
8. **Pattern duplication** — a visual pattern now in 2+ places that isn't a
   shared component yet. Ask for extraction (or a follow-up ticket).
9. **New dialog** — challenge it: would inline editing, a popover, or the
   properties panel do?
10. **Touch targets** — new dense controls carry the `data-size`/`data-slot`
    stamps so the coarse-pointer exemption applies.

## Wiring it into agents (target repo)

- Copy this whole `design-language/` folder into the target repo as
  `.claude/skills/flow-design-language/` (SKILL.md frontmatter makes it
  loadable) and add one line to the repo's `CLAUDE.md`:
  "All UI work must follow `.claude/skills/flow-design-language/SKILL.md`;
  review UI diffs against its MAINTENANCE.md checklist."
- Optionally add a CI grep step for the mechanical rules (1–3 above) so drift
  is caught even without an agent in the loop.

## Evolving the language

Changes to tokens, radius, the status map, or the scale happen in the source
repo first (`flow`), then propagate here by re-sync. If the target app needs a
divergence (e.g. brand accent color), record it in this file under a
"Deviations" section with the reason — undocumented drift is how design
languages die.

## Deviations (fused-render)

- **Dark keyed off `data-theme`, not `.dark`.** The pre-paint bootstrap in
  `frontend/index.html` stamps `data-theme` on `<html>`, and framed shells
  (embed panes, `.fused` apps) inherit theme through that attribute. The
  `dark` variant in `styles/tailwind.css` is `:root:not([data-theme="light"])`.
  Dark stays the no-attribute default.
- **Primitives are base-ui, not Radix.** `components.json` is shadcn
  `base-nova` on `@base-ui/react`; the app already shipped 15 primitives on
  it before adopting Flow. Same `data-slot`/`cn()`/`cva` shape as
  `assets/button.tsx`; only the underlying headless library differs.
- **No preflight.** ~27k lines of legacy CSS predate Tailwind; the token
  sheet imports `theme.css` + `utilities.css` only and applies form-control
  font inheritance to `[data-slot]` roots. Revisit once the legacy files are
  gone.
- **Two token vocabularies coexist.** `styles/tokens.css` (legacy `--fg`,
  `--bg`, `--border`, `--accent` …) is re-pointed at the same neutral OKLCH
  values as the Flow set, so unconverted surfaces wear the language; shadcn's
  border/accent read `--sh-border`/`--sh-accent` to dodge the name clash.
  Delete legacy tokens as their last consumer is converted.
- **Brand chartreuse retired.** `--accent` (#E5FF44) now equals the neutral
  primary in both themes. Chromatic colour is status/priority/chart only.
