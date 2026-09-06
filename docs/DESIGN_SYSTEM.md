# fused-render design system

## 1. Overview

fused-render is dark-first: a cool near-black ground, hairline borders, and
off-white ink, with exactly one loud colour — Fused Lime (`--accent`,
`#E5FF44`) — reserved as a signal (selection edge, focus ring, active state),
never a fill except the brand mark and the one primary button per screen.
Type is dense and OS-native (the system sans stack, not a webfont); radii are
quiet and small; motion is short and always has a reduced-motion escape.

The system has exactly one set of tokens with two consumers: hand-written CSS
reads them as `var(--token)`, and `platform/ui` composites (Tailwind + shadcn)
read the same values through named utility classes (`text-dense`, `p-3`,
`rounded-card`). A legacy rule and a composite therefore always resolve to the
same pixel, the same millisecond — there is no second source of truth.

Two guard tests hold this together:
- `tests/test_theme.py` — every colour is a token, the light palette redefines
  every dark token, and no colour literal survives outside the two palette
  blocks (`tokens.css`, plus each tier-1 built-in template's own copy).
- `tests/test_design_scale.py` — every font-size, radius, spacing and
  duration is a token from `scale.css`; a raw px/ms literal for one of those
  properties anywhere else fails the suite unless it is a genuine one-off in
  `tests/design_scale_allowlist.txt`.

## 2. Tokens: where they live

| File | Carries | Theme-aware? | Consumers |
|---|---|---|---|
| `frontend/src/styles/tokens.css` | Colour — `--fg`, `--bg`, `--border`, `--accent`, status colours, series colours, icon colours, scrims, shadow ink | Yes — dark `:root` + `:root[data-theme="light"]`, one-for-one | Legacy CSS (`var(--fg)`), the shadcn bridge |
| `frontend/src/styles/scale.css` | Rhythm — type roles, spacing, radius, control heights, motion durations/easings, fonts | No — deliberately outside the palette blocks, so `test_theme.py` never asks a font-size for a light twin | Legacy CSS (`var(--text-dense)`), Tailwind `@theme inline` |
| `frontend/src/styles/tailwind.css` | The shadcn→token bridge, the `@theme inline` mapping of `scale.css` names into Tailwind utilities, the four names Tailwind and the app both claim (`--shadow-*`, `--ease-out`) | Inherits theme from the tokens it aliases | shadcn primitives, Playground composites |

**Rule: never a literal outside these three files.** A rule that needs a
colour, a size, a radius or a duration reaches for a token; if the value
doesn't exist yet, it is added to `tokens.css` or `scale.css`, not inlined.
`scale.css` and `tailwind.css` are the two files `test_design_scale.py`
exempts from the no-literal check (they *are* the scale); the vendored
`platform/shadcn/ui/*` primitives are exempt too, as installed, unmodified
code.

## 3. Colour

All values from `frontend/src/styles/tokens.css`. Dark is the default
`:root`; light is `:root[data-theme="light"]`.

| Token | Dark | Light | Role |
|---|---|---|---|
| `--fg` | `#e8eaed` | `#1f2023` | body text |
| `--fg-muted` | `#9aa0a6` | `#61656c` | secondary text, labels, metadata |
| `--border` | `#2a2d33` | `#d8dade` | every hairline |
| `--bg` | `#131417` | `#ffffff` | page ground |
| `--bg-alt` | `#1b1d21` | `#f4f5f7` | raised surfaces, row hover ancestry |
| `--bg-card` | `#0f1013` | `#ffffff` | raised card (dark: one shade *below* `--bg`) |
| `--bg-panel` | `#202329` | `#ffffff` | pickers, tooltips |
| `--bg-popover` | `#1c1e24` | `#ffffff` | popovers |
| `--sel` | `#2b3a52` | `#cfe0f7` | text selection |
| `--accent` | `#E5FF44` | `#5f7300` | the one signal colour (see below) |
| `--on-accent` | `#10131a` | `#ffffff` | ink on an `--accent` fill |
| `--accent-soft` | `#c9d95e` | `#5f7300` | accent as readable body text |
| `--success` / `-bright` / `-soft` | `#3fb950` / `#3fca6b` / `#86d786` | `#1a7f37` / `#1a8f45` / `#1f7a3a` | success states |
| `--warning` / `-soft` / `-strong` | `#d29922` / `#dfb054` / `#e0a33e` | `#8f6a10` / `#8a6410` / `#9a6a10` | warning states |
| `--error` | `#ff6b6b` | `#c62828` | error states (also shadcn `--destructive`) |
| `--row-bg-hover` | `#26282c` | `#e5e6e8` | opaque hover fill (pre-composited) |
| `--row-bg-active` | `#353a25` | `#e1e4d7` | opaque selected-row fill (accent wash, pre-composited) |
| `--shadow-ink` / `-soft` / `-deep` | `rgba(0,0,0,.4/.2/.6)` | `rgba(0,0,0,.1/.05/.25)` | shadow colour (geometry lives in Tailwind, see §7) |
| `--scrim` | `rgba(0,0,0,.55)` | `rgba(0,0,0,.4)` | modal backdrop |
| `--series-1..6` | blue/orange/green/purple/teal/red | deepened, same order | chart lines — meaning is "this one, not that one", never sentiment |
| `--icon-folder/-code/-data/-json/-html/-image/-doc/-media/-geo/-archive/-db/-file` | tuned bright for dark | darkened to the same weight | file-type icon hues |

**The signal rule.** `--accent` is a signal, not a fill: the selection edge,
the focus ring, an active/checked state. It is never a large fill or body
text — the light palette's `--accent` (`#5f7300`, a deep olive-lime) exists
*because* raw `#E5FF44` fails contrast as text/border on white; using it as
prose or a big panel would fail exactly the same way it was designed not to.
The one exception is the brand mark itself.

**Hover.** The default hover treatment across the app is the `--row-bg-hover`
wash — a pre-composited opaque fill, not a translucent overlay, because a
stretched-link row cannot stack a translucent wash over its own background.
`--row-bg-active` is the same idea for a selected row (the accent wash, at
weight, pre-composited per theme).

**Status colours.** `--success` / `--warning` / `--error` (plus their
`-bright`/`-soft`/`-strong` variants) are the only colours that carry
*meaning*; they are never repurposed for anything decorative. `--activity`
(`#60a5fa` dark / `#2563eb` light) is a fourth, separate hue for "something is
in flight" (a live turn, an unread dot, a run-now affordance) — it used to be
the status colour for "upcoming" and was pulled out once that lane went grey.

**Scrim family.** `--scrim-fg`, `--scrim-bg` / `-hover`, `--scrim-border` /
`-hover`, `--scrim-veil`, `--scrim-lift`, `--scrim-chip-bg` / `-hover`,
`--scrim-letterbox` — the one token group that is **identical in both
themes**. These paint controls sitting on arbitrary pixels (a photo, a map
tile, a webcam frame) where the app's own theme says nothing about what's
underneath; white-on-dark-wash reads over anything, and a token that flipped
with the theme would put light ink on a light photo half the time.

**The shadcn bridge** (`tailwind.css`, one un-themed `:root` block — theme
comes for free from the tokens it aliases):

| shadcn var | → app token | Note |
|---|---|---|
| `--background` / `--foreground` | `--bg` / `--fg` | |
| `--card` / `--card-foreground` | `--bg-card` / `--fg` | |
| `--popover` / `--popover-foreground` | `--bg-popover` / `--fg` | |
| `--primary` / `--primary-foreground` | `--fg` / `--bg` | the filled button is fg-on-bg, **not** the brand lime |
| `--secondary`, `--muted` | `--bg-alt` | |
| `--muted-foreground` | `--fg-muted` | |
| `--sh-accent` (shadcn "accent") | `--row-bg-hover` | the **hover wash**, not the brand accent |
| `--destructive` | `--error` | |
| `--sh-border`, `--input` | `--border` | |
| `--ring` | `--fg-muted` | shadcn's own hover/focus halo — quiet, not lime; the app's own controls draw their own lime focus-visible outline separately |
| `--chart-1..5` | `--series-1..5` | |
| `--sidebar*` | `--bg` / `--fg` / `--accent` / `--on-accent` / `--row-bg-hover` / `--border` / `--fg-muted` | |

**The Bridge Rule:** a shadcn semantic variable is only ever an alias onto an
app token (`var(--<app-token>)`), never a literal — and the bridge itself
carries no light/dark branch, because the tokens it points at already flip
with `data-theme`. A second light block here would be a second place for the
two palettes to drift apart.

## 4. Typography

Fonts (`scale.css`):
- `--font-sans`: `-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
  Helvetica, Arial, sans-serif` — the system stack; the product's voice is
  the OS's voice.
- `--font-mono`: `ui-monospace, SFMono-Regular, Menlo, Consolas,
  "Liberation Mono", monospace` — measurements only.

Nine type roles, each `size / line-height / weight`:

| Role | Size | Line-height | Weight | Use when |
|---|---|---|---|---|
| `micro` | 10px | 1.3 | 500 | badges, uppercase tags, sub-captions |
| `caption` | 11px | 1.4 | 500 | secondary labels, footnotes |
| `meta` | 12px | 1.4 | 400 | labels, hints, measurements |
| `dense` | 13px | 1.4 | 400 | list rows, controls in dense surfaces, composer text |
| `body` | 14px | 1.5 | 400 | default document size (set on `<body>`) |
| `control` | 14px | 1.0 | 500 | buttons, tabs, card titles — single line |
| `title` | 15px | 1.3 | 600 | section / stage titles |
| `heading` | 16px | 1.3 | 600 | page-level headings inside a panel |
| `display` | 20px | 1.2 | 600 | the one big number, or the page title |

Each role is three CSS custom properties (`--text-<role>`,
`--text-<role>--line-height`, `--text-<role>--font-weight`), mapped 1:1 into
Tailwind's `@theme inline` so `text-dense` etc. set all three at once.

**The mono-for-measurement rule.** Anything measured — file sizes, byte
counts, timestamps, durations — renders in `--font-mono`, at `meta` (12px,
one size down from `dense`'s 13px), muted, with `font-variant-numeric:
tabular-nums`. Prose and names never take mono; measurements never take the
sans voice.

**Snap table** (old size → scale role/size, applied by the legacy-CSS sweep;
UI may shift up to 2px):

| Old | New |
|---|---|
| 9px | 10 (`micro`) |
| 10.5px | 10 (`micro`) |
| 11.5px | 12 (`meta`) |
| 12.5px | 13 (`dense`) |
| 13.5px | 14 (`body`/`control`) — sidebar nav rows included |
| 22px | 20 (`display`) |

## 5. Spacing

A 4px base scale with halves, defined once and read two ways.

| Name | px | Tailwind number |
|---|---|---|
| `--space-0-5` | 2 | `0.5` |
| `--space-1` | 4 | `1` |
| `--space-1-5` | 6 | `1.5` |
| `--space-2` | 8 | `2` |
| `--space-2-5` | 10 | `2.5` |
| `--space-3` | 12 | `3` |
| `--space-3-5` | 14 | `3.5` |
| `--space-4` | 16 | `4` |
| `--space-5` | 20 | `5` |
| `--space-6` | 24 | `6` |
| `--space-7` | 28 | `7` |
| `--space-8` | 32 | `8` |
| `--space-10` | 40 | `10` |
| `--space-12` | 48 | `12` |

Tailwind's own numeric scale *is* the scale — `p-3` already equals
`--space-3` (12px) — so a composite writes `p-3`/`gap-2`/`px-4` directly;
hand-written CSS writes `var(--space-3)` for the identical value. The
`--space-*` names exist only so CSS that can't reach a Tailwind class can
still say the same number.

Common rhythms: row padding `8×12` (`--space-2 --space-3`), card padding
`16` (`--space-4`), section gap `24` (`--space-6`), control gap `8`
(`--space-2`).

**Snap rule for odd values** (legacy-CSS sweep): odd spacing (3/5/7/9px) →
nearest even step; 15px → 16; 18px → 16 or 20 depending on context (e.g. a
rail's own padding vs. a column gap); 22px → 24.

## 6. Shape

| Token | px | Sits at |
|---|---|---|
| `--radius-hairline` | 2 | progress tracks, meters |
| `--radius-xs` | 4 | checkboxes, tiny chips, thumbnails |
| `--radius-control` | 6 | buttons, inputs, menu items, tabs |
| `--radius-md` | 8 | rows, attach chips, small cards |
| `--radius-card` | 10 | model rows, cards |
| `--radius-panel` | 12 | composer, result cards, dialogs |
| `--radius-pill` | 999 | pills, switches |

`--radius-hairline` is also the smallest structural border width concept in
the ladder's spirit, but the app's actual hairline *border* is always `1px`
(exempt from the no-literal rule, alongside `0`).

shadcn keeps its own separate `--radius` ladder (`0.625rem` = 10px base, with
`sm`/`md`/`lg`/`xl`/`2xl`/`3xl`/`4xl` as fractions/multiples of it, declared
on the bare `:root` in `tailwind.css` since radius is not a theme concern).
The two ladders coexist deliberately: shadcn primitives keep using their own
`rounded-lg` etc. so they don't drift from upstream shadcn conventions, while
composites prefer the named app radii (`rounded-card`, `rounded-control`)
when a value belongs to *this* system rather than to shadcn's.

## 7. Elevation

Shadow **colour** is themed (`--shadow-ink-soft` / `--shadow-ink` /
`--shadow-ink-deep` in `tokens.css`, deeper in dark, softer in light — a drop
that reads as depth on a dark page reads as dirt on a light one). Shadow
**geometry** is Tailwind's own published `shadow-sm/md/lg/xl` box-shadow
values. Because these are two different vocabularies claiming the same four
names, `tailwind.css` re-declares them, unlayered, scoped to `[data-slot]`
(the attribute every shadcn component stamps on its root):

```
--shadow-sm: 0 1px 3px 0 var(--shadow-ink), 0 1px 2px -1px var(--shadow-ink);
--shadow-md: 0 4px 6px -1px var(--shadow-ink), 0 2px 4px -2px var(--shadow-ink);
--shadow-lg: 0 10px 15px -3px var(--shadow-ink), 0 4px 6px -4px var(--shadow-ink);
```

Tailwind's raw geometry stays; only the ink is swapped for the app's own
`--shadow-ink*`, so a shadcn component's shadow deepens in dark and softens
in light instead of wearing a hardcoded light-mode shadow on a dark page.

**When a border beats a shadow:** resting, in-flow surfaces (rows, cards
sitting flush on the page) separate by ground step (`--bg` → `--bg-alt` →
`--bg-card`) plus a `--border` hairline — never a shadow. A shadow is
reserved for something that actually floats above the page: a popover,
dialog, dropdown menu, tooltip — anything a click or hover raised off the
document flow.

## 8. Motion

| Token | ms | For |
|---|---|---|
| `--dur-instant` | 80 | hover colour/border change |
| `--dur-fast` | 120 | reveal/hide a control, swap an icon |
| `--dur-base` | 160 | fade a panel in/out |
| `--dur-glide` | 240 | move layout (rail fold, grid tracks) |

| Easing | Curve | For |
|---|---|---|
| `--ease-out` | `cubic-bezier(0, 0, 0.2, 1)` | most transitions; also the value Tailwind's own `ease-out` re-declares on `[data-slot]` |
| `--ease-glide` | `cubic-bezier(0.2, 0.7, 0.3, 1)` | layout-moving transitions (paired with `--dur-glide`) |

**Mandatory:** no animation ships without a `prefers-reduced-motion: reduce`
guard (or the Tailwind `motion-reduce:` variant). The guard doesn't have to
zero every duration to nothing meaningful for the user — see the Playground
settings fold (`tailwind.css`), which drops the animation but keeps the JS
exit delay so state changes stay correct — but *something* must acknowledge
reduced motion, every time.

## 9. Controls

| Token | px | shadcn Button size |
|---|---|---|
| `--h-control-xs` | 24 | `xs` |
| `--h-control-sm` | 28 | `sm` |
| `--h-control-md` | 32 | default |
| `--h-control-lg` | 36 | `lg` |

- **focus-visible:** a 2px `--accent` outline, offset 2px — this is one of
  the sanctioned lime signals (see §3).
- **disabled:** `opacity: .5` (plus `pointer-events: none` where the element
  would otherwise still be interactive).
- **hover:** the grey `--row-bg-hover` wash — never lime; lime hover is
  reserved for the one primary-fill button.

## 10. Components

Three layers, thin boundaries between them:

1. **shadcn primitives** — `frontend/src/platform/shadcn/ui/*`, installed
   with `npx shadcn@latest add <name>` and left unmodified. They paint only
   through shadcn semantic classes (`bg-primary`, `border-input`,
   `text-muted-foreground`), which the bridge (§3) resolves to app tokens.
   Never hand-edit a file in this directory — regenerate it if shadcn ships a
   fix, don't patch around it.
2. **`platform/ui/<page>` composites** — hand-written, Tailwind + `cva` +
   `cn()`, no CSS files of their own. One file per composite plus an
   `index.ts` and a `README.md` mapping every old class to its
   composite/prop (see `platform/ui/playground/README.md` for the live
   example — 78 rows of `old class → composite`). A composite that is
   still specific to one page's needs lives under that page's own directory
   (`platform/ui/playground/` today).
3. **Page files** hold *behaviour* — handlers, hooks, timers, URL sync,
   aria wiring, refs. They import composites and wire className-only; they
   do not carry their own CSS.

**Promotion rule:** a composite moves from a page-specific directory into
`platform/ui/` (the shared layer) the moment a **second** page needs it —
not before, so nothing is prematurely generalized against a single caller's
assumptions.

**Behaviour-frozen rule** (from the shadcn migration playbook): moving a
piece of UI onto composites is a skin swap, never a redesign. Handlers,
hooks, timers, URL sync, `aria-*`, `title`, `disabled`, `role`, `tabIndex`,
`stopPropagation`, measuring refs are byte-identical before and after; only
JSX wrappers and `className` change. The one accepted class of additive
change is where shadcn's own primitive comes with behaviour the old markup
lacked (e.g. a `Dialog`'s focus trap, `Progress`'s aria role) — never invent
new behaviour beyond what the primitive already provides for free.

**Tour hooks convention:** `platform/lib/tours/*` drives onboarding tours off
live CSS selectors, and a test (`registry.test.ts`) pins specific class names
(e.g. `.pg-side`, `.pg-composer`, `.pg-send`, `.pg-answer-block`). When a
component migrates off its old class name, keep the tour-hook class on the
new element as a **style-free selector** — it carries no rules, it exists
only so the tour still finds the element — until the tour itself is
retargeted to a new selector.

## 11. How to add

**A token:** add it to `scale.css` (rhythm) or both palette blocks of
`tokens.css` (colour — dark and light, same key, in `tokens.css`'s existing
order). If it's a rhythm token consumed by Tailwind, add the matching line
to `tailwind.css`'s `@theme inline` block. Run `tests/test_theme.py` (colour)
or `tests/test_design_scale.py` (rhythm) — the latter's
`test_scale_declares_every_role_tailwind_maps` fails loudly if the Tailwind
mapping points at a name `scale.css` never declared.

**A composite:** put it in the owning page's `platform/ui/<page>/` directory
(or `platform/ui/` directly if it's already shared), give it its own file, no
CSS file, only tokens/Tailwind names from §2–9, and record it in that
directory's `README.md`.

**A page:** follow `docs/SHADCN_MIGRATION_PLAYBOOK.md` — inventory
(`parity-events.md` / `parity-css.md`), a `design.md` reviewed before
building, primitives/bridge changes if the page needs new shadcn components,
composites, then stage files migrated one at a time with computed-style
probes and a screenshot diff against `main`.

## 12. Do / Don't

**Do:**
- Paint every colour with `var(--token)` from `tokens.css`; give any new
  dark token a light counterpart.
- Reach for the shadcn component first and style it with semantic classes,
  never a canon token name or a literal in `className`.
- Keep measurements in `--font-mono`, `meta` size, muted, `tabular-nums`.
- Use `--row-bg-hover` for hover; reserve `--accent` for selection edge,
  focus ring, and the one primary fill per screen.
- Guard every animation with `motion-reduce:` / `prefers-reduced-motion`.
- Snap an odd legacy value onto the nearest scale step (§4/§5) rather than
  keeping it exact.

**Don't:**
- Write a raw hex, `rgb()`/`rgba()` literal, or arbitrary Tailwind value like
  `text-[13px]` / `p-[15px]` / `rounded-[8px]` / `duration-[110ms]` — both
  guard tests fail on these outside `scale.css`/`tailwind.css`.
- Use lime as a fill for anything but the brand mark and the single primary
  button on a screen; never as prose colour or a decorative wash.
- Reach for `ToggleGroup` (or any roving-tabindex primitive) to replace a row
  of plain, independently-tabbable buttons — that's a behaviour change, not
  a skin swap.
- Ship an animation with no reduced-motion guard.
- Use `shadow-md` or heavier on a resting, in-flow surface — that's what
  the ground-step + hairline border is for (§7).
- Invent a new spacing/radius/type size instead of picking the nearest scale
  step, even under deadline pressure.
- Hand-edit a file under `platform/shadcn/ui/` — it's vendored, unmodified
  code.
- Add a CSS file for a page — behaviour lives in the page file, style lives
  in composites, and a CSS file for a page is a second, drifting source of
  the tokens it should just be reading.
- Give a shadcn bridge var (`tailwind.css`) a literal or a light/dark branch
  of its own — it must always be `var(--<app-token>)`, and the theme split
  belongs only to `tokens.css`.

## 13. Guards

**`tests/test_theme.py`** — the whole colour contract:
- every built-in template is classified tier-1 / exempt / self-toggling /
  deferred, exhaustively (no template escapes classification);
- the shell resolves theme in an inline `<head>` script, before first paint,
  reading a single storage key spelled identically in `theme.ts`,
  `index.html`, and `static/runtime.js`;
- `tokens.css`'s light `:root[data-theme="light"]` block redefines *every*
  key the dark `:root` block defines (no missing, no extra);
- no colour literal exists in any `styles/*.css` file outside the two
  palette blocks, and the same holds per-file for every tier-1 built-in
  template and for `templates/shared/*.js` scripts that inject CSS into a
  host template;
- a `var()` a tier-1 template reads must resolve to something the template
  (or the shell) actually defines — an undefined token is silently inert,
  not an error, so this is checked explicitly;
- `templates/shared/folder-picker.js` paints only through its own `--fp-*`
  namespace, never a host token directly.

**`tests/test_design_scale.py`** — the whole rhythm contract:
- every `styles/*.css` file except `scale.css`/`tailwind.css`: no px literal
  in font-size, border-radius (all corners), padding/margin (all sides,
  inline/block), gap/row-gap/column-gap, or a duration in
  transition/animation — `1px` (hairlines/borders) and `0` are allowed;
- every `.tsx`/`.ts` file outside `platform/shadcn/`: no Tailwind arbitrary
  value restating one of those properties (`text-[…px]`, `p-[…px]`,
  `rounded-[…px]`, `duration-[…ms]`, etc.);
- every `--text-*` / `--radius-*` / `--font-*` / `--ease-glide` name
  `tailwind.css` maps must actually be declared in `scale.css`;
- every allowlist entry must still match something real, so the allowlist
  can't silently accumulate dead debt.

**Allowlist format** (`tests/design_scale_allowlist.txt`, both guards use the
same shape): one entry per line, `path::literal  # why` — e.g.
`styles/ai-playground.css::border-radius: …14px  # svg ring geometry, not
rhythm`. The `#` comment explaining *why* is required; a line without one is
rejected by the allowlist parser itself.
