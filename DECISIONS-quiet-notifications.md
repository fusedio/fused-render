# Decisions — Quiet Notifications

Build log for SPEC-quiet-notifications.md. Read that spec, plus
SPEC-actionable-notifications.md / DECISIONS-actionable-notifications.md and
SPEC-toasts-become-notifications.md / DECISIONS-toasts-become-notifications.md,
before picking this up — this file assumes all four.

## Status at handoff

**Built, tested, committed, pushed** (commits `3be42e4f7`, `b054269cb`,
`6d32d2a38` on `worktree-quiet-notifications`):

- **§1 Presence registry** — `frontend/src/platform/lib/presence.ts` (new),
  `frontend/src/platform/lib/presence.test.ts` (new).
- **§2a Client-message suppression** — `notifications.ts`'s `notify()` gains
  `source`/`isSuppressed`, wired at `AppPage.tsx`'s two `notify()` call sites.
- **§2b Server job-row suppression** — `jobs.ts`'s `jobRows`/`popupJobs`/
  `popupTick`/`terminalNotifications` take an optional `isOpenAnywhere`
  callback; new exported `isRecentOnly(job, isOpenAnywhere)`. Wired in
  `ActivityDock.tsx`, the sole feed into Notifications.

**NOT built** — left for whoever picks this up next:

- **§3 Grouping** (server `group` field on `Job`/`upsert()`, client grouping
  by `(page, group)`, D-C pop rules). Nothing touched: `fused_render/jobs.py`,
  no client grouping logic.
- **§4 The "Recent" section** in `RepoUpdatesDock.tsx`. `isRecentOnly` above
  is the hook a Recent-section implementation needs — right now a job that
  satisfies it simply **disappears** from `jobRows`/`popupJobs` rather than
  moving anywhere. That is a real, if smaller, behavior change already
  shipped in this branch and worth flagging loudly: **a suppressed success
  currently has no visible trace anywhere in the UI**, not even a folded
  count. `jobRows`'s and `popupJobs`'s doc comments say only "does not pop /
  does not enter Needs-you/Worth-keeping" — silently reads as "goes to
  Recent (§4)" once §4 exists, but until it does this is a quieter panel
  with no compensating Recent list. Do not ship §2b alone past this branch
  without §4 close behind it.
- **§5 Claude task notifications** (`started` schedule event, the
  `schedule-toast.ts` reversal, task-status-poll diffing, narrator-gated
  raising). Nothing touched: `fused_render/schedule.py`,
  `scheduleEvents.ts`, `schedule-toast.ts`, `tasks_watch.py`,
  `tasks_store.py`. **D661 and the `schedule-toast.ts:30` reversal are
  therefore NOT yet made** — do not append "tasks now notify" to
  `DECISIONS-actionable-notifications.md` until this actually lands; doing
  so before the code exists would document a change that hasn't happened.

The required documentation appends to `DECISIONS-actionable-notifications.md`
and `DECISIONS-toasts-become-notifications.md` (and their SPECs' Constraints/
Out-of-Scope sections) have been made for §1/§2a/§2b, which are real and
shipped. The §5-specific appends the parent spec's Documentation section asks
for (D661 reversal, schedule-toast.ts reversal) are deliberately **not**
included yet, for the reason above — add them when §5 actually lands.

## Design decisions taken while building §1/§2/§2b

- **`isFocusedHere` does not read the presence registry at all.** It was
  tempting to make it "read my own entry back out of localStorage", but a
  document always knows its own focus/visibility/page precisely and
  instantly; going through localStorage (JSON round-trip, `PRESENCE_REFRESH_MS`
  cadence) would make a document's read of *itself* stale by up to 5s for no
  benefit. `isFocusedHere` calls `document.hasFocus()`/`visibilityState`
  directly and compares against `currentPresencePage()` (this document's own
  live page, not what it last wrote to storage).
- **`matchesSource` gained a field not in the spec's literal `PresenceEntry`
  shape**: `topLevel: boolean`, needed to answer narrator election honestly
  ("lowest non-stale windowId **among top-level entries**") — a pane's own
  entry must not be eligible to narrate even though every pane registers.
  Computed as `IS_TOP_EMBED || window === window.top`.
- **`matchesSource`'s prefix rule is symmetric** (`page` a prefix of `source`
  OR `source` a prefix of `page`), not just "source is a folder containing
  page". A job's `page` and a window's displayed page can be nested either
  direction depending on which one names the deeper path, and both
  directions need the same "same place" answer. Query-bearing routes
  (`?tab=`) are carved out first and matched exactly, never by prefix, in
  either direction.
