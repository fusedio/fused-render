# Consolidate the self-update UI into two status-bar notifications

## The problem

The self-update currently narrates itself from **five** places, three of which
can be on screen simultaneously during a single download:

| # | Surface | File |
|---|---|---|
| 1 | `UpdateBadge` — sidebar row above Settings | `frontend/src/platform/ui/UpdateBadge.tsx`, mounted `shell/GlobalSidebar.tsx:834` |
| 2 | rail dot on the collapsed Preferences icon | `shell/GlobalSidebar.tsx:443-449` |
| 3 | `UpdateProgressCard` — floating bottom-right card | `platform/ui/UpdateProgressCard.tsx`, mounted `platform/ui/NotificationHost.tsx:103` |
| 4 | `UpdateDialog kind="restart"` — blocking modal | `platform/ui/UpdateDialog.tsx`, raised `platform/ui/ServerStatusBanner.tsx:301` |
| 5 | the `sys:update:<version>` job row in the Activity panel | `platform/ui/DownloadManager.tsx` / `shell/ActivityDock.tsx` |

#3 and #5 are the *same* `JobRow` component drawn twice, while #1
simultaneously says "Downloading…" in the sidebar.

## The rule this establishes

> **Activity = progress. Notifications = decisions.**

The download's progress already lives in the Activity chip as a real job. The
two moments that need a *decision* — "there is an update, fetch it?" and "it is
installed, restart?" — become two notifications. Nothing else draws update UI.

## Target state

| Was | Becomes |
|---|---|
| `UpdateBadge` sidebar row (check + install) | **deleted**; the manual check moves to a new Preferences section |
| rail dot on Preferences | **deleted** — the Notifications chip count covers it |
| `UpdateProgressCard` floating card | **deleted** — the `sys:update:` job already draws in the Activity chip/panel |
| Activity `sys:update:` job row | **unchanged** |
| `UpdateDialog kind="restart"` | **deleted** — replaced by notification #2 |
| `UpdateDialog kind="refresh"` | **unchanged** — that is a stale *frontend build* needing a page reload, a different thing |

### Notification #1 — Download

Raised when the shared store flips to `state === "available" && !check_only`.

```
┌──────────────────────────────────┐
│ Update available                 │
│ v0.5.81 is out — you're on 0.5.80│
│                    [ Download ]  │
└──────────────────────────────────┘
```

- `tier: "attention"` so it is retained in the Notifications panel.
- The action calls `updateInstall(status.latest_version)` then
  `pokeUpdateStatus()` — the exact body of `UpdateBadge`'s current `install()`
  (`UpdateBadge.tsx:174-186`), including its comment about the server
  force-rechecking the manifest and installing the newest version it finds.
- On press the notification is **dismissed**. Activity owns the progress from
  there — one narrator at a time. Do **not** replace it with a "Downloading…"
  card; that is the duplication this change removes.
- `state === "error"` re-raises it as "Update failed · Try again" with the same
  action and `tone: "error"` (carries `status.error` in `detail`).
- `check_only` (the dev-run manager, `mac.DEV_MANAGER_ENV`) raises **nothing** —
  there is no bundle to swap, and a card with no working action is noise.

### Notification #2 — Restart

Raised when the store flips to `state === "installed"`.

```
┌────────────────────────────────┐
│ Update ready                 ✕ │
│ v0.5.81 installed — restart to │
│ start using it.                │
│                 [ Restart now ]│
└────────────────────────────────┘
```

- `tier: "attention"`, action calls `requestRestart()` from
  `platform/lib/restart-store.ts`. That function is already the one true way to
  restart (it latches, records to `localStorage`, broadcasts to other windows,
  then navigates the deep link) — **call it, never navigate `RELAUNCH_HREF`
  directly.**
- **Dismissible = "later".** On dismiss, record the version in `sessionStorage`
  (suggested key `fused_update_restart_dismissed`) and do not re-raise for that
  version. `sessionStorage` is deliberate: it survives page reloads in the same
  window but dies with the app, which is exactly the agreed "re-offers on next
  launch" semantic.
