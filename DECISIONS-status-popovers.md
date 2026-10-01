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
- `.dl-section + .dl-section > .dl-section-head` carries a `1px solid
  var(--border)` top border plus a touch more top padding (visual finding:
  the sticky, opaque heading band alone did not read as a boundary once a
  reader had scrolled a section's own rows up underneath it — nothing marked
  where one ended and the next began). Scoped to the header of every section
  but the first, so the line stays attached to the sticky band and scrolls
  away with it rather than sitting pinned at a fixed spot while the band
  slides out from under it.

## R6 — retention must not bury attention

- `capRetained` now evicts in two passes: first every non-attention row,
  oldest first (append order, so index 0 is oldest); only once none are left
  does it evict an attention row, and even then it skips any row
  `isUpdateNotification` matches (below).
- `notifications.ts` exports `UPDATE_DOWNLOAD_FAMILY_KEY = "app-update:download"`
  and `UPDATE_RESTART_FAMILY_KEY = "app-update:restart"` — two distinct
  families, not one shared `UPDATE_NOTIFICATION_FAMILY_KEY`. A single shared
  key let `retainAndCollapse` merge the download and restart cards into one
  row (shown with a "×2" badge) and made dismissing one dismiss both, since
  collapsing and dismissal both key off `family`. `UpdateNotifier.tsx` sets
  `UPDATE_DOWNLOAD_FAMILY_KEY` on its download-flow `notify()` calls
  (available, failed) and `UPDATE_RESTART_FAMILY_KEY` on its restart-flow
  ones (ready, restarting, gave-up, keep-alive) — the two cards track two
  independent pieces of the update lifecycle and now behave like it. The
  exported `isUpdateNotification(n)` helper matches either (checks the
  shared `app-update` prefix) — every place that used to compare against the
  single family constant (R3's "Needs you" pinning, R6's never-evict check,
  R7's chip logic) now calls this instead.
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
  `messagesTrail` is newest-first by each row's own timestamp, and renders in
  that same newest-first order — matching SPEC-status-popovers.md and how
  "Needs you" above it already reads. **An earlier draft here reversed the
  capped list back to oldest-first before rendering, to match
  `terminal`'s own oldest-first arrival order; that reversal is gone** — it
  put the newest arrivals at the bottom of the list, under "Earlier"'s own
  heading, which is backwards for a section whose whole point is "most
  recent first". The "N older notifications" fold consequently now sits at
  the **bottom** of "Earlier", since the rows it hides are the
  chronologically oldest and newest-first rendering puts them there.
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

- `notifications-seen-store.ts`: a `localStorage`-backed
  `{ seen: string[]; firstSeenAt: Record<string, number> }`, with
  `stampFirstSeen`/`isSeen`/`getFirstSeenAt`/`markSeen`/`useSeenSnapshot`,
  degrading silently to "nothing recorded" on any storage failure (matching
  `dismiss-store.ts`'s own established try/catch pattern) rather than
  throwing.
- **An earlier draft here (`syncPresentKeys`) pruned the persisted store down
  to exactly whatever keys were present in the CURRENT render's call —
  gone.** The panel's very first paint after a reload calls this with an
  empty or partial key list before data has loaded, which that draft read as
  "everything else is gone" and silently wiped every row's seen state on
  every reload. Pruning is now driven by age and size, not render presence,
  and happens only as a side effect of a write: an entry drops once it is
  older than 30 days, and once the store holds more than 500 entries the
  oldest-by-`firstSeenAt` are dropped first (`pruneByAgeAndSize`, called from
  `stampFirstSeen`; `markSeen` never prunes, since it has no caller-supplied
  notion of "now" to prune against).
- A client message's id (`notify()`'s row id) is not stable across a reload
  or a second window, so message keys (`message:`-prefixed) never touch
  localStorage at all — they live only in an in-memory, module-level
  `messageSeenSet` that starts empty every time the module loads. `isSeen`
  and `markSeen` both branch on the key's prefix to route to the right
  store.
- The store is now subscribable: `useSeenSnapshot()` wraps
  `useSyncExternalStore`, so `markSeen` triggers a re-render in every
  subscribed component instead of leaving a row in the wrong section (and
  the chip's own count stale) until some unrelated render happens to catch
  up. `stampFirstSeen` runs from a `useLayoutEffect` with no dependency
  array — after render, not during it — specifically because it can emit to
  subscribers (including the very component calling it), and doing that
  synchronously mid-render would mean asking React to re-render a component
  it is still in the middle of rendering.
- A row counts unseen until the panel has been open while it was present.
  **An earlier draft's close effect only called `markSeen` when a 1.5s
  "open long enough to register as read" timer had NOT already fired,
  which meant any row that first appeared on screen AFTER that timer went
  off was never marked seen on close — gone, along with the timer itself.**
  `RepoUpdatesDock.tsx` now accumulates every row key present at any point
  during an open into an `everPresentRef`, cleared fresh on each open, and
  marks every one of them seen unconditionally when the panel closes (the
  effect's own cleanup, keyed on the `collapsed` prop) — the timer added
  nothing once that accumulation + unconditional mark-on-close covers every
  case it used to, including the one it missed.
- While the panel is OPEN, "New" vs "Earlier" classification (and the unread
  dot) is frozen to a snapshot of seen-state taken the instant it opened
  (`openSnapshotRef`, captured on the closed→open transition), not read live
  — a row already on screen must not jump sections out from under the
  reader's pointer just because an unrelated render happens to land while
  they're still looking at it. The chip itself (closed) always reads the
  live snapshot, so its count/tone stay current between opens.
- Every row's stable identity for seen-tracking is a module-level key
  function (`pairingKey`, `repoRowKey`, `attentionRowKey`, `messageKey`,
  `jobGroupKey`) — not exported, since nothing outside this module needs to
  reconstruct one.

## R5 — timestamps on every row

- `compactAge()` (added to `format.ts`) turns an epoch-ms timestamp into
  "now"/"Xm"/"Xh"/"Xd"/"Xmo"/"Xy". Every Notifications row now computes and
  passes an `age` prop through to `NotificationCard` (via `JobRow`/
  `GroupJobRow`/`AttentionRowView`/`MessageRowView`/`RepoRowView`/
  `PairingRowView`).
- **Visual finding: a caption-less row (most repo rows) has no `.dl-eyebrow`
  line to share with `age`, so an earlier draft drew the age alone on that
  otherwise-empty line above the title — a whole line spent on one small
  muted figure.** `NotificationCard.tsx` now branches on whether there is a
  caption: with one, `age` still draws right-aligned on the shared
  `.dl-origin`/`.dl-eyebrow` line (`.dl-meta-line` modifier), unchanged; with
  none, no eyebrow line renders at all and `age` instead draws inline inside
  `.dl-row-head`, at the right end of the title line, before the row's
  action buttons (`.dl-row-age-inline` in `notifications.css`, same muted
  color and 11px size the eyebrow-line placement already used).
- Since not every Notifications row has a caption, `.dl-origin` (the eyebrow
  container) only renders for rows that have one — a test isolating caption
  text queries `.dl-origin-text`, and a test on the age figure itself checks
  `.dl-row-age` (shares the eyebrow line) or `.dl-row-age-inline` (the
  title-line placement) depending on which case it's exercising.
- A row's own `age` reads its `getFirstSeenAt`/`finished_at`/`updatedAt`
  (whichever timestamp that row kind already tracks) — no new timestamp
  field was added to any stored type.

## R7 — chip

- The chip reads `"N needs you"` in failure tone when at least one
  *ordinary* attention row (not an update-family message, checked via
  `isUpdateNotification`) is present. When the only attention row is an
  update message, the chip instead shows that row's own title as its label,
  with no numeral (`chipCount = 0`) — an update notice is not a failure, and
  repeating "needs you" next to its own title would be redundant. **Tone in
  that case is NOT always the quiet "on" one**: an earlier draft gave every
  update row the "on" tone regardless of content, flattening a genuine
  failure ("Update failed", "fused-render didn't come back") to the same
  neutral look as "Update available". The tone now checks the row's own
  `tone: "error"` and keeps the loud failure treatment when it is set — only
  a row genuinely offering something ("Update available", "Update ready")
  gets "on". The label is always the row's own title either way.
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
- `_resetSeenStoreForTest()` re-reads from that same fake `localStorage`
  rather than resetting to empty directly, so clearing the in-memory cache
  alone is not enough: the fake's own backing `Map` is itself a module-level
  object that outlives any one test, so `RepoUpdatesDock.test.tsx`'s
  `beforeEach` clears `seenStorageBacking` (its own fake store) in the same
  breath it calls `_resetSeenStoreForTest()` — found the hard way, once
  age/size pruning (R4) replaced the old "prune to current presence" bug
  that had accidentally been masking this: without clearing it, every key
  any earlier test in the file ever stamped stayed "seen" forever, since
  nothing in a short test run ever ages past 30 days or over 500 entries.
- `useSeenSnapshot`'s subscriber list (`listeners`, module-level in
  `notifications-seen-store.ts`) is also cleared by
  `_resetSeenStoreForTest()`. `RepoUpdatesDock.test.tsx`'s test renderers are
  never unmounted (several helpers build a fresh one per test and let the
  previous ones linger for the rest of the run); a stale, still-subscribed
  component from an earlier test would otherwise have its own
  `useLayoutEffect` fire on ANY `emit()` — including ones a LATER test's own
  `markSeen`/`stampFirstSeen` calls raise — re-stamping its own stale keys
  into the store the current test is relying on.
- `RepoUpdatesDock.test.tsx` adds `createSeenInstance`/`renderSeenView`
  helpers that simulate "a second look" at the panel (toggle `collapsed`
  closed, then open) to exercise R4's seen-marking in tests that need rows
  in "Earlier" rather than "New" — the key-computation helpers inside
  `RepoUpdatesDock.tsx` are not exported, so there is no way to construct a
  matching seen-key directly from a test.
