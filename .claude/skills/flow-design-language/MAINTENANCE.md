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
