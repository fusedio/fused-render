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
