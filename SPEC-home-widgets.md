# SPEC: Configurable Home (widget grid)

Replace the fixed strips on Home (`frontend/src/shell/Home.tsx`, route `/home`) with a
user-configurable 4-column widget grid. This is "v2 · Widget grid" from the design
mockup: https://claude.ai/artifact/Pq3WM1PMn3tMSXgD4unx6Z (the mockup is the visual
reference for both everyday and edit states).

## What stays the same

- The hero search (`FilesSearch` from `apps/explorer/FilesHome.tsx`) stays at the top, full
  width, and is NOT a widget. While a search is live, the grid hides and results take over
  the page, exactly as today.
- `ClaudeHealthStrip` and `FdaStrip` stay between the hero and the grid.
- The `bots_enabled` front-door logic in `shell/App.tsx` and `shell/GlobalSidebar.tsx` is
  untouched. Do not edit it.
- Existing card components (`AppPreviewCard`, `PlaygroundPreviewCard`, `FolderPreviewCard`,
  `RecentPreviewCard`) and their data loaders are reused, not rewritten.

## Layout model

```ts
type WidgetSource = "apps" | "playground" | "sessions" | "recents"
                  | "tasks" | "bots" | "folder" | "index";
type WidgetSize = "1x1" | "2x1" | "2x2" | "4x1";   // cols x rows
type WidgetFormat = "cards" | "list" | "icons" | "board" | "count";
interface Widget {
  id: string;            // random, stable
  source: WidgetSource;
  size: WidgetSize;
  format: WidgetFormat;
  folderId?: string;     // source === "folder": a BookmarkFolder id from platform/lib/bookmarks.ts
}
interface HomeLayout { version: 1; widgets: Widget[] }   // array order = grid order
```

Per-source allowed sizes / formats (first entry is the default):

| source     | sizes                 | formats               | content |
|------------|-----------------------|-----------------------|---------|
| apps       | 4x1, 2x1, 2x2         | cards, icons          | `getHomeApps` |
| playground | 4x1, 2x1, 2x2         | cards, list           | `PLAYGROUND_GROUPS` |
| sessions   | 4x1, 2x1, 2x2         | cards, list           | `getHomeClaudeSessionFolders` |
| recents    | 4x1, 2x1, 2x2         | cards, list           | `loadRecents` / `useRecentsVersion` |
| tasks      | 2x2, 2x1, 4x1         | list, board, count    | `getTasks` (api.ts). Board = 3 columns Queued / In progress / Needs you (`needs_attention`). Count = big number of open tasks + "N need you". Exclude `done`/`archived`. Rows click through to the task (reuse whatever `shell/TaskCards.tsx` / Tasks page uses to open a task). |
| bots       | 1x1, 2x1, 2x2         | list, count           | bots `api.status` (`apps/bots/lib/api.ts`). List rows: name, status pill, `step`/`step_cap`, `title`/`note`. Click → `/bots`. |
| folder     | 2x1, 1x1, 2x2, 4x1    | list, icons           | children of the bookmark folder `folderId`; title = folder name. Missing folder → empty state "This bookmark folder was deleted." |
| index      | 1x1, 2x1              | count                 | `useIndexStatus` — file count + "updated N ago". |

**Default layout** (used when the server reports no saved layout — must reproduce today's
Home so nobody opens an empty page): apps 4x1 cards, playground 4x1 cards, sessions 4x1
cards, recents 4x1 cards — same order as today.

A `4x1` widget with format `cards` is today's strip: reuse the existing `Section` +
`useStripCount` one-row behaviour inside it (CARD_W / CARD_GAP / MAX_ROW unchanged).

## Persistence (server)

New file `fused_render/shell/home_layout.py`, modelled exactly on `fused_render/shell/bookmarks.py`:

- `GET /api/home/layout` → `{ exists: bool, layout: HomeLayout | null }`. Absent or corrupt
  file → `exists: false, layout: null`.
- `PUT /api/home/layout` → whole-document, last-write-wins, same `X-Fused` guard
  (`_require_fused` pattern), same atomic write via `fused_render/shell/storage.py`.
  Validate: `version == 1`, `widgets` is a list of ≤ 48 dicts, each with known `source`,
  `size`, `format` strings; reject with 400 otherwise. Unknown extra keys are dropped.
