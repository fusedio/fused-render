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
| tasks      | 2x2, 2x1, 4x1         | list, board, count    | `getTasks` (api.ts). Board = 4 lanes Queued / In progress / Needs you (`needs_attention`) / Done. Count = big number of open tasks + "N need you". Done tasks are kept; `archived` and drafts are excluded. A per-tile Show option (Open only / Open + done) drops the Done lane and the done rows. Rows click through to the task (reuse whatever `shell/TaskCards.tsx` / Tasks page uses to open a task). |
| bots       | 1x1, 2x1, 2x2         | list, count           | bots `api.status` (`apps/bots/lib/api.ts`). List rows: name, status pill, `step`/`step_cap`, `title`/`note`. Click → `/bots`. |
| folder     | 2x1, 1x1, 2x2, 4x1    | list, icons           | children of the bookmark folder `folderId`; title = folder name. Missing folder → empty state "This bookmark folder was deleted." |
| index      | 1x1, 2x1              | count                 | `useIndexStatus` — file count + "updated N ago". |

**Default layout** (used when the server reports no saved layout; saved layouts are never
touched): the Builder preset (`presetRows("builder")`) — search 4x1, build 2x1, apps 2x2
icons, bots 2x2 list, tasks 2x2 list — with stable `default-<source>` ids.

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
- `AddWidgetPanel.tsx` — the add sheet, a macOS-style widget gallery: one card per (source, format) variant (`gallery.ts` `galleryEntries`), grouped into a section per source in `SOURCES` order. Each card draws the real widget body (`WidgetBody` from `Widget.tsx`, real data) at the variant's default size `sizesFor(source, format)[0]`, at its real tile size (the live `.hw-grid` column width by 52px rows plus 16px gaps, `previewPx`), scaled by one factor shared by every card (`previewScale`: a full-row preview fits the sheet, capped at 0.6) and inert so no preview takes focus. The section header carries the source label and its description once; each card's caption is the format label (several formats only) and the footprint ("Board · 2×2", `footprintLabel`; "Full row" for search and build), with a disabled note under it. Cards pack left to right in a grid whose captions share a baseline per row. Folder and page cards draw a stand-in. Clicking a card adds it at the first free slot (`api.add`) and closes the sheet; a folder or page card first opens its picker in the sheet (a folder or app click commits, a file path or website needs the header's Add). The sheet header carries the title, the room note (`freeSpaceNote`: "N free cells" while a one-cell tile fits inside the used rows, else "Adds a new row") and a close X; there is no Show-as row, size row, mini-map or footer, and no sort or show choice (those stay on the Change card). A second File search stays listed but disabled ("Already on Home"); a full Home disables every card ("Home is full").

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
sources; new ones: tasks "No tasks yet." (or, when only drafts and archived tasks exist, "No active tasks · 1 draft, 19 archived"), bots "No bots yet.", folder "This folder is
empty."), overflow (lists show what fits + "+N more" linking to the full page; long names
ellipsize, never wrap the frame).
The Tasks widget shows recent tasks, newest first, with drafts and archived tasks excluded (done are kept). Its board has Queued / In progress / Needs you / Done in one fixed four-column row, so Done never adds height (each lane shows the same cap plus "+N more"); the list shows open tasks first, then recent done ones; the count stays open-only (not done). A per-tile "Show" option (Open only / Open + done, default Open + done; hidden on the count format) drops the Done lane (the board becomes three columns) and the done rows; with nothing open the list reads "Nothing open".

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

## Fixed grid

