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
