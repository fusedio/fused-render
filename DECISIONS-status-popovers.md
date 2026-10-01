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

## R3 — new vs earlier, important first

- `RepoUpdatesCardView` now draws three sections, top to bottom: **Needs
  you** (every attention-tier row — the update-family message pinned first,
  then waiting tasks, then every other attention row newest-first), **New**
  (unseen non-attention rows, newest-first) and **Earlier** (seen
  non-attention rows, newest-first). Each gets its own `.dl-section-head`
  with a `.dl-section-count`, per R2 — never gated on a sibling section
  being present.
- Every "Needs you" row gets a left accent bar: `dl-row-attention` for an
  ordinary attention row, `dl-row-attention-update` for the pinned update
  message, so the section still reads as one coherent "needs you" surface
  even though it merges several previously-separate row kinds.
- **Deviation — the volume cap's scope widens from "terminal-trail jobs
  only" to "the whole merged Earlier list".** The pre-R3 code scoped
  `TERMINAL_VISIBLE_CAP` to the flat terminal-job trail alone; a repo row, a
  pairing or a waiting task was always drawn in full, uncounted. Once every
  non-attention row kind is merged into one newest-first "Earlier" list
  there is no single kind left to scope the cap to without re-fragmenting
  the list back into per-kind runs — so the cap now bounds the merged list
  itself. A repo row or pairing landing in the fold is rare in practice (a
  repo stays behind until fixed; a pairing is dismissed once read), so this
  is a simplification, not a rule anyone asked for by name. Nothing is ever
  deleted by it, only folded behind the existing "N older notifications"
  row — the full list is still one click away. A waiting task is unaffected
  either way, since it lives in "Needs you", never under the cap.
- The merge sort over `pairings`/`visible` (repo rows)/`terminalTrailGroups`/
  `messagesTrail` is newest-first by each row's own timestamp, but the
  **displayed** order of whatever the cap keeps is reversed back to
  oldest-first before rendering — matching the long-standing terminal-trail
  reading order (`terminal` itself always arrives oldest-first, per
  `jobs.py`'s `list_jobs`). The cap picks the newest N for retention; the
  reversal is purely about reading order once those N are chosen.
- Two terminal-trail job groups that finish in the same second carry an
  identical `finished_at`, which would otherwise make the merge sort fall
  back to `Array.prototype.sort`'s stability and keep ties in `terminal`'s
  own oldest-first array order — backwards for a newest-first merge. Each
  group's sort key gets a `+ idx * 0.001` nudge (its position in
  `terminalTrailGroups`/`terminalAttentionGroups`) so a later arrival
  outranks an earlier one only when their real timestamps already tie; it
  never outweighs an actual difference in `finished_at`. The row's
  displayed age (`compactAge`) still reads the untouched timestamp — only
  the sort key carries the nudge.

## R4 — seen/unread state

- New `notifications-seen-store.ts`: a small `localStorage`-backed
  `{ seen: string[]; firstSeenAt: Record<string, number> }`, with
  `syncPresentKeys`/`isSeen`/`getFirstSeenAt`/`markSeen`, degrading silently
  to "nothing recorded" on any storage failure (matching `dismiss-store.ts`'s
  own established try/catch pattern) rather than throwing.
- A row counts unseen until the panel has been open while it was present.
  `RepoUpdatesDock.tsx` tracks every row key present while the panel is open
  in a `presentKeysRef`, and marks them seen on panel close — not on open —
  via `globalThis.setTimeout`/`globalThis.clearTimeout` (not `window.*`: the
  test harness's `withNav()` helper temporarily swaps out `globalThis.window`
  for a bare stub during a simulated click, and a real browser's
  `window.setTimeout` and `globalThis.setTimeout` are the same function
  either way, so there is no behavioral difference in production, only in
  that one test seam).
- Every row's stable identity for seen-tracking is a module-level key
  function (`pairingKey`, `repoRowKey`, `attentionRowKey`, `messageKey`,
  `jobGroupKey`) — not exported, since nothing outside this module needs to
  reconstruct one.

## R5 — timestamps on every row

- `compactAge()` (added to `format.ts`) turns an epoch-ms timestamp into
  "now"/"Xm"/"Xh"/"Xd"/"Xmo"/"Xy". Every Notifications row now computes and
  passes an `age` prop through to `NotificationCard` (via `JobRow`/
  `GroupJobRow`/`AttentionRowView`/`MessageRowView`/`RepoRowView`/
  `PairingRowView`), which draws it right-aligned and muted on the same
  line as the row's caption (`.dl-origin`/`.dl-eyebrow`, with a `.dl-meta-line`
  modifier once an age is present) rather than a second line nothing asked
  for — a caller with no caption still gets the line, age alone.
- Since every Notifications row now always carries an `age`, the
  `.dl-origin` container renders on every row even when there is no
  caption — so any test isolating caption text on its own has to query
  `.dl-origin-text` (the caption-only inner span), not `.dl-origin`
  (the shared container). Fixed in `JobRow.test.tsx` and
  `RepoUpdatesDock.test.tsx`.
- A row's own `age` reads its `getFirstSeenAt`/`finished_at`/`updatedAt`
  (whichever timestamp that row kind already tracks) — no new timestamp
  field was added to any stored type.

## R7 — chip

- The chip reads `"N needs you"` in failure tone when at least one
  *ordinary* attention row (not the update-family message) is present.
  When the only attention row is the update message, the chip instead
  shows that row's own title as its label, in a non-failure "on" tone, with
  no numeral (`chipCount = 0`) — an update notice is not a failure, and
  repeating "needs you" next to its own title would be redundant.
- With no ordinary attention and no update message, the chip numeral is the
  unseen ("New") count, not the total row count — so once every present row
  has been seen, the numeral disappears (`chipCount = 0`, hidden by
  `StatusChip` itself) even though the rows are all still there under
  "Earlier". Zero unseen and zero attention draws the plain "Notifications"
  label.

## Test-infra notes (apply to any future work here)

- Bun's test runtime has no `localStorage` global at all — a bare read
  throws `ReferenceError`, not merely "unavailable". Every suite exercising
  localStorage-backed persistence stubs `globalThis.localStorage` with a
  `Map`-backed fake (see `notifications-seen-store.test.ts`, replicated in
  `RepoUpdatesDock.test.tsx`), and resets the seen-store's in-memory state
  in `beforeEach` (`_resetSeenStoreForTest()`) since `bun test` shares one
  process across every file in a run.
- `RepoUpdatesDock.test.tsx` adds `createSeenInstance`/`renderSeenView`
  helpers that simulate "a second look" at the panel (toggle `collapsed`
  closed, then open) to exercise R4's seen-marking in tests that need rows
  in "Earlier" rather than "New" — the key-computation helpers inside
  `RepoUpdatesDock.tsx` are not exported, so there is no way to construct a
  matching seen-key directly from a test.
