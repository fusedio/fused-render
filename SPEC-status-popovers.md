# Status-bar popovers: one scroll, clear sections, new-vs-old

Two status-bar popovers become unusable once they hold many items:

- **Activity** (`platform/ui/DownloadManager.tsx`, composed by `shell/ActivityDock.tsx`) shows Running jobs and Background tasks (engines).
- **Notifications** (`shell/RepoUpdatesDock.tsx` → `RepoUpdatesCardView`) shows "Needs you" and "Worth keeping".

What users report:

1. Each section scrolls on its own (`.dl-rows { max-height: min(46vh, 340px); overflow-y: auto }`, `styles/notifications.css` ~826). Two nested scroll areas in one popover are confusing.
2. The sections are hard to tell apart. A heading appears only when two or more sections are present, so a lone section has no label at all, and the only divider is a hairline.
3. In Notifications, users can't tell what is new and what is old. Important, actionable items get buried, the fused-render "Update available/ready" row worst of all.

## Requirements

### R1. One scroll region per popover

- `.dl-panel` becomes the only scroll container: `max-height: min(70vh, 560px); overflow-y: auto; overflow-x: hidden` (pick the exact values, but it must be one region). It stays anchored the way it is now (`bottom: 100%`, `right: var(--status-bar-gutter)`), keeps the shared fixed width (`min(340px, calc(100vw - 32px))` when non-empty; see memory: sibling popovers open at equal widths), and keeps its radius, shadow and so on.
- `.dl-rows` loses its own `max-height` and `overflow`. No nested scrolling anywhere inside either panel.
- The Notifications "Clear all" footer (`.dl-head` / `.dl-clear`) stays reachable without scrolling: make it `position: sticky; bottom: 0` with the panel background, or put it outside the scroller. Choose one and record why in DECISIONS-status-popovers.md.
- Rewrite the comment block above `.dl-rows` so it describes the new behavior.

### R2. Clear section separation (both panels)

- A section heading is shown whenever its section is present, **even if it is the only section** (drop the `sectionCount > 1` / "both present" rule in both files). Each heading carries a count, e.g. `Running 3`, `Background tasks 2`, `Needs you 1`, `Earlier 4`, with the count as a muted secondary span.
- Headings are `position: sticky; top: 0` inside the panel's single scroller, on an opaque background (use the panel's `--bg-alt` or a token one step off it), so while you scroll you always know which section you're in. Use a small-caps/uppercase muted label (match the existing `.dl-section-head` type scale and tokens; don't invent colors, and follow the `ui:ui-conventions` skill).
- Sections are separated by the sticky header band itself, not by a lone hairline.
- Activity order stays Running, then Background tasks.

### R3. Notifications: new vs earlier, important first

Sections, top to bottom:

1. **Needs you** (attention rows: waiting tasks, attention jobs, attention messages including update notifications). Order:
   - fused-render update rows (from `UpdateNotifier.tsx`) **always first**, pinned. Give them a stable marker on the stored notification (e.g. a `familyKey`/`origin` value such as `"app-update"`; find the cleanest hook), not a title match.
   - then waiting tasks, then everything else, **newest first**.
   - Every Needs-you row gets a visible attention treatment: a 2–3px left accent bar in the existing attention/failure-adjacent token. The update row uses the accent (non-error) color, because it is an opportunity rather than a failure.
2. **New**: non-attention rows the user hasn't seen yet (see R4), newest first.
3. **Earlier**: non-attention rows already seen, newest first. The existing "N older notifications" fold for terminal jobs applies here.

These replace "Worth keeping". A section with no rows is not rendered.

### R4. Seen/unread state

- A notification row counts as **unseen** until the Notifications panel has been open while that row was present. Mark rows seen when the panel **closes** (or after it has stayed open about 1.5s), not on open, so the user can still see what was new during the open in which they're looking at it.
- Unseen rows show a small unread dot (accent color) at the leading edge or next to the title. Seen rows show none.
- Stable keys per row kind: message `id`/family, job group key, repo signature (reuse `repoDismissSignature`), pairing id, waiting-task signature (reuse `attentionDismissSignature`). If a row's key changes (repo moved further behind, task changed state, message repeated with a higher `count`), it is unseen again.
- Persist the seen-key set in localStorage (`fused-render:notifications-seen`), pruned on every write to keys currently present so it can't grow without bound. Wrap every read and write in try/catch, the way `dismiss-store.ts` does. Look at `dismiss-store.ts` first and reuse its pattern or helper.
- Needs-you rows also get the unseen dot, but they never move to New or Earlier. They stay in Needs you until resolved or dismissed.

