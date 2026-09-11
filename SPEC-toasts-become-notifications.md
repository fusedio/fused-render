# Toasts Become Notifications

## Context

Two notification surfaces exist side by side, and only one of them has a
lifetime model.

**The status-bar system** (`SPEC-actionable-notifications.md`,
`DECISIONS-actionable-notifications.md`) is the considered one. A job pops a
`JobPopupCard` in the floating `.notif-host` column for
`JOB_POPUP_VISIBLE_MS` (2500 ms, `platform/lib/jobs.ts:431`), then exits.
What happens *after* that pop is governed by `JobTier`
(`platform/lib/jobs.ts:29-50`):

| tier | pops | kept afterwards |
|---|---|---|
| `attention` | yes | in the Notifications panel until dismissed, in "Needs you" |
| `trail` | yes | in the panel until dismissed, in "Worth keeping" |
| `transient` | yes | nowhere — the card is its only trace |
| `silent` | no | nowhere |

`effectiveTier` (`platform/lib/jobs.ts:240`) promotes any `error`/`cancelled`
row to `attention` regardless of what its producer declared, so a failure is
never lost to a tier choice.

**The toast system** (`platform/lib/toast.ts`, `platform/ui/Toast.tsx`) has
none of that. Every toast is equally permanent: it sits in the floating
column until the user clicks its ✕ or the code that raised it dismisses it,
capped only by `MAX_TOASTS = 5`. Deleting four models in a row leaves four
cards stacked over the page, each needing an individual ✕, none of them
reachable again once dismissed. There is no history, no count, no tier, and
no relationship to the Notifications panel that exists three pixels below
them.

69 call sites across 22 files raise toasts.

## What's Changing

Toasts stop being their own surface and become notifications, with the same
pop-then-retain lifetime the job system already has.

A new client-side notification store replaces `toast.ts`. Every message pops
a card in `.notif-host` for `JOB_POPUP_VISIBLE_MS` and then leaves the screen
on its own. Retention after the pop follows the existing `JobTier`
vocabulary, reusing the same words for the same meanings — not a parallel
enum. Kept messages become a fifth row source in the Notifications panel,
drawn through `NotificationCard` like every other row, and counted in the
chip's total and its red/attention state.

The floating column keeps working in embedded panes, because it already
renders in every document. Only *retention* is top-level: the status bar is
behind `App.tsx`'s `!IS_EMBED` guard.

### Reversal of a standing decision — must be recorded, not silently made

`SPEC-actionable-notifications.md` lists **Toasts** under *Out of Scope* and
its Constraints say:

> **No auto-dismiss timer, anywhere.** D663 … `toast.ts:7-10` records the
> same decision for the toast stack. Both stand.

This change reverses that **for client-raised messages only**. D663's actual
finding — that a timer deleting a *job* row fired the permanent server-side
`dismiss()` and broke `fused.watchJob` — does not apply here: these messages
are client-only, have no server row, and nothing watches them. The
distinction is real, but it must be written down. Update
`SPEC-actionable-notifications.md`'s Constraints and Out-of-Scope sections
and append a decision to `DECISIONS-actionable-notifications.md` explaining
why the job-row rule stands while this one changes. **Leaving the old text in
place is a defect**: the next reader treats the auto-dismiss as a regression
and reverts it.

D663's rule for **job** rows is unchanged and must stay unchanged.

## Affected Flows

```
CURRENT
  pushToast(error) ──> .notif-host card ── stays until ✕ ──────── no history
  pushToast(info) ───> .notif-host card ── stays until ✕ ──────── no history
  terminal job ──────> .notif-host card ── 2.5s ──> panel per tier ✓

DESIRED
  notify(attention) ─> .notif-host card ── 2.5s ──> panel "Needs you" ─┐
  notify(trail) ─────> .notif-host card ── 2.5s ──> panel "Worth keeping" ┼─> one model
  notify(transient) ─> .notif-host card ── 2.5s ──> gone ──────────────┘
  terminal job ──────> unchanged ✓
```

## Work

### 1. `frontend/src/platform/lib/notifications.ts` (new, replaces `toast.ts`)