Home is one fixed grid. A unit row is exactly 52px (`--hw-unit`; two units plus the 16px gap make the 120px cell) in view and edit mode alike, and a tile's height is purely a function of its footprint. Nothing a widget renders may grow a row: grid children are `min-height: 0; overflow: hidden`, and only the search takeover (`.hw-grid.is-searching`, a page rather than a tile) keeps auto rows. A widget's content adapts to its tile instead: lists and board lanes show what fits plus "+N more" (`useFitCount` in widgets/bits.tsx, driven by the pure `fitCount`; `itemCapacity` is only the first-paint guess and the count can go below it), icons and cards measure the real tile. If a format cannot fit a size, that size is not offered for that format: `FORMAT_MIN_ROWS` (icons need 2 cells = 4 units), `sizesFor(source, format)`, `minFootprint(source, format)`. A stored icons tile too short for icons falls back to the source's first fitting format on load (`normalizeLayout`, not moved or resized). The Change card lists every size of the tile's format with its cell dimensions (Large 2x2); sizes that do not fit where the tile sits are disabled ("No room here"), and the Icons format is disabled on a short tile ("Needs a taller tile"). The add sheet has no size row: each gallery card is one format at `sizesFor(source, format)[0]`.

## Builder notes

Deviations and decisions:
- Superseded: grid rows are no longer content-sized; see "Fixed grid" below.
- The Customize/Done button sits in a toolbar row below the hero (inside `.home-strips`), not inside the hero.
- Widget "meta" text was omitted.
- Tasks "blocked" and "needs_attention" both map to the "Needs you" lane.
- The welcome tour's readyWhen needs an apps widget to exist (anchors `#home-sec-apps`, first apps/playground/sessions widget carries `home-sec-<source>`).
- Each widget measures and fetches on its own, so duplicate widgets of one source re-fetch. Cards widgets use the old measured-count logic (`useStripCount`); 2x2 uses `limit * rows`.
- `itemCapacity` (layout.ts) is only the first-paint guess; see "Fixed grid" below.
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

App embed widget: source `app`, format `live`, sizes 2x2 (default) / 2x1 / 4x1, `appPath` = `AppInfo.path`.
- Body is an iframe on `embedUrlForFsPath(app.path)`, mounted only near the viewport (`useNearViewport`), skeleton until `onLoad`/`onError`, no `loading="lazy"`. The app list comes from `getApps()` through a 5-second shared cache (`loadAllApps`), fetched only by app widgets and the add sheet's App picker. The frame title is the app name and the header link is "Open ↗" (`hrefFor` + `openApp`).
- `_preview=1` is NOT appended. `IS_PREVIEW` makes the embedded shell forward `_preview=1&_nofocus=1` to /render and put an embedded Claude chat into read-only `preview` mode (Preview.tsx, ChatMount). That is a display-only contract, so a live app would lose autofocus and chat input. Cost: each Home visit with an app widget is recorded as an open of that app (D301), which reorders the /apps recents. Revisit by adding a separate "no record" flag if that bites.
- Server keeps `appPath` (string, 1..4096) only on `app` widgets; an `app` widget with no path is kept server-side and dropped by `normalizeLayout` (same split as `folder`/`folderId`).
- Polish: thumbnails centred in `.hw-thumb`; per-source lucide icons (LayoutGrid, AppWindow, Sparkles, FolderGit2, Clock, ListChecks, Bot, Bookmark, Database) replace the olive square; size chips always sorted Small, Half, Large, Full row (`sortSizes`).
- Not verified without a browser: the embedded app actually loading through `/embed/<folder>` and being interactive, edit-mode shield and drag, 2x2/2x1/4x1 heights, the mini browser-frame preview, centred thumbnails, lucide icon tint in both themes, the app picker (search at >8 apps, empty and error states), removed-app state.

App picker order: the add sheet's App picker now lists apps in the shared `sortApps` order (recent, then modified, then name), memoised in the picker (not in data.ts). Each row has a muted folder line (home as `~`, via `useHome`; `appPicker.ts` `appFolderLine`), search matches title, name and folder, and the default selection is the first sorted app. Not verified without a browser: row height and icon centring.