### R5. Timestamps on every row

- Show a compact relative time ("now", "4m", "2h", "3d") right-aligned in each notification row's meta line, muted.
- Sources: messages use `updatedAt`; jobs use `finished_at` (watch out for the server-vs-client clock: DownloadManager already uses the server `now`, so reuse that); repo rows, pairings and waiting tasks use first-seen time tracked client-side alongside the seen store (`firstSeenAt` per key, same localStorage entry, same pruning), unless the API already provides a time.
- Sort "newest first" by these same times.

### R6. Retention must not bury attention

- `notifications.ts` `MAX_RETAINED = 5` / `capRetained` currently evicts the oldest regardless of tier. Change eviction so non-attention rows are evicted before any attention row, and an update row is never evicted. Add a unit test in `platform/lib/notifications.test.ts`.

### R7. Chip

- Keep the label `"N needs you"` and the failure tone when there are attention rows. **But** when the only attention row(s) are update rows, use the non-failure `on` tone with the label `Update ready` or `Update available`, matching the row. An update is not a failure.
- The chip numeral for the non-attention case becomes the **unseen** count, not the total. With zero unseen and zero attention, show the plain `Notifications` label with the `on` tone if any rows exist, else `idle`.

### R8. Repo-update rows are clickable

In the Notifications popover (`frontend/src/shell/RepoUpdatesDock.tsx`, `RepoUpdatesCardView`, repo rows built from `frontend/src/shell/repo-updates-lib.ts`), a row like "sandbox — Newer changes available [Update] [×]" opens the explorer at that repo's folder with the **Git sidebar** open when its body is clicked, and closes the popover. The Update and × buttons (and, on a failure, "Fix with Claude") keep their own behavior — a click on any of them never triggers the row's navigation.

- The destination is the repo's own folder, `_side=git` appended (`apps/explorer/listing/pane-side.ts`'s `git` companion — a `?_side=<companion>` deep link wins over whatever the folder's own session state last left the pane on, per `paneReopenedByUrl`).
- The clickable body is keyboard reachable (Enter/Space), has a hover state and `cursor: pointer`, and carries an aria-label/title "Open \<name\> in Git".
- No nested buttons inside buttons: the row uses `NotificationCard`'s existing `rowClick` seam (a `role="button"` div, not a `<button>`) — the same mechanism the waiting-task and pairing rows already use — with the action buttons' own `stopPropagation` (already built into `NotificationCard`) keeping them independent.
- Scope is repo rows only; other row kinds that already have a click target (waiting tasks, pairings, a message with a `page`) are unchanged.

## Out of scope

- The floating toast/pop-up column (`NotificationHost`, `.notif-host`). Don't touch it.
- The Models chip (it stays its own chip with its filled-dot semantics).
- Server-side changes. This is frontend only.

## Conventions

- Use `bun`/`bunx`, never npm/npx. Tests: `cd frontend && bun test <path>` (scoped). Typecheck: `bunx tsc --noEmit` once at the end.
- Comments describe the code as it is now. No "used to", "previously", PR numbers, or "this change". Delete stranded code and comments the redesign makes obsolete (for example, comments explaining the "heading only when 2+ sections" rule) rather than leaving them inert.
- Update existing tests that assert the old behavior (the heading-only-when-both rule in `DownloadManager.test.tsx` and `RepoUpdatesDock.test.tsx`, chip numeral = total, Worth-keeping naming). Breaking changes are fine.
- Add CSS source assertions (like `StatusBar.test.tsx` does) that `.dl-rows` has no `overflow-y`/`max-height` and `.dl-panel` has `overflow-y: auto` plus a max-height, so nested scrolling can't come back.
- Record decisions and deviations in `DECISIONS-status-popovers.md` at the repo root.