Same module-store + `useSyncExternalStore` shape `toast.ts` already uses —
keep that, it works and is tested. Keep the `globalThis.setTimeout` rule and
its comment (`toast.ts:56-62`): timers through `window` aborted whole `bun
test` runs between files.

```ts
export interface NotificationInput {
  title: string;
  detail?: string;
  tier?: JobTier;          // default derived from tone, see below
  tone?: "error" | "info"; // retained as the ergonomic shorthand
  action?: NotificationCardAction;
  page?: string;           // click destination, per SPEC-actionable-notifications
}
export function notify(input: NotificationInput, replaceId?: number): number;
export function dismissNotification(id: number): void;
```

- `tone: "error"` with no explicit `tier` ⇒ `attention`. `tone: "info"` with
  no explicit `tier` ⇒ `transient`. An explicit `tier` always wins.
- Keep `replaceId` and its exact current semantics (`toast.ts:70-104`) — the
  "Still undoing…" repeat-collapse depends on it.
- Keep `MAX_TOASTS`-style capping for the *kept* list and the `leaving` /
  `TOAST_EXIT_MS` exit-animation mechanics. `JobPopupCard` imports
  `TOAST_EXIT_MS` from `toast.ts` today — re-export or move it, do not
  duplicate the constant.
- The store holds two things: the pop queue (latest-wins, one at a time,
  mirroring `popupTick` at `platform/lib/jobs.ts:378`) and the retained list
  (`attention`/`trail` only).

### 2. `frontend/src/platform/ui/NotificationHost.tsx`

Add a message-popup slot beside the existing `jobPopup`. It must draw a
`NotificationCard`, not the old `Toast` — the user's own rule for the job
pop-up applies identically here: "the UI should still be the same
notification card" (`NotificationHost.tsx:36-41`).

Reuse `JobPopupCard`'s lifecycle wholesale rather than re-implementing it:
the mount-once timer, the `leaving` exit, the outside-click dismissal, and
the iframe-blur edge case (`JobPopupCard.tsx:96-160`). Those four behaviours
were each found the hard way; a second hand-rolled copy will get the
iframe-blur edge wrong. Extract the shared lifetime into a hook or wrapper
used by both cards.

**The ✕ on a popped card closes the card only.** It must not delete the
retained panel row — same rule, same reason, as `JobPopupCard.tsx:22-31`.

### 3. `frontend/src/shell/RepoUpdatesDock.tsx` — a fifth row source

`RepoUpdatesCardView` (line 440) already takes `rows`, `terminal`,
`pairings`, `attention`. Add `messages`.

Split it the way `terminal` is already split (line ~497): messages whose tier
is `attention` join "Needs you"; `trail` joins "Worth keeping". Feed
`messages` into **`total`** (line ~520) and into the attention count — the
header comment at line 517 names a miscounted `total` as the likeliest bug in
this kind of change, and it was right once already.

Rows draw through `NotificationCard`. A message with a `page` gets a
clickable body that navigates and clears the row, satisfying
`SPEC-actionable-notifications`'s "every row goes somewhere" rule. A message
without one is dismiss-only — acceptable, but prefer setting `page` at call
sites where an obvious destination exists.

### 4. Panes and embeds

`.notif-host` renders in every document, so the pop works in a pane with no
change. The panel does not exist there (`App.tsx:1065`, `!IS_EMBED`).

A pane forwards its **retained** (`attention`/`trail`) messages to the
top-level shell. **Use the established idiom, not `postMessage`**: a plain
global called on a same-origin window, wrapped in try/catch. See
`apps/explorer/lib/snapshot-clear.ts:32-60` for the exact pattern and the
cross-origin `SecurityError` guard it documents — this codebase deliberately
avoids `postMessage` (`main.tsx:53`, `ChatFrame.tsx:20`,
`snapshot-clear.ts:33`). Direction here is child→parent, so the shell
installs the global and the pane calls it on `window.top`.

**`IS_TOP_EMBED` is the exception** (`platform/lib/router.ts:169`): a tab or
bookmark opened standalone is its own top window, so there is no shell to
forward to. There, an `attention` message's card **stays until dismissed**
rather than expiring at 2.5s — otherwise an error in that context is shown
for two and a half seconds and then lost with no history anywhere. `trail`
and `transient` expire normally.

