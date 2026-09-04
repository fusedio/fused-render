# shadcn migration playbook

How a page moves from hand-written CSS to shadcn + Tailwind **without changing
what the user sees or does**. Written from the AI Models Playground migration
(`agent/20260903-playground-shadcn`, Sept 2026). Reuse it page by page.

## The contract

- **Skin swap, not redesign.** Same tokens, same px, same durations, same
  easing. Lime stays a signal (ring, selection, active), never a fill.
- **Behaviour is frozen.** Handlers, hooks, timers, URL sync, aria, `title`,
  `disabled`, `role`, `tabIndex`, `stopPropagation`, refs that measure — all
  byte-identical. Only JSX wrappers and `className` change.
- **shadcn provides chrome, never semantics you didn't have.** `<details>` stays
  `<details>`, native `<select>` stays native, a `div role="button"` stays a
  div. Do not "upgrade" a toggle row to `ToggleGroup` (roving tabindex is a
  behaviour change). A shadcn `Dialog` for a lightbox is the one accepted
  additive change (Esc + click-outside kept, focus trap gained).
- **One page per branch, one commit per step, screenshot-diffed against
  `main` at every step.**

## Layers (already in place, extend — don't fork)

1. **Token bridge** — `frontend/src/styles/tailwind.css`. Every shadcn
   semantic var aliases a `tokens.css` token in one `:root` block; light/dark
   comes free from `tokens.css`. `primary` = the app's filled button
   (`--fg` on `--bg`), `ring` = `--accent`, `accent` = hover wash
   (`--row-bg-hover`), `card` = `--bg-card`. Add a new shadcn var only as
   `var(--<app-token>)`, never a literal. `tests/test_theme.py` enforces the
   light palette covers every dark token.
2. **Primitives** — `frontend/src/platform/shadcn/ui/`. Installed with
   `npx shadcn@latest add <name>`, left unmodified. Installed today: badge,
   button, card, checkbox, dialog, empty, field, input, kbd, label, progress,
   scroll-area, separator, skeleton, slider, tabs, textarea, toggle,
   toggle-group, tooltip. `TooltipProvider` is mounted once in `main.tsx`.
3. **Page composites** — `frontend/src/platform/ui/<page>/`. Hand-written,
   Tailwind + `cva` + `cn()`, **no CSS files**. One file per composite, an
   `index.ts`, and a `README.md` mapping every old class → composite/prop.
   Generic pieces (Composer, CopyButton, ProgressBar, Chips, Lightbox,
   ResultSlot, StageHeader/ConfigRail…) live in `platform/ui/playground/` for
   now; promote to `platform/ui/` when a second page uses them.

## Steps

### 0. Fresh base
Worktree off `origin/main`: `git worktree add ../fused-render-wt/agent-<date>-<page>-shadcn -b agent/<date>-<page>-shadcn origin/main`.
Run `scripts/dev.sh` there (own port). Keep a `main` server running too
(`scripts/dev.sh --port 1778` from the main checkout) — it is the reference
for every probe and screenshot.

### 1. Inventory (two agents, parallel, read-only)
Save both into `.design/` (git-excluded via `.git/info/exclude`).

- **`parity-events.md`** — every interactive element: file:line, classes,
  events (`onClick/onKeyDown` keys/`onChange`/drag/drop/focus/blur/paste),
  what the handler does, `disabled` rule, aria/title, auto-behaviours
  (autofocus, debounce, timers, Esc/click-outside, ResizeObserver, rAF),
  conditional render states, imperative DOM. Plus cross-cutting: URL params,
  persisted prefs, tour hooks / `querySelector` consumers, tests that pin
  class names.
- **`parity-css.md`** — every class the page's TSX uses: selector, file:line,
  base look, `:hover :focus-visible :active :disabled`, state classes,
  `::placeholder`, transitions (property/duration/easing), `@keyframes`,
  `:has()`, light overrides, reduced-motion, `@media`/`@container`. Token set
  used. Then: which pieces map to an installed primitive, which need a new
  primitive, which are bespoke composites.

### 2. design.md → get OK
Goal, non-goals, the three layers, the composite table (old class →
composite → built on), the hard rules, the step sequence, the verification
gate, open decisions. Inherit `~/.claude/design-principles.md`.

### 3. Bridge + primitives (only if the page needs new ones)
`npx shadcn@latest add …`, fix unused-import lint in generated files, run
`tests/test_theme.py`, tsc, screenshot every existing shadcn surface
before/after (bridge changes are app-wide).

### 4. Composites (one opus agent)
Build every composite from `parity-css.md`. Each value copied, not
approximated. Then migrate the page's shell/rail/controls files. Write the
`README.md` map so stage agents can swap className-only.

### 5. Stage / section files (parallel agents, one file each)
Each agent: reads design + parity files + README; swaps `pg-*`/`.btn` for
composites; forbidden from touching composites or other files (adds a local
wrapper if a prop is missing and reports it). Verifies with tsc, `bun test`,
computed-style probes vs main.

### 6. Cleanup (one agent)
Delete the old stylesheet + its `@import` in `shell.css` (the barrel test
requires both). Remove transitional selectors, local workarounds, `!`
hacks that only existed because the old sheet still leaked. Grep for
leftover class strings **outside** the page dir too (`AiModelsPage.tsx`
still emitted `pg-fill` — the one thing this pass missed). Keep tour-hook
classes as style-free selectors until the tour is retargeted.

### 7. Gates (every step, and before handoff)
- `npx tsc --noEmit`, `node scripts/check-boundaries.mjs`.
- `bun test` — same pass count as main. Never delete a test; rewrite
  source-substring class pins as DOM/behaviour assertions.
