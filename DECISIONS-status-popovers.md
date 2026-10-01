# Decisions — status-bar popovers (SPEC-status-popovers.md)

## R1 — one scroll region per popover

- `.dl-panel` gets `max-height: min(70vh, 560px); overflow-y: auto; overflow-x: hidden`.
  `.dl-rows` loses its own `max-height`/`overflow-y`/`overflow-x` entirely — it is
  now a plain block list, and `.dl-panel` is the only element that ever scrolls.
- Notifications' "Clear all" footer (`.dl-head`) is kept **inside** the single
  scroller and made `position: sticky; bottom: 0` with the panel's own
  background (`--bg-alt`), rather than moved outside `.dl-panel` into a second
  sibling element. Reasoning: `RepoUpdatesCardView` already renders `.dl-head`
  as the last child inside `.dl-panel`, after the sections; pulling it out to a
  sibling of the scroller would mean splitting the panel's JSX into a scrolling
  part and a fixed part in two places (the collapsed/idle/empty branches would
  all need to duplicate which case gets a footer and which doesn't). Sticky
  keeps the existing single-`<div className="dl-panel">` structure intact and
  reads correctly in a browser: the footer stays pinned to the bottom of the
  visible panel while content scrolls underneath it.
- `.dl-section-head` also becomes `position: sticky; top: 0` on the same
  `--bg-alt` background, for the identical reason (R2): it has to stay opaque
  and readable while the section's own rows scroll past underneath it.

## R2 — headings always shown, with counts

- The `sectionCount > 1` / "heading only when 2+ sections" rule is deleted in
  both `DownloadManager.tsx` and `RepoUpdatesDock.tsx`. A section heading now
  renders whenever its section renders, carrying a count in a new
  `.dl-section-count` span (muted, `--fg-faint`).
- Removed the `.dl-section + .dl-section { border-top }` hairline — the sticky,
  opaque heading band is now what visually separates one section from the
  next, so a redundant hairline directly under it was pure clutter.

## R6 — retention must not bury attention

- `capRetained` now evicts in two passes: first every non-attention row,
  oldest first (append order, so index 0 is oldest); only once none are left
  does it evict an attention row, and even then it skips any row whose
  `family` is `UPDATE_NOTIFICATION_FAMILY`.
- Added `UPDATE_NOTIFICATION_FAMILY_KEY = "app-update"` and
  `UPDATE_NOTIFICATION_FAMILY = `familyKey:${UPDATE_NOTIFICATION_FAMILY_KEY}``
  to `notifications.ts`, both exported. `UpdateNotifier.tsx` sets
  `familyKey: UPDATE_NOTIFICATION_FAMILY_KEY` on every `notify()` call across
  its whole flow (available, failed, ready, restarting, gave-up) — this is
  the "stable marker on the stored notification" R3 and R6 both ask for,
  rather than a title match (titles change — "Update available" ->
  "Update ready" -> "Restarting fused-render" — across the same logical
  update).
- Rewrote `UpdateNotifier.test.tsx`'s "a genuine cap eviction still
  resurrects the restart card" test: that scenario is no longer reachable —
  the restart/update card can no longer be evicted by a busy notification
  stream at all, so the test now asserts exactly that (the card keeps its
  original id through a flood of other attention notices) instead of
  asserting the old resurrect-with-a-new-id behavior.
- Added two focused tests to `notifications.test.ts`: non-attention rows
  evict before any attention row, and the update row is never evicted even
  once only attention rows remain.

(remaining sections filled in as R3–R5, R7 land)