### 5. Migrate the 69 call sites

22 files. `grep -rn "pushToast(" frontend/src | grep -v '\.test\.'`

Default mapping is `tone`-driven (§1), so a mechanical rename gets the right
answer for most sites. Deviate deliberately where the message deserves a
record:

- **Destructive-but-successful** operations were originally meant to get
  `trail`, not `transient` — "Freed 1.4 GB — deleted superwhisper/s1-mini",
  a completed move, a duplicate — on the theory that the user should be
  able to find out what was deleted after the card is gone. **Reversed**
  (user: "don't keep this in the list. just show popup. anything non
  actionable or error doesn't belong in the list" — see
  DECISIONS-toasts-become-notifications.md's "Reversal: retention narrows
  to error-or-actionable"): a client-raised message is retained only if it
  is an error, or carries something to act on (`action`/`page`). A
  destructive-but-successful operation with neither now only pops, same as
  any other ordinary confirmation. `trail` is no longer even a type a
  client call site can pass (`ClientNotificationTier` in
  `notifications.ts`) — it remains valid only on the server-side `JobTier`
  a job row declares.
- **Ordinary confirmations** stay `transient` — "Path copied", "Duplicated as
  foo.py".
- **Failures** are `attention` by default and should stay there.

Record every non-default tier choice in `DECISIONS-toasts-become-notifications.md`
as a short table (call site → tier → why). The orchestrator will surface that
table to the user for review, so it must be complete.

### 6. Delete the old surface

Remove `platform/lib/toast.ts` and `platform/ui/Toast.tsx` and their tests
once nothing imports them. Keep the `.toast-slot` / `.toast` CSS that
`JobPopupCard` still depends on; rename only if every reference moves in
lockstep.

**`grep tests/ for every deleted symbol before finishing.** Python tests in
`tests/` assert against literal frontend source lines and will break on a
rename while `bun test` stays green — this has bitten this repo before.
`tests/test_theme.py` in particular asserts no literal colour hexes; use the
`--success`/`--error` tokens, never hexes.

## Constraints

- `JobTier`'s meanings are reused verbatim. Do not introduce a parallel enum
  with different words for the same four states.
- Job rows keep D663's no-auto-dismiss rule exactly as it is. Nothing in this
  change may add a timer that fires a server-side `jobs.dismiss()`.
- `effectiveTier`'s error/cancelled promotion must have a client-side
  equivalent: a message declared `transient` that reports a failure is still
  `attention`. A tier is a default, not the last word.
- The chip's count, red state, `TERMINAL_VISIBLE_CAP` fold, and the
  localStorage dismissal keys keep their current behaviour for existing row
  kinds.
- Retained messages are **in-memory and session-only**. No server store, no
  localStorage, no new endpoint. They are gone on reload, by design —
  persisting them would need an expiry rule that D663 spent a revert
  avoiding.
- `platform/` may not import `shell/` or `apps/`
  (`frontend/scripts/check-boundaries.mjs`). The store lives in `platform/`;
  anything needing shell knowledge is handed in as a prop, the way
  `StatusBar` already takes `models`/`activity`/`repoUpdates`.
- Nested controls inside a clickable row must not activate the row — the
  guard already in `NotificationCard`.

## Out of Scope

- Native OS notifications (`fused_render/app.py:362-363` records that as a
  deliberate decision).
- Any change to how server-side jobs are produced, tiered, or dismissed.
- `sys:schedule:*` rows.
- Persisting messages across reloads.
- The commit already on this branch (`845d7ca9b`, AI models curation seal) —
  unrelated, carried over from the user's working tree at their request.

## Verification

- Scoped tests only during the build. `bun test <file>` for the touched
  frontend files; `pytest tests/test_theme.py` and any `tests/` file that
  greps the frontend sources you changed.
- New tests must cover: the tone→tier default mapping; the error promotion
  override; that a `transient` message leaves nothing in the panel; that an
  `attention` message does; that `total` and the attention count include
  messages; that the pop-up ✕ does not clear the retained row; and the
  `IS_TOP_EMBED` no-expiry path.
- The full suite is the orchestrator's job, run once at the end. Do not run
  it per commit.