- It must survive eviction. `capRetained` (`notifications.ts:410`) drops the
  **oldest** row past `MAX_RETAINED = 5`, so five unrelated notifications can
  evict this one. The notifier re-raises it if its id is no longer in
  `useRetainedNotifications()` *and* the version is not recorded as dismissed.

### The restart, in flight

After the press the app quits for 10–40s. The **same card** narrates it, in
place, via `notify(input, replaceId)`:

```
┌────────────────────────────────┐
│ Restarting fused-render        │
│ Reconnecting…                  │
└────────────────────────────────┘
```

- Stage copy comes from `flow.stage` (`platform/lib/restart-flow.ts`):
  `quitting` → "Quitting…", `restarting` → "Restarting…", `reconnecting` →
  "Reconnecting…", `back` → "Back on v<new>". Reuse the vocabulary
  `UpdateDialog` uses today rather than inventing new words.
- **Drop the ✕ while in flight** (`restartInFlight(flow.stage)` is the exact
  predicate, already exported) — there is nothing to defer once the app is
  going down.
- **Keeping it on screen:** a notification popup auto-expires after
  `JOB_POPUP_VISIBLE_MS` (~2.65s). The supported way to hold one open is to keep
  calling `notify(..., replaceId)` — that path explicitly re-arms the exit timer
  ("a caller that keeps replacing the SAME id … is saying 'this is still
  going'", `notifications.ts:665-672`). `restart-store` already ticks at
  `TICK_MS = 1000`, so re-notifying on each tick holds the card open with **no
  change to `notifications.ts`**. Do not add a `sticky` flag.
- On `gave-up`, replace with an error card ("fused-render didn't come back") and
  restore the ✕.

## Where it is driven from

Add one **headless** component, `platform/ui/UpdateNotifier.tsx`, mounted once
in the top document beside `NotificationHost` in `App.tsx` (guard with
`!IS_EMBED` exactly as its siblings do — otherwise each pane raises its own
copy). It renders `null`. It subscribes to `useUpdateStatus()`,
`useRestartFlow()` and `useRetainedNotifications()`, and drives both
notifications through `notify()` / `replaceId`.

It must **not** be driven from `update-status.ts`'s `set()`: the restart
narration needs `useRestartFlow()`, which is a hook, and a module-store hook
would fire in every pane.

## Files

**Delete:** `platform/ui/UpdateBadge.tsx`, `UpdateBadge.render.test.tsx`,
`platform/ui/UpdateProgressCard.tsx`, `UpdateProgressCard.test.tsx`.

**`platform/ui/UpdateDialog.tsx`:** strip `kind="restart"` and everything only
it used (`stage`, `verifying`, `requestedAt`, `onRestart`). Keep `kind="refresh"`
whole. Trim `UpdateDialog.test.tsx` to match.

**`platform/lib/server-status.ts`:** `bannerSurface` must no longer return
`"restart-dialog"`. It must still **suppress the "fused-render isn't running"
card while a restart is in flight** — that suppression is the whole of step 5
and the bug PR #1214 fixed; return `"none"` for those stages instead. Update
`server-status.test.ts` in lockstep. `updateDialogMode` / `updateDialogPreview`
lose their restart preview branch (`?update-dialog=restart`); drop that branch
and its tests rather than leaving a preview for a component that no longer
exists.

**`platform/ui/ServerStatusBanner.tsx`:** drop the `restart-dialog` branch, the
`installedProbedRef` wake effect's *dialog* justification (keep the probe — the
notification still wants the real version), and the restart preview branch.

**`shell/GlobalSidebar.tsx`:** remove the `<UpdateBadge>` mount, the `updateDot`,
and the now-unused `updateLabel` / `updateRelevant` / `useUpdateStatus` imports.

**`shell/Preferences.tsx`:** add an "Updates" section following the existing
`<section className="prefs-section"><h2>` pattern (see the Appearance section at
line ~90). It shows the running version from `/api/config`, the last check
result, and a "Check for updates" button. Move `checkForUpdates`,
`checkNowLabel`, `ManualCheckPhase` and `CHECK_RESULT_HOLD_MS` usage here from
the deleted badge — including its `awaiting` ref handling for the case where a
non-forced check returns `state: "checking"` because the auto tick was already
in flight (`UpdateBadge.tsx:108-129`). That is a real bug fix, not decoration;
carry it over rather than re-deriving it.

