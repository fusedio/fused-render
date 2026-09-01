---
version: 1
slug: "frontend-src"
primary_target: "frontend/src"
related_targets: ["frontend/index.html"]
---

# Surface brief: frontend shell (full redesign)

## Scope & mode
Whole React shell (`frontend/src`): global sidebar, Home, Explorer + split preview, Tasks/Schedule, Apps, AI Models, Canvases, Preferences, dialogs/toasts. Mode: **Operate**. Behavior, IA, routes, copy, and tests are invariant; only the visual system is replaced.

## Direction (approved 2026-09-01)
Canon — the modern dev-tool category standard played straight, benchmarked against **Linear, Vercel dashboard, Raycast**. Approved comps (combined by user):
- `.impeccable/mocks/decision/canon.webp` — **Split Workbench**: grammar for all working views (Explorer, listings, split preview, settings-family pages).
- `.impeccable/mocks/comp-c-workspace-home.webp` — **Workspace Home**: treatment for Home only (recents, current apps, activity).

**User constraint:** sidebar items and per-screen content are already well thought out — preserve the existing IA verbatim. Comp nav items (Shares, Queries, Notebooks, Audit Logs, account rows, avatars) are placeholder art, never IA. Product has no accounts.

## Sampled record (from comp pixels; supersede card chips)
- Main ground: `#0a0d10` (canon) / `#111213` (C main) — near-black, cooler and deeper than incumbent `#131417`.
- Sidebar: darker than main pane (`#070709`), inverted ordering vs incumbent.
- Panels/cards: hairline-bordered surfaces one step above ground, soft shadows.
- Accent: Fused Lime `#E5FF44` (binding brand token); comps render it on active nav marker, selected row edge, primary button fill, status dot.
- Text: off-white ≈ `#fafafa` primary, muted gray ≈ `#878b91` secondary.

## Component grammar (from comps)
- Radii: ~8px controls/cards, ~10-12px panels/search field; larger than incumbent 6px.
- Left-edge lime marker bar for active nav item and selected row (both comps show it).
- Search field prominent in header; ⌘K affordance.
- Table rows ~40-44px in canon comp, generous cell padding, muted mono metadata.
- Preview pane: titled header, tab row (Preview/Schema/Metadata/Statistics), chip row of facts, paginated table footer.
- Cards: thumbnail top, name + path + timestamp meta, kebab menu; hairline border + slight lift.
- Status dots (green running / yellow background) + MB figures right-aligned in list rows.
- Primary button: **lime fill, dark text** (comps override the incumbent ink-fill rule; canon world's call).

## Not literalized
Accounts/avatars, Shares/Queries/Notebooks/Alerts/Integrations/Audit Logs nav, "Alex Chen", invented file content. Dark+light contract stays: every color a token with light counterpart.

## Fidelity inventory (medium per ingredient)
| Ingredient | Medium |
|---|---|
| Shell chrome, sidebar, tables, cards, forms | HTML/CSS (restyle existing TSX) |
| File-type icons | existing SVG set (FileIcons.tsx), hues retuned |
| Nav/UI icons | existing inline SVG, stroke normalized 1.5px |
| Card thumbnails | real previews already rendered by app |
| Lime markers, status dots, focus rings | CSS |
| Shadows/elevation | CSS tokens |
| Accepted omission | comp's fake content, account row |

## Unresolved
None blocking. Light palette derived per token during build (contract: same relationships on white).
