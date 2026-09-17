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

## Fix 4 — spaLinkProps/navigate had two encodings of one destination

- `router.ts`'s `spaLinkProps` built `href` from `opts.search` alone while its
  own `onClick` serialized `mode`/`sel`/`q` through `navigate` — a caller that
  used the typed `mode`/`sel`/`q` options (ModelRow's "Open model card") got
  an anchor whose href disagreed with what a left-click did. Middle-click and
  Cmd-click landed on the model's default template mode instead of the model
  card.
- Fixed at the root: `destOptsQuery` is now the one place `mode`/`sel`/`q`
  become a query string, used by both `navigate` and `spaLinkProps`. A caller
  that only needs `mode`/`sel`/`q` no longer has anywhere to drift; `opts.search`
  stays as an escape hatch for a destination not expressible that way (none
  today) and wins outright over the derived query when given, rather than
  merging with it.
- `router.test.ts`'s "opts (mode, sel, q) pass through to navigate" test never
  checked `href`, so it passed while asserting the very split that was the
  bug. Rewritten to assert `href` and the click destination are the same URL.

## Fix 5 — copyToClipboard's clear was unguarded across its own await

- `fs-actions.ts`'s `copyToClipboard` wrote to the system clipboard, then
  cleared a pending copy on success — with nothing pinning "the copy I'm
  about to clear" to "the copy that was pending when I started the write".
  A slow `navigator.clipboard.writeText` (a permission prompt can gate it for
  seconds in Firefox/Safari) racing a fresh Cmd+C meant the old write's
  resolution could wipe a newer copy the user made in the meantime, even
  though the OS clipboard still held that newer copy's files.
- Fixed with the same `getClipboardEpoch()` guard the focus-time reconcile
  already uses across its own `await` — capture the epoch before the write,
  skip the clear if it moved. No new mechanism.

## Fix 6 — a failed clipboard persist write left stale data in place

- `fs-clipboard.ts`'s `writeStoredState` swallowed a failed `setItem` and left
  whatever was written last time still under the key — a quota failure (a
  large multi-select copy serializes to a large blob) or storage revoked
  mid-session degraded to STALE persistence, not no persistence: the next
  reload restored a clipboard/token pair the user had since replaced or
  explicitly cleared, inverting the guarantee `persist()`'s comment claims.
- Fixed with a best-effort `sessionStorage.removeItem` on the catch path,
  itself wrapped in try/catch since it can fail for the same reasons
  `setItem` did.

## Known limitations documented, not fixed (findings 4, 5, 7 — comments only)

- **Linux: an empty OS-clipboard read can mean the publication died, not just
  that a copy expired.** There is no OS-owned clipboard on Linux —
  `_linux.write_files` forks `xclip`/`wl-copy` to hold the selection — so if
  that helper process dies (server restart, the app quitting and taking its
  process group with it, session cleanup) `read_files()` comes back `[]` with
  a changed token, same as any other empty clipboard. The reconcile in
  `os-clipboard.ts` treats this as "the copy is gone," which is the correct
  call under the invariant (a copy is only a view of the OS clipboard; if the
  view's target disappeared, the copy is gone either way) — but the file list
  it drops is still perfectly pasteable in-app. Documented at the branch in
  `os-clipboard.ts`, not changed.
- **sessionStorage clones into a child tab.** The header comment in
  `fs-clipboard.ts` used to justify sessionStorage with "a cut made in one tab
  has no business reappearing in another" — that isn't what sessionStorage
  does. Chrome and Firefox clone it into a tab opened FROM the current one
  (`target=_blank`, middle-click, Cmd-click on a link) — exactly the gesture
  every folder link in this app offers via `spaLinkProps`. Cut three files,
  middle-click a folder card, and the new tab starts with the same pending
  cut and cut-dimming; paste there and the files move, while the original tab
  still shows a cut whose sources are gone. The two tabs' clipboards diverge
  independently from that point — nothing coordinates them, and nothing
  should (out of scope, per the brief). sessionStorage is still the right
  choice over localStorage, which would share one clipboard across every
  window including long-dead ones. Comment corrected in `fs-clipboard.ts` to
  state this rather than the false "one clipboard per window" claim.
- **`copyToClipboard` in `fs-actions.ts` only covers explorer-originated
  writes.** Its comment claimed to be "the one place the rule has to live";
  it is, but only for text writes that originate in the explorer. Four other
  SPA surfaces write straight to `@platform/lib/clipboard` without clearing a
  pending explorer copy: the app-card context menu's "Copy path"
  (`platform/lib/appCardMenu.ts`, used by `apps/builder/Apps.tsx`),
  `shell/TaskPeek.tsx`'s "copy resume command", and
  `apps/claude_config/sections/SkillsSection.tsx` and
  `PluginsSection.tsx`. None of these move focus away from the window, so
  `os-clipboard.ts`'s reconcile never runs to catch the mismatch either — the
  explorer keeps offering a Paste whose file flavor is already gone until the
  next focus change. Routing those four call sites through an explorer-aware
  wrapper is follow-up work, not part of this branch; the comment now says
  what is actually true instead of overclaiming coverage.

## Test/verification state at handoff

All six fixes (three original + fixes 4/5/6 above) are committed as separate
commits on `worktree-explorer-clipboard-fixes`. Final scoped state:
- `bun test src/apps/explorer src/platform/lib/router.test.ts src/apps/ai_models` —
  1896 pass, 0 fail
- `bun run typecheck` — clean
- `node scripts/check-boundaries.mjs` — `boundaries OK (804 files)`

Out of scope, untouched, exactly as instructed: the two pre-existing
failures under `src/apps/claude/` (`ChatMount.render.test.tsx`, a
preview-hover test), the Linux single-clipboard-target limitation, cross-tab
clipboard coordination, routing the four uncovered call sites (finding 7)
through an explorer-aware wrapper, and all Python code.

## Fix 7 — Linux multi-target clipboard write (backend, `SPEC-linux-multitarget-clipboard.md`)

- Built per the spec on `worktree-explorer-clipboard-fixes`, no deviations —
  every fact the spec listed as verified held on re-check, including the
  end-to-end read-back against a real `wl-paste` (all four targets present,
  bytes matching the spec's table).
- `_write_via_owner`'s Popen uses `stdin=subprocess.PIPE, stdout=subprocess.PIPE,
  stderr=subprocess.DEVNULL` — stdout stays a pipe here, unlike the fallback
  write's `DEVNULL`, because we genuinely need to read one line off it (the
  readiness handshake). The pipes-and-forking trap the fallback's comment
  describes doesn't recur the same way here since the owner doesn't fork —
  but it's every bit as resident, so the same fix applies from the *owner's*
  side instead: it dup2's fd 1 onto `/dev/null` itself right after printing,
  so nothing is left for anyone to block on afterward. The parent still
  closes its own read end once it has the line, purely as pipe-count
  hygiene for a long-lived server process, not because leaving it open
  would hang anything.
- The interpreter-probe cache (`_gi_interpreter`) and the owner-spawn path
  both go through the exact same `_linux.subprocess.run`/`.Popen` seam the
  existing fallback tests fake — meaning every one of those existing tests'
  first `write_files()` call would otherwise also trigger a probe, consuming
  a slot in their `run.calls` recorder and shifting positional assertions
  like `run.calls[0][0][0] == "wl-copy"` by one. Fixed with an autouse
  fixture in `test_pasteboard_linux.py` that pins the cache to "no capable
  interpreter" before and after every test; the handful of tests that
  actually exercise probing/spawning opt back in with
  `_linux._reset_gi_interpreter_cache()` explicitly, and the autouse
  teardown always restores the pinned value afterward regardless of what
  they left it as — so no ordering assumption between tests is load-bearing.
- `_probe_gi_interpreter`'s candidate `/usr/bin/python3` and
  `shutil.which("python3")` can collide (a normal desktop install commonly
  has `/usr/bin/python3` be exactly what `which` finds), which the dedup-by-
  set candidate list already handles; the test for the third candidate
  therefore monkeypatches `shutil.which` directly to a distinct fake path
  rather than going through the shared `env` fixture's `f"/usr/bin/{name}"`
  fake, which would have produced the same string as the second candidate
  and proven nothing.