**`platform/ui/NotificationHost.tsx` / `App.tsx`:** drop the `updateJob` prop and
its plumbing; mount `UpdateNotifier`. Check whether `jobs.ts`'s
`updateJobInFlight` still has another caller before deleting it.

**CSS:** remove `.update-badge*` (`styles/sidebar.css`) and
`.update-progress-card`; grep for other orphans.

## Verification

- TDD. Test first, watch it fail.
- Scoped runs only: `bun test <file>` for the touched frontend tests.
  **Never run the full `bun test` suite** — it OOMs the machine unless every
  tree is unmounted (PR #1291). The orchestrator runs the full suite at the end.
- `bun run build` must pass — a glob in a CSS comment only fails there.
- `frontend/scripts/check-boundaries.mjs` must pass: `platform/` may not import
  `shell/` or `apps/`. `UpdateNotifier` lives in `platform/ui` and must only
  reach `platform/lib`.
- New tests to write: the notifier raises #1 on `available` and nothing on
  `check_only`; pressing Download calls `updateInstall` and dismisses; the
  notifier raises #2 on `installed`; dismissing #2 records the version and
  suppresses re-raise for it but not for a *later* version; the card loses its ✕
  and re-notifies while `restartInFlight`; `bannerSurface` still returns `"none"`
  (never the down card) for every in-flight restart stage.

## Out of scope

- Any change to `fused_render/update/*.py` or the quit/relaunch timings.
- `UpdateDialog kind="refresh"`.
- The Windows/Linux updaters' own surfaces.

## Build notes

Items 1-5 of "Files" (`UpdateBadge`/`UpdateProgressCard` deletion,
`UpdateDialog`'s restart-mode strip, `server-status.ts`/`ServerStatusBanner.tsx`,
and the sidebar/CSS cleanup) landed in earlier commits on this branch
(`2aaf54f0d`, `c77ce9a78`, `d78cf9879`) and were verified still correct —
`bannerSurface` returns `"none"` for every in-flight restart stage,
`updateDialogPreview`'s restart branch is gone, `GlobalSidebar.tsx` has no
`UpdateBadge`/rail-dot/`useUpdateStatus` references left, and the two CSS
classes still named `.update-badge-*` (`sidebar.css`) are explicitly
documented as shared with `TroubleCard`/`ClaudeHealthStrip`, not orphans.

This pass added `UpdateNotifier.tsx` (and its test), removed the
`onUpdateJob`/`updateJobInFlight` plumbing (`ActivityDock.tsx`, `jobs.ts`,
`App.tsx`), added the `dismissible` field, and added the Preferences
"Updates" section. Notable decisions and one bug found along the way:

- **`dismissible` field, not named in the spec's Files section but required
  by "drop the ✕ while in flight"**: added to `NotificationInput`/
  `StoredNotification` (default `true`), read by `MessagePopupCard`. A
  pre-existing hand-built `StoredNotification` fixture in
  `RepoUpdatesDock.test.tsx` needed `dismissible: true` added once the field
  became required — a type-only fallout, not a behavior change for that file.

- **App.tsx has two `NotificationHost` mount sites** (the onboarding-wizard
  early return, and the main return) — not mentioned in the spec. Mounted
  `<UpdateNotifier />` at both, beside `NotificationHost` at each, since
  either one could be the page that's up when a status/restart change lands.

- **The spec's claimed keep-alive mechanism does not hold in general.** It
  says restart-store's own `TICK_MS = 1000` tick is enough to keep
  re-notifying the popup ("no change to `notifications.ts`"). Tracing
  `restart-store.ts`'s `dispatch()` shows it early-returns (skips
  `publishView()`) when a tick doesn't change `stage`/`requestedAt`/`fails`/
  `before` by value — which is exactly what happens during several seconds of
  an unchanging "Reconnecting…" between failed probes. `useRestartFlow()`
  does not reliably re-render on every tick, so a keep-alive effect keyed off
  it alone would let the popup's own exit timer (`JOB_POPUP_VISIBLE_MS`) win
  during that gap. Fix: `UpdateNotifier` runs its own independent
  `setInterval(1000ms)` and reads the stage fresh off the non-reactive
  `restartStageNow()` export on every tick. This satisfies the spec's actual
  intent ("keep it on screen") without a `sticky` flag on `notifications.ts`,
  which is the part of the spec's reasoning that does hold.