- `.venv/bin/python -m pytest -q tests/test_theme.py`.
- Computed-style probes vs main (see below), 0 mismatches.
- Screenshot matrix vs main, pixel-diff per state (see below).
- Console: zero errors on load and on every exercised flow.

## Verification recipes (cmux browser CLI)

Probes work even when cmux is hidden; screenshots do not.

```sh
# one surface, both servers through it (only the pane's active surface paints)
SID=$(cmux browser open "http://127.0.0.1:1778/..." | grep -oE 'surface:[0-9]+')
cmux browser $SID viewport 1440 900          # emulated viewport, pane size irrelevant
cmux browser $SID goto "http://127.0.0.1:<branch>/..."
cmux browser $SID eval "document.querySelector('.driver-popover-close-btn')?.click()"   # kill tour
cmux browser $SID eval "String(document.hidden)"   # must be "false" before any screenshot
cmux browser $SID hover --selector "#x" ; cmux browser $SID click --selector "#x"
cmux browser $SID screenshot --out main-1.png
```

- **Computed-style probe**: `getComputedStyle(el)` for padding, border,
  radius, bg, color, font-size/weight, height, box-shadow, opacity,
  transition at rest and after `hover --selector`; plus
  `aria-*/title/role/tabindex/disabled` on every `button, [role=button],
  input, textarea, select, a[href]`. Same script on both servers, diff the
  JSON.
- **Screenshot matrix**: fixed viewport, same URL (`?model=` etc.), same
  scripted steps (landing → hover row → open cog → select → type → each
  section), main then branch, then `PIL.ImageChops.difference` per pair.
  Equalise profile state first (sidebar collapsed flag in localStorage,
  tour dismissed).
- **Selectors must be scoped to the page**: `main button[aria-expanded]`,
  not the first `button[aria-expanded]` in the document (that was the
  sidebar Settings menu on main).

## Tailwind gotchas that bit us (check for each in review)

- Named `text-sm` etc. also set `line-height`. Use `text-[13px]` when the
  original set font-size only.
- `border-solid` next to a one-side width (`border-r`) gives the other three
  sides a width too. Use `border-r border-solid` only when the original had
  `border: 1px solid` all round.
- `[font-variant:inherit]` is a shorthand and resets `tabular-nums`.
- `transition-transform` ≠ `transition: transform` (adds a filter list).
  Use `transition-[transform]`.
- `border-0` is `border-style: none`, not solid-at-zero; `rounded-full` is
  `9999px` not `50%`.
- Same-specificity utilities collide: put `border-[var(--border)]` and
  `border-[var(--accent)]` in **exclusive** variant branches, never base +
  override.
- Unlayered app CSS (`.cc-main`, tour skins, anything left in `styles/`)
  beats `@layer utilities` regardless of specificity. Either delete the
  rule or use the `!` modifier (`pb-4!`) — and remove the `!` once the rule
  is gone.
- `hover:` compiles to `@media (hover: hover)`; `:hover:not(:disabled)` via
  arbitrary variant does not. Identical on pointer devices; note it.
- Every `@media (prefers-reduced-motion)` opt-out → `motion-reduce:`.
  `@container (min-width: 768px)` → `@container` + `@3xl:` (verify the px).
- `cva` prop types: `ComponentProps<typeof Button> & VariantProps<…>`
  intersects two `variant` unions → collapses. `Omit<…, "variant">` first.
- `React` import in generated shadcn files trips `noUnusedLocals`.

## Environment gotchas

- **Dev server child dies silently** (zombie, watcher alive, port closed)
  after Python reloads — macOS fork/atfork issue. `pkill` the worktree's
  `dev.sh`/`watchfiles`/`vite` and relaunch with `nohup`.
- `vite build --watch` empties `shell-dist/` mid-rebuild; a `FileNotFoundError:
  shell-dist/index.html` 500 for a few seconds is not a bug.
- Harness background tasks are capped (~10 min) and get killed with the
  servers inside them. Launch long-lived servers with `nohup … &`.
- cmux hidden (user in another app) ⇒ `document.hidden === true`, rAF dead,
  screenshots return stale/blank frames. Poll `document.hidden` before
  capturing; computed-style probes still work.
- Opus 529s: retry the same prompt on sonnet; it did every stage file fine.

## Agent prompt skeleton (stage file)

```
Worktree <path>. Do not commit. Work ONLY on <file>. Others own the rest.
READ FIRST: .design/design.md, .design/parity-events.md (<section>),
.design/parity-css.md (<sections>), platform/ui/<page>/README.md.
TASK: swap every <old-prefix>-* / .btn className for the matching composite
so the result is visually and behaviourally identical to main. Skin swap only.
HARD RULES: no change to handler bodies, hooks, timers, URL sync, fetch/abort,
aria-*, title, placeholder, disabled, role, tabIndex, stopPropagation, refs.
Keep tour hooks. No new CSS files, no eslint-disable, don't edit the old sheet.
VERIFY: tsc clean; bun test = baseline; probes vs main (list elements);
git diff audit: grep 'onClick|onKeyDown|onChange|useState|useEffect|useRef|
setTimeout|writeParams|aria-|title=|disabled=' — every hit a moved line.
REPORT: probe table, tsc/test counts, deviations + why, git diff --stat.
```

## Playground outcome (for calibration)

- 2233 lines of CSS deleted; 30 composites; 5 commits.
- ~640 computed properties probed, 0 mismatches after fixes.
- 2939/2939 tests, 133/133 theme tests, tsc + boundaries clean.
- Accepted additive deltas: Dialog focus trap + aria on lightbox,
  Progress/ProgressRing aria roles, `(hover: hover)` gate.
- Time sink to avoid next time: screenshots. Set the emulated viewport and
  check `document.hidden` **first**.
