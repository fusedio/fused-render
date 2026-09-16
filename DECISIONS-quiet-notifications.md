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
