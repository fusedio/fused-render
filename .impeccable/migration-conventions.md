# shadcn migration conventions (read fully before editing)

Goal: replace the hand-rolled CSS component vocabulary with shadcn components everywhere in `frontend/src`. Tests are EXPECTED to go red — do not update, delete, or contort tests, and do not let a red test change your approach. Behavior, IA, copy, routes, and handlers must not change.

## Project facts
- shadcn base-nova on **base-ui** (NOT radix): custom triggers use the `render` prop, never `asChild`. 38 components installed at `@platform/shadcn/ui/*` (button, input, field, dialog, alert-dialog, dropdown-menu, context-menu, tooltip, popover, select, combobox, table, tabs, breadcrumb, sidebar, scroll-area, command, empty, spinner, progress, switch, checkbox, radio-group, toast, sheet, toggle-group, toggle, input-group, alert, collapsible, kbd, badge, separator, skeleton, card, textarea, slider, label).
- Icons: lucide-react. In a Button: `<SomeIcon data-icon="inline-start" />`, no size classes on icons inside components.
- Tailwind v4; semantic vars are BRIDGED to the app's canon tokens in `src/styles/tailwind.css` (`--primary`=Fused Lime, `--primary-foreground`=on-accent, `--sh-accent`=hover wash — shadcn "accent" is a HOVER WASH, never the brand lime). Use semantic classes (`bg-background`, `text-muted-foreground`, `border-border`); NEVER raw colors (`bg-zinc-900`) and never manual `dark:` overrides — the theme flips via `data-theme` automatically.
- `cn()` from `@platform/lib/utils` for conditional classes. Layout via `flex gap-*`, never `space-x/y-*`. `size-*` for equal dims. `truncate` shorthand.

## Mapping table (old → new)
- `.btn` → `<Button variant="outline">`; `.btn-primary` → `<Button>` (default = lime fill); `.btn-secondary` → `variant="ghost"` or `outline` per context; `.btn-danger` → `variant="destructive"` (outline-destructive look: keep destructive); `.btn-danger-text` → `variant="ghost"` + `text-destructive` + `mr-auto`.
- `.icon-btn` and other square glyph buttons → `<Button variant="ghost" size="icon">`.
- `.field` / `.field-label` / `.field-hint` / `.field-control` → `Field`, `FieldLabel`, `FieldDescription` + `Input` / `Textarea` / `Select`. Form layout: `FieldGroup` + `Field`. Validation: `data-invalid` on Field, `aria-invalid` on control.
- Native `<select className="field-control">` → shadcn `Select` (base-ui) unless it's inside a dense bar where a native select is load-bearing — then keep native but style via Field classes.
- Modal chassis (`.modal-*`, `.deploy-*` dialogs, setup modal) → `Dialog`/`DialogContent`/`DialogHeader`/`DialogTitle`/`DialogFooter` (Title REQUIRED — `sr-only` if hidden). Confirmations → `AlertDialog`.
- Context menus (`.context-menu*`) → `ContextMenu` with `ContextMenuGroup`/`Item`. Menus opened from buttons → `DropdownMenu`.
- Tooltips (title attrs / custom) → `Tooltip`. Popovers/pickers → `Popover`.
- Toasts (`.toast*`) → `toast` from `@platform/shadcn/ui/toast` (base-ui version, NOT sonner).
- Tabs (`.tab`, `.cc-pill` tab rows) → `Tabs`/`TabsList`/`TabsTrigger` (triggers only inside TabsList).
- Chips/tags/count pills → `Badge` (variant per meaning; status colors via semantic tokens or existing status token classes).
- Skeletons (`.skel-bar`, shimmer) → `Skeleton`.
- Empty states → `Empty`.
- Separators/hr/border-t dividers → `Separator`.
- Breadcrumb (`#breadcrumb .path-crumb`) → `Breadcrumb` family (keep drag/drop + spring handlers by attaching to BreadcrumbItem via props/render).
- Search inputs with icons → `InputGroup` + `InputGroupInput` + `InputGroupAddon`.
- Spinners/loading → `Spinner` composed in Button with `disabled`, `data-icon`.
- 2–7 exclusive options → `ToggleGroup`.

## Behavior invariants (port handlers, never adopt defaults that change them)
- FS-5/D460: plain press RELEASE opens; modified (Shift/Mod) press only selects — every row, every listing. Keep existing onMouseDown/onClick logic verbatim when re-wrapping rows.
- Sort state in URL (`sort`/`order`); URL is state everywhere — never move state into component-local defaults.
- Folder-scoped split preview (FS-10/11) stays as-is structurally.
- Keep `aria-*` semantics that exist today (comboboxes in search, roles on listings).

## Style rules
- className for LAYOUT only; never override component colors/typography with utilities. Variants first.
- No manual z-index on overlays. No custom animate-pulse. No styled spans where Badge exists.
- One lime signal per screen: the default Button (lime) is for the PRIMARY action only; everything else outline/ghost.

## CSS retirement rule
- When you migrate markup, REMOVE the old classNames from the JSX you touched in the same edit.
- Delete a CSS file only when zero of its class names remain referenced anywhere in `frontend/src` (grep first). Then remove its `@import` line from `src/shell.css`. If any reference remains, leave the file untouched and list it as residue in your report.
- NEVER touch `src/styles/tokens.css`, `src/styles/tailwind.css`, `src/styles/base.css`, `src/styles/reduced-motion.css`.

## Verification per agent
- `bunx tsc --noEmit` scoped errors: your territory must introduce ZERO new TypeScript errors (run `npm run typecheck` in frontend/; pre-existing errors listed in your packet baseline are not yours).
- Do not run bun tests. Do not screenshot; the coordinator smokes visually.

## Report format
Return: files migrated (count + list), components used, CSS files deleted / residue left, handlers you ported carefully (rows, drag, keyboard), any place you intentionally kept custom markup and why (one line each).
