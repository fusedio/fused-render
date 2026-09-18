# Quiet Notifications: Presence Suppression, Grouping, and Task Coverage

## Context

Notifications are fire-and-forget. Every terminal job and every client message
announces itself regardless of whether the user was watching the thing that
raised it, nothing merges related events, and Claude tasks are deliberately
excluded from the whole surface. The result is a Notifications panel that
fills with confirmations of things the user already saw, while a task stalled
on an unanswered permission prompt — which blocks a run for up to an hour —
surfaces nowhere but the Tasks list.

Three prior documents govern this area and must be read before touching
anything: `SPEC-actionable-notifications.md`,
`SPEC-toasts-become-notifications.md`, and their two `DECISIONS-*` logs. They
record why the current rules exist, including a reverted auto-expiry (D663)
and the deliberate exclusion of tasks (D661).

## Settled decisions

These three were decided by the user during scoping. They are not open.

- **D-A — suppression reaches as far as each store honestly can.** A
  server-side job row is shared across every window, so it is suppressed when
  its source is open in *any* window or pane. A client-raised message lives
  only in the document that raised it, so it is suppressed only when *that*
  document is focused and its source is on screen.
- **D-B — successful rows move to a folded "Recent" section**, not out of the
  panel entirely. They leave "Needs you" and "Worth keeping", stop counting
  toward the chip, and remain findable under a collapsed heading.
- **D-C — a multi-member group pops on start and on failure only.** No pop as
  members complete, and no pop when the group finishes. A *single*-member
  group keeps today's pop-on-terminal behaviour unchanged (see §3).

## What's Changing

1. A client-side presence registry that knows which pages are open, where, and
   whether they are focused and visible.
2. Suppression of pure-success notifications whose source the user is already
   looking at, per D-A.
3. Grouping of jobs by source and kind into one row that counts up, per D-C.
4. A folded "Recent" section that successful rows drain into, per D-B.
5. Four Claude task moments that now notify, gated on the same presence check.

---

## 1. Presence registry — `frontend/src/platform/lib/presence.ts` (new)

The one thing this branch adds that nothing in the codebase has today. Read
`frontend/src/apps/claude/ui/useAwayRecap.ts` first — it is the existing
focus/visibility heuristic (`visibilitychange` + `blur`/`focus`, `AWAY_MS` at
:38, an on-screen `offsetParent`/rect check `recapRootVisible` at :91) and its
shape should be reused rather than reinvented.

**Cross-window transport: the established idiom, not a new one.** This
codebase signals between sibling top-level windows through `localStorage` plus
the `storage` event — see `JOB_PING_KEY` (`platform/lib/jobs.ts:156`) written
by `fused_render/static/runtime.js` and read in
`platform/ui/DownloadManager.tsx:260-267`. Use that, not `BroadcastChannel`,
not `postMessage` (this repo deliberately avoids `postMessage`;
`apps/explorer/lib/snapshot-clear.ts:32-60` documents why, and
`platform/lib/notifications.ts:217-308` uses a same-origin `window.top` global
for the parent/child case instead).

Shape:

```ts
interface PresenceEntry {
  page: string;      // the route or fs path this document is showing
  focused: boolean;  // document.hasFocus() && visibilityState === "visible"
  ts: number;        // last heartbeat, epoch ms
}
```

- One `localStorage` key holding a map of `windowId -> PresenceEntry`. Mint
  `windowId` once per document.
- Write on: mount, route change (the shell already dispatches `fused:urlchange`
  — see the note in `platform/lib/router.ts`; do **not** write the URL from an
  effect, that caused a render loop once already), `visibilitychange`,
  `focus`, `blur`, and a slow periodic refresh.
- Entries older than a staleness window (pick ~3x the refresh interval) are
  ignored on read and pruned on write. A window closed without cleanup must
  not suppress notifications forever — this is the failure mode that makes the
  whole feature look broken, so it needs a test.
- Wrap **every** `localStorage` read and write in try/catch. It throws in
  private windows and with site data blocked, and a throw here must degrade to
  "nobody has anything open" (notify), never to a crash and never to silent
  blanket suppression.
- Remove this document's entry on `pagehide`/`beforeunload`, best-effort.

Two exported predicates:

- `isOpenAnywhere(source: string): boolean` — any non-stale entry matching.
- `isFocusedHere(source: string): boolean` — *this* document only: its own
  page matches, `document.hasFocus()`, and `visibilityState === "visible"`.

**Source matching** is its own helper and needs care. A job's `page` is either
an absolute fs path (an app folder) or a shell route. A window showing
`/explorer/view/<path>` under an app folder counts as having that app open;
`/preferences?tab=lan` must match `/preferences?tab=lan` and not
`/preferences?tab=indexing`. Reuse `router.ts`'s existing pathname/prefix
constants and helpers (`IS_EMBED`, `IS_TOP_EMBED` :169, `IS_PANEL_PANE` :228,
`IS_FOREIGN_EMBED` :258) rather than inventing new parsing. Write the match
helper with tests before wiring it to anything.

**Every pane and tab registers**, including embedded panes — a pane is a place
the user can be looking at the app.

