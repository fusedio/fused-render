# Decisions — notification ingest collapse fix

## A. Ingest collapse: call `notify()` directly, not a shared helper

Chose to route `_fusedIngestNotification` through `notify(input)` itself,
rather than extracting a shared "find the matching family" helper both
`notify()` and the ingest handler call.

Why this is safe (checked against the brief's stated risks before picking):

- **Id contract preserved.** `notify()` mints from the same module-local
  `nextId` sequence (or reuses an existing retained row's id on a family
  collapse) and returns that id — exactly the contract the ingest handler
  already had and the existing tests (`notifications.test.ts` findings #1,
  #2, #8) assert on.
- **No double-forwarding.** `notify()`'s own `forwardToShell` only fires when
  `effectiveIsEmbed() && !effectiveIsTopEmbed()` — i.e. only when the
  *current* document is itself a non-top embedded pane. The document that
  legitimately receives an ingest (the shell, or an intermediate pane in a
  nested-embed chain) is never in that state for the message it just
  received, so nothing re-forwards what was just forwarded in. (A nested
  pane that is itself embedded WOULD forward further up its own chain if it
  ever received an ingest — that's correct, cascading behavior, not a
  regression; it just doesn't come up in today's single-level pane->shell
  topology.)
- **No suppression surprise.** `forwardToShell` only ever sends `origin`
  (a precomputed caption), never `source` (the suppression key). `notify()`'s
  `isSuppressed` short-circuits to `false` whenever `input.source` is absent,
  so an ingested message can never be suppressed by the receiving document's
  own presence state.
- **Popup behavior change is intentional, not incidental.** Before this fix,
  ingest never popped a card in the receiving document — only local `notify()`
  calls did. Routing through `notify()` means the shell now also pops (and
  "latest wins" replaces) a popup for a forwarded message, which is what the
  module's own pre-existing doc comment already claimed ("exactly as if
  `notify()` had been called locally") but the code never actually did. No
  existing test asserted the old no-popup-on-ingest behavior, so this isn't a
  hidden regression — it's the fix actually delivering on a documented
  contract that was previously false.

A shared "collapse lookup" helper was the other option considered. Rejected
because it would have meant re-implementing `notify()`'s id-minting,
popup-arming, and `capRetained`/`forwardToShell` bookkeeping a second time
next to it (or awkwardly splitting `notify()` itself into two halves just for
this), which is exactly the kind of two-copies-of-one-rule setup that caused
this bug in the first place (the ingest handler drifted from `notify()`
because it started as a hand-rolled duplicate of a subset of its logic).
Calling `notify()` directly means there is now structurally only one place
that decides "does this collapse, and how."

## B. Embed guard: inside the hook's effect body, not at the App.tsx call site

Chose to add `if (IS_EMBED) return;` as the first line of
`useTaskStatusNotify`'s `useEffect` body, rather than `!IS_EMBED &&
useTaskStatusNotify();` at the App.tsx call site.

Reasoning: `IS_EMBED` is a frozen, module-scope constant for a document's
entire lifetime, so a conditional call at the call site would not actually
violate the rules of hooks in practice (the condition can't change across
renders) — but it still *reads* as a conditional hook call, and the brief
explicitly favors reading honestly over relying on that invariant. Guarding
inside the effect keeps `useTaskStatusNotify()` an unconditional, ordinary
hook call at the App.tsx site (no diff there at all) and keeps the "when do
we actually raise a notice" decision entirely inside the hook that owns it.

Test coverage for this branch is a structural source-text assertion
(`useTaskStatusNotify.test.ts`), not a runtime toggle — `IS_EMBED` cannot be
flipped for one test file within a single `bun test` process (bun shares one
module registry across every file in a run; this is the same constraint
`notifications.ts`'s own `effectiveIsEmbed`/`_setIsEmbedForTest` override
exists to work around, and this hook doesn't warrant introducing a second
such override just for one guard). `home-performance.test.ts` already
establishes this "assert the guard string exists and precedes the
gated code" convention for `main.tsx`'s own `if (IS_EMBED) return;` boot
guard; this fix's test follows the same shape.

## What the brief got wrong / had to be adjusted

- The brief suggested the ingest fix might need to "not re-forward" as a
  behavior distinct from what `notify()` already does. On inspection,
  `forwardToShell`'s existing `!effectiveIsEmbed() || effectiveIsTopEmbed()`
  guard already makes re-forwarding structurally impossible for the ordinary
  (single-level pane -> top shell) case — no extra guard was needed in
  `installIngest` itself. Initially wrote a test asserting ingest "must never
  re-forward" under a simulated `IS_EMBED: true` override, but that model was
  wrong: setting `IS_EMBED` true in the single-module test environment models
  a *nested* embedded receiver, for which re-forwarding further up its own
  chain is correct, cascading behavior — not a bug. Replaced it with a test
  that only asserts the realistic case (a top-level, non-embed receiver never
  re-forwards), which is the case that actually occurs today.

## Test environment note (unrelated to this fix, worth recording)

`bun test src/platform/lib/notifications.test.ts` (or
`useTaskStatusNotify.test.ts`) run **alone** crash with
`ReferenceError: location is not defined` inside `router.ts`'s module-init
`rewriteLegacyPath()` — this reproduces identically on an unmodified
`notifications.test.ts` (verified via a WIP stash before making any edits),
so it is a pre-existing environment quirk, not something introduced by this
branch. Root cause: `router.ts` reads `location` at module scope, and one of
this file's own *static* imports (`@platform/lib/jobs` → `api.ts` →
`presence.ts` → `router.ts`) gets hoisted and evaluated before the file's own
`installDomShim()` call runs, despite `installDomShim()` appearing first
textually — static imports always evaluate before a module's own top-level
statements, and `router.ts`'s module cache is shared process-wide, so once
that first, un-shimmed evaluation throws, no shim installed by a *different*
file arriving later can fix it for this file's copy of the module (bun
caches the failed module). Running the whole `src/platform/lib` directory
(or the full suite) works around it, because some other file's static import
chain that resolves `router.ts` first happens to run after DOM globals are
already patched by an earlier file's `installDomShim()` call. Used
`bun test src/platform/lib` and `bun test src/shell` as the actual test
commands for this branch instead of single-file invocations.

---

# Code review round (2026-09-18) — F1, F2, F3 fixes; F4/F5 calls

The orchestrator independently re-verified F1 against this branch's actual
code (not against what section A above claimed) and found section A's
decision wrong: `installIngest` really was calling `notify(input)` directly,
which really does pop a card in the receiving document for every forwarded
message. Section A's own reasoning rationalized this as "the fix actually
delivering on a documented contract" — but the *brief that started this
branch* explicitly said the opposite ("the ingest path must get
family-collapse + retain WITHOUT popping"), and no test enforced the
no-popup behavior either way, so this shipped as a live regression class
(F1) that the previous round's own tests could not have caught. Section A is
**superseded** by this section for the popping question; its reasoning about
id-minting, no-double-forwarding and no-suppression-surprise still holds and
is unaffected.

## F1. Split `notify()` into a shared retain/collapse half and a popup half

Extracted the "mint-or-reuse an id, run family-collapse, update `retained`,
forward a genuinely-new row to the shell" logic out of `notify()` into a new
module-private `retainAndCollapse(input)` — same code, no behavior change for
`notify()` itself. `notify()` now calls `retainAndCollapse` and then does its
own popup bookkeeping (latest-wins swap, exit timer) on top, exactly as
before.

`installIngest` now wires `_fusedIngestNotification` to a new
`ingestNotification(input)` function that calls `retainAndCollapse` directly
and stops — it never touches `popup`, `exitTimer`, or `armExitTimer`. This
is the fix: a pane's own card (already popped by the pane's own local
`notify()` call before it ever forwards) stays the only card for that event;
the receiving document's Notifications panel gets the row (and any
family-collapse/`count` increment against it), but no second popup, and no
"latest wins" eviction of whatever the receiving document was already
showing.

`ingestNotification` still runs `isSuppressed` first, matching what a local
`notify()` call would do at that point — even though it is a structural
no-op today: `forwardToShell` only ever sends `origin`, never `source`, and
`isSuppressed` returns `false` immediately whenever `input.source` is absent.
Kept for parity/future-proofing rather than removed, since it costs nothing
and keeps `ingestNotification`'s contract described honestly as "everything
`notify()` does short of popping," not "everything except suppression too."

Tests added (`notifications.test.ts`): an ingested message is retained
without ever popping; an ingested message does not evict/replace a popup the
receiving document is already showing; two ingested messages sharing a
family still collapse into one row with `count` 2 while neither ever pops.
Ran these against the pre-fix code path mentally (and confirmed by writing
them before the `ingestNotification` split existed) — the "never pops"
assertion fails immediately against `installIngest`'s old
`notify(input)` wiring, which is what makes this a real regression test, not
a tautology.

## F2. Structural self-forward guard in `forwardToShell`

Added `if (window.top === window) return undefined;` as the first line of
`forwardToShell`'s body after its existing `effectiveIsEmbed`/
`effectiveIsTopEmbed` guard. This is the "prefer the structural check" option
the brief called out: `IS_TOP_EMBED` is `IS_EMBED && window === window.top &&
!IS_PREVIEW && !IS_SNAPSHOT`, so a top-level window loaded at an embed URL
with `_preview=1` or `snapshot=1` is `IS_EMBED` but not `IS_TOP_EMBED` — the
existing guard does not fire for it, and `window.top` for that document IS
the document itself. Without this check, `notify()` in that document calls
`forwardToShell`, which calls its own `_fusedIngestNotification`
(`ingestNotification`, post-F1), which — since `isRetained`/collapse always
runs regardless of popping — would forward the freshly-collapsed item again
on every non-collapsed call, recursing until a stack overflow.

The check is deliberately independent of the `IS_TOP_EMBED`/`IS_PREVIEW`/
`IS_SNAPSHOT` combination above it — it covers any future embed variant that
reaches this function while still being its own `window.top`, not just
today's `_preview`/`snapshot` cases.

Test added: `forwardToShell` is exercised with `_setIsEmbedForTest(true)`,
`_setIsTopEmbedForTest(false)` (modeling exactly the "top-level window at an
embed URL with a framing param" case) and `window.top` pointed at `window`
itself, with the module's own `_fusedIngestNotification` wrapped to count
calls. Asserts `notify()` neither throws nor ever calls the ingest handler,
and that the message still lands locally (popup fires) — the guard
suppresses only the self-forward, not the notice itself.

## F3. Narrowed `useTaskStatusNotify`'s embed guard

This branch's own prior commit (`c6b613706`) introduced the bare
`if (IS_EMBED) return;` guard that F3 flags as too wide — it was this
branch's fix for a real bug (N embedded panes duplicating a task notice) but
it over-corrected by also silencing standalone top-embed windows (a Finder
double-click on a `.fused` file, a CLI/deeplink `/explorer/embed/` URL),
which have no parent pane to forward a notice on their behalf and therefore
went from "notifies" to "silently notifies nobody." Changed to
`if (IS_EMBED && !IS_TOP_EMBED) return;`, matching the pairing every other
embed rule in `notifications.ts` already uses (`effectiveIsEmbed() &&
!effectiveIsTopEmbed()`, e.g. `neverExpiresHere`).

Test coverage stays structural, same reasoning as the existing
`useTaskStatusNotify.test.ts` guard test (module-scope `IS_EMBED`/
`IS_TOP_EMBED` cannot be flipped at runtime within one `bun test` process):
added a second structural test asserting the guard body no longer contains
the bare `if (IS_EMBED) return;` spelling and does contain the narrowed one.
Scoped the substring check to the effect body only (between
`useEffect(() => {` and the first `notify(input)` call) rather than the whole
file — this hook's own header comment quotes the OLD guard spelling verbatim
while explaining the fix, and a naive whole-file `not.toContain` check
against that string fails on the comment itself, not the code.

## F4. Cross-pane `forwardedIds` aliasing — judged: acceptable, documented, not fixed

Confirmed the mechanics: pane A and pane B are separate documents, each with
their own module instance of `notifications.ts` (separate `retained`/
`forwardedIds`). If both forward a message landing in the same family (e.g.
two sub-documents watching the identical finished task), the shell's
`retainAndCollapse` collapses them into one row and returns the SAME shell id
to both callers. Each pane's own `forwardedIds` then maps its own local id to
that shared shell id. A's `dismissNotification` calls
`forwardDismissToShell(sharedId)`, which removes the shell's row — silently
also "dismissing" B's still-live, never-actually-dismissed notice from B's
own point of view. Worse, B's own local `retained` entry survives (nothing
told B its shell-side copy is gone), so a subsequent local repeat of the same
family in B hits `collapseIdx !== -1` in B's own document and does not
re-forward — the family is now unreachable in the shell for B's remaining
lifetime, exactly as F4 describes.

**Not fixed in this round.** The brief's own suggested fix — "making the pane
re-forward when its mapped shell id is gone" — requires the pane to *learn*
that the shell discarded the row, which requires a NEW message direction
(shell → pane) that does not exist today: the wire only carries pane → shell
`_fusedIngestNotification`/`_fusedDismissNotification` calls and a bare
return-value id at forward time. There is no channel for the shell to later
tell a pane "the id I gave you for that forward is no longer live" (e.g.
because a DIFFERENT pane dismissed it, or the shell's own `capRetained` cap
evicted it). Building that channel is a real feature (a second same-origin
global, a subscription, or polling `forwardedIds` liveness some other way),
not a review-round bug fix, and risks its own new correctness questions
(ordering, a pane forwarding again into a fresh collapse mid-flight, etc.)
that deserve their own design pass rather than a patch bolted onto this
branch.

Judged acceptable to ship without it because: (1) it requires TWO separate
panes to be showing the identical family AND to be dismissed independently
by the user rather than together — panes showing "the same task's" notice
are typically split views of the SAME session that close together, not
independently dismissed by a user working two different corners of the
screen; (2) the failure mode is a missed/stuck notification, not data
corruption, a crash, or a security issue — the family simply stops
reappearing in the shell for that one pane until it is remounted (a page
reload/pane close-reopen resets its module state and `forwardedIds`); (3)
this exact "two watchers, one shell row" collapse is the FIX this whole
branch shipped (replacing "two watchers, two duplicate shell rows" — which
was strictly worse, since a duplicate row could never be dismissed at all as
one visible unit) — F4's aliasing is a secondary, narrower defect *introduced
by fixing a worse one*, not a regression this round created from a clean
baseline.

## F5. `count` semantics under ingest-side collapse — judged: keep current behavior, documented as a known limitation

`retainAndCollapse` increments `count` identically whether it is called from
a local `notify()` or from `ingestNotification` (ingest, post-F1) — this was
true before F1 too (both paths always shared the same collapse logic; F1 only
split off the popup half). The question F5 raises: when N *different
documents* each forward what is semantically the SAME single real-world event
(the live repro this branch's `acd656d8b` commit fixed — three sub-documents
each observing one task finish), should that count as 1 (one thing happened)
or N (N times this family was reported to the shell)?

**Decision: leave it counting N (current behavior), documented as a known
imprecision, not fixed this round.** Reasoning: the ingest boundary has no
way to distinguish "N documents independently observed the SAME single
event" from "N genuinely separate repeats of this family, forwarded from one
or more panes" — both look identical as N calls to `_fusedIngestNotification`
sharing a family key. Telling them apart requires the sender to pass some
kind of event-identity/idempotency key (e.g. the task's own
`finished_at`/run id) that `NotificationInput` does not carry today, and
`messageFamily`'s whole design is deliberately per-FAMILY (title+caption),
not per-event, so retrofitting an event id changes the shape of the type
every caller passes, not just the ingest path. That is a real design change,
not a review-round fix. In the meantime: `count`'s own doc comment ("how many
times this family has fired") is arguably now imprecise for the multi-watcher
case, but it is not WRONG for the more common case this collapse logic
exists for — two genuinely separate runs of the same task, forwarded once
each — and a `count` of 3 for a triple-observed single event is a cosmetic
overcount on a row that already merged three duplicate rows into one, which
is strictly better than the pre-`acd656d8b` state (three separate,
un-merged rows, i.e. an implicit "count of 3" spread across three cards
instead of one badge). Flagged here for whoever next touches
`NotificationInput`/`messageFamily`, rather than patched blind.

---

# F6. Finished-task notices collapse per folder, not per title (2026-09-18)

User screenshot: two finished-task rows sitting side by side in "Worth
keeping" —

    fused-render / hi          / Finished
    fused-render / New session / Finished

— "these 2 fused render notifications should have been grouped together as
count." Same complaint as the branch's original motivating bug, not fully
fixed by it: `messageFamily`'s default key is `caption:${caption}::${input.
title}` (the DEFECT/2026-09-17 fix documented above), so two finished tasks
in the SAME folder with DIFFERENT titles ("hi" vs. "New session" — a task's
own title, not a fixed label) never shared a family and never collapsed.

## Why an opt-in field, not a looser default

Considered just dropping `title` from the default family key so any two
retained messages sharing a caption collapse. Rejected: that would also
merge an unrelated error and an unrelated info message raised from the same
folder into one row, silently hiding one of them behind a `count` bump —
exactly the failure mode the brief calls out and the existing test "same
caption but different titles stay as two separate rows"
(`notifications.test.ts`) already locks in for the general case. Loosening
the default breaks that test's actual intent even if the literal assertion
happened to still pass for some inputs.

Added `familyKey?: string` to `NotificationInput` instead — an explicit
per-caller opt-in. `messageFamily` checks it first and, when present, uses
`familyKey:${input.familyKey}` outright, skipping the caption/page/title
chain entirely. Only `task-status-notify.ts`'s `in_progress -> done` branch
sets it (`task-finished:${caption}`, only when `caption` is non-empty — a
captionless finished task has no folder identity to key on, so it falls
back to the ordinary chain, matching every other captionless caller's
existing behavior unchanged).

## Why no change was needed for "show the newest title/page"

The brief's design decision — collapsed row shows the most recent task's
title and page, `count` carries the rest — falls out of the EXISTING
`retainAndCollapse` code for free: on a family match it already rebuilds the
stored row from the latest `input` via `toStored`, only carrying `count`
forward (`{ ...base, count: retained[collapseIdx].count + 1, ... }`). This
is the identical mechanism the pre-existing per-run-page collapse (DEFECT,
2026-09-17) already relies on to point a collapsed row at the newer run's
`page`. No new "pick the latest" logic was written — `familyKey` only
changes which rows land in the SAME bucket, not what happens once they do.

## Trade-off, stated plainly

A folder's newest finished task overwrites the title of whatever finished
task was shown before it under the same `familyKey` — "hi" then "New
session" finishing in the same folder ends up as one row reading "New
session" with `count: 2`, not a row naming both. Accepted because this is
exactly the same trade-off the per-run-page collapse above already made
(and shipped) for the identical reason: the row's job is to point at what's
most likely to matter right now (the newest thing), and `count` is what
communicates "there's more than one" — a literal list of every past title
was never how this panel worked even before this fix (a family collapse
always discarded the PREVIOUS row's exact content, this just changes which
rows are considered the same family).

## Tests (TDD, confirmed red before the fix)

Reverted the `messageFamily`/`familyKey`-consuming changes via a tagged WIP
stash (`notif-collapse-fix-wip-verify-red`), ran `bun test src/platform/lib
src/shell`, and confirmed exactly 3 of the newly-added tests failed (the
two direct `familyKey`-collapse tests in `notifications.test.ts` plus one
in `task-status-notify.test.ts`) while every other new test — the
"unrelated non-familyKey notice doesn't collapse" case and the "no caption
-> no familyKey" fallback case — passed unmodified, confirming those are
genuinely independent of the fix rather than tautological. Re-applied the
stash (`git stash apply <sha>`, then dropped it by that sha) to restore the
fix; full targeted run came back to 2629 pass / 0 fail (up from the
pre-change 2624), and `bun run typecheck` is clean.

Added to `notifications.test.ts`: two notices sharing a `familyKey` but
different titles/pages collapse into one row with `count: 2`, showing the
newer title and page; a `familyKey`-bearing notice and an unrelated non-
`familyKey` notice sharing a caption do NOT collapse; the same collapse via
`_fusedIngestNotification` (ingest path), confirming `retainAndCollapse` —
shared by `notify()` and `ingestNotification` since F1 — carries the field
through the ingest boundary with no special-casing needed. Added to
`task-status-notify.test.ts`: the `in_progress -> done` branch sets a
non-empty `familyKey` shared across two different-titled tasks in the same
folder, and sets no `familyKey` at all when there's no caption to key on.

## What the brief got right / nothing found wrong

The brief's read of the bug (caption+title family key, title differs across
finished runs, never collapses) matched the code exactly on inspection — no
correction needed here, unlike some earlier rounds' DECISIONS entries.

# F7. Scope finished-task notices to fused-render's own sessions (2026-09-18)

## The bug

`useTaskStatusNotify` watches every session `~/.claude/projects` holds — the
whole machine-wide pool, including a `claude` session someone starts by hand
in a plain terminal, unrelated to fused-render entirely. Every one of those
now raises a fused-render "Finished" notice too, because the poll behind it
(`/api/tasks/pulse`, tasks.py) has never distinguished "a chat opened from
our own Claude template" from "any transcript this machine happens to have."

## The signal, and why it can only ever be a proxy

Every Claude Code transcript's `type: "user"` records carry an `entrypoint`
field — `"cli"` for an interactive terminal, `"sdk-cli"` for a headless or
programmatic spawn, which is what `templates/claude/agent.py`'s own spawn
produces. That is the entire signal available: there is no field that says
"opened from fused-render" directly. An unrelated SDK-driven session (some
other tool's own headless Claude spawn) also reports `"sdk-cli"`, so this
can never be tightened into "only notify on our own template's sessions" —
only into "also notify on the ones we're fairly confident are someone's own
terminal, unless they've said they want those too." That imprecision is
exactly why this round adds an opt-in preference rather than trying to make
the classifier exact.

## Server placement: `tasks_store.py`, not `claude_sessions.py`

The brief pointed at `fused_render/server/routers/claude_sessions.py`'s
`_parse_head`/`_head`/`_HEAD_CACHE` as the starting point, and at
`tasks.py` around line 439. Neither was quite right, and the brief itself
warned this file has two independent, near-duplicate head-parsers with a
documented history of drifting apart — exactly the trap it named.

Tracing what actually builds a `Task` row: `tasks.py`'s `_place()` (the
function `_row()`'s caller feeds project/target/order into) calls
`tasks_store.head(task["path"])` — `fused_render/tasks_store.py`'s OWN
`head()`/`_parse_head()`, a separate implementation from
`claude_sessions.py`'s. `claude_sessions.py`'s pair only feeds the unrelated
`/api/claude-sessions/summaries` endpoint (the session picker), never the
Task/TaskPulseTask row the notification pipeline reads. Extending
`claude_sessions.py` would have created a THIRD independent entrypoint
reader alongside the two already-diverging head-parsers, the opposite of
"one source of truth." Extended `tasks_store.py`'s `_parse_head()`/`head()`
instead: `entrypoint` is read off the exact same `type: "user"` record
`cwd`/`first_ts`/`prompt` already come from, at no extra IO, appended as a
5th tuple element (`_HEAD_CACHE`'s cache-tuple shape grew to match). Two
production call sites needed their unpacking updated
(`tasks_store.backfill()`, `tasks.py`'s `_place()`); three existing
`test_tasks_store.py` tests that unpack the tuple directly needed a 5th
target added.

`_place()` sets `task["entrypoint"]` (never defaulted — `None` when the
transcript has none or predates the field). `_row()`'s return dict adds
`"entrypoint": task.get("entrypoint")` (`.get`, not `[...]`, since a draft
row is built by a separate function that never calls `_place()`).
`_PULSE_FIELDS` adds `"entrypoint"` so `/api/tasks/pulse` carries it too —
that dict comprehension indexes with `row[field]`, not `.get`, so every row
`_row()` builds must always carry the key, even as `None`.

## Frontend gate: exact `"cli"`, fails open on everything else

`task.entrypoint === "cli"` is the ONLY value the gate treats as
"interactive terminal, unless the preference says otherwise" — not
"anything that isn't `sdk-cli`". `undefined`/`None` (no transcript yet, an
older session, a head read that raced the write) fails OPEN and still
notifies, exactly as every task did before this branch. Narrower-than-
"not sdk-cli" deliberately: the whole premise of the preference is that
`"sdk-cli"` is a guess, not proof, so treating every unlabeled case as
"probably interactive" would silence sessions this signal was never
confident about.

`notificationForTransition` stays pure (its whole point, per this file's
own header) — the preference is threaded in as a 4th argument
(`notifyTerminalSessions = false`), not read off a module inside the
function. `useTaskStatusNotify.ts` owns the one live subscription
(`useTaskNotifyTerminalSessions`, a new `task-notify-terminal-flag.ts`
module cloned from `task-card-title-flag.ts`'s tri-state idiom: shared GET,
generation guard, fails to `false` on any read failure) and passes the
current value in on every tick, added to the effect's own dependency array
so flipping the Preferences toggle takes effect without a reload.

Scoped to ONLY the `in_progress -> done` branch, both the direct-transition
path and the first-sighting-but-after-watch-start synthesized path (both
flow through the same branch) — `needs_attention` and
`in_progress -> blocked` are untouched, matching the brief.

## Preference: `task_notify_terminal_sessions`, default off

New `fused_render/shell/prefs.py` getter `notify_terminal_sessions_enabled()`,
same off-by-default idiom as `task_card_last_message()` (only a stored
`true` is on). Wired into `_prefs_response()`'s new `"task_notify"`
namespace and `put_prefs()`'s body handling, with the key added to the
"no known preference" error's list. Frontend: `Prefs.task_notify?.{
terminal_sessions: boolean }`, `putTaskNotifyTerminalSessionsEnabled()` in
`api.ts`, and a new `TaskNotifyTerminalSection` in `Preferences.tsx`
("Tasks: notify when terminal sessions finish") right after
`TaskCardTitleSection`, cloned down to the busy/error/toggle shape. The
label and one-line explanation say plainly that fused-render can't tell
every headless session apart from its own.

## Tests (TDD, confirmed red before the fix)

Python: `tests/test_tasks_store.py` — extended `_transcript()`'s helper
with an optional `entrypoint` param; fixed the 3 tests broken by `head()`'s
tuple growing from 4 to 5 elements; added 4 new tests (`"cli"` and
`"sdk-cli"` read correctly, an absent field answers `None`, and the answer
is cached alongside the rest of the head so a later append with a
DIFFERENT entrypoint value doesn't retroactively change an already-resolved
read). `tests/test_tasks_api.py` — extended `_user()` with an optional
`entrypoint` param; added 3 new tests asserting `entrypoint` on both
`/api/tasks` and `/api/tasks/pulse` for a `"cli"` session, an `"sdk-cli"`
session, and a session with none; updated the pre-existing
`test_sidebar_pulse_is_the_compact_projection_of_the_task_rows`'s own local
`pulse_fields` tuple to include `"entrypoint"` (it independently rebuilds
`_PULSE_FIELDS`' field list and broke the moment the real one grew).
`tests/test_shell_prefs.py` — 3 new tests cloned from the
`task_card_last_message` trio (defaults off and toggles both ways; a junk
stored value reads as off; a non-boolean PUT is rejected with no write).
Ran `pytest tests/test_tasks_api.py tests/test_tasks_store.py
tests/test_claude_sessions_api.py tests/test_claude_sessions_merged.py -q`:
400 passed. Ran `pytest tests/test_shell_prefs.py -q`: 87 passed.

Frontend: `task-status-notify.test.ts` — 4 new tests (a `"cli"` task
notifies nothing with the preference off, and does with it on; an
`"sdk-cli"` task notifies either way; a task with no `entrypoint` notifies
either way — fails open; `needs_attention`/`in_progress -> blocked` are
unaffected by `entrypoint`). New `task-notify-terminal-flag.test.ts` (cloned
from `task-card-title-flag.test.ts`'s "the flag module" describe block,
plus a subscribe/publish round-trip). Confirmed each of these red first
against the pre-implementation code (the gate didn't exist; the flag module
didn't exist) before writing the corresponding implementation. Ran
`bun test src/platform/lib src/shell` from `frontend/`: 2638 pass, 0 fail,
9444 expect() calls across 85 files (up from the pre-change file/test
counts by the 2 new files and their tests). `bun run typecheck`: clean.

## What the brief got right / where it needed correcting

Right: the signal itself (`entrypoint`, `"cli"` vs `"sdk-cli"`, imperfect by
construction), the fail-open requirement, scoping to only the finished
transition, the pure-function-with-threaded-argument shape, the
Preferences pattern to clone, and the overall TDD/testing bar.

Needed correcting: the specific starting-point pointer
(`claude_sessions.py`'s `_parse_head`/`_head`/`_HEAD_CACHE`, and `tasks.py`
line ~439) named the wrong one of the two existing head-parsers. The single
source of truth for a Task row's project/target/entrypoint is
`tasks_store.py`'s own `head()`/`_parse_head()`, called from `tasks.py`'s
`_place()` — that is where this round's server-side change actually
landed, and doing it there (rather than at the brief's literal pointer) is
what keeps this at ONE entrypoint reader instead of a third, independently-
drifting one.

# F8. Popup-only suppression for a finished task whose own app/chat is already open (2026-09-18)

## The bug, restated from a screenshot

The shell open on `/Fused/sandbox/Akshil/virtual-office/index.html`, the
Claude side panel showing that exact session (T001 "Virtual office"), and
"Notifications 2" lit in the status bar — the user staring straight at the
finished run and still being told about it. Verbatim: "we never want to
show notifications for tasks when the claude template / app is already
opened."

## The THIRD position this file has taken on presence suppression, and why the answer keeps changing

F7's own header (above) already documents two earlier reversals on this
exact question. Restated so this round's answer reads as a continuation,
not a contradiction:

1. Originally: presence-suppress the finished-task notice (`source:
   taskSource(task)`), the same lever every other message uses.
2. 2026-09-17, first reversal: dropped presence suppression entirely — "a
   finished run is exactly the moment someone wants to jump back into it,"
   the run has ENDED so "already looking at the chat" no longer means
   "already knows it's done" for a job that's still running.
3. This round, second reversal: the user's screenshot is a case position 2
   didn't anticipate — not "already looking at the chat mid-run," but
   *the chat is open at the exact moment the finish notice fires*, so the
   user genuinely does already know. What changed the answer is that
   specific: presence at the moment of completion, not presence generically.
   Position 2's own reasoning ("the run has ended, so already-looking no
   longer means already-knows") is still correct for the general case — a
   window left open on that folder an hour ago, long since abandoned, tells
   you nothing about whether the user saw THIS run finish. It just doesn't
   cover the live-panel-open-right-now case the screenshot shows, which is
   `isOpenAnywhere`'s ordinary, current-and-non-stale presence check, not a
   memory of "this page was ever open."

Nothing about F7's `source`/`origin` split is touched: this branch still
sets `origin`, never `source`, for the caption. The suppression check below
is a wholly separate lever (`quiet`), consulted independently.

## Why popup-only, not full suppression

The brief's first draft (mine, before the user corrected it) suppressed the
notice outright — no popup, no retained row — on the theory that "the chat
itself is the notification." The user's correction: "by show I mean
popup, if that is not simple enough to do just dont have a notification."
Popup-only turned out simple (a handful of lines in `notify()`), so that's
what shipped: a finished task whose destination is already open still gets
a retained Notifications-panel row (the user may navigate away before
dismissing it, and the row is what lets them find their way back later —
exactly the reasoning F7's own second reversal already established for why
this branch carries a `page` at all), it just never interrupts with a card
for something already on screen.

## `quiet` as an explicit opt-in field, not a tier

`isRetained()`/`resolveTier()` already have a `tier` vocabulary
(`"silent"`, `"attention"`, `"transient"`) inherited from `jobs.ts`'s
`JobTier`. Reusing one of those was rejected: `"silent"` also kills
retention (`isRetained` returns false for it) — wrong, we want the row kept.
Inventing a new tier value would mean threading a whole new case through
`resolveTier`, `isRetained`, and every switch over `JobTier` in `jobs.ts`
that this module shares that type with, for a distinction (`pops` vs.
`doesn't pop`) that is orthogonal to retention, not another point on the
same axis. A plain boolean (`NotificationInput.quiet`) checked once, after
`retainAndCollapse()` in `notify()`, keeps the two axes (retained vs. not;
pops vs. not) independent instead of conflating them into a bigger tier
enum.

`notify()`'s fresh-item path now branches on `input.quiet` immediately
after `retainAndCollapse()`: quiet returns the same `id` after only
`refreshSnapshot()`/`emit()` (mirrors `ingestNotification`'s own
retain-without-pop shape for the pane→shell forwarding case — the same
"retain but don't pop" need, reached from a different boundary). Non-quiet
falls through to the existing "latest wins" popup-arming code unchanged.
`retainAndCollapse()` itself is untouched — a quiet notice still competes
for the same `familyKey`/count-collapse as a normal one (asserted directly:
a quiet second finished-task notice in the same folder still updates the
existing row's count, and does not disturb whatever the FIRST notify()
already popped).

## Where the check lives, and why it stays out of `notificationForTransition`'s purity

`notificationForTransition` (task-status-notify.ts) stays pure, per its own
header comment and per F7's already-established pattern: `notifyTerminalSessions`
is threaded in as a plain argument rather than read off a live subscription
inside the function. This round adds a second injected predicate,
`isDestinationOpen: (page: string) => boolean`, the same shape (and the
same reasoning) `jobs.ts`'s `isPopupSuppressed(job, isOpenAnywhere)` already
uses. `useTaskStatusNotify.ts` calls `snapshotIsOpenAnywhere()` ONCE per
poll tick (not once per task inside the loop — `snapshotIsOpenAnywhere`'s
own doc comment names exactly this N-calls-per-tick cost) and passes the
resulting closure in on every `notificationForTransition` call that tick.

Scoped to ONLY the `in_progress -> done` branch, and only after the F7
terminal-session gate has already had its say (a `cli`-entrypoint task with
the preference off returns `null` before the `isDestinationOpen` check is
even reached — the two gates compose, they don't race). `needs_attention`
and `in_progress -> blocked` are untouched, matching the brief: a task that
fails or parks on a question must still speak up even with its app open —
"already looking at the folder" is not "already knows it just failed" the
way it is for a finished run whose chat panel is showing the finish.

## The `/tasks` fallback exclusion

`taskDestination(task)` is `taskHref(task) ?? folderHref(task) ?? "/tasks"`
— the last-resort route for a task with no session AND no project/target.
Suppressing the popup whenever `isDestinationOpen("/tasks")` is true would
go quiet for EVERY such task the instant anyone has the Tasks page open,
which has no relation to that specific task at all — the exact over-wide
suppression the brief warned against. The check is skipped outright
(`destination !== "/tasks" && ...`) rather than relying on `isDestinationOpen`
to somehow answer "false" for that case.

## The normalization gap: `taskDestination()` and presence entries do not share a string shape

This was the single largest risk named in the brief, and it is real — verified
empirically, not assumed:

- `taskDestination(task)` (via `taskHref`/`folderHref` → `explorerUrl` →
  `queue.chatUrl`) returns a full shell HREF: `/explorer/view/<encoded fs
  path>?_side=claude&session_id=<id>` (or `&session_id=` empty for the
  folder-only fallback). Printed for a real session-backed task in an app
  folder: `/explorer/view/Fused/sandbox/Akshil/virtual-office/index.html?_side=claude&session_id=sess-abc123`.
- `currentPresencePage()` (presence.ts) — what a live window actually
  publishes into the shared registry when it has that page open — is
  `fsPathFromLocation()`: a BARE fs path, prefix and query both stripped.
  For a window sitting on the same page: `/Fused/sandbox/Akshil/virtual-office/index.html`.
- `matchesSource(published, raw)` between those two strings, confirmed by a
  throwaway test run against the real functions (not a hand-built fixture):
  **`false`**. The row's own destination would never have matched anything,
  ever — exactly the "silently no-ops" failure mode the brief was most
  worried about, and it would have shipped invisibly since nothing else
  exercises this pairing.
- `recentFsPath(url)` (`apps/explorer/lib/recents.ts`, already used by
  `Home.tsx`/`FilesHome.tsx` to turn a recorded recent-file URL back into a
  stable fs-path identity) does exactly the decode `fsPathFromLocation()`
  does, but as a pure function of a STRING instead of `location`: strips the
  query, strips a `VIEW_PREFIX`/legacy `/view/` prefix if present
  (`rootedFsPath` + decode), and falls through to the bare pathname
  unchanged when there's no such prefix — which is what makes `/tasks`
  round-trip as `/tasks` rather than being mishandled. Run against the same
  real `taskDestination()` output: `recentFsPath(raw)` =
  `/Fused/sandbox/Akshil/virtual-office/index.html` — an EXACT match for
  `currentPresencePage()`'s own output for that same window, and
  `matchesSource(published, normalized)` confirmed `true`. `taskDestination()`
  is now always run through `recentFsPath()` before being handed to
  `isDestinationOpen`, both at the shell (VIEW_PREFIX) and embed cases —
  presence entries are always written in bare-fs-path form regardless of
  which prefix the writing window itself loaded under, so normalizing only
  the READ side (the destination string) is sufficient; nothing on the
  write side needed to change.
- No boundary violation: `recentFsPath` lives under `apps/explorer/lib/`,
  and `shell/**` (where `task-status-notify.ts` lives) may import anything
  (`scripts/check-boundaries.mjs`'s own stated rule) — confirmed by running
  `bun run check:boundaries` clean after the import. Reusing it also keeps
  this at ONE decoder for "HREF with a VIEW_PREFIX/query -> bare fs path"
  instead of writing a second copy of `fsPathFromLocation()`'s own decode
  logic a third time in this codebase.

## Tests

Full TDD, confirmed red before each fix (not merely written and left to
pass): `notify()`'s `input.quiet` branch was stubbed out with `if (false &&
input.quiet)` and the run showed exactly the 2 quiet-specific tests failing
(the "false pops normally" test stayed green, correctly, since it exercises
the unaffected default path) before being restored. `task-status-notify.ts`'s
`const quiet = ...` line was hardcoded to `const quiet = false` and the run
showed exactly the 2 tests asserting `quiet: true` failing, while the
`/tasks`-fallback, blocked/needs_attention, and F7-composition tests stayed
green (as they should — they assert `quiet` is falsy, or that the branch
returns `null` before `quiet` is even computed).

`notifications.test.ts`: quiet retains without popping; quiet omitted/false
pops as before (unaffected-path regression guard); a quiet notice still
collapses into an existing `familyKey` row and increments `count`, without
disturbing whatever the first, non-quiet `notify()` call already popped.

`task-status-notify.test.ts`: a finished task with its destination open
sets `quiet: true` and is otherwise a completely normal retained/clickable
row; the injected predicate receives the `recentFsPath`-normalized
destination, not the raw querystring-bearing href (asserted directly, with
a companion assertion that the raw destination actually differs from what
the predicate saw — proving the normalization step does real work rather
than happening to be a no-op for this fixture); the same task pops normally
when nothing is open; a blocked task and a needs-attention task are
unaffected even when the predicate reports "open"; a task whose destination
falls through to `/tasks` still pops even with `/tasks` reported open; no
predicate passed at all defaults to "nothing open" (existing lower-arity
call sites keep compiling and behaving as before); the F7 terminal-session
gate and this gate compose (a `cli`-entrypoint task with the preference off
stays fully silent regardless of what the presence predicate says).

Ran `bun test src/platform/lib src/shell` from `frontend/`: 2771 pass, 0
fail, 9893 expect() calls across 90 files (up from the pre-round 2760/9819
by the 11 new tests above). `bun run typecheck`: clean. `bun run
check:boundaries`: clean (846 files) — checked explicitly given the new
`shell -> apps/explorer/lib/recents` import this round adds.

## What the brief got right / where it needed correcting

Right: open-anywhere (not merely focused-here) via `isOpenAnywhere`/
`snapshotIsOpenAnywhere`; keeping `notificationForTransition` pure with an
injected predicate, mirroring `jobs.ts`'s `isPopupSuppressed` shape;
scoping to the finished transition only; excluding the `/tasks` fallback;
and — the brief's own explicit warning — that the destination/presence
string-shape mismatch was the one thing most likely to make this silently
no-op. It was real, and the brief was right to gate the whole task on
checking it empirically rather than trusting a hand-built test fixture.

Needed correcting, both from the user directly rather than found by
inspection: the brief's original ask was full suppression (no popup, no
row); the user's own follow-up narrowed "show" to mean "popup" specifically,
which is what shipped instead. And the brief suggested `isPopupSuppressed`
in `jobs.ts` as the pattern to mirror for the injected-predicate shape,
which held up exactly as described once written.
