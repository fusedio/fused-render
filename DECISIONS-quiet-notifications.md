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

## §4 built: "Recent" section closes the fold-vs-vanish gap (D-B)

- **`recentJobs`/`recentNotifications` (`platform/lib/jobs.ts`)** are the
  exact complements of `jobRows`/`terminalNotifications`: every job the
  latter excludes *purely because `isRecentOnly` fired* lands in the former,
  and nothing else does — a schedule job (D661) or a terminal
  transient/silent job is excluded from both, rather than leaking into
  Recent the moment its page happens to be open. Pinned with a dedicated
  partition test on a 4-job mixed snapshot: every job lands in exactly one
  of `jobRows`/`recentJobs`, never both, never neither.
- **`RepoUpdatesCardView` gains a third section, "Recent (N)"**, rendered
  below "Needs you"/"Worth keeping" whenever non-empty. Collapsed by
  default with its own local `recentShown` toggle (nothing persisted,
  matching D603 — a fresh session always starts folded). The count lives
  IN the heading/toggle button itself (`Recent ({n})`), not deferred to a
  separate numeral, since the heading is also the section's own disclosure
  control here, unlike "Needs you"/"Worth keeping" which are passive
  labels gated behind the "2+ sections" rule. Capped client-side at
  `RECENT_VISIBLE_CAP = 20`, newest kept (same "arrives oldest-first, slice
  the tail" trick `shownTerminal`'s fold already uses) — "follow
  `MAX_RETAINED`'s shape" per spec, though unlike `MAX_RETAINED` this list
  is not accumulated client-side at all: it is recomputed fresh from the
  live job snapshot on every poll (`recentNotifications`), so the cap only
  ever matters when an unusual number of jobs are suppressed at once.
- **Excluded from `total` and `attentionCount`** — a suppressed success must
  not make the chip's numeral or its "N needs you" label read busier than
  "Needs you"/"Worth keeping" alone would. The `idle` panel-empty sentence
  ("No notifications") is gated on `idle && boundedRecent.length === 0`,
  not `idle` alone, so a panel holding only Recent rows draws the section
  instead of the "nothing here" sentence.
- **No timer anywhere in it** — D663 stands untouched. A row leaves Recent
  only because the underlying job's presence condition stops holding on a
  later read (moves back into `jobRows`'s output instead, nothing
  server-side to reconcile), because its own ✕ dismissed it, or because
  "Clear all" swept it — never a clock.
- **Dismissing a Recent row reuses `JobRow` verbatim**, patched through a
  NEW `onRecentPatch` seam (mirrors `onTerminalPatch` exactly) so its own ✕
  calls the real `dismissJob` and only ever touches the `recent` list —
  `terminal`/`recent` are disjoint by construction, so patching the wrong
  one would silently do nothing.
- **"Clear all" now reaches Recent too**, per spec. A Recent row IS already
  an ordinary terminal job server-side (only its DISPLAY is suppressed), so
  the existing `clearFinishedJobs()` call already dismisses it there with
  zero changes; the addition is calling `onRecentPatch?.(jobsAfterClear)`
  alongside the existing `onTerminalPatch?.(jobsAfterClear)` so the section
  empties the instant the server confirms rather than waiting for the next
  poll. `boundedRecent.length` also joins the button's own "plurality, not
  presence" (D604) threshold — two folded successes are as much a batch
  worth a bulk action as two visible rows are.
- **Wiring**: `ActivityDock.tsx` computes `recentNotifications(next,
  isOpenAnywhere)` off the same per-poll snapshot `terminalNotifications`
  already reads, forwarded through a new `onRecentJobs` callback with its
  own id-set change-detection ref (`recentIdsRef`), exactly mirroring
  `onTerminalJobs`/`terminalIdsRef`. `App.tsx` holds a parallel `recentJobs`
  state, wires it through, and extends the existing `subscribeJobDismissed`
  handler (used by `JobPopupCard.tsx`'s own dismiss path) to patch BOTH
  `terminalJobs` and `recentJobs` — the subscriber has no way to know which
  list a dismissed id is sitting in, so it filters both; whichever one never
  had the id pays a cheap no-op filter.
- **Deferred: "completed non-retained messages" in Recent.** The spec text
  says Recent "holds successful terminal job rows and completed non-retained
  messages." Traced every current call site that sets `source` on a
  `notify()` call (`shell/AppPage.tsx:351`/`:515`, per
  `DECISIONS-actionable-notifications.md`/`DECISIONS-toasts-become-
  notifications.md`) and found both use `tone: "error"` — which always
  resolves to `tier: "attention"` via `resolveTier`, and `isSuppressed`
  explicitly excludes `attention` from ever suppressing. So
  `isSuppressed` cannot fire for any call site that exists in the codebase
  today; there is no live message this session could exercise as
  "suppressed, would have been Recent." Building message-side Recent now
  would mean adding new state-recording logic to `notifications.ts` (which
  today just returns early with no record on suppression) for a code path
  with zero current callers — untestable against a real caller, and a
  second, parallel bounded-list mechanism next to the job-row one this
  commit already built. Left for whichever future call site actually needs
  presence-suppressed, non-error messages (§5's task-finished/scheduled-run-
  started moments are the most likely candidates) to build alongside that
  call site, where it can be pinned against something real rather than a
  hand-rolled mock.
- Tests: 8 new cases in `RepoUpdatesDock.test.tsx` (collapsed-by-default
  with a count in the heading; toggle opens/re-collapses; excluded from
  `total`/`attentionCount`; never turns the chip loud even with a
  would-be-attention job handed in; no spurious `dl-section-head` for a
  lone Recent section; ordering below "Needs you"/"Worth keeping" when both
  present; per-row dismiss patches only `recent`, never `terminal`; "Clear
  all" reaches Recent). Confirmed RED (6 of 8 failing — 2 already passed by
  accident against the untouched component, correctly caught once the
  component existed and were then re-verified as real assertions) before
  implementing, GREEN after (72 pass, 0 fail, up from the file's own
  baseline of 66 pass, 187 expect() calls). `jobs.test.ts` gained
  `recentJobs`/`recentNotifications` coverage in the previous commit (103
  pass, 0 fail for that file).

## What the next builder should do first

1. **§3 grouping** touches `fused_render/jobs.py` (new field, needs a
   Python-side test) and a client grouping layer — read
   `DECISIONS-actionable-notifications.md`'s existing notes on `jobRows`/
   `popupTick` composition order before changing either, since grouping's
   pop rule (D-C) has to compose with the presence suppression AND the new
   §4 Recent split already landed here (a grouped row's members can now be
   split across "Worth keeping" and "Recent" individually — §3's own text:
   "a group row is suppressed under §2b when *every* member satisfies the
   suppression condition" needs to be read against `recentJobs`/`jobRows`
   both, not just `jobRows`).
2. **§5** is the largest remaining piece and the one the parent spec's
   Documentation section most wants written up (D661, `schedule-toast.ts:30`)
   — do not write those decision-log entries until this section's code
   actually ships.

## Baseline (before any change in this branch)

- `bun test src/platform/lib/jobs.test.ts src/platform/lib/notifications.test.ts src/shell/RepoUpdatesDock.test.tsx`
  → 174 pass, 0 fail (recorded before touching any of these files).
- `pytest tests/test_jobs_api.py tests/test_index_jobs.py` → 109 passed.
- No pre-existing red tests found anywhere touched by this branch.

## §3 built so far: server-side `Job.group` (data model only — client not started)

Commit `9fae370eb`. This is the SERVER half of §3 only. Nothing client-side
has been touched yet — the TypeScript `Job` type does not even have a
`group` field. See "What the next builder should do first" below for the
exact resume point.

- `fused_render/jobs.py`: new `Job.group: str = ""` field, defaulted
  CENTRALLY in `upsert()` at creation time via `_default_group(job_id)`
  (`_GROUP_PREFIX_RE = re.compile(r"^(sys:[^:]+):")`): a `sys:<name>:...`
  id groups under `sys:<name>`; anything else (including a bare `sys:name`
  with no second colon) groups under its own full id.
- **Why this makes the single-member regression impossible by
  construction, not by a special case**: a job whose id has no
  `sys:<name>:` sibling gets `group === its own id` — an id nothing else
  on the server ever collides with — so it is a "group of one" and can
  never be client-grouped with anything else, no matter what the client
  grouping logic ends up doing. The spec's own highest-named regression
  risk ("today's single-member behaviour must not change") is closed here,
  on the server, before any client code exists to get it wrong.
- **No `server=True` gate on `group`** (unlike `tier`/`waiting_for`,
  both of which ARE gated): an ordinary page report may set `group`
  explicitly via the request body with no worker token. Reasoning: a
  forged `group` can only misfile a row among other rows on the same
  page — it cannot hide a failure (that's `tier`) or fake a "someone is
  waiting on you" state (`waiting_for`), so it carries none of the
  forgery risk those two guard against. This is a judgement call made
  without explicit spec text beyond "producers may set it explicitly" —
  flagging it here for review.
- The default is applied ONCE, at creation, and never reapplied — a later
  tick that explicitly sends `"group": ""` really clears it (pinned by
  `test_the_default_group_is_set_once_at_creation_not_reapplied_each_tick`),
  matching every other field's "only the keys present are applied"
  contract in this same function.
- `_public()`'s existing `asdict(job)` call serializes `group` onto the
  wire automatically — no separate wire-format change needed.
- Tests: `tests/test_jobs_api.py`, 5 new tests under the "§3 grouping"
  comment block — an ordinary page-owned id groups alone; a `sys:` id
  with no second colon also groups alone; two ids sharing a `sys:` prefix
  share a group; a report may declare `group` explicitly through the
  plain HTTP endpoint (no worker token — proves the no-gate decision
  above); the default is creation-only. The three tests that write a
  `sys:`-prefixed id call `jobs.upsert({...}, server=True)` directly
  rather than going through the HTTP `report()` test helper, because
  `report()` only ever sends `headers={"X-Fused": "1"}` with no worker
  token, and `upsert()` raises `JobError` for any `sys:`-prefixed id when
  `server=False` (`SERVER_ID_PREFIX` guard) — this matches the
  established pattern already used ~20 other places in the same file.
- Verified: grepped `tests/` for exact-dict-equality/closed-shape
  assertions on a job record (`.json() == {...}`, `set(...keys())`) that
  a new field could silently break — none found; the only
  `.json() == {...}` hits in this file are on the unrelated
  `/api/jobs/clear` response shape (`{"cleared": N}`).
- Ran `pytest tests/test_jobs_api.py` (81 passed) and a broader sweep
  (`tests/test_index_jobs.py tests/test_ai_supervisor_job_page.py
  tests/test_supervisor_job.py tests/test_ai_text_job_row.py`, 54 passed
  + 1 pre-existing skip) — no collateral breakage from the new field.

### What the next builder should do first (§3 continued, then §5)

The client side of §3 is **entirely unbuilt**: no `group` field on the
TypeScript `Job` type, no grouping-by-`(page, group)` data function, no
pop-rule change, no UI rendering of a multi-member group row. This was
deliberately left as its own unit rather than rushed, because:

1. The pop rule (D-C) is a NEW mechanism, not a tweak to an existing one:
   today `popupJobs`/`popupTick` pop only on a TERMINAL event
   (`terminalJobs(mergedRows(jobs))`). D-C requires a multi-member group to
   ALSO pop on a 0→some-running START transition, which nothing in
   `jobs.ts` currently tracks (there is no "job just started" event source
   today, only "job just went terminal"). Designing that alongside the
   existing terminal-pop path, without touching how a single-member "group"
   pops (which must stay byte-for-byte identical to today), is exactly the
   kind of change worth a dedicated, unhurried TDD pass rather than a
   last-minute addition.
2. The UI side (one row for a multi-member group: title, "N of M done"
   sub-line, attention stripe on any member's error) means either a new
   component or a meaningful `JobRow` extension, plus wiring through
   `RepoUpdatesDock.tsx`'s three-layer view/wrapper/top-level structure the
   same way §4's `Recent` section was — that's a full slice of UI work on
   top of the popup-mechanism work above.
3. Suppression composition: a group is suppressed under §2b only when
   EVERY member satisfies `isRecentOnly` — this has to be checked against
   BOTH `jobRows` and `recentJobs` (§4), since §4 already shipped and a
   group's members can now legitimately split across "Worth keeping" and
   "Recent" individually before grouping is even applied. Whether grouping
   happens BEFORE or AFTER the jobRows/recentJobs split (i.e., do you
   group first and then decide where the whole group goes, or filter each
   member first and then group what's left) is an open design question the
   next builder needs to resolve against the spec text before writing
   code — get this backwards and a group can silently vanish or double up
   across the two sections.

Concretely, next steps in order: (a) add `group: string` to the `Job`
interface in `platform/lib/jobs.ts`, mirroring the server doc comment; (b)
write the single-member-regression test FIRST (a lone job's popup/panel
behavior with today's exact assertions, now with a `group` field present)
before writing any grouping function at all, so it is red for the right
reason (missing function) and then genuinely proves nothing regressed once
the grouping function exists; (c) design and build the `(page, group)`
grouping data function with that test as the pinned baseline; (d) resolve
the suppression-composition question above with a dedicated test per
outcome; (e) only then touch the popup/pop-rule mechanism and the UI.

§5 (Claude task notifications) has not been started at all — see the
"Pending Tasks" list carried over from the prior session summary for its
four moments and open questions. Do not write the D661 /
`schedule-toast.ts:30` decision-log reversal entries until that code
actually lands.

## §3 client: `group` field, grouping data functions, suppression rewire

Resolved the suppression-composition question the prior handoff explicitly
left open, per the task brief's own instruction not to re-litigate it:
**group first, then classify the group.** `jobRows`/`recentJobs` now index
every job in the snapshot into its `(page, group)` group (`groupJobs` /
`indexGroups` in `frontend/src/platform/lib/jobs.ts`) and, when a job
belongs to a group, ask `isGroupRecentOnly(group.jobs, isOpenAnywhere)`
instead of asking `isRecentOnly` of the job alone. `isGroupRecentOnly` is
"fully terminal AND every member individually satisfies `isRecentOnly`" —
spec's own words. This composes correctly with §4 because both functions
now consult the SAME group-level verdict: a group is never split across
"Worth keeping" and "Recent", and never dropped from both — pinned by two
new tests (`jobRows/recentJobs: a two-member group with one suppressed
success and one failure appears exactly once, in jobRows` and the
all-suppressed counterpart that lands the whole group in Recent).

Added to `platform/lib/jobs.ts`: `JobGroup`, `groupJobs()`, `isGroupTerminal()`,
`isGroupRecentOnly()`, `groupEffectiveTier()`, and a private `indexGroups()`
helper that both `jobRows` and `recentJobs` now share. `groupEffectiveTier`
extends `effectiveTier`'s per-job promotion rule to a group: one
`attention`-effective member promotes the whole group tier; absent that, the
loudest declared tier among the rest wins (`trail` > `transient` > `silent`).
Not yet consumed anywhere (no UI reads it yet — that's the next unit, the
group-row UI in `RepoUpdatesDock.tsx`), but built and tested now so the pop-
rule mechanism (D-C, next) and the UI can both call it rather than
reimplementing the same promotion logic a third time.

**Single-member regression, proven, not just asserted:** the server's own
`Job.group` default (a job with no `sys:<name>:` family prefix defaults
`group` to its own id — already shipped, prior commit) makes an ungrouped
job a group of exactly one BY CONSTRUCTION. `jobs.test.ts`'s `job()` fixture
mirrors that (`group: over.id ?? "j1"`), so every test written before this
unit — none of which set `group` explicitly — already exercises the
single-member path with grouping active, and all pass unchanged. Two new
tests name this guarantee explicitly rather than leaving it implicit:
"two UNRELATED single-member jobs are never folded into each other's group
just because they share a page" (guards against a page-keyed-only grouping
bug) and the general "`jobRows`: the same job still shows..." tests already
in the file, now running through the grouping path.

**Blast radius of adding a required `group: string` field to `Job`:** every
direct `Job` object literal or `job()`/`failedJob()` test helper across the
frontend needed a `group` value (mirrors the exact `tier: "trail"` sweep the
prior builder documented doing during a main-branch merge). Touched, each
given its own id as the default group (a lone job, group of one):
`frontend/src/shell/ActivityDock.test.tsx`,
`frontend/src/platform/ui/NotificationHost.test.tsx`,
`frontend/src/platform/lib/jobs.test.ts`,
`frontend/src/apps/claude/ann/transcribe.test.ts`,
`frontend/src/apps/ai_models/shared/modelSize.test.ts` (all five have a
`job()`/`extra`-spreading helper — `group: over.id ?? "<default-id>"`),
plus four static `Job` literals with no helper —
`frontend/src/apps/ai_models/playground/client.test.ts`,
`frontend/src/platform/ui/DownloadManager.test.tsx`,
`frontend/src/platform/ui/JobPopupCard.test.tsx`,
`frontend/src/platform/ui/JobRow.test.tsx` — and one more helper,
`frontend/src/shell/RepoUpdatesDock.test.tsx`'s `failedJob()`. Verified via
`bunx tsc --noEmit -p .` (clean after the sweep) rather than by grepping for
every literal by hand — a required-field addition surfaces its own blast
radius exhaustively through the compiler, which grep cannot promise.

Deliberately NOT done yet in this unit: the D-C pop-rule mechanism (start-
transition / failure-transition tracking for multi-member groups) and the
group-row UI in `RepoUpdatesDock.tsx`. Both are next, in that order — the
UI needs the pop-rule's `groupEffectiveTier`/`isGroupRecentOnly` primitives
already built here, and the pop rule needs nothing further from this file.

Commands run for this unit: `bunx tsc --noEmit -p .` (frontend/) → clean.
`bun test` across the same 12 files as the prior baseline (jobs,
notifications, RepoUpdatesDock, schedule-toast, DownloadManager,
JobPopupCard, JobRow, NotificationHost, ActivityDock, transcribe,
modelSize, playground/client) → 400 pass, 0 fail (up from the pre-edit
baseline of 218 pass across the 4-file subset — the wider 12-file run here
is a superset chosen because those are exactly the files this unit's `group`
field addition touched). `node scripts/check-boundaries.mjs` → OK (806
files).

## §3 client: D-C pop rule for multi-member groups (start / failure only)

Built the genuinely-new event `popupTick`'s terminal-only path cannot
express — a group going from no running members to some — rather than
bending the existing terminal-event mechanism, per the trap this build was
explicitly told to avoid ("D-C needs a genuinely new 'group just started'
event/tracking mechanism, not a bent version of the terminal-only path").

`groupPopupTick` (`frontend/src/platform/lib/jobs.ts`) is a wholly separate
function from `popupTick`, with its own carried state (`GroupPopupState`:
`runningGroups` — a set of `(page, group)` keys with >=1 running member last
tick, for detecting the 0-to-some START edge; `failedSeen` — `popupKey`-
shaped keys for members already popped failing, so a failure pops once and
stays quiet while it remains failed). It only ever considers groups with
more than one member — `groupJobs(jobs).filter((g) => g.jobs.length > 1)` —
so a lone job never reaches this path at all.

To keep D-C's "never on ordinary or full completion" rule true, `popupJobs`
(which `popupTick` filters through) now excludes every member of a
multi-member group outright — their popping is `groupPopupTick`'s job
entirely, not `popupTick`'s with an extra filter bolted on. A group of one
is unaffected (the exclusion is a no-op for it), which is what keeps
"single-member groups pop on every terminal event exactly like today"
literally true rather than merely approximately true — pinned by
`jobs.test.ts`'s existing `popupJobs`/`popupTick` tests (all running on the
default group-of-one) passing unchanged, plus two new tests naming the
exclusion and the single-member carve-out explicitly.

**Combining the two pop sources — "latest wins" extended across both:**
`ActivityDock.tsx`'s single `onJobsReported` tick now calls both `popupTick`
and `groupPopupTick` off the same snapshot and the same `isFirstTick` flag,
then picks whichever candidate's own "moment" is later — a single job's
`finished_at` for `popupTick`'s pick, a group start's `started_at` or a
group failure's `finished_at` for `groupPopupTick`'s pick (`momentOf = j =>
j.finished_at ?? j.started_at ?? 0`). This was an explicitly-flagged open
design question in the interrupted-work notes ("exact tie-break semantics
when both a start-event and a failure-event are candidates in the same
popup tick") — resolved here rather than left as a TODO, since leaving both
sources un-arbitrated would let one tick pop two cards, breaking "the latest
notification always pops up, not a queue of them" (a guarantee `popupTick`
itself already documents and this build must not regress).

**Group pop events are never presence-gated** — deliberately, and
documented in `groupPopupTick`'s own doc comment rather than left implicit:
a START is never terminal, so `isGroupRecentOnly` (which requires the whole
group to be fully terminal first) is always false for it regardless of
where the user is; a FAILURE is always `effectiveTier === "attention"`,
which `isRecentOnly` itself already never suppresses. There is no
presence check this function could apply that would ever change either
outcome, so none is threaded through — avoids a parameter that would always
be a no-op.

**What was NOT built in this unit:** a representative-Job simplification was
adopted rather than a new multi-job popup type — a group START pops the
specific member that just started running (the newest by `started_at` if
several start in the same tick), and a group FAILURE pops the specific
failing member — both reuse `JobPopupCard`/`JobRow` completely unchanged,
since both already take a single `Job`. This is the scoping decision flagged
as tentative in the interrupted-work notes, now adopted rather than
revisited: building a new multi-job popup card was out of scope for what
D-C actually asks for (pop the CARD, not necessarily a card naming every
member), and no spec text requires the popup itself to enumerate members —
the group ROW in the panel (next unit, not yet built) is where "N of M done"
actually needs to render.

Tests added: `jobs.test.ts` — 15 new tests directly on `groupPopupTick` and
the `popupJobs` exclusion (start-once, start-after-idle, no-pop-on-ordinary-
completion, no-pop-on-full-completion, failure pops, cancelled pops via
`effectiveTier`, failure doesn't re-pop, latest-wins across a start and a
failure in different groups, single-member groups never candidates).
`ActivityDock.test.tsx` — 2 new integration tests proving the real
`onJobsReported` wiring (both `popupTick` and `groupPopupTick` combined)
actually reaches `onJobPopup` for a group START, and stays silent for an
ordinary member completion.

Commands: `bunx tsc --noEmit -p .` → clean. `bun test` across the same
12-file set as the prior unit → 413 pass, 0 fail (up from 400). `node
scripts/check-boundaries.mjs` → OK (806 files).

Still pending: the multi-member group row UI in `RepoUpdatesDock.tsx`
("N of M done" subline, attention stripe) — next unit — then §5 in full.

## §3 client: the multi-member group row UI in `RepoUpdatesDock.tsx`

Completes §3's client-side work. Added `GroupJobRow` (built directly on
`NotificationCard`, mirroring `JobRow`'s own busy/failure/dismiss shape) and
a shared `renderJobRows(jobs, onChanged, onPatch)` helper used by all three
job-row render sites (terminal-attention, terminal-trail/`shownTerminal`,
Recent/`boundedRecent`) — one place turns a flat `Job[]` into rows, grouping
via `groupJobs` first and rendering a `GroupJobRow` for any `(page, group)`
with more than one member, `JobRow` unchanged for a lone member.

**Title**: the group's oldest-arrival member's own title
(`group.jobs[0].title`) — a judgment call, not spec text; `groupJobs`
preserves the snapshot's own oldest-first order, and the oldest member is
least likely to still be mid-rename the way a just-finished sibling
sometimes is.

**Subline**: `"${doneCount} of ${members.length} done"` via
`NotificationCard`'s `secondary` prop, where `doneCount` excludes any
member whose `effectiveTier` reads "attention" (so a cancelled member counts
as not-done, same as a failed one).

**Attention stripe**: `NotificationCard`'s new `className` prop (added this
unit — the one caller so far needing a class beyond what the component
already derives) carries `.dl-row-group-attention` (new rule in
`notifications.css`, `--error` token, left border, mirrors `.dl-row.is-
stalled`'s own placement) whenever any member is attention-effective.

**Section placement is "group first, then classify the group", not
per-job** — the same composition rule already applied for suppression
(`isGroupRecentOnly`) in the prior unit, now applied to the "Needs
you"/"Worth keeping" split too: `terminalAttention`/`terminalTrail` are now
built by grouping `terminal` via `groupJobs`, classifying each WHOLE group
by `groupEffectiveTier`, then flat-mapping the winning groups back into a
`Job[]` (kept as `Job[]`, not `JobGroup[]`, to leave every other consumer of
those two names — `attentionCount`, `hasTrailSection`, `shownTerminal`'s
fold — untouched). The pre-existing code filtered `terminal` by each job's
OWN `effectiveTier`, which would tear a mixed group's members across both
sections — exactly the split D-C's "one failing member keeps the whole
group visible" rule exists to prevent, now proven by a dedicated test
(a two-member group with one failure renders as ONE row, under one count).

**Counts stay raw-job, not group-based** — resolved the open question from
the interrupted-work notes ("whether `total`/`attentionCount`/the fold cap
count raw jobs or rows/groups") in favor of raw job counts, the
lower-risk/least-surprising choice: `attentionCount` for a two-member group
with one failure is 2, not 1 — both members "need a look" in the sense that
neither is filed under "Worth keeping" while the group shows unresolved,
matching what the badge already means for `visibleAttention`/
`messagesAttention` (a count of things, not a count of rows). Pinned by a
test asserting `"2 needs you"` for exactly that fixture.

**Blast-radius fix in the test fixtures, found by running the tests, not
suspected up front**: `RepoUpdatesDock.test.tsx`'s `failedJob()`/`doneJob()`
helpers defaulted `group: over.id ?? "sys:ai-image:boom"` — correct for
`failedJob()` alone, but `doneJob()` spread `failedJob(over)` using the
OUTER `over` (which lacks an `id` override in most tests), so a bare
`doneJob()` silently inherited `failedJob`'s own hardcoded default group.
Two unrelated, differently-`id`'d bare fixtures (`failedJob()`, `doneJob()`)
were accidentally landing in the SAME `(page, group)` group the moment
grouping-aware code actually looked at `group` for classification —
surfaced immediately by the pre-existing "counts a waiting task and an
attention-tier job together" test flipping from `"2 needs you"` to
`"3 needs you"`. Fixed by threading each fixture's own resolved `id` into
its own `group` default (`doneJob` now explicitly passes
`id: over.id ?? "sys:ai-image:done"` into the `failedJob` call it builds
on), so the two helpers' defaults are independent again.

Tests added (`RepoUpdatesDock.test.tsx`, 4 new): a two-member group renders
one row with the right title and "2 of 2 done"; a two-member group with one
failure gets the attention-stripe class, stays one row, and counts 2 toward
the badge; a single-member group renders exactly as `JobRow` always has (no
subline, no stripe) — the single-member regression trap named by number in
the task brief; dismissing a group's row calls dismiss on every member and
removes all of them from state on success.

Commands: `bunx tsc --noEmit -p .` → clean. `bun test` across the same
12-file baseline set as the prior two units → 411 pass, 0 fail.
`bun test src/platform/ui/NotificationCard.test.tsx` → 15 pass (the new
`className` prop, regression-checked directly). `node
scripts/check-boundaries.mjs` → OK (806 files).

This completes §3's client-side work in full. Next: §5 (Claude task
notifications), not yet started.

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

## §5 built: scheduled-run events, task-status-poll diffing, both narrator-gated

**Built, tested, committed, pushed** (commits `4d8fc3eed`, `7ce6f588b`,
`e677f6463`):

- **`fused_render/schedule.py`**: new `EVENT_STARTED = "started"`, emitted
  in `_send()` right after a spawn is confirmed to have actually taken —
  never on a failed spawn (claim died, `spawn_helper` error, or an
  exception), whichever of the three paths caused it. Every Python test
  whose exact-event-kind-list assertion this affects was updated across
  `tests/test_schedule_reporting.py`, `tests/test_task_sync_matrix.py`, and
  `tests/test_schedule_recurring.py` — see those files' own updated
  comments for which code paths do and do not reach the new emission.
- **`schedule-toast.ts`/`scheduleEvents.ts`**: `toastForEvent` no longer
  returns `null` for `done`; `started` gets the same suppressible,
  non-retained `tone: "info"` shape. `useScheduleEvents` gates its poll on
  `isNarrator()`, re-checked every tick (not just at mount), and only acks
  a batch of events AFTER narrating it — the mechanism that makes an
  unattended run's notification unlosable (server-held-until-acked; a
  narrator that dies mid-narration sees the event again next time any
  window polls). Pinned directly in `scheduleEvents.test.ts`.
- **`task-status-notify.ts`/`useTaskStatusNotify.ts`** (new files): the
  "interactive turns and needs-input" half of §5's Sources bullet — no new
  server channel, derived entirely from `tasksPulse.ts`'s existing pulse
  poll. Diffs each task's `taskColumn()` across polls in the narrator
  window and calls `notify()` on `in_progress->done` (suppressible info),
  `in_progress->blocked` (never-suppressed, retained, actioned failure),
  and `*->needs_attention` (a plain, non-retained popup only — see below).
  Wired into `App.tsx` alongside `useScheduleEvents`.

**Reconciling `needs_attention` with the pre-existing `attentionRows`
mechanism** (`tasks-lib.ts`, built 2026-09-03 in direct response to
Akshil's own "there should be notifications with blocked tasks as well,"
wired into `RepoUpdatesDock.tsx` well before this branch's §5 work
started): that mechanism ALREADY gives `needs_attention` a dedicated,
always-current, dismissible Notifications-panel row — "retained: yes,
attention" from the spec's table is therefore already true today, with no
§5 code at all. What it does NOT do is announce the MOMENT a task parks —
it is a passive, continuously-recomputed display, never a `notify()` call,
so a task that parks while nobody is on the Tasks page or has the panel
open produces no popup, only a row waiting to be found.

`useTaskStatusNotify.ts` closes exactly that gap and no more: on
`*->needs_attention` it calls `notify()` with no tone/tier at all, which
resolves to `"transient"` — pops, does not retain (`notifications.ts`'s
`resolveTier`/`isRetained`). Using `notify()`'s ordinary `tone: "error"`
shape instead would have always-retained a SECOND, independently
dismissible row for the exact same task, duplicating `attentionRows`
rather than complementing it. This was a judgment call, not something
either `DECISIONS-quiet-notifications.md` or `schedule.py`'s own header
comment (the Akshil 2026-09-03 "no event for a run parked on a card"
note, about a DIFFERENT, narrower mechanism — the schedule event log
specifically, not this task-status-poll diff) settles directly; recorded
here so the next reader does not "fix" the missing tone/page as an
oversight and reintroduce the duplicate row.

**What `schedule.py`'s own "no event for a parked run" comment is, and is
not, about**: that decision is scoped to the SCHEDULE EVENT LOG
(`_emit`/`event_log`, `started`/`done`/`failed`/`missed`) — a toast
specifically for a *scheduled send* that parks on a card was tried on this
branch and reverted, because the Tasks page/panel row already covered it.
It says nothing about, and does not preclude, the separate task-status-poll
mechanism this section describes, which covers EVERY task (scheduled or
manually run) and is the spec's own named "interactive turns and
needs-input" bullet — a distinct code path (`/api/tasks` status, not the
schedule event log) that this branch's spec text explicitly requires.

**Documentation**: the D661 partial-reversal entry is in
`DECISIONS-actionable-notifications.md`; the `schedule-toast.ts:30`
reversal entry is in `DECISIONS-toasts-become-notifications.md`; both
specs' Constraints/Out-of-Scope sections were updated to point at those
entries rather than left contradicting this branch.

**Test state**: `bun test frontend/src/platform/lib/scheduleEvents.test.ts
frontend/src/platform/lib/schedule-toast.test.ts
frontend/src/shell/task-status-notify.test.ts
frontend/src/shell/useTaskStatusNotify.test.ts
frontend/src/shell/tasksPulse.test.ts frontend/src/shell/tasksPulse.lane.test.tsx
frontend/src/shell/tasks-lib.test.ts frontend/src/shell/RepoUpdatesDock.test.tsx
frontend/src/shell/sidebar-tasks.test.ts` → 725 pass, 0 fail. `bunx tsc
--noEmit -p frontend` clean. `node frontend/scripts/check-boundaries.mjs` →
OK (811 files). Python: the three schedule test files listed above, plus
the wider schedule suite (441 tests) run during this build, all passing.
Full suite (frontend `apps/`, backend beyond the files touched here) not
run — scoped-tests-only per the build instructions; that is the
orchestrator's job at the end.

## Code review fixes (2026-09-16)

A round of code review against the branch above found ten defects, all
fixed on top of the green baseline (full frontend suite 6188 pass, Python
failure set byte-identical to `main`, `tsc`/`check-boundaries.mjs` clean).
Each fix has its own test that fails before and passes after (verified via
a `git stash`/patch-revert-and-reapply round for every one, not just a
read of the diff). Recorded here, in review order, so none of the
reasoning below gets "simplified" back by a future reader who only sees
the final code.

**1. `presence.ts` — narrator election let an embed win but never
narrate.** The old `topLevel` computation was
`IS_TOP_EMBED || window === window.top` — a no-op, since `IS_TOP_EMBED`
(embed AND top-level) already implies `window === window.top`. Any
top-level window, embed or not, registered `topLevel: true`, so an embed
opened standalone in its own tab could win the narrator election even
though nothing ever narrates from an embed (`useScheduleEvents` bails out
on `IS_EMBED` before calling `isNarrator()`; `useTaskStatusNotify` only
mounts inside the shell's `App`). Extracted `computeTopLevel(isEmbed, win)`
as a pure, exported function — `!isEmbed && (win undefined or win===top)`
— so `isEmbed` is excluded outright rather than folded into an `||` that
never mattered. Pulled out as its own function (not inlined in
`writeSelf`) specifically because `IS_EMBED` is a module-load-time
constant computed once from `location.pathname` and cannot be varied
per-test-file within one `bun test` process (`testDomShim.ts`'s own
header). Pinned by 5 new cases in `presence.test.ts`.

**2. `jobs.ts` `popupTick` — a presence-suppressed popup's key never
entered `seen`.** The old code called `popupJobs(jobs, isOpenAnywhere)`
directly, so a suppressed job never even reached the loop that builds
`seen`. The instant the user navigated away and `isOpenAnywhere` flipped
false for that page, the same already-finished job looked brand new (its
key still absent from `seen`) and popped a stale card. Fixed by gathering
candidates via the UNFILTERED `popupJobs(jobs)` so every candidate's key
lands in `next`/`seen` regardless of suppression, and applying the
presence check only as a per-candidate gate on the POP decision itself.
Pinned by 2 new tests in `jobs.test.ts`.

**3. `fused_render/jobs.py` `_default_group` — group key meant "one
family of work," not "one burst."** This was a spec error the orchestrator
made themselves, not a code defect — fixed client-only per an explicit,
pre-made decision handed down before this round of fixes started: a new
`GROUP_GAP_MS` constant (2 minutes) and `clusterFamily()` helper in
`jobs.ts` split each `(page, group)` family into separate clusters
whenever a job's `started_at` is more than `GROUP_GAP_MS` after the
cluster's latest activity. `groupKeyOf(g)` now reads the cluster-scoped
`g.key`, not the bare family, so two different bursts of the same family
(e.g. two unrelated sessions of "render 20 tiles") never share one tracked
popup/fold identity. Server-side grouping (`fused_render/jobs.py`) is
untouched — the burst distinction is purely a client-side clustering
layer over the same stored `group` field.

**4. `RepoUpdatesDock.tsx` `TERMINAL_VISIBLE_CAP` — capped raw jobs, not
groups.** `renderJobRows()` re-derives `groupJobs()` from whatever job list
it's handed. Slicing the flat, job-level `terminalTrail` at the cap could
land the cut in the middle of a group's contiguous member run, so the
re-derived `groupJobs()` call on that slice produced a wrong PARTIAL group
(fewer members shown than actually exist, with no way to tell from the
row itself). Fixed by capping `terminalTrailGroups` (the row/group list)
instead and flat-mapping the kept groups' jobs back out — a group either
shows complete or not at all. Pinned by a new test asserting a 6-member
group straddling the cap boundary renders as one complete "6 of 6 done"
row, never a partial one.

**5. `fused_render/jobs.py` `_default_group` — a blank posted `group` was
stored literally.** `group` is a clustering key, not a display value,
but `upsert()`'s `"group" in body` handling stored an explicitly-blank
`""` verbatim instead of falling back to the id-derived default —
`job.group = group` regardless of whether `group` was truthy. Two
unrelated jobs that both posted `group: ""` would collide into one
nonsense group. This DELIBERATELY REVERSES a decision recorded both
inline in `jobs.py` and earlier in this file's own §3 section (the
"default is applied once, at creation, and never reapplied — a later tick
that explicitly sends `group: ""` really clears it" rule, pinned by
`test_the_default_group_is_set_once_at_creation_not_reapplied_each_tick`):
that rule matched every other field's "only the keys present are applied"
contract, but treating `group` as just another such field is exactly the
bug this finding names — `group` is not display data a producer might
legitimately want to blank out, it is the identity two otherwise-unrelated
jobs are clustered by, and a blank one is never a meaningful clustering
key. Fixed at both the `upsert()` body-group-assignment path AND a
previously-overlooked second one: the synthetic stand-in `Job` built for a
late tick on an already-dismissed id, which constructed its `group` as the
implicit `""` default with no fallback at all. The pinning test's
docstring and assertions were updated to describe the deliberate reversal
(both `res["group"]` and `res2["group"]` now assert the id-derived
default, not `""`), and two new regression tests were added:
`test_an_explicit_blank_group_never_lets_two_unrelated_jobs_collide` and
`test_a_late_tick_on_a_dismissed_job_never_returns_a_blank_group`.

**6. `ActivityDock.tsx` `isOpenAnywhere` — O(N)-ish synchronous storage
reads every poll tick.** `isOpenAnywhere` does a `localStorage.getItem` +
`JSON.parse` on every single call; one poll tick called it once per
terminal job, once per recent job, once per popup candidate, and once per
member of every multi-member group, all against the identical underlying
snapshot. Added `snapshotIsOpenAnywhere(env)` to `presence.ts`, which
reads and prunes the registry exactly ONCE and returns a same-shaped
`(source: string) => boolean` predicate closed over that one snapshot.
`ActivityDock.tsx`'s `onJobsReported` now calls it once at the top of each
tick and passes the resulting predicate to every call site that used to
take the raw `isOpenAnywhere` import. Pinned by a new test in
`presence.test.ts` using a storage spy that counts `getItem` calls: one
read for four `predicate()` calls, versus four reads for four raw
`isOpenAnywhere()` calls on the same inputs.

**7. `RepoUpdatesDock.tsx` `GroupJobRow` — a folded group row had no
`rowClick`.** Unlike `JobRow`, a folded multi-member group's row was not
clickable at all. Gave it a `rowClick` that opens the oldest member's page
(`navigateToJobPage(members[0]?.page)`) — the same "oldest member
represents the row" convention its `title` already used. Deliberately does
NOT reuse `JobRow`'s "opening dismisses" convention: dismissing on a
look-only click into a multi-member group would destroy every sibling
member's own state for a click that was never a request to clear the
group. Pinned by a new test asserting the folded row opens
`/ai-models/local` (a `JOB_PAGE_ROUTES` member) via `history.pushState`.

**8. `jobs.ts` `popupJobs`/`groupPopupTick` — a group shrinking to one
member re-popped its own already-popped failure.** A multi-member group's
members are excluded from `popupJobs` entirely (their popping is
`groupPopupTick`'s job). If a sibling was later dismissed/swept and the
group shrank to one member, the survivor became a `popupJobs` candidate
for the FIRST time — its key had never touched `popupTick`'s own `seen`
set — and looked brand new, popping the same failure `groupPopupTick`
already showed a card for. Fixed two ways: `groupPopupTick`'s
`failedSeen` now carries a key forward even after its group drops below
two members (never creates a NEW entry for a genuine singleton's own first
failure, only preserves an existing one), and `popupTick` takes an
optional `alreadyPoppedByGroup` set — the prior tick's `failedSeen` —
seeding any matching key into its own `seen` silently instead of popping
it. `ActivityDock.tsx` wires `groupPopupStateRef.current.failedSeen`
through on every tick. Pinned by 2 new tests simulating the exact
shrink-to-one transition for both `groupPopupTick` and `popupTick`.

**9. `useTaskStatusNotify.ts` — the narrator check short-circuited before
the `previous` map was seeded.** The effect returned before touching
`previous` at all whenever `!isNarrator()`, so a non-narrator window's
per-task column bookkeeping never advanced while it wasn't the narrator.
The instant that window became the narrator (the old one's tab closed),
its first tick as narrator could find `previous` stale or empty and
misread a genuine transition landing on that very tick as a first
sighting — silently dropping its notification instead of raising it. The
book-keeping (`prev.set` + pruning) now runs on every tick regardless of
narrator status; only the `notify()` call itself stays gated on
`isNarrator()`. Pinned by a new test: a task transitions to `blocked`
while non-narrator (silently tracked, no popup), the foreign narrator's
entry is removed (this window is elected), and the very next transition
(`blocked -> needs_attention`) still notifies correctly.

**10. `presence.ts` `writeSelf`/`removeSelf` — unsynchronized
read-modify-write let a closed window's entry get resurrected.** Both
functions read the whole registry, changed only their own entry, and
wrote the whole map back, with nothing guarding against a different
document doing the same to a different entry in between. Concretely:
window A's `pagehide` reads `{A, B}`, deletes its own entry, and is about
to write `{B}` back; if window B's own heartbeat reads `{A, B}` before A's
write lands and then writes `{A(stale), B(refreshed)}` back after A's
write lands, A's already-closed entry is resurrected and stays "open"
until it eventually ages out via `PRESENCE_STALE_MS`. Plain `localStorage`
has no compare-and-swap, so this can only be narrowed, not eliminated
outright: added `mutateRegistry`, which reads the raw string once,
computes the mutated map, then re-reads the raw string immediately before
committing — a mismatch means someone else wrote in between, and the
whole mutation is retried against that fresher snapshot rather than
overwritten (up to `MAX_MUTATE_ATTEMPTS`, falling back to a last-write-wins
commit only if every retry keeps racing). `writeSelf`/`removeSelf` are now
thin transforms passed through it. Pinned by 3 new tests in
`presence.test.ts` using a storage stub whose `getItem` returns a
different snapshot on the verification read than on the initial read,
simulating the exact interleaving above without needing real concurrency.

**11. `jobs.ts` `groupPopupTick`/`clusterFamily` — a group's START tracking
was keyed on cluster IDENTITY, which is partly positional.** `clusterFamily`
numbers a family's burst clusters 0, 1, 2... purely by their order in the
CURRENT snapshot (finding 3's own doc comment already names this: "computed
off a copy sorted by `started_at`"). `groupJobs` baked that ordinal into
`JobGroup.key` (`${page} ${group}#${clusterIdx}`), and `GroupPopupState`
(then `runningGroups: Set<groupKey>`) tracked "has this group had a running
member" by that same key. Two ways the ordinal moves under a still-running
cluster without any of ITS members changing: an older, fully-terminal
cluster in the same family leaves the snapshot ("Clear all", a dismissal,
a sweep) and every later cluster's index shifts down by one; or a late
report arrives whose `started_at` predates everything seen so far by more
than `GROUP_GAP_MS` and splits what was one cluster into two, again
shifting every later ordinal. Either way, a group already running (already
announced) gets a NEW key, `runningGroups.has(newKey)` reads false, and
`groupPopupTick` pops a duplicate "started" card for work the user was
already told about — while the OLD key vanishing also remounts
`GroupJobRow` in `RepoUpdatesDock.tsx` (same key used as the React list
key), discarding whatever local state it held mid-interaction.

THE FIX, in two parts, per the orchestrator's ruling — only the first one is
load-bearing:

1. **Correctness.** `GroupPopupState.runningGroups` (a `Set<groupKey>`) is
   replaced with `runningMemberIds` (a `Set<jobId>`) — the ids of every
   member, across all multi-member groups, that was RUNNING as of the last
   tick. A group now pops START when it has running members and NONE of its
   CURRENTLY running members' ids was in that set last tick — the same
   "0 running members to some" edge, expressed in terms that never touch
   group-key identity at all. A job's id never changes, so no amount of
   cluster renumbering can turn an already-seen running member into an
   apparently-new one. This is WHY the tracking moved from group keys to job
   ids — not a stylistic preference, a structural fix: as long as ANY
   representation of "have I seen this group running" is derived from a
   value that can change without the underlying membership changing, the
   same class of bug reappears under a different key shape. Job ids are the
   one identity in this system that is guaranteed stable across a
   membership-preserving snapshot change. Do not "simplify" this back to a
   set of group/cluster keys — that is exactly what this fix undoes.
   `runningMemberIds` is rebuilt fresh every tick from the current
   snapshot's running members, the same "recomputed, not accumulated" shape
   the old `nextRunning` already had — ids of jobs that have left the
   snapshot are not carried forward (unlike `failedSeen`, which finding 8
   requires to persist past a group's shrink; that field and its carry-
   forward rule are unchanged).
2. **Hygiene (defence-in-depth, not the correctness fix).** `clusterFamily`
   now keys each cluster on its own earliest member's id instead of a
   positional index, so `JobGroup.key` (and the React list key derived from
   it in `RepoUpdatesDock.tsx`'s `renderJobRows`) only moves when that
   cluster's own earliest member actually changes, not when an unrelated
   older cluster leaves the snapshot. This closes the `GroupJobRow` remount
   problem, but it must not be relied on to close the START-popup bug —
   `groupPopupTick` doesn't use `JobGroup.key` for its running-state tracking
   at all, by design, specifically so a future key-shape change here (however
   well-intentioned) can never resurrect finding 11.

`failedSeen` (finding 8) was already keyed by `popupKey(member)` — a job id
plus its `finished_at` — so it was never affected by this bug; left
unchanged. Pinned by 2 new tests in `jobs.test.ts`: an older, fully-terminal
cluster removed from the snapshot between ticks does not re-pop the
surviving running cluster, and a late-arriving report that splits an
earlier cluster does not re-pop its already-seen running members.

**Test state (code review fixes)**: `bun test
frontend/src/platform/lib/presence.test.ts
frontend/src/platform/lib/jobs.test.ts
frontend/src/shell/RepoUpdatesDock.test.tsx
frontend/src/shell/useTaskStatusNotify.test.ts
frontend/src/shell/ActivityDock.test.tsx
frontend/src/platform/lib/scheduleEvents.test.ts` → all pass, 0 fail.
`.venv/bin/python -m pytest tests/test_jobs_api.py tests/test_index_jobs.py
-q` → all pass. `bunx tsc --noEmit -p frontend` clean.
`node frontend/scripts/check-boundaries.mjs` → OK (811 files). Each of the
eleven fixes above landed as its own commit
(`5f9580d62`, `72ac1b54d`, `c277eb00e`, `ea034e4bc`, `4bf76d98b`,
`69f294e05`, `f4dc38ff8`, `41816f083`, `bd74ca789`, plus finding 11's own
commit), pushed immediately.

## Fix 12: `groupPopupTick`'s finding-11 predicate regressed serialized handoffs

Finding 11's fix moved START tracking from group keys to running member ids
(the right call — see finding 11's own doc, kept unchanged) but landed the
wrong predicate for deciding whether a group is "already in flight":

```
const allUnseen = runningMembers.every((m) => !state.runningMemberIds.has(m.id));
if (!isFirstTick && allUnseen) { ...pop START... }
```

`runningMembers` here is only the members running THIS tick. A serialized
burst — a1 finishes, then a2 starts, then a3 starts, all within
`GROUP_GAP_MS` — has a DIFFERENT, newly-running member on every tick, so
`allUnseen` reads true on every single handoff (the one member that's
running right now was never running before, because it just started) and
`groupPopupTick` popped a duplicate START card per handoff — exactly the
pile-up D-C exists to prevent, and a straightforward regression of finding
11's own intent.

Fixed by asking about the whole group, not just its currently-running
subset:

```
const wasRunning = g.jobs.some((m) => state.runningMemberIds.has(m.id));
if (!isFirstTick && !wasRunning) { ...pop START... }
```

**Why this iterates `g.jobs` (every member) and not `runningMembers` (only
the currently-running ones) — do not "simplify" this back:** the question
`groupPopupTick` needs answered is "was this GROUP already in flight last
tick", not "was this SPECIFIC currently-running member already running
last tick". Those two questions coincide for a group whose members overlap
in time, but diverge exactly at a handoff — the moment one member finishes
and a different member starts in the same or a later tick. `g.jobs` still
includes the just-finished member (now `done`, not running, but still a
member of the group this tick), so checking whether ANY of them —
including the one that just went terminal — was in `runningMemberIds`
correctly recognizes "this group has had a running member continuously,
this is a handoff, not a fresh start." Restricting the check to
`runningMembers` throws away exactly the information (the group's PAST
running members, now terminal) that makes the handoff case distinguishable
from a genuine 0-to-some edge. This is still id-keyed per finding 11's own
fix (a job id never changes, so cluster-ordinal churn still cannot forge a
"new" member) — finding 12 only corrects which members' ids get checked
against that set, not the id-keying itself.

Added a regression test (`groupPopupTick: a serialized handoff (members
running one at a time within GROUP_GAP_MS) pops START exactly once`,
`jobs.test.ts`) simulating exactly this: a1 running alone pops the group's
one legitimate START; a1 finishes and a2 takes over — no pop; a2 finishes
and a brand-new a3 takes over — still no pop. Confirmed it fails under the
old `allUnseen` predicate (pops a spurious second START at the a1→a2
handoff) and passes under the fix, by reverting to the old predicate,
re-running, and restoring the fix. The three pre-existing finding-11 tests
("removing an older, fully-terminal cluster...", "a late-arriving report
that splits an earlier cluster...") and the ordinary-completion test all
still pass unchanged.

Commands: `bun test src/platform/lib/jobs.test.ts
src/shell/RepoUpdatesDock.test.tsx src/shell/ActivityDock.test.tsx` → 229
pass, 0 fail. `bunx tsc --noEmit -p frontend` clean. `node
frontend/scripts/check-boundaries.mjs` → OK (811 files).

## Fix 13: panel section counts now count rows, not raw jobs

User decision, verbatim: "yes we should count rows." Previously `total`,
`attentionCount`, and the "Recent (N)" heading in `RepoUpdatesDock.tsx` all
summed raw job counts, so eight downloads folded into one `GroupJobRow`
still read as "8" on the chip and in "N needs you" — a count that
disagreed with what was actually on screen (one row).

Fixed by deriving each count from the SAME row-level collection its
section already renders, rather than a parallel count that could drift:

- `total` now adds `terminalGroups.length` (the exact `groupJobs(terminal)`
  call the attention/trail split already computes) instead of
  `terminal.length`.
- `attentionCount` now adds `terminalAttentionGroups.length` (the exact
  collection "Needs you" maps a row per entry of) instead of
  `terminalAttention.length`.
- The "Recent (N)" heading now reads a new `boundedRecentGroups =
  groupJobs(boundedRecent)`'s `.length` instead of `boundedRecent.length` —
  `boundedRecent` is the exact array `renderJobRows` groups and renders
  below, so this reads the same collection the section shows rather than
  introducing a second, independent count.

This deliberately REVERSES the "counts stay raw-job, not group-based"
call recorded earlier in this file (§3 client: the multi-member group row
UI unit) — that entry's own reasoning is now superseded by the user's
explicit decision above; do not read that earlier entry as still current.

Not changed: the "Clear all" button's plurality threshold (`visible.length
+ terminal.length + messages.length + boundedRecent.length > 1`) — this is
a boolean gate deciding whether a bulk-action button exists at all, not a
displayed count a reader compares against what's on screen, and the task
that ordered this fix named `total`, the attention count, and the Recent
heading specifically. Left alone rather than guessed into scope.

Updated `RepoUpdatesDock.test.tsx`'s existing "a two-member group with one
failing member..." test: its badge assertion changes from `"2 needs you"`
(the old raw-job count) to `"1 needs you"` (one row). Added two new tests:
a two-member group counts as ONE toward the chip's total numeral, and a
two-member group in Recent reads `"Recent (1)"`, not `"Recent (2)"`.

Commands: `bun test src/platform/lib/jobs.test.ts
src/shell/RepoUpdatesDock.test.tsx src/shell/ActivityDock.test.tsx` → 229
pass, 0 fail (same run as Fix 12's — both fixes verified together).
`bunx tsc --noEmit -p frontend` clean. `node
frontend/scripts/check-boundaries.mjs` → OK (811 files). Grepped `tests/`
for the touched symbols (`groupPopupTick`, `attentionCount`,
`boundedRecent.length`, `RepoUpdatesDock`) — the only Python hits
(`test_activity_bar_structure.py`, `test_jobs_api.py`) reference the
filename or an unrelated localStorage/fold check, not the specific lines
changed here; `test_activity_bar_structure.py` re-run directly to confirm
(4 passed).

## Fix 14: Bug 1 (client) — `Job.source`, `isRecentOnly`/`familyKey` read it, and the Playground actually sends it

Live report: on the AI Models Playground, generating an image while the
Playground itself was open still popped a success notification (should
have suppressed), and two sequential generations popped two separate
notifications (should have grouped into one). This entry covers the
CLIENT half of bug 1 — the server half (`Job.source`, `X-Fused-Source`
header, `fused_render/jobs.py`/`ai_runtime.py`/`ai/supervisor.py`) is a
separate backend commit (`1dc9d0164`).

**Root cause, restated for the client:** `page` on a `Job` row is
overloaded — for a server-backed render it is deliberately left to fall
back to the OUTPUT IMAGE PATH once no caller page survives
(`_start_render`'s `page or done_page or out_dir or ""`), so a click still
opens the file. `isRecentOnly` (`frontend/src/platform/lib/jobs.ts`) read
`job.page` for its presence check, comparing the open shell route against
an absolute `.png` path — which can never match, so a server-backed
render could never be suppressed no matter where the user was.

**Fix:** added `source: string` to the `Job` interface (mirrors the
server's own `Job.source` exactly, including its own doc comment about
never inheriting `page`'s fallback), and changed `isRecentOnly` to read
`job.source` instead of `job.page`. Tests (`jobs.test.ts`): a render whose
`page` is an output path but whose `source` is the raising route IS
suppressed when that route is open; the same job with an empty `source`
is NOT suppressed (empty degrades to notify, never silence).

**Deviation from the brief, found while fixing the above (not in the
original brief):** `familyKey` (the function `groupJobs`/`groupPopupTick`
use to cluster jobs into one popup) was keyed on `job.page` alone. For a
REAL render this is a per-job output path, so two Playground image
generations sharing the same `group` value would never share a
`familyKey` in production — they would never cluster into one group at
all, regardless of any fix to the popup-tick mechanism itself. Changed
`familyKey` to `${job.source || job.page} ${job.group}` — falls back to
`page` only when `source` is empty (an ordinary, non-render producer,
where the two already carry the same value), so no existing single-member
or non-render grouping test's behavior changes. Flagged here explicitly
because it is scope beyond bug 1/bug 2 as originally described, but the
live symptom ("2 popups for 2 image gens") cannot be fixed without it —
`groupPopupTick`'s own fix (Fix 12, and the new lastStartPopAt gate below)
only matters once two renders actually land in the same group.

**Blast radius of the required `source: string` field** — every direct
`Job` object literal or `job()`/`failedJob()` helper across the frontend
needed a `source` value, found via `bunx tsc --noEmit -p .` (10 errors
across 9 files, same technique as the `group` field's own rollout — see
above). Fixed, each defaulting to `""` or (in `jobs.test.ts`'s own
`job()` helper) to whatever `page` resolved to, matching the server's own
default and keeping every pre-existing fixture passing unchanged:
`frontend/src/platform/lib/jobs.test.ts`,
`frontend/src/apps/ai_models/playground/client.test.ts`,
`frontend/src/apps/claude/ann/transcribe.test.ts`,
`frontend/src/apps/ai_models/shared/modelSize.test.ts`,
`frontend/src/platform/ui/DownloadManager.test.tsx`,
`frontend/src/platform/ui/JobPopupCard.test.tsx`,
`frontend/src/platform/ui/JobRow.test.tsx`,
`frontend/src/platform/ui/NotificationHost.test.tsx`,
`frontend/src/shell/ActivityDock.test.tsx`,
`frontend/src/shell/RepoUpdatesDock.test.tsx`. Checked
`RepoUpdatesDock.test.tsx`'s `notify()`/`message()` calls (lines
~1279/1325) before touching anything near them — confirmed they build a
separate notifications type, not `Job`, and left untouched.

**Verified the producer actually sends the header** (the brief's own
explicit instruction, not assumed): `frontend/src/apps/ai_models/playground/client.ts`'s
`startImage`/`startVideo` sent NO attribution headers at all before this
fix — confirmed by reading `postJson`'s call sites directly. Fixed by
adding a `sourceHeaders()` helper (`X-Fused-Source: currentPresencePage()`)
passed to both. Deliberately does NOT also send `X-Fused-Page`: sending it
would make `page`'s existing output-path fallback lose to the caller's
route permanently (the router reads `X-Fused-Page` once at job creation
and threads it through every server-side tick), breaking "click opens the
file" for every Playground render. TDD: wrote
`startImage sends X-Fused-Source...`/`startVideo sends X-Fused-Source...`
in `client.test.ts` first (mocking `fetch` to capture the request), 2
failing (`X-Fused-Source` undefined), then implemented, both green, and
both assert `X-Fused-Page` is absent.

Commands: `bun --cwd frontend test src/platform/lib/jobs.test.ts
src/platform/lib/presence.test.ts src/shell/RepoUpdatesDock.test.tsx
src/shell/ActivityDock.test.tsx src/apps/ai_models/playground/client.test.ts
src/apps/ai_models/shared/modelSize.test.ts src/apps/claude/ann/transcribe.test.ts
src/platform/ui/DownloadManager.test.tsx src/platform/ui/JobPopupCard.test.tsx
src/platform/ui/JobRow.test.tsx src/platform/ui/NotificationHost.test.tsx` →
422 pass, 0 fail. `bun --cwd frontend test src/apps/ai_models` (full app
sweep, collateral check on the client.ts change) → 630 pass, 0 fail.
`bunx tsc --noEmit -p frontend` clean. `node
frontend/scripts/check-boundaries.mjs` → OK (811 files). Backend:
`.venv/bin/python -m pytest tests/test_jobs_api.py
tests/test_ai_supervisor_job_page.py tests/test_ai_runtime.py` → 667
passed. Grepped `tests/` for `familyKey`/`lastStartPopAt`/`X-Fused-Source`/
`job.source` — only hits are the new tests added in this same pass
(`test_jobs_api.py`, `test_ai_runtime.py`), no stale line-literal
assertions on the touched frontend source.

## Fix 15: Bug 2 — a finished group re-popped when a new member started (lastStartPopAt gate)

Live report, same symptom as Fix 14 above: "similar tasks for same page
should be grouped. I got 2 notification popups for 2 image gen." Fix 12
(`wasRunning` checked by member id across the whole group, not just the
currently-running subset) already closes the case where the group's OWN
member list never shrinks below 2 — see that entry. It does not close a
second, narrower gap: `GroupPopupState.runningMemberIds` is rebuilt each
tick from ONLY the members running THAT tick
(`for (const m of runningMembers) nextRunningMemberIds.add(m.id)`, inside
the `if (runningMembers.length > 0)` branch). A tick where the group has
ZERO running members — one member already finished, the next merely
queued/not yet started, still a multi-member group — adds nothing to that
set, so a genuinely continuous burst that happens to pass through one
observed all-idle tick has its "was running" memory erased. The next
member starting then reads as a fresh 0-to-some edge and pops a spurious
second START.

Fix: `GroupPopupState.lastStartPopAt`, a `familyKey(job) -> started_at of
that family's last START pop` map, rebuilt fresh each tick from live
multi-member groups only (same pattern as `runningMemberIds`) but,
critically, explicitly CARRIED FORWARD through an all-idle tick (the
`else if (priorPop !== undefined) nextLastStartPopAt.set(fam, priorPop)`
branches, both when a group currently has 0 running members and when a
family isn't in `groups` in `runningMembers.length > 0`'s own miss case).
A START now pops only when BOTH `wasRunning` is false AND the family's
last pop (if any) is more than `GROUP_GAP_MS` in the past
(`recentlyPopped`). Keyed on `familyKey`, not the cluster-scoped `g.key`,
per Fix 12/finding 11's own rule that cluster identity is partly
positional and must never key persisted state.

Why a genuinely new burst still pops: `clusterFamily` splits a family into
separate clusters whenever a member's `started_at` is more than
`GROUP_GAP_MS` past the cluster's own latest activity — so two members
that land in DIFFERENT clusters are, by construction, always more than
`GROUP_GAP_MS` apart, and `recentlyPopped` can never suppress them.

Tests added (`jobs.test.ts`): `a member finishing before the next one
starts does not re-pop START` — a1 runs and finishes while a2 is only
"waiting" (an idle-but-still-multi-member tick sits in between), a2 then
starts 67s later (well inside `GROUP_GAP_MS`) — must NOT re-pop; `a
genuinely new burst, more than GROUP_GAP_MS after the family's last START
pop, still pops` — a brand-new pair (`b1`/`b2`) starting far enough past
`a1`/`a2` that `clusterFamily` itself puts them in a new cluster — must
pop. The pre-existing serialized-handoff regression test (commit
`18352ff42`) re-run unmodified and still passes, along with every other
`groupPopupTick` test in the file (144 total, 0 failing).

Commands: same full command list as Fix 14 above (both fixes verified
together in the same run) — 422 pass, 0 fail; `bunx tsc --noEmit -p
frontend` clean; `node frontend/scripts/check-boundaries.mjs` → OK (811
files).

## Fix 16: Bug 2, round 2 — `source` made ambient by default, not opt-in

Live testing found the exact same symptom Fix 14 fixed for image/video
generation — a success popup while the user was still on the page that
raised the work — but for TEXT generation this time. Tracing it down: Fix
14's fix (`Job.source`, distinct from `page`, read from a caller's
`X-Fused-Source` header) was wired into `/api/ai/image` and `/api/ai/video`
ONLY. Text generation mints its job row from two call sites in
`fused_render/server/ai.py` (`_local_relay`, a resident local model, and
`_apple_relay`, Apple's on-device model) — both pre-dating Fix 14, neither
ever given a `source=` at all — and `/api/ai/transcribe`
(`ai_runtime.py`) and `/api/capture/start` (`capture/__init__.py`) had the
identical gap, just not yet reported live.

**The real defect was never "text forgot it" — it was the SHAPE of Fix
14's fix.** `source` arrived by OPT-IN: every producer that mints a job row
had to remember, on its own, to read `X-Fused-Source` and thread it through
to `jobs.upsert`/`supervisor._report`. Two rounds of live regressions
later, by two different producers, is a pattern, not a coincidence — an
opt-in a producer can forget is an opt-in that WILL eventually be
forgotten, silently, because a producer that never sends `source` just
notifies forever and no test catches a missing opt-in (there is nothing to
assert "on" that isn't there). The fix has to change the DEFAULT: `source`
must arrive without a producer doing anything, and a producer has to work
to lose it, not remember to gain it.

**Server** (`fused_render/jobs.py`, `fused_render/server/common.py`): a
request-scoped `contextvars.ContextVar` (`_ambient_source`, jobs.py),
consulted by `jobs.upsert` ONLY when a producer's own `source=` argument
resolves empty/falsy (`if not source: source = _ambient_source.get()`) —
an explicit truthy `source=` still always wins, unchanged from Fix 14's
own rule. The ContextVar is set for the duration of every request by
`server/common.py`'s `no_cache_and_log` — the one ASGI middleware that
runs unconditionally for every request regardless of route, unlike
`shell_calls.begin`, which only fires when a page sends its own
`X-Fused-Page`. Read from `request.headers` only, never the body, same
spoof-proofing `X-Fused-Page` already gets. This one chokepoint is why
`fused_render/server/ai.py`, `ai_runtime.py`'s transcribe route, and
`capture/__init__.py` needed NO changes at all: `supervisor._report`
already defaults `source = fields.pop("source", page)`, and every one of
these producers' `page` is empty at the point that matters (the
Playground's own text/transcribe calls carry no `X-Fused-Page`; capture's
`page` is a separate, real destination that stays whatever it is) — so
`source` was already resolving empty going into `upsert`, exactly the gap
`_ambient_source` fills.

A `contextvars.ContextVar` is the right shape specifically because it does
NOT propagate into a `threading.Thread` spawned with `.start()` (only into
`asyncio.to_thread`, which explicitly copies the context) — relied on as a
FEATURE, not worked around: a render's background thread already loses
`page`'s own request-scoped values for the same reason (Fix 14's own
design), and a late ambient-less tick from that thread resolves to `""`,
which is harmless because `Job.source` (like `page`/`origin`) is a STICKY
field — a falsy value on one tick never overwrites a truthy value an
earlier tick already set. `_local_relay`/`_apple_relay` run via
`asyncio.to_thread` (not a bare `Thread`), so the ambient value DOES
propagate to them — confirmed by a real end-to-end test posting through
`/api/ai`, not by inspection alone.

**Frontend** (`frontend/src/platform/lib/api.ts`): `getJson`/`mutateJson`
(and therefore every `postJson`/`putJson` caller) attach
`X-Fused-Source: encodeURIComponent(currentPresencePage())` automatically,
before a caller's own `opts.headers` are spread on top — explicit still
wins over ambient, same rule as the server. This is a deviation from a
literally-universal "every same-origin request" reading of the brief: two
raw-`fetch` producers in
`frontend/src/apps/ai_models/playground/client.ts` (`streamChat`'s
`/api/ai` call — the literal Round 2 regression — and `embedTexts`/
`embedPaths`) cannot go through `getJson`/`postJson` at all, because they
need to read a streamed response body, which `postJson` cannot do (its own
header comment says so, pre-dating this fix). These three call sites use
a small exported `sourceHeader()` (renamed from the old local, per-file
`sourceHeaders()`) explicitly. This narrow, well-flagged opt-in surface —
three call sites, all in one file, all commented as to why they cannot be
automatic — is the most complete fix achievable without giving `postJson`
a streaming mode it does not have; every other producer (transcribe,
capture start/stop/cancel, `startImage`/`startVideo` via `postJson`) is
now fully automatic. `startImage`/`startVideo` already sent an explicit
header before this fix (Fix 14) — they keep doing so via `sourceHeader()`,
now percent-encoded to match `X-Fused-Page`'s own convention
(`encodeURIComponent`/server-side `unquote`) rather than Fix 14's raw,
unencoded value, which happened to work only because `unquote` is a no-op
on a plain path with no `%` sequences.

**Deviation from the brief's Step 3 test list**: image and video already
had real-endpoint `Job.source` producer tests from Fix 14
(`tests/test_ai_runtime.py`:
`test_an_image_rows_source_defaults_to_the_caller_supplied_page`,
`test_an_image_rows_source_diverges_from_page_when_the_playground_sends_one`,
and their video equivalents) — no new tests were added for those two.
New tests: `tests/test_job_source_ambient.py` (text, both `_local_relay`
and `_apple_relay`, driven through the real `/api/ai` endpoint with
TestClient — through the real ASGI middleware, not a direct call to
`_local_relay`/`_apple_relay`, which would bypass the very layer under
test); a new transcribe test in `tests/test_ai_runtime.py`
(`test_a_transcript_rows_source_comes_from_the_ambient_X_Fused_Source`);
a new capture test in `tests/test_capture.py`
(`test_the_rows_source_comes_from_the_ambient_X_Fused_Source`); a
frontend test, `frontend/src/platform/lib/api.test.ts`, proving
`getJson`/`postJson` attach a non-empty `X-Fused-Source` with no explicit
attribution and that an explicit caller header still wins.

**What the guard tests protect** (`tests/test_job_source_ambient.py`):
two structural guards against a THIRD round of this bug.
`test_every_job_minting_prefix_is_accounted_for` enumerates every
`*_JOB_PREFIX` constant across `fused_render/` and pins the set — a new
kind of job-minting producer (a new prefix) fails this test until a
producer-level source test for it is added and the prefix is added to the
pinned set, forcing the same conscious check this round skipped twice.
`test_job_source_is_only_ever_written_by_jobs_upsert` greps for any
`.source =` assignment outside `jobs.py` — a producer that bypassed
`jobs.upsert` entirely (mutating a `Job` object directly) would silently
skip the ambient fallback and the explicit-wins-over-ambient rule both;
today the only such assignment in the tree is `jobs.py`'s own, inside
`upsert` itself.

Commands: `.venv/bin/python -m pytest tests/test_jobs_api.py
tests/test_ai_supervisor_job_page.py tests/test_ai_runtime.py
tests/test_capture.py tests/test_ai_text_job_row.py tests/test_ai_apple.py
tests/test_job_source_ambient.py tests/test_server_ai.py` → 869 passed
(one pre-existing, unrelated failure confirmed on a clean checkout of this
same branch before this fix's edits:
`tests/test_ai_metrics.py::test_a_missing_claude_binary_is_counted`, not
run as part of this fix's targeted set); `bun --cwd frontend test` → 6224
pass, 0 fail; `bunx tsc --noEmit -p frontend` clean; `node
frontend/scripts/check-boundaries.mjs` → OK (812 files).

## Fix 17: CHANGE 1 — every notification names its emitting page

**User's exact words** (from a screenshot): "every notification imo should
have a top row/section for the 'emitting page' context." Motivating bug: a
toast reading only "Public link token flash finished" with nothing saying
which project/app/page it came from.

**Read-only enumeration performed first** (per the dispatch's own
requirement), of every surface that renders a notification:
- The popup card: `frontend/src/platform/ui/MessagePopupCard.tsx` (client
  messages), `frontend/src/platform/ui/JobPopupCard.tsx` (jobs, untouched —
  already wired, see below).
- The panel: `frontend/src/shell/RepoUpdatesDock.tsx` — job rows (`JobRow`,
  already wired), grouped job rows (`GroupJobRow`, NOT wired), waiting-task
  attention rows (`AttentionRowView`, NOT wired), client-message rows
  (`MessageRowView`, NOT wired).

**The reused labeller**: `fused_render/jobs.py`'s `origin_for_page(page, *,
default="")` is the existing, already-established "who raised this"
labeller — a closed shell-route table, else a project display name, else a
bare filename stem. `Job.origin` (computed from it) was already wired onto
`JobRow` via `caption={job.origin || undefined}` in PR #1104
(commit c90e0ecfb), predating this branch — CHANGE 1 was already half-built
for plain job rows before this fix started. The true gap was: (1) grouped
job rows lacked the caption entirely, (2) client-raised messages had no
`origin` concept at all — `NotificationInput.source`/`StoredNotification`
never stored or exposed a rendered label, only used `source` at
suppression time, and (3) waiting-task attention rows had no origin either.

Server-side `origin_for_page` cannot be reached synchronously from a
client-raised `notify()` call or from a waiting task's own row (no request
round trip happens at either site) — so a client-side counterpart,
`labelForSource(source)`, was added, mirroring exactly `origin_for_page`'s
own bare-path fallback branch (basename, extension stripped) since every
`source` these call sites set is already a bare fs path. This is the
reused-not-reinvented precedent line: one labelling *rule*, one
server-side implementation reading it from routes/projects, one
client-side implementation reading the same rule off a bare path with no
round trip available.

**Where `labelForSource` lives, and why it moved mid-fix**: it was first
written inline in `frontend/src/platform/lib/notifications.ts`, then moved
to `frontend/src/platform/lib/format.ts` (zero imports, zero side effects)
after `shell/tasks-lib.ts` needed it too and importing it via
`notifications.ts` dragged in `platform/lib/router.ts`'s module-scope
`location` read, breaking `tasks-lib.test.ts` (which has never needed a DOM
shim) with `ReferenceError: location is not defined`. `notifications.ts`
now imports it from `format.ts` directly and re-exports it for existing
callers.

**What changed**:
- `frontend/src/platform/lib/format.ts`: new `labelForSource(source)`.
- `frontend/src/platform/lib/notifications.ts`: `StoredNotification` gains
  `origin?: string`, computed in `toStored()` via `labelForSource(input.source)`
  (`|| undefined`, so "" never stores — same "no line at all" rule
  `Job.origin`/`caption` already follow, never a placeholder).
- `frontend/src/platform/ui/MessagePopupCard.tsx`: passes
  `caption={notification.origin || undefined}` to its `NotificationCard` —
  the popup half of the fix.
- `frontend/src/shell/RepoUpdatesDock.tsx`: `MessageRowView` and
  `AttentionRowView` both gain `caption={... .origin || undefined}`;
  `GroupJobRow` gains `caption={members[0]?.origin || undefined}`, mirroring
  the existing "oldest member represents the row" convention already used
  for `title`/`openPage`.
- `frontend/src/shell/tasks-lib.ts`: `AttentionRow` gains `origin: string`,
  computed in `attentionRows()` via `labelForSource(task.target || task.project)`
  — the same value `taskSource()` (task-status-notify.ts) already uses for
  suppression, now also rendered.

**Rendering itself already satisfied every rule in the brief** because it
reuses `NotificationCard`'s pre-existing `caption` prop (PR #1104) and its
`.dl-origin` CSS (`frontend/src/styles/notifications.css`): no source (or
one resolving to "") draws no element at all (never a placeholder);
`.dl-origin` is `font-size: 11px; color: var(--fg-muted)` (secondary,
theme-aware via the CSS variable, does not clip the title, which sits on
its own line above); it renders on its own line, never as a second click
target (the row's own `rowClick`/`onClick` destination is unchanged);
right-ellipsis is acceptable because every value here is a short basename
or route label, never a raw long path.

**Tests**: `frontend/src/platform/lib/notifications.test.ts` (labelForSource
basename/passthrough/empty; `origin` stored with/without `source`);
`frontend/src/shell/tasks-lib.test.ts` (`origin` from target, from project
when target is empty, "" when neither); `frontend/src/shell/RepoUpdatesDock.test.tsx`
(caption present/absent on job rows, grouped rows using the oldest member,
message rows, waiting-task rows).

## Fix 18: CHANGE 2 — a finished task is now retained and clickable (reversal)

**This reverses this branch's own earlier position.** Until this fix,
`task-status-notify.ts`'s header comment argued that a plain "it's over"
confirmation "is not something to hunt for again," and `in_progress -> done`
returned `{ title, tone: "info", source }` with no `page` — which
`lib/notifications.ts`'s `isRetained` (`Boolean(input.action || input.page)`)
resolves to a transient popup, shown once and never kept.

**The user reversed this, from the same screenshot**: "the user does want
to open the app along with claude template to go back." A finished run is
exactly the moment someone wants to jump back into it — losing the row the
instant the popup's ~2.5s expire was the bug this fix closes, not a
feature working as designed.

**What changed** (`frontend/src/shell/task-status-notify.ts`): the
`in_progress -> done` branch now returns `page: taskDestination(task)` —
the same destination `in_progress -> blocked` already carries, which alone
makes `isRetained` keep the row — plus a new `recent: true`. `source`
(`taskSource(task)`) is unchanged, so suppression when the run's own
chat/project is already on screen still applies exactly as before; being
retained once shown is not the same as always showing it.

**Where it actually lands — verified, not assumed**: adding only `page`
would have landed the row UNFOLDED at the top of RepoUpdatesDock.tsx's
"Worth keeping" section, because `messagesTrail` (all retained,
non-attention messages) renders unconditionally with no cap or fold. That
contradicts the brief's requirement that a finished success land in the
FOLDED §4 "Recent" section, not shout at the top of the list. Fixed by
adding a new opt-in `recent?: boolean` to `NotificationInput`/
`StoredNotification`, threaded into `RepoUpdatesDock.tsx`'s existing
job-only §4 "Recent" fold (previously `recent`/`boundedRecent`/
`boundedRecentGroups`, job rows only) — now extended to messages via
`messagesRecent`/`boundedMessagesRecent`, following the exact same
cap/fold/heading-count/total-exclusion pattern jobs already used:
`messagesTrail` now excludes `recent` messages, `total` excludes
`messagesRecent` (a settled call — a success persisting is fine, a success
shouting is not), and the "Recent (N)" heading count includes both bounded
job groups and bounded recent messages.

**The stale block comment was rewritten**, not left in place, to document
both the new behaviour and the reversal with the user's own stated reason
(see the file itself for the full text) — it explicitly calls out that
this reverses the file's own prior position rather than presenting the new
behaviour as if it had always been the plan.

**Tests**:
- `frontend/src/shell/task-status-notify.test.ts`: the done-branch test now
  asserts `page`, `recent: true`, and that `source`/`tone`/`title` are
  unchanged.
- `frontend/src/shell/useTaskStatusNotify.test.ts`: the integration-level
  done test now asserts the notification is retained (`getRetainedNotifications().length === 1`)
  with `page` defined and `recent: true`, replacing the old assertion that
  retention was empty.
- `frontend/src/shell/RepoUpdatesDock.test.tsx`: a `recent` message lands
  in the folded "Recent" section (not drawn until the toggle opens it, not
  counted in the chip total, heading shows "Recent (1)"); opening the
  toggle reveals a clickable row (`dl-row-open` class + a real `onClick`
  wired to its `page` — NOT `role="button"`, since `MessageRowView` sets an
  explicit ARIA `role` of "status"/"alert" for its content, which
  `NotificationCard` deliberately lets win over `rowClick`'s own implicit
  `role="button"`); a non-`recent` retained message still lands in "Worth
  keeping" unfolded (regression pin for existing behaviour).

## Fix 17/18 verification

Commands run from the worktree root:
- `bun --cwd frontend test` → 6239 pass, 0 fail, across 297 files (two runs
  confirmed stable; a `tasksPulse.test.ts` "N subscribers" failure seen once
  mid-fix, before `useTaskStatusNotify.test.ts` was updated, did not
  reproduce on a clean checkout of the pre-fix commit or on a second run
  after the fix — a pre-existing flake, not a regression from this fix).
- `bunx tsc --noEmit -p frontend` → clean, no output.
- `node frontend/scripts/check-boundaries.mjs` → `boundaries OK (812 files)`.
- No Python/server files were touched by either fix (both are frontend-only
  — `origin_for_page` itself was read but not modified), so no targeted
  pytest run was required. `tests/` was grepped for every frontend symbol
  touched (`labelForSource`, `AttentionRow`, `StoredNotification`,
  `NotificationInput`, `dl-origin`, `messagesRecent`/`messagesTrail`,
  `origin_for_page`); the only hits are pre-existing comments/assertions
  about the server-side `origin_for_page`, which was not changed.

**Not verified — no browser/visual tooling available in this session**:
- That `.dl-origin`'s caption actually reads legibly, doesn't clip, and
  looks correct in both light and dark theme on a real popup/panel render.
- That the folded "Recent" section's toggle interaction feels right and the
  caption doesn't crowd the row visually at real notification-panel width.
- That truncation of a genuinely long origin label (this branch's values
  are all short basenames/route labels in practice, per `labelForSource`'s
  own fallback) doesn't produce an awkward wrap given `.dl-origin`'s
  existing `white-space: nowrap; text-overflow: ellipsis` (right-ellipsis,
  pre-existing, not changed by this fix).
- A human should open the app, trigger a real "Task finished" transition,
  and a real client-raised message with a `source` set, and visually
  confirm both the popup and panel rows in light and dark mode.

## Fix 19: one shared route table, not three copies of the same knowledge

**The defect Fix 17 left behind**: it built `labelForSource` (`format.ts`) as
a basename-only fallback and never wired it to the server's own route table,
so it disagreed with `origin_for_page` (`fused_render/jobs.py`) on every
known shell route — `/tasks` labelled "Scheduler" server-side and "tasks"
client-side, same for `/ai-models/local`, `/ai-models/benchmark`,
`/claude-config`, `/preferences`. A source carrying a query string (a task
destination like `/explorer/view/Users/x/app?_side=claude&session_id=…`,
`schedule-lib.ts`'s `explorerUrl`/`chatPaneUrl`) was not stripped either, so
`labelForSource` could return a raw URL fragment as a caption. Meanwhile
`router.ts`'s `JOB_PAGE_ROUTES` held the SAME closed route set a third time,
as a bare `Set` with no labels — three independently-maintained copies of
one fact ("which routes are shell surfaces, and what do we call each one"),
and `jobs.py`'s own comment already said outright that any drift between
its table and `router.ts`'s was a bug to fix, without naming the third copy
that had since appeared.

**Why three copies existed instead of one, even though everyone knew it was
wrong**: Python cannot import TypeScript, so the server-side dict was always
going to be its own literal — that one copy is structural, not a mistake.
The client-side duplication was the real defect, and it existed because the
one place client code COULD have put a single shared table
(`notifications.ts`, which already imports `router.ts`) is unreachable from
`tasks-lib.ts` without breaking `tasks-lib.test.ts`
(`ReferenceError: location is not defined` — `router.ts` reads `location` at
module scope, and `notifications.ts` imports it eagerly). Fix 17's builder
worked around that constraint by writing a second, independent labelling
rule in `format.ts` rather than restructuring the module graph — which
solved the import problem but reintroduced the drift problem the comment in
`jobs.py` was already warning about.

**The fix**: reproduced the `location`-at-module-scope constraint before
touching anything (confirmed: `router.ts`'s `IS_EMBED`/`IS_PREVIEW`/the
`rewriteLegacyPath` IIFE all read `location` at module scope, and
`notifications.ts` imports `router.ts` eagerly for `IS_EMBED`/`IS_TOP_EMBED`
— so anything that imports `notifications.ts`, even transitively, needs a
DOM shim installed before its own static imports evaluate). Rather than
routing around `router.ts`, pulled the table BELOW it: a new leaf module,
`frontend/src/platform/lib/originRoutes.ts` — `ORIGIN_BY_ROUTE`, a plain
`Record<string, string>`, zero imports, zero module-scope side effects
(the same property `format.ts` already had, and for the same reason:
`tasks-lib.ts` needs to reach it with no DOM shim). Three consumers now read
this one table instead of each holding their own copy:
- `router.ts`'s `JOB_PAGE_ROUTES` is now `new Set(Object.keys(ORIGIN_BY_ROUTE))`
  instead of a second literal list — its own behaviour is unchanged
  (`router.test.ts`'s existing `isJobPageRoute`/`navigateToJobPage` tests,
  which already pinned the exact six-route set literally, passed unmodified
  and are the regression proof).
- `format.ts`'s `labelForSource` now tries `ORIGIN_BY_ROUTE[source]` FIRST
  (the full, unstripped string — because a query-bearing key can itself be a
  table entry, see below), then `ORIGIN_BY_ROUTE[withoutQueryOrHash]`, and
  only falls through to the basename-and-strip-extension rule when neither
  hits.
- `fused_render/jobs.py`'s `_ORIGIN_BY_ROUTE` keeps its own literal (Python
  cannot import the TS module) but its header comment now names
  `originRoutes.ts` explicitly as the table it mirrors, and points at the
  new cross-language test below.

**The `/preferences` vs `/preferences?tab=indexing` ordering, preserved
correctly**: `_ORIGIN_BY_ROUTE` carries both keys with different labels
("Preferences" vs "Explorer") precisely because the query string changes
what the route MEANS. `labelForSource` therefore checks the full string
before stripping anything — stripping first and looking up second would
have collapsed the indexing tab's "Explorer" into "Preferences", the exact
bug the brief called out by name. Query/hash stripping only happens for the
SECOND lookup and for the basename fallback, which is also what turns a
task-destination URL's junk tail into a clean label instead of carrying it
through.

**The deliberate, bounded divergence that remains**: `labelForSource` still
cannot replicate `origin_for_page`'s PROJECT-name resolution for an ordinary
fs path (`projectenv.project_root_for` + `projectenv.display_name`) — that
needs server-side filesystem access no client call site has, since neither
a client-raised `notify()` call nor a waiting task's own row makes a request
round trip. This was already true before this fix and is unchanged by it;
what changed is that every route the client COULD name authoritatively
(the closed shell-route set) now agrees with the server byte-for-byte, so
the only remaining disagreement is on fs paths, where the client's
"basename, extension stripped" answer was always documented as the
next-best fallback, never a bug.

**Cross-language drift test**: `tests/test_jobs_api.py`'s
`test_origin_by_route_matches_the_client_table` parses `originRoutes.ts`'s
object-literal source text with a regex (pytest cannot import or execute
TypeScript) and diffs the resulting dict against `jobs._ORIGIN_BY_ROUTE`
key-for-key. Verified it actually catches drift, not just passes vacuously:
temporarily changed one Python-side value (`"/tasks": "Scheduler"` ->
`"/tasks": "WRONG"`) and confirmed the test fails with a clear diff, then
restored the file and confirmed it passes again. This was chosen over
maintaining two independently-pinned literal lists (one per language, each
just asserting its own table's shape) because a same-literal-list test can
pass on both sides while the two literals still disagree with each other —
the whole class of bug this fix exists to close; parsing the actual TS
source and comparing it to the actual Python dict cannot pass unless the
two are identical.

**Other tests added**: `frontend/src/platform/lib/format.test.ts` —
`labelForSource` labels every `ORIGIN_BY_ROUTE` entry with its own table
value (a loop over all six routes, not one example); the
`/preferences?tab=indexing` vs `/preferences` vs `/preferences?tab=engines`
ordering; a task-destination-shaped URL (`?_side=claude&session_id=…`)
strips to a clean basename; a bare fs path still falls back to basename
(regression pin for existing behaviour untouched by this fix).

**Verification**:
- `bun --cwd frontend test` -> 6243 pass, 0 fail, across 297 files (4 more
  than Fix 17/18's 6239, all new `labelForSource`/`ORIGIN_BY_ROUTE` tests;
  same file set otherwise). One unrelated pre-existing issue found and
  ruled out during this work, NOT a regression from this fix: running
  `format.test.ts router.test.ts notifications.test.ts tasks-lib.test.ts`
  together as one `bun test` invocation throws
  `ReferenceError: location is not defined` from `router.ts`'s module-init
  `rewriteLegacyPath` IIFE — reproduces identically on a stash of this
  fix's own changes (confirmed via `git stash`/`git stash apply`, not just
  asserted), and each of those four files passes cleanly run on its own or
  as part of the full suite. A pre-existing test-isolation quirk in how
  bun orders module-scope DOM reads across hand-picked file subsets, not a
  regression.
- `bunx tsc --noEmit -p frontend` -> clean, no output.
- `node frontend/scripts/check-boundaries.mjs` -> `boundaries OK (813 files)`.
- `.venv/bin/python -m pytest tests/test_jobs_api.py -q` -> 91 passed (up
  from Fix 17/18's 81; the one new cross-language test plus this branch's
  existing coverage).
- Grepped `tests/` for every touched/added symbol
  (`labelForSource`, `JOB_PAGE_ROUTES`, `ORIGIN_BY_ROUTE`,
  `_ORIGIN_BY_ROUTE`, `originRoutes`) — the only hits are this fix's own new
  test and pre-existing prose comments naming `_ORIGIN_BY_ROUTE`/
  `JOB_PAGE_ROUTES` in `test_jobs_api.py`, unaffected by this change.

**Not verified — no browser in this session**: that the caption on a real
`/tasks`-sourced or `/preferences?tab=indexing`-sourced notification now
visibly matches its job-row counterpart's caption in a live popup/panel
render, in both themes. A human should trigger a client-raised notification
or a waiting-task row from one of the six shell routes and confirm the
caption reads the same as a job row raised from that same route.

## Fix 20: Defect 1 (live testing, 2026-09-17) — presence identity was the full query string, breaking grouping and suppression

**Root cause, pre-confirmed by a curl showing two duplicate Playground job
rows**: `currentPresencePage()` (`platform/lib/presence.ts`) fell back to
`currentUrl()`, which includes the full query string. The Playground syncs
its prompt/model into that query string on every keystroke/model change, so
the page's "identity" changed constantly. Two consequences, both from the
same cause: (a) `familyKey` in `jobs.ts` is `${job.source || job.page}
${job.group}`, so two renders from the same page essentially never shared a
key once `source` carried a different query string each time; (b)
`matchesSource` required an exact string match once either side had a `?`,
so a job's captured `source` almost never matched the CURRENT live page
either — suppression silently stopped working and popups kept firing for a
page the user was already looking at.

**Fix — canonicalize identity in exactly one place.** Added
`canonicalPresenceIdentity()` in `presence.ts`: strip everything from the
first `?` or `#` onward, UNLESS the untouched string is itself a registered
key in `ORIGIN_BY_ROUTE` (`platform/lib/originRoutes.ts`) — in which case
keep it whole. This makes `ORIGIN_BY_ROUTE` the single authority for "is
this query-bearing string a genuinely distinct identity, or just app
state" — the same table `router.ts`/`format.ts`/`jobs.py` already share, so
no second list of "queries that matter" gets invented. `currentPresencePage()`
now runs its `currentUrl()` fallback through this function, which means
every consumer of that one choke point — the `X-Fused-Source` header sent
by `api.ts`, `isOpenAnywhere`/`isFocusedHere`, and `familyKey` — gets the
fix for free. No changes were needed in `api.ts` or `jobs.ts` themselves.

**Decision 1 — `matchesSource` DOES canonicalize both of its inputs.**
Chose this over leaving it comparing raw strings, for backward compatibility
with `Job.source` values already persisted (in `localStorage`/in-flight
state) from before this fix shipped — a "dirty" source captured pre-fix must
still suppress correctly against a freshly-canonicalized live page, or every
already-open tab would show one wrong popup on first load after the deploy.
Canonicalizing on read, at the comparison boundary, costs nothing extra (the
values are short strings) and needs no migration of stored data.

**Decision 2 — the "exact match required once either side has `?`" rule
still earns its keep, and is NOT dead weight.** Once both sides are
canonicalized, the ONLY strings that can still carry a `?` are the
registered `ORIGIN_BY_ROUTE` entries themselves (e.g.
`/preferences?tab=indexing`) — every non-registered query was just stripped
to its bare route. For those registered entries, the rule is exactly what
stops `/preferences?tab=indexing` from prefix/cross-matching its bare
sibling `/preferences`, which is the one case this whole fix is designed to
keep distinct (per the brief: "`/preferences?tab=indexing` stays distinct
from bare `/preferences`"). Removing the rule would silently merge every
registered query-bearing surface back into its bare route, undoing the one
thing the "keep it whole" branch of canonicalization exists to protect.

**Test-update tradeoff, made explicitly rather than silently.** An existing
test, `"matchesSource: a query-bearing page never prefix-matches a bare
route"`, asserted `/preferences?tab=lan` never matches bare `/preferences`.
`tab=lan` is NOT a registered `ORIGIN_BY_ROUTE` key (only `tab=indexing`
is), so under the new rule it canonicalizes down to the bare route and DOES
match — which is correct per the brief's algorithm (an unregistered query is
app state, not identity). Grepped the codebase: `tab=lan` appears exactly
once, as a plain navigation `href` on a "Fix with Claude"-style button in
`RepoUpdatesDock.tsx:224` — never as a source any job/message producer
names — so there is no live functional case that depended on treating it as
a distinct identity. Updated the stale test to describe the new intended
behaviour (`"an unregistered query is app state and canonicalizes down to
the bare route"`) and added a new test using the actually-registered
`tab=indexing` key (`"a REGISTERED query-bearing surface (ORIGIN_BY_ROUTE)
never matches the bare route"`) as the real regression guard for
genuinely-distinct-surface behaviour.

**Tests added** (`platform/lib/presence.test.ts`): the two `matchesSource`
tests above; `"matchesSource: two Playground URLs differing only by
prompt/model app-state still match (grouping/suppression bug)"` using the
exact URL shapes from the curl evidence; `"currentPresencePage: two renders
on the same route with different app-state query strings canonicalize to
the same identity"`; `"currentPresencePage: a registered ORIGIN_BY_ROUTE
query stays whole, distinct from the bare route"` (the latter two mutate
`globalThis.location` directly, save/restore in try/finally — bun does not
reset globals between test files in one run, see `testDomShim.ts`).
Targeted run: `bun test src/platform/lib/presence.test.ts` -> 32 pass, 0
fail, 52 expect() calls.

## Fix 21: Defect 2 — same-family ERROR rows now cluster, with zero additional production code

As predicted in the brief, Defect 2 ("two ERROR rows from the same failing
job family did not group into one attention row") was a downstream
consequence of Defect 1's stale-query `job.source` values defeating
`familyKey`. Added a test to confirm this rather than assuming it:
`RepoUpdatesDock.test.tsx`'s `"DEFECT 2: two same-source, same-group ERROR
jobs cluster into one attention row, not two"`. It passes with NO
production code changes beyond Fix 20 above — `groupJobs`/`clusterFamily`
in `jobs.ts` already grouped correctly once `job.source` values became
canonically identical. Errors are still never suppressed and never folded
away; grouping two error rows into one attention row is not suppression —
both member jobs remain individually visible inside the expanded group, per
D-C. Full-file run: `bun test src/shell/RepoUpdatesDock.test.tsx` -> 89
pass, 0 fail, 235 expect() calls.

## Fix 22: Defect 3(a) — the emitting-page caption moves above the title, in the one shared component

Fix 17 drew the `.dl-origin` caption AFTER `secondary` (`.dl-model`), so a
card read title, then model name, then the page name buried on a third
line — backwards from "context first". Moved the caption's render to BEFORE
`.dl-row-head` in `platform/ui/NotificationCard.tsx` (class `"dl-origin
dl-eyebrow"`), so it is the row's first line, a small dimmed eyebrow above
the title. `.dl-model` is untouched, exactly where it was. Because all six
caption-bearing surfaces (JobRow, GroupJobRow, AttentionRowView,
MessageRowView, JobPopupCard, MessagePopupCard) delegate to this ONE shared
component's `caption` prop, this single change applies everywhere Defect
3(a) named — no per-surface edits were needed or made.
`styles/notifications.css`: added `.dl-eyebrow { margin-top: 0; }` (no
font-size/colour override needed — it reuses `.dl-origin`'s existing dim
styling, just repositioned). Verification across the six related test
files (`NotificationCard.test.tsx`, `JobRow.test.tsx`, `JobPopupCard.test.tsx`,
`AttentionRowView`/`MessageRowView` coverage in `RepoUpdatesDock.test.tsx`,
etc.): 199 pass, 0 fail, 505 expect() calls.

## Fix 23: Defect 3(b) / Addition 2 — "Recent (1)" disclosure now styled as a section heading

User's own words, relayed mid-round as the stated priority: "the recents
thing is very ugly." Grepped `notifications.css` BEFORE touching anything
and confirmed `.dl-recent-toggle`/`.dl-section-recent` had ZERO existing
CSS rules — it was rendering as a raw, unstyled `<button>`, exactly matching
the bug report ("a bordered box floating mid-panel... an unstyled default
button"). Added a full rule set matching `.dl-section-head`'s established
look: 10px uppercase muted text, full-width clickable target, a rotating
`▸` chevron via `::after` keyed off `aria-expanded` (closed vs. open), a
hover state, and `.dl-section-recent { padding-bottom: 6px; }` for the
section's own spacing. Pure styling, per the brief's own note that this
needs no new tests beyond not breaking the existing tests asserting the
section exists and folds — confirmed those still pass in the full suite
run below.

## Fix 24: Addition 1 — a task's caption named its entry-page basename ("index"), not its app

Relayed mid-round, root cause given verbatim: a Claude-template task inside
an app called "Transcripto" captioned "index" ("Transcripto YouTube
transcriber finished" / "index"). `shell/tasks-lib.ts`'s `attentionRows()`
computed `origin: labelForSource(task.target || task.project)` —
target-FIRST. Per `folderHref`'s own comment (`schedule-lib.ts`, ~line
583), a task made from inside an app targets that app's ENTRY PAGE
(`.../index.html`), so target-first yields the basename "index" once the
extension is stripped, for every app in the system alike.

**Fix**: swapped to `origin: labelForSource(task.project || task.target)`,
matching `folderHref`'s own established order exactly, with a comment in
`tasks-lib.ts` pointing at `folderHref` as the precedent so this does not
get swapped back. `project` names the app/folder, which is what a caption
is for; `target` remains the fallback for a task with no project at all.

**The general case, not just the symptom.** `labelForSource`'s
(`platform/lib/format.ts`) basename fallback will produce "index" for ANY
source ending in an index-like entry file, from ANY caller — not just this
one now-fixed call site (`notifications.ts` can reach the same fallback via
a client-raised message with no project to prefer). Decision: when the
extension-stripped basename is exactly `"index"` (case-insensitive — the
one entry-file spelling this codebase documents, per `folderHref`), walk up
one path segment to the containing folder name instead, since that is what
actually varies between apps and "index" alone never does. Deliberately
scoped to this one closed, documented convention rather than generalizing
to "any uninformative-looking basename" (`main`, `app`, etc.) — those would
be unevidenced guesses, whereas "index" is a known, closed spelling. A path
with nothing above the entry file (no parent segment) falls through to
"index" unchanged; there is nothing truer to say without a project name,
which this function still cannot resolve (per its own header comment on the
server-side `projectenv` divergence).

**Tests added**: `shell/tasks-lib.test.ts` —
`"names the row from the app folder (project), not its entry page (target)
— the 'index' caption bug"` (pins the exact Transcripto shape) and
`"falls back to the containing folder when only an entry-page target is
available"` (project absent entirely, general-case coverage via
`labelForSource`). Had to update one now-stale pre-existing test in the
same describe block (`"names who raised the row, from the task's own
target/project"`) — its `withTarget` case relied on `task()`'s factory
default `project` ("/Users/me/Desktop/fused") being irrelevant when only
`target` was set; now that `project` wins, that case needed `project: ""`
added explicitly to still exercise the target-only path. `platform/lib/format.test.ts` —
`"walks up to the containing folder when the basename is an uninformative
entry file — the 'index' caption bug"`, covering: lower/upper-case
`index.html`, extensionless `index`, no-parent-segment fallthrough, and a
negative case (`main.py` is left alone, proving the walk-up does not
generalize beyond "index").

Grepped Python `tests/` for `labelForSource`, `attentionRows`, and both
orderings of `task.target`/`task.project` before editing: the only hits are
`tests/test_claude_live_run.py` (a docstring describing an UNRELATED
Python-side "live run adoption" lookup that also happens to be named
`task.target || task.project`, not this function) and
`tests/test_jobs_api.py` (parses `originRoutes.ts`'s object literal, not
`format.ts`/`tasks-lib.ts`) — neither asserts on the lines this fix
touched. Ran `test_origin_by_route_matches_the_client_table` directly as a
sanity check: 1 passed.

## Verification for Fix 20–24 (this round)

- `bun --cwd frontend test` (full suite) -> 6251 pass, 0 fail, 23074
  expect() calls across 297 files.
- `bunx tsc --noEmit -p frontend` -> clean, no output.
- `node frontend/scripts/check-boundaries.mjs` -> `boundaries OK (813 files)`.
- `.venv/bin/python -m pytest tests/test_jobs_api.py -k
  test_origin_by_route_matches_the_client_table -q` -> 1 passed (the only
  Python-side test touching a symbol this round's edits share a name
  with; no `.py` file was edited this round, so no broader pytest run was
  warranted).
- Grepped `tests/` for `labelForSource`, `attentionRows`, `matchesSource`,
  `currentPresencePage`, `canonicalPresenceIdentity` — confirmed no other
  literal-source-line assertions exist against the touched TypeScript.
- Confirmed the `ArraysCache.trim` mlx-lm text-generation bug (explicitly
  out of scope) is untouched by this round and by this branch as a whole:
  `git diff main...HEAD` touches `fused_render/ai/supervisor.py`,
  `fused_render/server/routers/ai_runtime.py` (only the `api_ai_image`/
  `api_ai_video` render routes — threading a new `X-Fused-Source` header
  through, from earlier work in this branch, not this round), `jobs.py`,
  `schedule.py`, `server/common.py`, and `server/routers/jobs.py` — none of
  these touch the text-generation/mlx-lm engine path where `ArraysCache`
  lives. This round's own edits are entirely `frontend/src/platform/lib/
  presence.ts`, `frontend/src/shell/tasks-lib.ts`,
  `frontend/src/platform/lib/format.ts`, `frontend/src/platform/ui/
  NotificationCard.tsx`, `frontend/src/styles/notifications.css`, and their
  test files.

**Not verified — no browser in this session:**
- That the eyebrow caption (Fix 22) visually reads as a top row above the
  title, rather than overlapping or misaligning, on every one of the six
  surfaces, in both light and dark theme.
- That the "Recent" disclosure (Fix 23) now visually matches the panel's
  other section headings' font size, weight, colour, and spacing exactly,
  rather than merely sharing the same CSS rules on paper.
- That a live Playground render sequence (two renders, differing only in
  prompt/model query state) now visibly clusters into one row in the actual
  running app, matching the curl-evidence bug this round fixes at its root.
- That a real Transcripto-style (or any other app-hosted) Claude task now
  visibly captions with the app's folder name instead of "index" in a live
  notification popup/panel render.
A human should exercise all four of the above in a running instance before
calling Defects 1–3 and Additions 1–2 fully closed.

## Fix 25: merged origin/main to clear a DIRTY PR (two conflicts)

PR #1183 reported `mergeStateStatus: DIRTY`, which queues zero CI workflow
runs — the PR was unverified, not green. Merged `origin/main` in
(`git fetch origin main && git merge origin/main`); two files conflicted,
both against real behavioral commits on main:
`95bd752d4` ("Project queue: one task in progress per folder, the rest wait
in line") and `1ce6df9fc` ("Tasks: a send is a row the moment it is sent").
Read both commits' full diffs before resolving either conflict.

**`frontend/src/shell/tasks-lib.ts`** — one conflict, in the import block.
HEAD added `import { labelForSource } from "@platform/lib/format"` (this
branch's Fix-16 "source-is-ambient-by-default" caption work); main added a
`@platform/lib/queue` import block (`CHAT_ENTRY_ORIGIN`, `chatUrl`,
`pendingEntryId`, `queuePosition`, `runningWaitingLabel`, `waitingLabel`) for
the project-queue feature. Both imports are used elsewhere in the file by
code neither side touched — kept both, no further changes needed. Verified
the load-bearing hazard this task was explicitly warned about: `attentionRows`
(the function that reads `labelForSource(task.project || task.target)`,
project-first, per Fix 24's "index caption" fix) sits well outside main's
diff and merged clean with **zero** conflict markers inside the function
body — the project-first ordering (`task.project || task.target`, not
`task.target || task.project`) survived byte-for-byte. Confirmed by grepping
the merged file directly rather than trusting the absence of a conflict
marker.

**`fused_render/schedule.py`** — one conflict, in `_send()`, right after the
spawn's `run_id` is confirmed and `_watching(entry["id"], True)` is called.
HEAD's side (this branch's §5) called `_update(..., state=SENT, run_id=...,
error="")` and then unconditionally `_emit(EVENT_STARTED, entry)` — the
"scheduled run started" notification moment. Main's side (`95bd752d4`, the
project-queue work) changed the SAME `_update(...)` call to also pass
`host_sent=True` when the send was actually delivered as a guest message
into an already-live chat session (`host_sent = res is not None` a few lines
above, unrelated to anything this branch touches) and to also set
`entry["host_sent"] = True` on the in-memory dict so the code just below
(`cancellable=not host_sent`, an existing, unconflicted line right after
this hunk) reads it correctly. These are two independent additions to
adjacent statements, not competing edits to the same behavior: kept main's
whole `_update(...)`/`entry["host_sent"]` block verbatim (a real host_sent
send needs its `host_sent` flag persisted or a later cancel would kill the
reader's own chat session, per `test_a_cancel_on_a_host_sent_entry_leaves_
the_chats_session_alone` in `tests/test_schedule_project_queue.py`), then
kept this branch's `_emit(EVENT_STARTED, entry)` call immediately after,
with its existing comment plus one added sentence: a host_sent guest
delivery still emits `EVENT_STARTED`, because the message really did reach
a live session and start running there — the narrator's own presence
suppression on `entry["target"]` already covers the "already looking at
that chat" case, so no separate carve-out is needed for the guest path.

**Checked the specific hazard the task named** — whether `1ce6df9fc`'s "a
send is a row the moment it is sent" changes the status-transition shape
`task-status-notify.ts`/`useTaskStatusNotify.ts` diffs against. That commit
does not touch `frontend/src/shell/tasks-lib.ts` at all (confirmed:
`git diff 1ce6df9fc~1 1ce6df9fc -- frontend/src/shell/tasks-lib.ts` is empty);
its surface is server-side (`fused_render/server/routers/tasks.py`,
`tasks_watch.py`) — a live send now appears as a placeholder task row, keyed
by session id, with a synthetic message (`state: SENT`, `turn: ""`) that
`_message_running`/`_status` already read as `in_progress` on the very first
poll after send, rather than the row not existing at all until a transcript
file appears. Traced this against `useTaskStatusNotify.ts`'s transition
logic:
- First sighting of a task is still explicitly never a transition
  (`notificationForTransition` returns `null` when `previous === undefined`),
  so the earlier appearance of the row does not itself fire anything new.
- `previous`/`prev` is a `Map` keyed by `task.key` (the session id), pruned
  every tick for keys no longer present. A send whose run dies before any
  transcript exists makes its placeholder row **disappear** entirely (server
  comment: "nothing happened") rather than transition to `done`/`blocked` —
  the map entry is deleted on the next tick, not read as a transition, so no
  spurious notification fires for a dead send.
- The commit's own comment states the transcript row that follows is
  "literally this row: same key, same number, no swap for the reader to
  watch" — so a real send's lifecycle is placeholder(`in_progress`) →
  transcript-backed(`in_progress`, same key) → eventually `done`/`blocked`,
  which is if anything a STRICT IMPROVEMENT for this branch's notifier: a
  transition that used to only become observable once a transcript file
  existed (and could already be evaluated at `previous === undefined` for a
  faster-than-poll turn, silently dropping the "just happened" notification)
  is now observable one poll earlier, with `previous === "in_progress"`
  already recorded, so `in_progress -> done`/`in_progress -> blocked` fire
  strictly more reliably than before.

**Net finding: no notifier defect from this merge.** Did not find a status
transition this branch's notifier now fires on that it shouldn't, or a real
transition it now misses, as a result of the row-at-send-time change.
Reporting this explicitly per the build brief's instruction, rather than
silently expanding scope — no code changed in `task-status-notify.ts` or
`useTaskStatusNotify.ts` for this reason.

**Verification after the merge** (both conflicts resolved, `git add`, not
yet committed at time of these runs):
- `bun --cwd frontend test` → 6550 pass, 0 fail (24199 `expect()` calls,
  305 files).
- `bunx tsc --noEmit -p frontend` → clean, no output.
- `node frontend/scripts/check-boundaries.mjs` → `boundaries OK (828 files)`.
- `.venv/bin/pytest tests/test_schedule.py tests/test_schedule_api.py
  tests/test_schedule_recurring.py tests/test_schedule_reporting.py
  tests/test_schedule_run_now.py tests/test_schedule_project_queue.py
  tests/test_schedule_queue.py tests/test_schedule_queue_endpoint.py
  tests/test_schedule_resend.py tests/test_schedule_session_liveness.py
  tests/test_schedule_status_sync.py tests/test_schedule_wake.py
  tests/test_task_sync_matrix.py tests/test_tasks_api.py
  tests/test_tasks_sent_mark.py tests/test_tasks_watch.py
  tests/test_tasks_store.py tests/test_tasks_queue_api.py` → 844 passed
  (repo's own `pyproject.toml` runs xdist automatically; no `-n` flag was
  passed).