Search as a widget: source `search`, format `bar`, sizes 4x1 (default) / 2x1, at most one per Home (`hasSearch`; `normalizeLayout` drops later duplicates, `addWidget` no-ops, the add sheet hides the source once one exists). It is the FIRST widget of `DEFAULT_LAYOUT`; "File search" is first in the sheet (lucide `Search`, `FormatPreview` kind `search-bar`).
- Migration: layout `version` is now 2 (`LAYOUT_VERSION`). `normalizeLayout` accepts 1 or 2; a version-1 document gets a `search` 4x1 widget prepended (unless it somehow has one) and is stamped 2, so a user who later removes search keeps it removed. The server accepts 1 and 2 and keeps whichever it is given (no server-side migration); the next client PUT writes 2. Any other version still yields the default.
- Takeover: `Home` owns the search state (`useSearchHost`, provided by `SearchHostContext`); while a query is live `WidgetGrid` renders only the search widget (others are skipped in the same keyed list, so the box stays mounted and keeps its query), the grid class `is-searching` makes it full width, and the health strips, Customize/edit bar, empty message and add sheet are hidden. The widget has no header or card in view mode (`.hw-widget.is-search`); edit mode shows the normal chrome with a static placeholder bar (the live box would autofocus and swallow typing). The grid waits for the layout GET, so the box mounts slightly later than the old hero. `?q=` is only read by a mounted search widget, so without one it is ignored.
- Tour/shortcuts: the Home tour's first step now targets `#home-sec-search` (the grid anchors the first search widget like apps/playground/sessions), and `presentSteps` drops it when search was removed. The type-anywhere-to-focus shortcut and autofocus live inside `FilesSearch`, so they vanish with the widget and need no guard. `home-performance.test.ts` and `new-task-form.test.ts` needed no change (they pin data.ts/strip.ts/widgets, not the search); `registry.test.ts` was updated for the tour selector.
- Not verified without a browser: bar look in view mode (no card, padding, 120px min grid row around a 4x1 bar), takeover layout and restore, 2x1 bar width, edit-mode placeholder, sheet preview and icon, the tour step highlight, keyboard focus when search is the first widget.

## Edit mode v2: presets + swap

Users found the free-grid edit mode (drag, edge resize, size menu, cell lattice, Alt+arrows, Tidy up, Reset) overwhelming and asked for layout presets; they liked the bento look. Edit mode is now a row of presets plus one action per tile: Change (swap what the tile shows), and ×. The stored document (HomeLayout v5) and the server are unchanged: a preset is just a HomeLayout.

Presets (`PRESETS`, `presetLayout(id, { folderId? })` in `shell/home/layout.ts`; units are half cells, the grid is 8 wide; fresh widget ids each time):

| Preset | Tiles (x,y; size in cells; custom = explicit cols x rows in units) |
| --- | --- |
| Workbench | exactly `defaultLayout()` |
| Builder | search 4x1 (0,0); build 2x1 (0,1); apps 2x2 icons (4,1); bots 2x2 list (0,5); tasks 2x2 list (4,5) |
| Mission control | search (0,0); tasks board custom 6x4 (0,1); index 1x1 (6,1); bots 1x1 (6,3); sessions 2x1 (0,5); recents 2x1 (4,5) |
| Files | search (0,0); recents list custom 6x4 (0,1); index 1x1 (6,1); bookmark folder 1x1 list (6,3), or sessions list custom 2x2 when no folder exists; apps 2x1 (0,5); tasks 2x1 (4,5) |
| Focus | search (0,0); build 4x1 (0,1) |

Every preset is hole-free over its used rows and survives `normalizeLayout` unchanged. `matchPreset(layout)` compares the multiset of (source, x, y, cols, rows, format) with each preset (ids, sort, folder and page choices ignored; Files also matches its no-folder variant) and returns null for a custom layout; the edit bar shows a checked "Custom" chip then. Clicking a preset replaces the layout and offers Undo (`api.restore`) until the next preset click or until edit mode closes. The Files folder is the first bookmark folder, if any.

