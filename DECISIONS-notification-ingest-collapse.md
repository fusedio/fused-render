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
