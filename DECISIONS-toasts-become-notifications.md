# Decisions — SPEC-toasts-become-notifications.md

This build replaces `platform/lib/toast.ts` + `platform/ui/Toast.tsx` (the
old always-visible, always-timed, never-retained toast stack) with
`platform/lib/notifications.ts`'s `notify()`/`dismissNotification()`/
`dismissPopup()`, which reuses the status-bar Notifications panel's
`JobTier` retention model (`attention`/`trail`/`transient`/`silent`) for
client-raised messages. This document records every call-site tier
assignment, the decisions the spec left open, the dead ends ruled out
along the way, and everywhere the spec's own text was wrong or
self-contradictory.

## How a tier is resolved (recap, see `notifications.ts`)

- `tone: "error"` with no explicit `tier` → `attention`, and `tone:
  "error"` **always** promotes to `attention` even over an explicit lower
  tier (mirrors `jobs.ts`'s `effectiveTier()` error/cancelled promotion).
- `tone: "info"` (or no tone at all) with no explicit `tier` → `transient`
  — pops, never retained.
- An explicit `tier` on a non-error message wins over the tone default —
  this is how "destructive-but-successful" messages (spec's own examples:
  a completed move, a batch delete, "Freed X — deleted…") get `tier:
  "trail"` so they can be found again in "Worth keeping" after the popup
  is gone.
- `silent` is never used by any of the 69 call sites below — no migrated
  message needed to skip the popup entirely.

## The full call-site table (69 sites, 22 files)

All 69 `notify()` call sites this migration produced, found by walking
every `notify({...})` call under `frontend/src` (excluding `*.test.*`).
`pushToast`/`dismissToast`/`toast.ts`/`Toast.tsx`/`toast.test.ts` have
zero remaining references anywhere in `frontend/` or `tests/` as of this
table.

| File:line | Message (abridged) | tone | tier | why |
|---|---|---|---|---|
| `shell/AppPage.tsx:336` | "Could not change the icon: " + (e as Error).message | error | attention | tone: "error" default → attention (always promotes, even over a lower explicit tier) |
| `shell/AppPage.tsx:490` | "Could not export " + slug + ": " + (e as Error).message | error | attention | tone: "error" default → attention |
| `shell/ScheduleTaskViews.tsx:2851` | `Deleted ${task.task_id}` | info | trail | Destructive-but-successful ('Deleted <task>') — explicit trail override so it can be found again after the popup is gone |
| `shell/ScheduleTaskViews.tsx:3589` | `Deleted ${task.task_id}` | info | trail | Destructive-but-successful — explicit trail override |
| `shell/CurrentAppsSection.tsx:646` | "Could not rename " + app.name + ": " + (e as Error).message | error | attention | tone: "error" default → attention |
| `shell/ActivityDock.tsx:144` | `${engineLabel(prev)} retired (idle)` | info | transient | tone: "info" default → transient (ordinary confirmation, popup only) |
| `shell/useMissingFolders.ts:53` | MISSING_FOLDER_TOAST | error | attention | tone: "error" default → attention; dedup target changed from the old toast queue to `getRetainedNotifications()` since `notify()` is "latest wins, one at a time" by construction — but repeated presses would otherwise stack duplicate attention rows (nothing there auto-expires) |
| `shell/TaskCards.tsx:472` | said | error | attention | tone: "error" default → attention |
| `shell/TaskCards.tsx:686` | `Deleted ${task.task_id}` | info | trail | Destructive-but-successful — explicit trail override |
| `shell/TaskCards.tsx:963` | `Deleted ${task.task_id}` | info | trail | Destructive-but-successful — explicit trail override |
| `platform/ui/FdaStrip.tsx:58` | FDA_COPY.deniedToast | error | attention | tone: "error" default → attention |
| `platform/ui/FdaStrip.tsx:72` | FDA_COPY.grantedToast | info | transient | tone: "info" default → transient |
| `platform/ui/AppPreviewCard.tsx:453` | "Could not export " + app.name + ": " + err.message | error | attention | tone: "error" default → attention |
| `platform/lib/scheduleEvents.ts:104` | t.msg | error | attention | Failed schedule event, actionable (Open action) — attention via tone default; dismiss handler retracts fully (`dismissPopup()` + `dismissNotification(id)`) before navigating |
| `platform/lib/mountHealth.ts:61` | `${e.name} reconnected` | info | transient | tone: "info" default → transient |
| `platform/lib/mountHealth.ts:69` | `${name} disconnected` | error | attention | Actionable failure (Reconnect action) — attention via tone default; dismiss handler calls both `dismissPopup()` and `dismissNotification(id)` since the reconnect attempt raises its own fresh outcome notification |
| `platform/lib/mountHealth.ts:83` | `${name} reconnected` | info | transient | tone: "info" default → transient |
| `platform/lib/mountHealth.ts:85` | `${name} — reconnect failed: ${(err as Error).message}` | error | attention | tone: "error" default → attention |
| `platform/lib/appCardMenu.ts:63` | "Could not reveal " + app.name + ": " + e.message | error | attention | tone: "error" default → attention |
| `platform/lib/appCardMenu.ts:81` | "Could not export " + app.name + ": " + e.message | error | attention | tone: "error" default → attention |
| `platform/lib/appCardMenu.ts:96` | "Path copied" | info | transient | tone: "info" default → transient |
| `apps/explorer/Preview.tsx:243` | (e as Error).message \|\| "clone failed" | error | attention | tone: "error" default → attention |
| `apps/explorer/Preview.tsx:327` | `Duplicated as ${basename(dst)}` | info | transient | Ordinary confirmation, not destructive — resolves the spec's own "Duplicated as X" contradiction (see below) in favor of the more specific bullet; duplication deletes nothing |
| `apps/explorer/Preview.tsx:329` | friendlyFsError(e, { verb: "duplicate", … }) | error | attention | tone: "error" default → attention |
| `apps/explorer/Preview.tsx:352` | friendlyFsError(e, { verb: "delete", … }) | error | attention | tone: "error" default → attention |
| `apps/explorer/Preview.tsx:376` | friendlyFsError(r.message, { verb: "delete", … }) | error | attention | tone: "error" default → attention |
| `apps/explorer/Preview.tsx:392` | err | error | attention | tone: "error" default → attention |
| `apps/explorer/Preview.tsx:414` | friendlyFsError(e, { verb: "rename", … }) | error | attention | tone: "error" default → attention |
| `apps/explorer/Preview.tsx:421` | "Path copied" | info | transient | tone: "info" default → transient |
| `apps/explorer/Preview.tsx:427` | friendlyFsError(e, { verb: "reveal", … }) | error | attention | tone: "error" default → attention |
| `apps/explorer/Preview.tsx:457` | "Command copied — paste it in your terminal" | info | transient | tone: "info" default → transient |
| `apps/explorer/Preview.tsx:503` | "Preview not captured — the app frame has to be fully on screen" | error | attention | tone: "error" default → attention |
| `apps/explorer/Preview.tsx:511` | "Preview not captured — nothing was changed" | info | transient | tone: "info" default → transient |
| `apps/explorer/Preview.tsx:516` | "Preview replaced/saved — " + name + "/preview.png" | info | transient | tone: "info" default → transient (a saved preview file is a routine confirmation, not framed by the spec as destructive) |
| `apps/explorer/Preview.tsx:521` | "Could not save preview: " + (e as Error).message | error | attention | tone: "error" default → attention |
| `apps/explorer/Preview.tsx:528` | "preview.png here is a folder — move it first" | error | attention | tone: "error" default → attention |
| `apps/explorer/Preview.tsx:579` | friendlyFsError(e, { verb: "reveal", … }) | error | attention | tone: "error" default → attention |
| `apps/explorer/Preview.tsx:2436` | "Fixed — reloading this file's preview…" | info | transient | tone: "info" default → transient |
| `apps/explorer/Preview.tsx:2448` | "Nothing to repair — the registry file already reads fine." | info | transient | tone: "info" default → transient |
| `apps/explorer/Preview.tsx:2650` | "Your template registry could not be read, so your own view bindings are not applying: …" | error | attention | Registry read failure with a Copy-details action — attention via tone default; its `syncRegistryToast` dismiss path calls both `dismissPopup()` and `dismissNotification(id)` as a **correction** (the registry claim is now false), not a record |
| `apps/explorer/EntryActionsMenu.tsx:199` | "Could not export " + name + ": " + (e as Error).message | error | attention | tone: "error" default → attention |
| `apps/explorer/Breadcrumb.tsx:478` | (e as Error).message | error | attention | tone: "error" default → attention |
| `apps/explorer/Breadcrumb.tsx:489` | (e as Error).message | error | attention | tone: "error" default → attention |
| `apps/explorer/Breadcrumb.tsx:493` | `Can't open ${scheme}:// URLs in the explorer` | error | attention | tone: "error" default → attention |
| `apps/explorer/lib/fs-move.ts:111` | friendlyFsError(report.failed.error, { verb: "move", … }) | error | attention | tone: "error" default → attention |
| `apps/explorer/lib/fs-move.ts:122` | `Moved ${what} to ${basename(dir)}` | info | trail | Destructive-but-successful — spec's own named example ("a completed move") |
| `apps/explorer/listing/row-drag.ts:575` | `"${basename(target.path)}" isn't a folder — nothing was moved.` | error | attention | tone: "error" default → attention |
| `apps/explorer/listing/useListingShortcuts.ts:148` | `Still ${undo/redo}…` | info | transient | tone: "info" default → transient; `replaceId`-updated repeatedly against the same live popup while undo/redo runs (see the notify() timer fix below) |
| `apps/explorer/listing/useDirListing.ts:99` | err.message | error | attention | tone: "error" default → attention |
| `apps/explorer/listing/useFileOps.ts:132` | ctx ? friendlyFsError(e, ctx) : (e as Error).message | error | attention | tone: "error" default → attention |
| `apps/explorer/listing/useFileOps.ts:142` | err | error | attention | tone: "error" default → attention |
| `apps/explorer/listing/useFileOps.ts:192` | `Copying 1 of ${paths.length}…` | info | transient | tone: "info" default → transient; paste-progress popup, `replaceId`-updated repeatedly (same timer-fix relevance as above) |
| `apps/explorer/listing/useFileOps.ts:215` | `Copying ${i + 1} of ${paths.length}…` | info | transient | same as above — `replaceId` update of the same popup |
| `apps/explorer/listing/useFileOps.ts:278` | `Copy cancelled — ${pasted.length} of ${paths.length} copied` | info | transient | tone: "info" default → transient |
| `apps/explorer/listing/useFileOps.ts:324` | friendlyFsError(e, { verb: "move", … }) | error | attention | tone: "error" default → attention |
| `apps/explorer/listing/useFileOps.ts:418` | t.msg (relocationToast wrap) | conditional | trail / attention | Undo/redo outcome: success (tone info) → trail (a completed move/delete/copy record, same reasoning as fs-move.ts's own confirmation); failure (tone error) → attention via the tone default, no explicit override needed |
| `apps/explorer/listing/useFileOps.ts:497` | friendlyFsError(e, { verb: "reveal", … }) | error | attention | tone: "error" default → attention |
| `apps/explorer/listing/useFileOps.ts:506` | "Path copied" | info | transient | tone: "info" default → transient |
| `apps/explorer/listing/useFileOps.ts:514` | `${paths.length} paths copied` | info | transient | tone: "info" default → transient |
| `apps/explorer/listing/useFileOps.ts:524` | "Command copied — paste it in your terminal" | info | transient | tone: "info" default → transient |
| `apps/explorer/listing/useFileOps.ts:618` | friendlyFsError(e, { verb: "rename", … }) | error | attention | tone: "error" default → attention |
| `apps/explorer/listing/useFileOps.ts:715` | "Deleted" / `Deleted ${trashed.length} items` | info | trail | Destructive-but-successful (batch trash) — spec's own named example |
| `apps/explorer/listing/useFileOps.ts:735` | friendlyFsError(failed.message, { verb: "delete", … }) | error | attention | tone: "error" default → attention |
| `apps/explorer/listing/useFileOps.ts:819` | "Could not export " + row.name + ": " + e.message | error | attention | tone: "error" default → attention |
| `apps/claude_config/bits.tsx:43` (`toastOk`) | msg | info | transient | tone: "info" default → transient |
| `apps/claude_config/bits.tsx:47` (`toastErr`) | msg | error | attention | tone: "error" default → attention |
| `apps/ai_models/benchmark/ShareChartButton.tsx:82` | msg (share outcome) | info | transient | tone: "info" default → transient |
| `apps/ai_models/benchmark/ShareChartButton.tsx:85` | (e as Error).message | error | attention | tone: "error" default → attention |
| `apps/ai_models/local/LocalTab.tsx:333` | "Freed X — deleted…" / "Nothing deleted…" | conditional | trail / attention | Spec's own named motivating example for destructive-but-successful: trail on a clean run (`tier: "trail"` set explicitly); a run with failures stays attention via the tone: "error" default — no override needed on that branch |

## Decisions the spec left open, resolved here

- **"Duplicated as X" tier — spec self-contradiction.**
  `SPEC-toasts-become-notifications.md`'s tier guidance names "a
  duplicate" under its "destructive-but-successful → trail" bullet, then
  two sentences later names "Duplicated as foo.py" — the exact string
  `Preview.tsx` produces — under its "ordinary confirmations → transient"
  bullet. Both cannot be right for the same message. Resolved in favor of
  **transient**: the more specific/explicit bullet (verbatim message
  text beats a generic category word), and because duplicating a file is
  not actually destructive — nothing is deleted, unlike every other
  `trail` example in the spec (a move, a batch delete, freed disk space).
  If this reading is wrong, the fix is a one-line `tier: "trail"` at
  `apps/explorer/Preview.tsx:327`.
- **`useMissingFolders.ts`'s dedup target.** The old code deduplicated
  against the toast queue (`getToasts().some(...)`) because up to
  `MAX_TOASTS` (5) identical toasts could stack. `notify()`'s popup is
  "latest wins, one at a time" by construction, so a second press can no
  longer stack a second POPUP — but every call still pushes a fresh
  `attention` row into the RETAINED list, and nothing there auto-expires
  (D663). Three quick presses on a missing-folder row would otherwise
  leave three identical "Needs you" rows forever. Dedup now checks
  `getRetainedNotifications()` instead.
- **`dismissToast(id)`'s "fully retract this one" behavior has no 1:1
  replacement.** The old single call removed a toast whichever queue slot
  it was in. The new store splits this into `dismissPopup()` (ends only
  the currently-showing popup) and `dismissNotification(id)` (removes
  only a retained row). Three call sites need "fully retract THIS
  specific notification" and now call both together:
  `mountHealth.ts`'s Reconnect handler, `scheduleEvents.ts`'s Open
  handler, and `Preview.tsx`'s `syncRegistryToast` dismiss wiring (a
  correction, since the registry claim being retracted is now false —
  contrast the `trail`-tier messages elsewhere in the file, which are
  meant to leave a trace). `useFileOps.ts`'s paste-progress toast only
  ever needed `dismissPopup()` alone, since it is transient and never
  retained.
- **D663 reversed for client-raised messages, not for job/repo rows.**
  See `DECISIONS-actionable-notifications.md`'s newest entry ("Fifteenth
  round") and `SPEC-actionable-notifications.md`'s updated Constraints
  section. Short version: a job row is watched server-side
  (`fused.watchJob`) and D663 protects that watcher; a client-raised
  message has no server row and nothing watches it, so its transient
  POPUP is allowed to expire — but once a message is retained
  (`attention`/`trail`), it is governed by the same no-auto-dismiss rule
  as every other retained row from that point on.

## A regression found and fixed during this migration

`notify()`'s `replaceId` path had two branches: one for updating a
RETAINED row (re-armed the popup's exit timer on every content update)
and one for updating a currently-LIVE POPUP (updated content but left
the *original* exit timer — armed on the very first `notify()` call —
running). Two of the newly-migrated call sites repeatedly call
`notify(..., id)` against the same live popup while a potentially
long-running operation is in flight: `useFileOps.ts`'s paste-progress
toast ("Copying N of M…") and `useListingShortcuts.ts`'s "Still
undoing…/redoing…". Under the old code, an operation slower than
`JOB_POPUP_VISIBLE_MS` (2.5s) could see its progress card silently start
leaving mid-operation, regardless of how recently its content had been
updated. Fixed in `platform/lib/notifications.ts` by re-arming the exit
timer in the live-popup branch too (mirroring the retained-row branch),
with new coverage in `notifications.test.ts` ("replaceId against a live
popup re-arms the exit timer…", "replaceId against a live, never-retained
popup does not re-arm once it has left").

## Dead ends ruled out

- **Module-caching for `notifications.topEmbed.test.ts`.** An earlier
  attempt to test `IS_TOP_EMBED` behavior via a second test file that
  re-imported `notifications.ts` under a different global flag value hit
  bun's module cache — the second import saw the first test file's
  already-initialized module state, not a fresh one. `_setIsTopEmbedForTest`
  (a test-only setter in the same module) replaced this approach; no
  second test file exists for this.
- **A double-timer race in `MessagePopupCard.tsx`.** An early draft had
  both the popup's own mount-effect timer and `notify()`'s store-owned
  `armExitTimer()` racing to decide when the card starts leaving. Caught
  before landing — the store now owns the timer exclusively (`notify()`/
  `dismissPopup()` call `armExitTimer()`/`clearTimeout` directly), and
  `MessagePopupCard`/`JobPopupCard` are purely presentational, reading
  `leaving` off the store's own snapshot rather than keeping any timer
  state of their own.
- **Keeping `Toast.tsx` as a shared component under a new name.** Ruled
  out once `JobPopupCard.tsx` and `MessagePopupCard.tsx` were confirmed
  independently to render their own markup (`NotificationCard`'s
  `.dl-row`, not `Toast`'s `.toast-msg`/`.toast-action`/`.toast-close`
  structure) — `Toast.tsx` had zero remaining importers by the end of the
  migration and was deleted outright rather than renamed.

## CSS left untouched, deliberately

Per the spec: "Keep the `.toast-slot` / `.toast` CSS that `JobPopupCard`
still depends on; rename only if every reference moves in lockstep."
`JobPopupCard.tsx` and `MessagePopupCard.tsx` both still use
`.toast-slot` as their outer wrapper class (`src/styles/notifications.css`).
The bare `.toast`/`.toast-info`/`.toast-leaving`/`.toast-msg`/
`.toast-action`/`.toast-close` rules were only ever consumed by the now-
deleted `Toast.tsx` component and are technically dead CSS after this
migration — left in place rather than pruned, taking the instruction
literally and avoiding CSS-cleanup scope creep in a call-site-migration
pass; a follow-up pass can remove them along with their entries in
`src/styles/base.css`'s shared transition/press-feedback selector lists
if desired.

## Test state at the end of this build

- `bun test src/platform/lib/notifications.test.ts` — 19 pass, 0 fail
  (includes the two new `replaceId`-timer tests).
- `bun test src/shell/tasks-lib.test.ts` — 485 pass, 0 fail (two
  literal-source-line assertions updated for the new `notify()` call
  shapes: the `ScheduleTaskViews.tsx`/`TaskCards.tsx` "Deleted <task>"
  trail-tier call, and `useMissingFolders.ts`'s retained-list dedup).
- `bun test src/apps/explorer/Listing.test.tsx src/shell/AppPage.test.tsx
  src/shell/ActivityDock.test.tsx src/shell/tasks-lib.test.ts
  src/shell/current-apps-lib.test.ts src/apps/explorer/lib/fs-move.test.ts
  src/platform/lib/appCardMenu.test.ts src/platform/lib/schedule-toast.test.ts`
  — 553 pass, 0 fail.
- `bun test src/shell/RepoUpdatesDock.test.tsx` — 62 pass, 0 fail
  (covers the messages 5th-row-source addition from earlier in this
  build).
- `bun run typecheck` (`tsc --noEmit`) — clean, run after every batch of
  edits including the final deletion of `toast.ts`/`Toast.tsx`/
  `toast.test.ts`.
- `node scripts/check-boundaries.mjs` — clean (743 files).
- `python3 -m pytest tests/test_theme.py` — 133 pass, 0 fail (no new
  literal color hex was introduced by this build).
- Grepped `tests/` (Python) for every deleted/renamed frontend symbol —
  `pushToast`, `dismissToast`, `Toast`, `ToastTone`, `ToastAction`,
  `MAX_TOASTS`, `getToasts`, `useToasts` — zero references found.
- No full `bun test` suite run was performed mid-build, per the working
  agreement; the orchestrator runs the full suite once at the end.