- **`isOpenAnywhere`/`isNarrator` degrade to the "notify"/"narrate" answer
  on every failure path** (no storage, throwing storage, corrupt JSON,
  nobody on record) per the spec's own explicit direction — pinned with
  tests in `presence.test.ts`.
- **The heartbeat is a self-installing module-load side effect**
  (`installHeartbeat()` called unconditionally at the bottom of
  `presence.ts`), the same pattern `notifications.ts`'s `installIngest()`
  already uses — there is no shell-level call site that would reliably run
  once per document otherwise, and `platform/` can't be handed a mount
  point from `shell/`/`apps/` without inverting the dependency direction
  the boundary rule requires.
  - **Dead end found the hard way**: this side effect runs on import in
    *every* test file that transitively imports `notifications.ts` or
    `jobs.ts` (nearly all of `platform/`'s and `shell/`'s test suite), not
    just files testing presence itself. Several existing test files stub a
    **minimal** `globalThis.window` before `installDomShim()` ever runs
    (first-registration wins for the whole `bun test` process — see
    `testDomShim.ts`'s own header comment) — `RepoUpdatesDock.test.tsx` is
    the concrete example, whose stub window has no `setInterval` and no
    `addEventListener`. The fix: the heartbeat's own timer goes through
    `globalThis.setInterval` (not `window.setInterval` —
    `notifications.ts`'s own "never window, for exactly this reason" rule,
    restated here for a second module), and every `addEventListener` call
    goes through a `safeListen()` helper that no-ops on a target with no
    such method rather than throwing. Confirmed clean by running the full
    `src/platform` + `src/shell` bun suites (2546 tests) after the fix, not
    just the two files that surfaced the bug.
- **`jobRows`/`popupJobs`/`popupTick`/`terminalNotifications` all take the
  suppression callback as an *optional* trailing parameter**, not a
  required one, so `DownloadManager.tsx`'s existing `jobRows(mergedRows(...))`
  call (which only ever sees non-terminal jobs by the time `inFlightJobs`
  is done with them, so the new parameter would be a no-op there anyway) did
  not need to change at all — pinned with an explicit
  "omitting isOpenAnywhere preserves today's behavior" test in `jobs.test.ts`.
- **`isRecentOnly` reads `effectiveTier(job) === "attention"` explicitly**
  rather than folding that into a single boolean expression, specifically so
  the "an error/cancelled job is never suppressed" invariant stays visibly
  true in the source rather than being an accidental consequence of also
  checking `state === "done"` (error/cancelled jobs are never `"done"`
  anyway, which would have made the `state` check alone sufficient by
  accident — writing both checks is the deliberate, defensive version).

## What the next builder should do first

1. **§4 before anything else that depends on `isRecentOnly`.** The
   suppressed-but-invisible gap noted above is real today; closing it is the
   highest-value next step and is scoped tightly (`RepoUpdatesDock.tsx`
   only).
2. **§3 grouping** touches `fused_render/jobs.py` (new field, needs a
   Python-side test) and a client grouping layer — read
   `DECISIONS-actionable-notifications.md`'s existing notes on `jobRows`/
   `popupTick` composition order before changing either, since grouping's
   pop rule (D-C) has to compose with the presence suppression already
   landed here (§3's own text: "a group row is suppressed under §2b when
   *every* member satisfies the suppression condition").
3. **§5** is the largest remaining piece and the one the parent spec's
   Documentation section most wants written up (D661, `schedule-toast.ts:30`)
   — do not write those decision-log entries until this section's code
   actually ships.

## Baseline (before any change in this branch)

- `bun test src/platform/lib/jobs.test.ts src/platform/lib/notifications.test.ts src/shell/RepoUpdatesDock.test.tsx`
  → 174 pass, 0 fail (recorded before touching any of these files).
- `pytest tests/test_jobs_api.py tests/test_index_jobs.py` → 109 passed.
- No pre-existing red tests found anywhere touched by this branch.

## State after this branch's commits

- `bun test src/platform src/shell` → 2546 pass, 0 fail.
- `pytest tests/test_index_jobs.py tests/test_mac_update.py tests/test_jobs_api.py tests/test_task_sync_matrix.py`
  → 207 passed (one benign `PytestUnhandledThreadExceptionWarning` from
  `test_mac_update.py`'s own teardown, pre-existing, unrelated to this
  branch — the thread's `SystemExit` is that test's own deliberate
  one-tick-then-stop mechanism, not a failure).
- `bunx tsc --noEmit` clean. `node scripts/check-boundaries.mjs` → OK
  (806 files).
- Full suite (frontend `apps/`, backend beyond the four files above) not
  run — scoped-tests-only per the build instructions; that is the
  orchestrator's job at the end.