- File: `~/.fused-render/home_layout.json` (resolve the dir the same way bookmarks.py does).
- Register the router wherever bookmarks' router is registered (grep for
  `bookmarks.router` / `shell.bookmarks`).

Frontend client in `frontend/src/platform/lib/api.ts` next to `getBookmarks`/`putBookmarks`:
`getHomeLayout()` / `putHomeLayout(layout)`.

## Frontend structure

New folder `frontend/src/shell/home/`:

- `layout.ts` — types above, `SOURCES` table (allowed sizes/formats/label/description),
  `DEFAULT_LAYOUT`, `normalizeLayout(raw)` (drops unknown sources, clamps a size/format not
  allowed for its source to that source's default, drops `folder` widgets with no
  `folderId`), pure helpers `moveWidget(layout, fromIdx, toIdx)`, `addWidget`,
  `removeWidget`, `setSize`, `setFormat`. All pure → unit-tested with `bun test`.
- `useHomeLayout.ts` — loads via `getHomeLayout` (falls back to `DEFAULT_LAYOUT`), exposes
  the layout plus mutators that update state optimistically and `putHomeLayout` the whole doc.
  A failed PUT keeps the local state and reports through the existing notification/toast
  mechanism Home's neighbours use (grep how bookmarks.ts surfaces a failed PUT; copy it).
- `WidgetGrid.tsx` — CSS grid, 4 columns, `grid-auto-rows` fixed (~120px), gap 12–16px.
  Container query: under ~640px wide, 2 columns and 4x1 spans 2.
- `Widget.tsx` — the frame (title, meta, "See all" link in view mode; edit toolbar in edit
  mode) + a body switch on `source`/`format`. One small component per source in
  `widgets/` (e.g. `TasksWidget.tsx`, `BotsWidget.tsx`, …).
- `AddWidgetPanel.tsx` — right-side panel in edit mode listing every source with its
  description and format choices; for `folder`, a picker of the user's bookmark folders.
  Choosing adds the widget at the end with that source's default size.

Home.tsx keeps the hero, strips, and search takeover, and renders `<WidgetGrid>` in place
of the four hard-coded `Section`s.

## Edit mode

- A "Customize" button in Home's header row (right side of the hero area). In edit mode it
  reads "Done". Esc also exits.
- In edit mode each widget shows: drag handle, size chip (menu of that source's allowed
  sizes, shown as "1×1 / 2×1 / 2×2 / Full row"), format segmented control (only if >1
  format), remove (×). A trailing dashed "+ Add widget" tile opens `AddWidgetPanel`.
- Reorder with native HTML5 drag and drop (no new dependency). Also provide keyboard
  reorder: focused widget + Alt/Option+Arrow moves it one slot (accessibility; also makes
  it testable).
- "Reset to default" link in the panel restores `DEFAULT_LAYOUT` (in-page confirm step,
  no `window.confirm`).

## States every widget must handle