**Narrator election.** Two top-level tabs each poll `/api/schedule/events`
independently and each pop their own card today (`scheduleEvents.ts:57-124`
only guards `IS_EMBED` at :60, which does not separate two top-level windows).
Use the registry to elect one narrator — lowest non-stale `windowId` among
top-level entries — and let only that window narrate schedule and task events.
This is part of "stop piling up", not a nice-to-have.

## 2. Suppression

### 2a. Client messages — `frontend/src/platform/lib/notifications.ts`

Add an **opt-in** `source?: string` to `NotificationInput`.

**Do not default `source` to the raising document's own page.** That would
suppress "Path copied" and every other gesture confirmation whose only
feedback *is* the card. `source` is set deliberately, only where the page
already displays the same result on screen.

Suppression rule, evaluated inside `notify()` before anything pops or is
retained:

> Suppress when `source` is set, `isFocusedHere(source)` is true, the resolved
> tier is not `attention`, and the input carries no `action` and no `page`.

An error, or anything actionable, is never suppressed. This deliberately
mirrors `isRetained()` (:193) — reuse or extend that predicate rather than
writing a second, subtly different one.

Call sites that set `source` in this branch, and no others:

- `frontend/src/shell/AppPage.tsx:351` and `:515` — the app install/run
  lifecycle messages. These are the user's named motivating example.

### 2b. Server job rows — `frontend/src/platform/lib/jobs.ts`

Suppression happens **client-side at display time**. The server learns
nothing about presence and gains no endpoint — `fused_render/jobs.py` stays
authoritative about the row's existence.

In the selection that feeds the popup and the panel (`jobRows`,
`popupJobs`/`popupTick` :277-392):

> A terminal job whose `state` is `done`, whose `effectiveTier` (:240) is not
> `attention`, and whose `page` satisfies `isOpenAnywhere(page)` does not pop
> and does not enter "Needs you" or "Worth keeping". It goes to Recent (§4).

`error` and `cancelled` are promoted to `attention` by `effectiveTier` before
this check ever runs, so failures pass through untouched. Verify that by
reading `effectiveTier`, not by assuming it.

## 3. Grouping