- **Infinite-loop bug found and fixed in the restart-ready effect.** The
  effect that raises Notification #2 depends on `retained` (to detect
  eviction past `capRetained`'s 5-row cap) and originally called `notify()`
  unconditionally on every run. `notify()`'s `replaceId` path against an
  existing retained row always returns a **new** array reference
  (`retained.map(...)`), so an unconditional call fed straight back into its
  own `retained` dependency — effect runs, calls `notify()`, gets a new
  array, `useRetainedNotifications()` reports a change, effect runs again,
  forever, with no actual state divergence. Caught live: a `bun test` run
  against this file pegged one core at ~258% CPU with no output for minutes
  before being killed. Fixed with a `restartRaisedForRef` guard that skips
  the `notify()` call when the card is already retained showing the same
  version — `notify()` now only fires on a genuine change (first raise, a
  version bump, or actual eviction).

- **"back" stage wording overrides `restartStageLabel`'s own text.** The spec
  calls out `` `Back on v<new>` `` by name, quoting the deleted `UpdateDialog`
  restart mode's vocabulary rather than `restart-flow.ts`'s own generic
  "Reconnected — fused-render is back." `UpdateNotifier`'s `inFlightLabel()`
  overrides just that one stage locally rather than changing
  `restartStageLabel` itself (which has no other caller left, but is not this
  file's to redefine).

- **Preferences "Updates" section has no dedicated test file** —
  `Preferences.tsx` (1000+ lines, a dozen sections) has never had a render
  test for any section; introducing that scaffold for one small section
  seemed disproportionate given the section itself is a straight port of
  already-tested-by-history `UpdateBadge` logic (`checkNowLabel`,
  `CHECK_RESULT_HOLD_MS`, the `awaiting` fix) onto a different shell.

- **Two pre-existing, unrelated standalone-test failures noticed, not
  caused by this work**: `bun test src/shell/RepoUpdatesDock.test.tsx` and
  `bun test src/platform/ui/NotificationHost.test.tsx`, each run in
  isolation, fail on a module-load-order issue (`window.addEventListener`
  missing / `location is not defined`) in code this branch never touched
  (`apps/claude/feature-flag.ts`, `platform/lib/router.ts`). Confirmed via
  `git diff HEAD` that neither file nor its dependencies were edited on this
  branch — these two test files are apparently only green when run as part
  of a larger `bun test` invocation alongside whatever file establishes the
  DOM globals first.

## Code review round 2 (2026-09-22)

Eight findings fixed. Each is testable and testable ones got tests except
where noted.

- **Finding #1 — `UpdateNotifier` mounted without `!IS_EMBED` at the main
  `App.tsx` return site.** This mount had NO guard at all (the onboarding
  branch's mount was already unreachable under `IS_EMBED` via its own outer
  `if`, but is now spelled out explicitly too, for the same reason). Decision:
  **guard alone**, not guard-plus-forwarding-fix. The compounding bugs named
  alongside this finding — `forwardToShell` dropping `dismissible` on the
  pane->shell copy, and `notify()`'s `replaceId` path against the popup
  returning before reaching the forwarding call — are real, but a grep of
  every `dismissible` usage in `frontend/src` turns up exactly one caller:
  `UpdateNotifier`'s own in-flight restart card. With the guard in place, no
  pane ever mounts `UpdateNotifier` any more, so nothing ever calls `notify()`
  with `dismissible: false` from inside a pane in the first place — the
  forwarding bugs have no live caller left to exercise them. Fixing them
  anyway would touch `notifications.ts`'s shared forwarding logic (read by
  every OTHER `notify()` caller too) for a case that can no longer occur,
  which is more risk than the finding's actual observed consequence
  (duplicate toast + duplicate shell copy) justifies. Left as a known,
  currently-unreachable rough edge in `forwardToShell`/`notify()` rather than
  rewritten.

