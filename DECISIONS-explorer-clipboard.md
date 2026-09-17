# Decisions — explorer clipboard fixes

Notes for whoever picks this up next: what was decided, why, and the couple of
places the brief's letter needed a judgment call to apply.

## Fix 1 — folder links navigate in-app

- `spaLinkProps` lives in `platform/lib/router.ts`, next to `navigateUrl`.
  `check-boundaries.mjs` passes unchanged (`boundaries OK (804 files)`) —
  every touched file already imported from `@platform/lib/router`, so this
  added no new edge.
- The event parameter is typed structurally (button/metaKey/ctrlKey/
  shiftKey/altKey/defaultPrevented/preventDefault), not as `React.MouseEvent`
  — `router.ts` is documented UI-free and has no React import anywhere else.
- `AiModelsPage.tsx:176` and the app-name row already had a *correct*
  modifier-click guard, hand-rolled. Refactored onto `spaLinkProps` anyway,
  per the brief's "exactly one copy of this logic. Do not leave the old
  inline version behind anywhere" — these were not bugs, just duplication.
- `ModelRow.tsx`'s "Open model card" link *was* a real bug: its `onClick`
  called `preventDefault()` unconditionally, breaking middle-click / cmd-click
  before this fix touched it.
- `AppFiles.tsx` / `AppApi.tsx` had a `folderHref` prop used at exactly one
  call site each (the "Open the folder" anchor). Deleted the prop entirely
  rather than leaving it half-migrated (house rule: correctness over
  compatibility, delete stranded code) — confirmed via grep that `AppPage.tsx`
  is the only renderer of either component.
- `PluginsSection.tsx` was audited and deliberately left alone — its anchor
  carries `target="_blank"`, so a plain click is not the browser's default
  in-app navigation to intercept.

## Fix 2 — sessionStorage persistence

- Followed the `side-store.ts` / `side-store.test.ts` precedent exactly:
  validate-on-read, try/catch every storage access, seed a module-level `let`
  at load time, and in tests install a fake `Storage`-shaped object on
  `globalThis` *before* the module's static import (the seed runs at load),
  then use a `?seed=N` dynamic-import trick to get a fresh module instance
  per test that needs to observe the seed.
- `clipboard` and `lastSeenOsToken` are stored as **one JSON blob under one
  key** (`fused-render:explorer-clipboard`), not two separate keys. The brief
  called persisting them together "not optional and not incidental" — one
  key is what makes that true by construction rather than by convention (two
  keys could still be read at two different moments and observed out of
  step, even if every write happened to touch both).
- `persist()` is called from `setClipboard` (including the `null` case) and
  from `commitOsToken`. `commitOsToken` has an early-return guard (a stale
  ticket); persistence only happens on the branch that actually commits.
- Validation (`parseStoredClipboardState`) treats a non-array `paths`, an
  `op` outside `"copy"|"cut"`, or an empty `paths` array as an invalid
  clipboard and falls back to `null` — the `lastSeenOsToken` half of the pair
  is validated and kept independently, since a corrupted clipboard says
  nothing about whether the token is trustworthy.
- Test isolation gap found during this fix: the existing `fs-clipboard.test.ts`
  suite never reset the module-level `lastSeenOsToken` between tests in its
  `beforeEach`/`afterEach` (only `setClipboard(null)`). That was invisible
  before because nothing asserted `getLastSeenOsToken() === ""` at the start
  of a test; the new "fresh module starts empty" persistence test does, and
  failed until `setLastSeenOsToken("")` was added to both hooks (matching the
  convention `os-clipboard.test.ts` already used for the same reason).

## Fix 3a — reconcileOsClipboard reordering

- Implemented exactly the order the brief specified: `!os.supported` →
  epoch guard → token guard → commit the token → branch on
  `os.paths.length === 0`.
- The token is committed **before** the empty-paths branch, not skipped for
  it — otherwise the next reconcile would see the same (never-recorded)
  token as "unchanged" and skip an emptied clipboard forever instead of just
  until the next real change on the OS side.
- The empty-paths branch only ever clears a pending `"copy"`
  (`setClipboard(null, false)`); a `"cut"` is left completely alone, in every
  branch of this function, matching the invariant "a cut is app-local state"
  — nothing on the OS clipboard, including its being empty, can confirm or
  refute a cut that was never published to it.
- The existing test `os-clipboard.test.ts`'s "skips an empty OS clipboard"
  (asserting a cut survives an empty read) passes unmodified — it happens to
  also short-circuit on the unchanged-token guard (both `lastSeenOsToken` and
  the stubbed token are `""`), so it never even reaches the new branch, but
  the outcome it asserts is unaffected either way.

## Fix 3b — clearing a pending copy on a text write

- The single shared place required by the brief is `fs-actions.ts`'s
  `copyToClipboard` export. It used to be a bare re-export of
  `@platform/lib/clipboard`'s `copyToClipboard`; it is now a wrapper
  (`writeSystemClipboard` is the renamed platform import) that clears a
  pending copy on a successful write and passes the boolean through
  unchanged.
- This one seam covers every explorer call site with **no changes to any of
  them** — `useFileOps.ts`'s `doCopyPath`/`doCopyPaths`/`doOpenInClaude` and
  `Preview.tsx`'s `doCopyPath`/`doOpenInClaude`/the trouble-report "Copy
  details" action all already imported `copyToClipboard` from
  `@apps/explorer/lib/fs-actions`, not from the platform module directly.
  Confirmed via grep before writing the fix, rather than editing each site.
- The platform module (`@platform/lib/clipboard.ts`) is unchanged and stays
  ignorant of the explorer's clipboard — it's shared with the app-card
  context menu in another app, which may not import `apps/explorer`, so the
  rule could not live there without violating the boundary layering.
- The trouble-report "Copy details" call in `Preview.tsx` (line ~2677) is not
  a file-path copy in spirit, but it is still a text write to the same OS
  clipboard, so it goes through the same wrapper and clears a pending copy
  like every other site — there is no special-casing by call site, only by
  outcome (success/failure) and by clipboard op (copy/cut).

## Test/verification state at handoff

All three fixes are committed as separate commits on
`worktree-explorer-clipboard-fixes`. Final scoped state:
- `bun test src/apps/explorer` — 1234 pass, 0 fail
- `bun run typecheck` — clean
- `node scripts/check-boundaries.mjs` — `boundaries OK (804 files)`

Out of scope, untouched, exactly as instructed: the two pre-existing
failures under `src/apps/claude/` (`ChatMount.render.test.tsx`, a
preview-hover test), the Linux single-clipboard-target limitation, and all
Python code.