**Server: a `group` field on `Job`** (`fused_render/jobs.py`, `Job` at
:338-442, `upsert()` at :525). Default it centrally in `upsert()` from the id's
`sys:<name>:` prefix when the id has one, else the job id itself (which yields
a group of one, i.e. today's behaviour). Producers may set it explicitly. This
is a smaller and more honest change than parsing job ids on the client.

**Client: group rows** keyed by `(page, group)` across running and terminal
jobs.

- **A group with one member renders and behaves exactly as today.** Same row,
  same pop-on-terminal. This is the single most likely regression in this
  branch — a lone download must not silently stop announcing itself. Pin it
  with a test.
- A group with more than one member renders one row: a group title, a
  `N of M done` sub-line, and an attention stripe if any member errored.
- **Pop rule (D-C), multi-member groups only**: pop when the group goes from
  no members to some, and pop when any member enters `error`/`cancelled`. No
  pop as members complete; no pop when the group finishes.
- A group row is suppressed under §2b when *every* member satisfies the
  suppression condition. One failing member keeps the whole group visible.

Grouping is by source **and** kind on purpose: downloads from the models page
group together, a file scan from that same page stays its own row.

## 4. The "Recent" section — `frontend/src/shell/RepoUpdatesDock.tsx`

`RepoUpdatesCardView` (:440) already takes `rows`, `terminal`, `pairings`,
`attention`, `messages`. Add a third section below "Needs you" and "Worth
keeping".

- **Collapsed by default**, with a count in its heading.
- Holds successful terminal job rows and completed non-retained messages.
- **Excluded from `total` and from the attention count.** The header comment at
  ~:517 names a miscounted `total` as the likeliest bug in this kind of
  change, and it has been right before. Read it.
- Client-side, in-memory, session-only, bounded (cap it, ~20, oldest dropped —
  follow `MAX_RETAINED`'s shape at `notifications.ts:94`). No server store, no
  localStorage, no new endpoint.
- Dismissing a job row from Recent calls the same server-side dismiss it
  already calls. "Clear all" (:815-833) clears Recent too.
- **No timer.** Rows leave Recent by being cleared or by falling off the cap,
  never on a clock. D663 reverted exactly that and must not be re-broken.

## 5. Claude task notifications

This partly reverses **D661** ("a task is not a job", the
`SCHEDULE_JOB_PREFIX` filter at `jobs.ts:265` and `:330`) and the decision
behind `schedule-toast.ts:30` (`toastForEvent` returns `null` for `done`,
"a run that just worked is not news"). The reversal is **conditional** — a
successful run is still not news *when you are looking at it*. Both reversals
must be written into the decisions log; leaving the old text in place is a
defect, because the next reader treats this as a regression and reverts it.

Four moments, all raised from the **elected narrator window only** (§1):

| Moment | Suppressed when its chat/app is open | Retained in panel |
|---|---|---|
| Scheduled run started | yes | no |
| Task finished | yes | no |
| Task failed | never | yes, attention |
| Task needs your input | never | yes, attention |

Sources:

- **Scheduled runs**: `fused_render/schedule.py` already emits `done`,
  `failed`, `missed` (`_report` :786-834). Add a `started` event kind.
  `frontend/src/platform/lib/scheduleEvents.ts` (POLL_MS :28) and
  `schedule-toast.ts` turn these into notifications; `done` stops returning
  `null`.
- **Interactive turns and needs-input**: derive from the existing task status
  poll rather than adding a new server channel. `/api/tasks` +
  `/api/tasks/changes` (`fused_render/server/routers/tasks.py`,
  `fused_render/tasks_watch.py` — generation :140, `wait` :243) already give
  per-task status, and `_status` (:1355-1438) already computes
  `needs_attention` as its rule 0 (:1361-1382) for a task parked on an
  unanswered permission card. Diff status across polls in the narrator window
  and notify on the transitions: `in_progress -> done`, `in_progress ->
  blocked`, and `* -> needs_attention`.
- The task's **source** for the presence check is its project folder
  (`fused_render/tasks_store.py`, `project_of()` :1129 — a project is a
  folder) and/or its chat route. A failed or needs-input notification carries
  a `page` so the row is clickable, per `SPEC-actionable-notifications.md`'s
  "every row goes somewhere" rule.

**An unattended run must not lose its notification.** Schedule events are
server-held and acked by the client, so a run that fires with no window open
is picked up on the next poll after one opens — that behaviour is existing and
must be preserved. Suppression means "you already saw it", never "nobody was
there, so it was dropped". Pin this with a test.

## Constraints

- **`platform/` may not import `shell/` or `apps/`**
  (`frontend/scripts/check-boundaries.mjs`). `presence.ts` lives in
  `platform/`. Anything needing shell knowledge is passed in as a prop, the
  way `StatusBar` already takes `models`/`activity`/`repoUpdates`. Check where
  the task-status poll can honestly live before writing it.
- **D663 stands.** No timer anywhere in this branch may fire a server-side
  `jobs.dismiss()`. `fused.watchJob` gives up the moment a row disappears.
- **`page`'s spoof-proofing is preserved.** A page-raised job's destination
  comes from the `X-Fused-Page` header, not the request body
  (`server/routers/jobs.py:61-64`).
- **No native OS notifications, no SSE, no WebSocket.** Recorded as a
  deliberate decision at `fused_render/app.py:362-363`. Polling stays.
- **No new server store and no new endpoint** for presence. The registry is
  client-side only.
- Errors and actionable items are never suppressed, never auto-expire, and
  keep their current retention.
- The chip's count, its red state, `TERMINAL_VISIBLE_CAP`'s fold (:98), and
  the localStorage dismissal keys (`shell/dismiss-store.ts`) keep their
  current behaviour for existing row kinds.
- Nested controls inside a clickable row must not activate the row — the
  guard already in `NotificationCard`.
- `tests/test_theme.py` asserts no literal colour hexes in frontend sources.
  Use the existing CSS tokens.

## Out of Scope

- Native OS / browser notifications.
- Per-source mute, snooze, or notification preferences.
- Persisting messages or Recent across reloads.
- Replacing polling with a push transport.
- Changing what any producer's title or status text says — only whether,
  when, and how it groups.
- Server-side knowledge of presence.

## Verification

- **Set the worktree up first.** A fresh worktree silently tests the main
  checkout otherwise. Use the repo's `setting-up-dev-env` skill. The
  map/geotiff daemons need the 3.12 venv with the `[bundled]` extra.
- **Scoped tests only during the build.** `bun test <file>` for touched
  frontend files; targeted `pytest` for touched Python. The full suite is the
  orchestrator's job, once, at the end. Do not run it between commits.
- **Grep `tests/` for every symbol you rename or delete.** Python tests in
  `tests/` assert against literal frontend source lines and break on a
  frontend rename while `bun test` stays green. This has bitten this repo
  repeatedly.
- New tests must cover, at minimum:
  - the source-matching helper, including the `/preferences?tab=` case;
  - a stale presence entry not suppressing forever;
  - `localStorage` throwing degrades to "notify", not to blanket suppression;
  - a suppressed success raises nothing, while an error with the same source
    still raises;
  - a **single**-member group still pops on terminal exactly as today;
  - a multi-member group pops on start and on a member failure, and not on
    ordinary member completion;
  - Recent is excluded from `total` and from the attention count;
  - an unattended scheduled run's notification survives to the next window;
  - `needs_attention` always notifies and is always retained.

## Documentation

Append to `DECISIONS-actionable-notifications.md` and
`DECISIONS-toasts-become-notifications.md`, and update the Constraints /
Out-of-Scope sections of both existing SPECs where this branch contradicts
them. Record specifically:

- the D661 reversal (tasks now notify) and why it is conditional;
- the `schedule-toast.ts:30` reversal (a successful run *is* news when you are
  not looking at it);
- that D663's no-timer rule is untouched, and why Recent does not violate it;
- D-A, D-B, D-C above, with the reasoning, so the next reader does not
  re-litigate them.