- **Finding #2 — Preferences "Check for updates" restored to `UpdateBadge`'s
  old gate.** `settle()` now checks `updateRelevant(result)` before claiming
  `"current"`; when the store is sitting on anything the deleted
  `UpdateBadge` would have called relevant (`available`/`installing`/
  `installed`/`error`), the manual-check result no longer overrides that with
  "Up to date". No dedicated render test (see the existing note above on
  `Preferences.tsx` having no test scaffold at all) — verified by `tsc`,
  `bun run build`, and reading `UpdateBadge`'s deleted git-history source
  against the restored gate line by line.

- **Finding #3 (hardest) — eviction vs. dismissal.** Fixed by keeping a
  `prevRestartRetainedIdsRef` snapshot of `retained`'s id set as of the last
  time the raise effect ran. When the restart card's id disappears from
  `retained`, the two cases that can cause that are told apart by whether
  some OTHER id appeared at the same time: `capRetained` only ever trims the
  single oldest row as a side effect of a NEW row being pushed past
  `MAX_RETAINED`, so a genuine eviction always comes with a gain elsewhere in
  the set. An explicit dismissal (`RepoUpdatesDock`'s ✕, or "Dismiss all",
  both calling `dismissNotification(id)` directly, bypassing the component's
  own "Later" handler) is exactly `retained.filter(n => n.id !== ours)` —
  nothing is ever added. "Did the id set gain a member we didn't have before"
  distinguishes them without the notification store needing to expose
  anything new. A dismissal now routes through the same
  `recordRestartDismissed()` "Later" already uses, so it sticks; a genuine
  eviction still clears the stale ref and re-raises, unchanged from before.
  Two regression tests added to `UpdateNotifier.test.tsx`: one presses
  `dismissNotification` directly and confirms the card stays gone even after
  the underlying status is re-posted unchanged; the other floods
  `MAX_RETAINED` past capacity with unrelated notifications and confirms the
  card resurrects (as a fresh row/id — its old slot is the one that got
  sliced off) in the SAME `act()` as the eviction, since the eviction itself
  changes `retained`, which is one of the raise effect's own dependencies.

- **Finding #4 — "Later" was a no-op when `latest_version` is null.** Fixed
  together with #3 (same effect, same code review comment). Introduced a
  `RESTART_DISMISSED_NONE` sentinel (`"\u0000none"`, which cannot appear in a
  real semver) stored in `sessionStorage` in place of the version string when
  it is `null`; `wasRestartDismissed`/`recordRestartDismissed` both take
  `string | null` now and compare/write the sentinel for the null case
  instead of skipping the write entirely. Regression test added: raises the
  card with `latest_version: null`, presses "Later", re-posts the same
  (still-null) status, and asserts it stays dismissed.

- **Finding #5 — `server-status.ts`'s `installedReady` silently suppressed a
  real outage for the rest of the session.** `updateState === "installed"`
  dropped from the OR entirely; `installedReady` renamed
  `diskAheadOfHealthyServer` and narrowed to `banner === "update-restart"`
  alone, which `reduceProbe` itself clears the instant consecutive probe
  failures cross `FAIL_THRESHOLD` (it unconditionally overwrites `banner` to
  `"down"` at that point, regardless of what it held before) — so this can no
  longer paper over a genuine crash. PR #1214's guarantee ("never show the
  down card during an in-flight restart") is untouched: it lives entirely in
  `restartInFlight(stage)`, checked independently in the same `||`
  expression, unaffected by this narrowing — confirmed by the existing sweep
  test over `RESTART_STAGES.filter(restartInFlight)` still passing unchanged.
  Rewrote the stale "TWO DOORS" comment block, which justified the removed
  clause by name. Regression test added to `server-status.test.ts`:
  `banner: "down", updateState: "installed", stage: "ready"` must now surface
  `"down"` (it used to return `"none"` — the bug).