Swapping: `sourceFits(layout, rect, source, replacingId?)` decides what a tile or empty slot may show, in order: search needs a one-row strip at least 2 units wide and refuses with "Already on Home" if another search exists; build needs exactly 4 rows and 4+ columns; any other source refuses a one-row rect ("Needs a taller tile") or one under its `minFootprint` ("Needs a bigger tile"); more than `MAX_WIDGET_ROWS` rows is "Too tall". `swapSource` keeps the widget's id and rectangle, takes a size preset when one matches the footprint and an explicit cols/rows otherwise, and keeps folderId/appPath/sort only where they belong; a failed fit or an unchanged result returns the same object. `emptySlots(layout)` finds holes of at least one cell square inside the used rows, shown as "+ Choose what goes here" slots; `fillSlot` adds a tile on exactly that rectangle. Removing a tile (x) leaves such a slot.

The Change popover (`ChangeTile.tsx`) lists all eleven sources with the reason under the disabled ones; folder and page rows end in "..." and open the add sheet aimed at the tile (`AddWidgetPanel` `target`: the gallery lists only that source, a card whose default footprint exceeds the tile is disabled "Too big for this tile", and a pick swaps or fills keeping the tile's footprint). Swap mode also offers "Show as" and, for apps, "Sort by".

Removed from the UI (`placeWidget`, `moveByArrow`, `resizeTo`, `resizeByArrow`, `compactLayout` and `emptyRows` are deleted from layout.ts; `setSize` and `allowedSizes` stay): dragging, edge resize handles and ghost, the size menu, the dashed cell lattice, Alt+arrow moves, Tidy up, Reset to default, the hint line. The hook lost `place`, `arrow`, `resizeTo`, `resizeArrow`, `tidy`, `resize`, `reset` and gained `applyPreset`, `swap`, `fill`, `restore`.

Not verified without a browser: every visual (preset chips and thumbnails, the pill on every tile size, the bare tiles' overhanging x, slot look, popover placement and flip, the narrow chip row scrolling), Escape layering with the popover open.

### Review fixes (edit mode v2)
- `.hw-swap > button` so pill styles no longer leak into the Change popover.
- Files preset without a folder falls back to bots (1x1); sessions has no size fitting 2x2 units. Tested for all presets with and without a folder.
- Undo clears on any layout change after the preset commit (a `presetPending` ref marks the preset's own commit); leaving edit mode already cleared it.
- Clicking the active preset is a no-op; `matchPreset` is computed once per layout.
- ChangeTile holds `onClose` in a ref so document listeners subscribe once.
- Fill target at MAX_WIDGETS disables every source ("Home is full"); swap targets unaffected.
- `emptySlots` splits holes taller than MAX_WIDGET_ROWS into stacked slots.

Add widget gallery (feat/home-fixed-grid):
- Not verified without a browser: preview look and legibility at 0.5 scale in both themes, bottom alignment of captions across a wrapping row, the hover lift and focus ring, three Large previews per row in the 900px sheet, live bodies inside previews (tasks, bots, apps) not stealing focus or firing requests per card, the folder and page picker view, Escape and Tab trap.

Compact cards: a card fills its tile's height (`.hw-cards` rows are `minmax(0, 1fr)`, each card root `height: 100%`, `container-type: size`). The header keeps its size; the body (app thumb, playground well, folder stack) takes the rest and crops. When the tile is 120px or shorter (`@container (max-height: 120px)`) the body and share chip are hidden and the card is its header: icon, title, path, time. Presets: Legacy (apps, playground, sessions, recents at y 1/5/9/13) and Workbench (apps y7, recents y11) give cards 8x4 cells (`custom`) so previews show; a saved layout of the old 8x2 Legacy shape reads as custom and gets header-only cards. Lists and board lanes show their "+N more" line only when it fits under the shown items (`moreLineFits`).
