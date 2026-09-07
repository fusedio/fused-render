# Form kit

Every settings-shaped surface (Preferences today; Claude Config/Mounts/setup
modal/new-task later, per `docs/DESIGN_SYSTEM.md`'s promotion rule) shares
this vocabulary: a page shell, a tab strip, a section heading, and the two row
shapes Claude Config already established — a two-column `SettingsRow` for
things with a fixed control column, and a stacked `ChoiceRow` for a switch or
radio-group item that reads as one sentence. Tailwind + `cva` + `cn()` only,
no CSS file of its own — every value here is a scale/token name
(`text-dense`, `p-3`, `rounded-control`, `duration-(--dur-fast)`), never a
literal.

## Old class → component

| `preferences.css` (+ `setup-modal.css`/`mounts.css`) | Component | Notes |
|---|---|---|
| `.prefs-page` / `.prefs-page > *` | `<SettingsPage>` | Kept in CSS too — `Mounts.tsx`/`Scheduled.tsx` still use the class directly; this is the kit's own version of the same shell. |
| `.prefs-title` (mounts.css) | `<SettingsTitle>` | Class stays in mounts.css for `.mounts-title`; Preferences no longer references it. |
| `.prefs-tabs` | `<PageTabs>` | Deleted from CSS — Preferences was the only consumer. |
| `.prefs-tab` (+`.active`) | `<PageTab active>` | Plain `<button>`, **not** base-ui `Tabs` — no roving tabindex, all four stay independently tabbable; caller drives `active` + the `?tab=` URL sync. |
| `.prefs-tabpanel` | `<PageTabPanel>` | |
| `.prefs-section` / `.prefs-section h2` | `<SettingsSection title>` | CSS rule kept — `Mounts.tsx`/`Scheduled.tsx`/`AddMount.tsx` still render `<section className="prefs-section"><h2>…</h2>` directly. |
| `.prefs-section code` | `<CodeChip>` | CSS rule kept (`AddMount.tsx` uses bare `<code>` inside `.prefs-section`); the kit's own `<code>` is Tailwind, not the class. |
| `.prefs-section p` (margin:0) | — | Deleted from CSS — no consumer used a bare `<p>` inside `.prefs-section`; `MutedText` replaces the only real use. |
| `.prefs-section button` (bare skin) | `<Button variant="secondary">` (shadcn) | Deleted from CSS — Indexing's Re-index/Full scan/Browse call logs were the only bare (unclassed) buttons; they're shadcn `Button` now. |
| `.prefs-section select:not(.field-control)` | `<NativeSelect>` (shadcn) | Deleted from CSS — no other page had an un-migrated bare `<select>`. |
| `.prefs-actions` | `<ActionRow>` | Deleted from CSS. |
| `.prefs-textarea` | `<MonoTextarea>` | Deleted from CSS; shadcn `Textarea` + mono classes. |
| `.prefs-radio`/`.prefs-field` (setup-modal.css) | `<ChoiceRow control={<Switch>\|<RadioGroupItem>}>` | Class rules **left in place** in setup-modal.css (per design.md) — Preferences/Indexing just stop referencing them. |
| `.btn`/`.btn-primary`/`.btn-secondary`/`.btn-danger`/`.btn-danger-text` | shadcn `Button` variants | `default` / `secondary` / `destructive` / `ghost` + `text-[var(--error)]` for danger-text. |
| `.hf-authorize-link` | `<Button render={<a .../>}>` | Deleted from CSS. |
| `.index-query-input` | `<MonoTextarea noWrap>` | Deleted from CSS. |
| `.index-query-sql` | `<SqlReadout>` | Deleted from CSS. |
| `.index-query-results` (+`table`/`th`/`td`) | `<DataTable>` + `dataTableHeadClass`/`dataTableCellClass` on shadcn `TableHead`/`TableCell` | Deleted from CSS. Sticky header via `sticky top-0`. |
| `.lan-pair` / `-qr` / `-text` | inline Tailwind in `LanPairing` (`shell/Preferences.tsx`) | Deleted from CSS. QR paper stays `bg-[var(--qr-paper)]`, `168px` (`h-42 w-42`, Tailwind's numeric 4px scale: 42×4=168). Bespoke, page-local — not promoted into the kit. |
| `.lan-devices` (+`-head`/`li`/`-name`) | inline Tailwind in `LanDevices` (`shell/Preferences.tsx`) | Deleted from CSS. Bespoke, page-local. |
| ad-hoc `<p className="deploy-muted">`/`<div className="deploy-muted">` | `<MutedText>` | Color-only, like `.deploy-muted` itself (fields.css:105) — inherits size from context (`SettingsPage` sets `text-dense`). |
| ad-hoc locked-by-env-var paragraphs (HF `forcedByVar`, Call log `*_forced_by`) | `<LockedNote>` | Additive: a small lock glyph, same copy. |

## What's NOT in the kit

`LanPairing`, `LanDevices`, `HuggingFaceSection` stay page-local in
`shell/Preferences.tsx` — bespoke, single-consumer composites built *from* the
kit (`ChoiceRow`, `MutedText`, `CodeChip`, `ActionRow`, shadcn `Button`), not
promoted into it. Per the promotion rule in `docs/DESIGN_SYSTEM.md`: a
composite moves into the shared layer the moment a *second* page needs it,
not before. `ErrorBanner` (`platform/ui/ErrorBanner.tsx`) is reused as-is —
no reskin, no promotion needed, it was already shadcn-agnostic Tailwind.

## Rules

- No CSS file for this directory — every rule is Tailwind, using only scale
  names (`text-<role>`, the numeric spacing scale, `rounded-<name>`,
  `duration-(--dur-*)`) or `var(--token)` for colors the bridge doesn't cover
  (e.g. `border-[var(--accent)]` for the true brand lime, since shadcn's own
  `accent` is the hover wash, never the brand color — see
  `docs/DESIGN_SYSTEM.md` §3's bridge table).
- `SettingsRow`'s 240px control column and `SettingsPage`'s 760px content cap
  are Tailwind's numeric scale (`w-60` = 240px, `max-w-[760px]` — `max-w`
  isn't a restricted rhythm property, so the arbitrary value is allowed by
  `test_design_scale.py`, and it's the same pixel value the CSS rule used).
- `Skeleton` (shadcn) always carries `motion-reduce:animate-none` — the old
  `.skel-bar` shimmer had no reduced-motion guard; this fixes that as part of
  the swap, per `docs/DESIGN_SYSTEM.md` §8's mandatory-guard rule.
- A composite here is page-agnostic; a composite still specific to one page
  (LAN pairing, Hugging Face's state machine) stays in that page's own file,
  built *from* this kit's pieces.