- **Finding #6 — Preferences flashed "Updates aren't managed..." on every
  visit.** `hasUpdater = status !== null` couldn't tell "no updater exists"
  apart from "haven't heard from the first `/api/config` poll yet" (both are
  `null`). Added `awaitingFirstStatus = status === null && version === null`
  (reusing `version`'s own one-shot fetch as the "have we heard back at all"
  signal rather than adding a second `useState`) and render nothing during
  that window, matching the deleted `UpdateBadge`'s own `if (!status) return
  null`. Fixed in the same edit as #2 since both touch the same render
  branch; see that finding's note on test coverage.

- **Finding #7 — `ServerStatusBanner.tsx`'s `installedVersion` was dead state
  behind a false comment.** Grepped every reader of `installedVersion` across
  `frontend/src`: `UpdateNotifier.tsx`'s `inFlightLabel()` takes a
  same-named parameter, but it is fed `status?.latest_version` from the
  SHARED update store (`update-status.ts`), never anything from
  `ServerStatusBanner`'s own local state — the two are unconnected. Removed
  `useServerStatus`'s `installedVersion`/`setInstalledVersion` state and its
  entry on the hook's return type; `result.installedVersion` (read off the
  probe body) is still fed straight into `reduceProbe()` as before — that
  path never went through the deleted `useState` and is unaffected. Replaced
  the false "the restart NOTIFICATION wants the real installed version"
  comment with what forcing the early probe actually still buys: feeding
  `bannerSurface`'s `banner` field (which this component DOES still read)
  sooner than the next scheduled 5s tick.

- **Finding #8 — CI-only flake in `UpdateNotifier.test.tsx`'s first test.**
  Root cause: `update-status.ts`'s `poll()` checked its `generation`
  staleness guard AFTER calling `set(next)`, not before — the check only
  gated whether the NEXT tick got scheduled, not whether the current
  in-flight response was allowed to mutate shared state at all. `getConfig()`
  is a real `await`; a poll started by an EARLIER bun test file's own
  `useUpdateStatus()`-consuming component mount (which calls
  `ensureStarted()` on this module-singleton store) can resolve arbitrarily
  later, including during a completely different, LATER-running test file in
  the same shared bun process (bun runs one `bun test` invocation as one
  process with one module registry and one event loop across every file).
  `resetUpdateStatusForTests()` — called in every test's own `afterEach` —
  bumps `generation` and clears the pending `setTimeout`, but cannot cancel
  an ALREADY in-flight `fetch` promise, so the old code let that stale
  response silently overwrite `current` and fire every subscriber (including
  a freshly-mounted `UpdateNotifier` in the unrelated later test) with data
  that test never asked for. This is timing-dependent by nature — fast
  locally (the round trip usually resolves before the next file's own
  `beforeEach` runs), far likelier on a slower/differently-scheduled Linux CI
  runner — which matches the exact "green 7138/0 locally, red 7137/1 on CI,
  identical file/test counts" signature. Fix: moved the `if (mine !==
  generation) return;` check to run BEFORE `set(next)`, not just before the
  re-arm. Also added explicit `resetUpdateStatusForTests()` /
  `resetRestartForTests()` / `_resetNotificationsForTest()` calls to this
  file's own `beforeEach` (previously only in `afterEach`) as defense in
  depth — belt-and-suspenders against a same-run leftover from whatever file
  happens to run immediately before this one, on top of the actual `poll()`
  fix. Verified deterministic under file-order variation: `bun test
  src/platform/ui/UpdateNotifier.test.tsx` alone (3 repeats, 8/8 pass each);
  `bun test src/platform/ui/MessagePopupCard.test.tsx
  src/platform/ui/UpdateNotifier.test.tsx` (3 repeats, 10/10 pass each). The
  coordinator's third suggested combo, `RepoUpdatesDock.test.tsx` immediately
  before `UpdateNotifier.test.tsx`, was also tried — it reproduces total
  failure of every subsequent test, but that combo fails the exact same way
  with `RepoUpdatesDock.test.tsx` run completely alone (`window.
  addEventListener is not a function`, from `apps/claude/feature-flag.ts`,
  code untouched by this branch) — i.e. it is the pre-existing,
  already-documented standalone-only failure above, not finding #8's
  mechanism; not something this branch introduced or can fix from
  `UpdateNotifier.test.tsx`'s side.