Loading (skeleton, reuse Home's `SkeletonCard`/`SkeletonRow` where shape fits), error
(copy says what failed + a Retry button; for bots: "Couldn't reach bots. Turn Bots on in
Preferences if it's off." ), empty (reuse today's empty strings for the four existing
sources; new ones: tasks "No open tasks.", bots "No bots yet.", folder "This folder is
empty."), overflow (lists show what fits + "+N more" linking to the full page; long names
ellipsize, never wrap the frame).

## Styling rules

- Tokens only (`frontend/src/styles/tokens.css`): `tests/test_theme.py` fails on hardcoded
  colours. Works in both `data-theme` values.
- Tailwind runs without preflight — headings/paragraphs keep UA margins; set `margin: 0`
  explicitly.
- Put styles in `frontend/src/styles/home.css` (existing). Never write a glob like `*/` in
  CSS comment prose — it closes the comment and only `bun run build` catches it.
- Widget card: `--bg-panel`, 1px `--border`, `--radius`; edit-mode outline dashed in
  `rgba(var(--accent-rgb), .55)`.

## Tests

- `bun test` unit tests for `layout.ts` (normalize, move/add/remove/setSize/setFormat,
  default layout reproduces the four existing sections).
- pytest for `home_layout.py` modelled on `tests/test_shell_bookmarks.py`: GET absent →
  exists false; PUT then GET round-trips; PUT invalid → 400; missing X-Fused → 403;
  unknown keys dropped.
- `frontend/src/shell/home-performance.test.ts` and other tests read `Home.tsx` source text
  and pin literal substrings. Before moving code out of Home.tsx, grep
  `frontend/src` and `tests/` for `Home.tsx` / `home.css` readers and keep them green —
  update the pinned strings only when the behaviour they guard is preserved elsewhere, and
  point the test at the new file.

## Out of scope

Free-form resizing, multiple Homes/pages, per-widget settings beyond size/format/folder,
widgets from third-party apps, changing the Bots front-door flag.

## Builder notes

Deviations and decisions:
- Grid rows are `minmax(120px, auto)`, not a fixed 120px: a cards widget is about 270px tall and cannot fit a fixed row.
- The Customize/Done button sits in a toolbar row below the hero (inside `.home-strips`), not inside the hero.
- Widget "meta" text was omitted.
- Tasks "blocked" and "needs_attention" both map to the "Needs you" lane.
- The welcome tour's readyWhen needs an apps widget to exist (anchors `#home-sec-apps`, first apps/playground/sessions widget carries `home-sec-<source>`).
- Each widget measures and fetches on its own, so duplicate widgets of one source re-fetch. Cards widgets use the old measured-count logic (`useStripCount`); 2x2 uses `limit * rows`.
- Lists and icons use a fixed item-capacity table (`itemCapacity` in layout.ts) rather than measuring.
- The apps error state now has a Retry button instead of the plain empty message.
- Source-text pin tests (`home-performance.test.ts`, `new-task-form.test.ts`) were repointed to `home/data.ts`, `home/strip.ts`, the widget files and `skeleton.tsx`; three regexes were loosened (`[limit(?:, \w+)*]`, `cards ? (<SkeletonRow`, `count={cap}`).
- The TDD red step was not observed for layout.ts (tests written alongside).
- The add panel is a flex sibling of the grid on the right; under 640px (container query) the grid goes to 2 columns and the panel goes full width.
- Border radius is the literal 16px used by neighbouring cards (no radius token in tokens.css).

Dead ends: BSD sed in-place line inserts; the worktree shell guard refuses heredocs and compound commands, so files were written with Write/Edit.

Not verified (needs a real browser): drag reorder, size/format menus, add panel, both themes, narrow width, keyboard Alt+Arrow focus retention.

Cards strip: a cards widget is a horizontally scrolling strip (`CardStrip`), not a wrapped row that shows only what fits. Cards are sized so `count` cards plus a 56px peek (`PEEK_W`) of the next one fill the row, with an edge fade and ‹ › buttons that page by one viewport minus the peek. Every fetched item renders. The row fetches `limit * rows + 1` so there is always a next card to peek; it must stay measured, never a fixed MAX_ROW (that brings back the server's exhaustive workspace walk, see strip.ts). Not verified without a browser: peek width, fade, button hover/touch visibility, snap, edit-mode drag.

Edit mode v1 (gallery sheet): replaces the inline chip, segmented control and right-hand panel.
- Edit bar (`.hw-editbar`) holds hint, Reset to default (with the in-page confirm), + Add widget and Done; view mode keeps the Customize pill.
- Each edit-mode widget has a corner remove button and one "Edit ▾" popover (`.hw-pop`, 360px) with size chips and format thumbnails; it flips to left-aligned when a right-aligned card would leave the viewport.
- The add sheet is portalled to `document.body` (a `container-type` ancestor would otherwise trap `position: fixed`), narrow layout keys off the dialog's own container width. `--scrim` is a new token in both palettes.
- `addWidget`/`makeWidget` accept `size`; SIZE_LABELS are now Small/Half/Large/Full row.
- `FormatPreview` is static and scaled with CSS `zoom` (zoom factors in `Pickers.tsx`). Grid is `row dense`.
- Scroll-to-new-widget after Add is a timed `scrollIntoView` on the last `.hw-widget`.
- Not verified without a browser: every visual (popover placement and flip, thumbnail crops and zoom factors, stage fit, narrow sheet, both themes), focus return and Tab trap, Esc layering, dense packing during drag, scroll-into-view.
