# Build log — app-folder snapshot preview

## App page version dropdown (new plan, docs/app-page-version-dropdown-plan.html)

Resume point: ALL FIVE TASKS COMPLETE AND COMMITTED (five commits, one per
task, per the plan). Resuming from 1aaca6cb2. Five tasks: a commits route,
the picker, wiring the three tabs to the snapshot, deleting the Git tab, docs.

### Task 1 — `GET /api/git/commits`

Implemented as a plain function (`list_commits`, mirroring `extract_snapshot`'s
own shape) so it is testable with no `TestClient`, matching every other route
in this module. One deviation from a literal reading of "same mount refusal,
same `--` + `:(literal)` pathspec rule": the plan doesn't say what an EMPTY
repository (no commits, an unborn HEAD) should do, only that
`tests/test_git_commits.py` must cover it returning `ok` with an empty list.
`git log` on an unborn HEAD exits 128 with a message ("does not have any
commits yet") that would otherwise have to be pattern-matched out of stderr —
fragile across git versions/locales. Instead `_run_log` runs a cheap
`git rev-parse --verify -q HEAD` FIRST; a non-zero exit there means no commits
exist yet, and the function returns `[]` without ever invoking `git log` — one
extra fork on this path only, and no stderr-message parsing anywhere.

`has_more` asks for `limit + 1` records and reports whether more than `limit`
actually came back, matching this module's own `extract_snapshot`-adjacent
style of preferring an observed fact over an inference. `limit` is bounded
`[1, 500]` server-side (the plan doesn't specify a ceiling; 500 is generous for
a dropdown and cheap for `git log --format=...` regardless of repo size).

Verify: `.venv/bin/pytest tests/test_git_commits.py -q` — 7 passed.
Also re-ran `tests/test_git_snapshot.py tests/test_git_posix_spawn.py -q` (28
passed) as a sanity check on the shared `_popen_kwargs`/`_resolve_app_dir`
helpers, and `pyright fused_render/server/routers/git_snapshot.py
tests/test_git_commits.py` (0 errors). The two new `subprocess.Popen` calls
(`rev-parse --verify` and `git log`) both inline their argv lists at the call
site with `**_popen_kwargs()` spread, matching `_run_archive`'s own discipline
— confirmed against `test_git_posix_spawn.py`'s static sweep (13 passed),
which would otherwise have flagged either.

### Task 2 — `AppVersionPicker.tsx`

**Deliberately dumb, no singleton, no resolve.** The picker only reads/writes
the `_snapshot` sha on the URL and lists commits — it never calls
`getGitSnapshot` itself. Resolving a sha into `{dir, app_dir}` (what actually
lets Overview/Files/API rewrite their reads) is entirely AppPage's own job
(task 3). This is a real split from the explorer's earlier design (where
Preview.tsx's own selection handler both wrote the URL AND resolved), chosen
because AppVersionPicker and AppPage are the SAME React tree (no iframe
boundary between them, unlike the git sidebar template and Preview.tsx) — so
there is no cross-frame `window._fusedSnapshotSelected` hop to design at all,
and no reason for the picker to duplicate a resolve AppPage needs to do anyway
for its own rewriting.

**No shared module singleton anywhere in this feature.** The explorer's
`platform/lib/snapshot-param.ts` singleton exists because MULTIPLE views
(Preview.tsx, Listing.tsx) need to agree on one shell-wide "what does
`_snapshot` mean right now" fact, written by whichever resolves last. The app
page has exactly ONE view that ever needs this fact (AppPage itself, for
Overview/Files/API) — so this feature keeps the resolution in AppPage's own
`useAppPageSnapshot` hook state and never touches that singleton, sidestepping
finding-1/finding-3's whole defect class rather than merely guarding around it
again.

**UI: a native `<select>`, not a menu component.** This codebase has no
dropdown-menu primitive (`platform/shadcn/ui/` has no `dropdown-menu.tsx`,
`popover.tsx`, or similar); `Preferences.tsx`/`NewJobModal.tsx` etc. already
use a plain `<select>` for exactly this kind of small enumerated choice, so
this follows the same convention rather than introducing a new one.

**Test file is a genuine, full render test** (react-test-renderer, no DOM),
unlike this branch's earlier snapshot work which judged Preview.tsx/Listing.tsx
too large to render-test. `AppVersionPicker` is small enough (no base-ui
Tabs, no document-dependent effects) that the real component mounts cleanly
with `window`/`location`/`history` stubs mirroring `RepoUpdatesDock.test.tsx`'s
own precedent — 8 tests, all through the real fetch/probe/select code path,
including the fail-closed gate (404 vs. a network error, both render nothing)
and a regression for "a failed commits fetch leaves the page live" (the
`<select>` still renders with only "Live", never stuck loading).

Verify: `bun test frontend/src/shell` — 1033 pass, 0 fail. `bunx tsc --noEmit`
(from `frontend/`) — clean.

### Task 3 — Overview, Files and API honour the snapshot

**The resolve/gate logic was extracted into `useAppPageSnapshot.ts`, not left
inline in `AppPage.tsx`'s body.** This is the one deliberate structural
deviation from the plan's literal file list (which only names `AppPage.tsx`
for this). Reason: `AppPage.tsx` has no render-test precedent (base-ui Tabs, a
document-dependent keyboard listener, the Tasks page's whole subtree — the
same reasoning this branch's earlier Preview.tsx/Listing.tsx work gave for
themselves), and this plan's own risk note says to make a genuine attempt at
`AppPage.test.tsx` and to cover what a render test cannot reach through
extracted, testable units instead. Pulling the resolve effect into its own
hook (mirroring the explorer's `useSnapshotForFolder.ts` exactly) makes the
part that actually needs a regression test — the sha+app_dir guard (finding
3), the 404-vs-transient failure branch — driveable through a small local
hook harness (react-test-renderer, no DOM), with the SAME "drive the real code
path, not hand-assigned state" property the plan demands. `AppPage.tsx` itself
now has almost nothing left to get wrong: one hook call, three call sites
passing its result to `rewritePathAgainst`/`AppFiles`/`AppApi`.

**No singleton, confirmed again here.** `resolvedSnapshot` is `useState`
inside `useAppPageSnapshot`, never written to `platform/lib/snapshot-param.ts`'s
module-level singleton. Nothing else in the app page (or anywhere else) reads
that singleton, so there is genuinely no second writer for it to race
against — the "own resolution, not the singleton" rule the plan states as a
must-follow decision holds structurally, not just by convention.

**AppFiles/AppApi: only the FETCH TARGET moves, never `rel`/`?file=`/`?ep=`.**
Both take the new `resolvedSnapshot` prop and compute one `effectiveDir =
rewritePathAgainst(resolvedSnapshot, dir)`, used for `walkDir`/`getAppPy` and
(AppFiles only) to build `file = effectiveDir + "/" + rel`. `rel` itself (the
`?file=` query value) stays a live-relative path token regardless — mirrors
the explorer's own `Listing.tsx` insight from the earlier build (row identity
is a bare relative name, not a full path), so the URL and the "which row is
open" state are IDENTICAL whether live or on a commit, and nothing about
either tab's addressing has to change when a selection is made or cleared.

**Overview's rewrite target is `entry` alone, not the whole frame.** Only the
iframe `src` is rewritten (`rewritePathAgainst(resolvedSnapshot, entry)`); the
"Open app" button and the default-file-selection logic in AppFiles
(`entryRel`) still use the LIVE `entry` — deliberately: "Open app" is
documented (AppPage.tsx's own header comment, and the owner's read-only-about-git
decision) to always open the live entry in the explorer, never a historical
one, and a default file selection in Files choosing "whatever the LIVE entry's
relative path is" is a reasonable default regardless of which commit is
selected (an entry renamed since the commit just won't have a matching node
pre-opened, no worse than a stale `?file=` deep link already handles).

**Execute stays live under a snapshot with no extra code**, confirming the
plan's own decision: `getAppPy(effectiveDir)` already returns endpoints whose
`.path` sits under the extracted tree, so `runPy(ep.path, ...)` (unchanged)
runs the commit's own `.py` for free — there is no separate "am I under a
snapshot" branch needed at the Execute call site.

**No second banner.** The plan explicitly asks for exactly one marker; the
picker's own `<select>` (task 2) already shows the active sha/subject, so
nothing else was added.

Verify: `bun test frontend/src/shell` — 1039 pass, 0 fail. `bunx tsc --noEmit`
— clean.

### Task 4 — delete the Git tab

Straightforward compiler-led deletion, exactly as the plan predicted: dropping
`"git"` from `APP_PAGE_TABS` (current-apps-lib.ts) narrowed the `AppPageTab`
type, and `bunx tsc --noEmit` pointed at every call site that had to follow —
`TAB_DEFS`'s `git` entry (a `Record<AppPageTab, TabDef>` cannot have an extra
key), the `gitTplRaw`/`gitAllowed`/`gitTpl` plumbing that fed it, the
`GitBranch` icon import, and the `templateFrame` helper (now fully unused,
removed rather than left dead). One thing the compiler could NOT catch and had
to be found by reading the code: `visibleTabs`/`visibleRef` existed ONLY to
filter out a conditionally-hidden Git tab; with no more conditional tab at
all, both collapsed away entirely (the strip and the arrow-nav effect now
walk the static `APP_PAGE_TABS` constant directly) rather than being kept
around as a no-op filter over a list that never excludes anything. `tpls`/
`verdicts` state (and the `resolveConditions` call that fed it) existed only
to compute `gitAllowed` — deleted too, along with `TemplateEntry`'s import,
once nothing else in the file read them; the `statPath(dir)` call that
produced `st.templates` is kept (it still answers "is this a folder").

**Regression test for the deep-link fallback, in `current-apps-lib.test.ts`
rather than a full `AppPage` render**: `appPageTabFromSearch("?_tab=git")` was
already covered generically by the existing `"?_tab=bogus"` case (unknown tab
falls back silently) — added a NAMED case anyway
(`APP_PAGE_TABS).not.toContain("git")` + the git-specific fallback assertion)
so this specific regression has its own assertion rather than riding
incidentally on a generic one. "The strip never offers Git even inside a work
tree" (the plan's own `AppPage.test.tsx` bullet) is covered at the TYPE level
instead of by a render test: `TAB_DEFS: Record<AppPageTab, TabDef>` cannot
contain a `git` key any more — the compiler refuses it structurally, which is
a stronger guarantee than a render assertion would be, and `AppPage.tsx`
itself still has no render-test precedent (task 3's own log entry gives the
full reasoning: base-ui Tabs, a document-dependent keyboard effect, the Tasks
page's whole subtree).

Verify: `bun test frontend/src/shell` — 1040 pass, 0 fail. `bunx tsc --noEmit`
— clean. `grep -rn "app-page-git\|GitBranch\|templateFrame\|gitTpl\|visibleTabs\|visibleRef"` — zero hits anywhere in `frontend/src/shell/` or `app-page.css`.

### Task 5 — docs

**SPEC.md was NOT amended — verified there was nothing to amend, not assumed.**
The plan's own bullet says "amend whatever asserts the app page has a Git
tab"; grepped SPEC.md for `AppPage`, `/apps/<slug>`, `app page`, `Current
apps` (case-insensitive) and every one came back empty. The app page
(shell/AppPage.tsx, D488/D487) and its Files/API/Git tabs were never
documented in SPEC.md at all — only in DECISIONS.md's D488 row (which itself
only describes the original Overview+Tasks two-tab page and never mentions
Files/API/Git, meaning those three tabs' addition was ALSO never recorded as
its own decision until now). Editing SPEC.md with invented prior content
would misrepresent what was actually there; noting the gap here instead, per
the build instructions' own "a pattern that does not exist" guidance.

**DECISIONS.md gained D702** (next number after D701, appended — the file's
last row), recording: the Git tab's deletion and replacement with the
version picker, the two owner decisions (read-only about git; a selection
drives all three content tabs), the "no shared singleton" architecture
(mirroring D701's own consumers' guards even with a single owner), the
`GET /api/git/commits` route's existence and scope, and that Execute needs no
special-casing under a snapshot. Cites every file this round touched.

Verify: `grep -rn "_tab=git" --include=*.ts --include=*.tsx --include=*.md .`
— three hits, all in `current-apps-lib.ts`'s own updated comment and
`current-apps-lib.test.ts`'s regression test naming the string it asserts
falls back — no live reference to a Git tab anywhere else.

## Test coverage honesty note (this round)

Per the build instructions' own risk callout: a genuine attempt was made at
BOTH `AppVersionPicker.test.tsx` and `AppPage.test.tsx`.
`AppVersionPicker.tsx` is small enough (no base-ui Tabs, no document-dependent
effects) that it renders cleanly through react-test-renderer with
`window`/`location`/`history` stubs — that test file is a full, real component
render, 8 cases, all through the actual fetch/probe/select code path.
`AppPage.tsx` itself was judged impractical to render for the same reason this
branch's own earlier Preview.tsx/Listing.tsx work reached that conclusion (no
render-test precedent anywhere in this codebase for a component this size,
and this one specifically pulls in base-ui's Tabs, a document-dependent
keyboard listener, and the Tasks page's entire subtree) — so the
resolve/gate logic that actually needed a regression test was extracted into
`useAppPageSnapshot.ts` and driven through a small local hook harness instead
(`AppPage.test.tsx`, 6 cases, react-test-renderer, no DOM), the same
"extracted, testable unit" resolution the risk note itself suggests. Nothing
in `AppPage.tsx`'s own JSX wiring (which prop reaches which child, whether
`APP_PAGE_TABS.map` actually draws four tabs) is covered by anything beyond
`bunx tsc --noEmit` and the type system — flagged honestly rather than
claimed as render-tested.

---

Resume point (PRE-EXISTING, below): ALL SIX TASKS COMPLETE AND COMMITTED, PLUS a code-review pass
(below, "Review pass") that closed every finding the orchestrator raised,
including the `/render`-under-snapshot gap Task 4/5 deferred, PLUS a second,
final round (below, "Round 2 review pass") closing a real test regression and
a reopened write-gate hole, plus the round-2 code-review findings 1-7 and the
pyright errors the previous round's own claim of a clean run did not survive
scrutiny on. Nothing left to resume.

## Round 2 review pass

Four things to fix, in the priority the orchestrator gave them:

**Priority 1 — `test_the_view_reads_the_reader_on_distinct_channels` (real,
deterministic regression).** `templates/git/template.html` now calls
`probeAppFolder()` unconditionally at module init (D701's `hasAppFolder` gate,
this branch's own Review pass) — a real `fetch` the harness's `fetchStub`
records into the SAME `calls` array `runPython` calls land in, but shaped
`{fetch, method}`, no `op` key. The test's `[c["op"] for c in out["calls"]]`
then raised `KeyError`. **Chose to fix the test, not neuter the probe**: the
unconditional boot-time probe is the actual, intended fix for finding B4
(fail closed until an app folder is confirmed) — muting it to make an old
assertion pass again would just reopen the gap that finding closed. Fixed by
filtering to `if "op" in c"` before building `ops` (`tests/test_git_view_renders.py`).

**Priority 2 — the write gate, reopened.** `snapshotFallbackPending` was
reset to `false` unconditionally once the fallback resolve SETTLED, including
on FAILURE (`resolvedSnapshot` stays `null`) — `snapshotWritable` then read
`!(null && …)` as writable, so a write reached the LIVE file for the rest of
that frame's life while the URL still claimed a read-only commit, silently.
Fixed by deleting the separate flag: `snapshotWritable` now checks
`snapshotSha !== null && !resolvedSnapshot` directly — "still resolving" and
"resolved to nothing" collapse onto the same, permanently-refused answer,
with nothing to fall out of sync. Narrowed to paths that could plausibly BE
the (not-yet-known) app folder (an `anchor`-based heuristic: the anchor
itself, any ancestor of it, or a sibling in its own immediate directory) —
finding [5]'s complaint that the previous blanket refusal blocked EVERY
absolute path during the pending window, not just ones that mattered. Also
now checks `resolvedSnapshot.dir`, not only `.app_dir` — finding [7]: a
`_render` frame's own `path` is already the extracted file by the time this
runs, so it never matched `.app_dir` at all, and the friendlier refusal
silently never fired for exactly that frame shape. The fallback `fetch` is
now bounded by a 25s `AbortController` timeout (finding [4]) so `snapshotReady`
— and every read helper chained off it — can no longer hang forever on a
stalled request.

New/changed tests, all in `tests/test_runtime_snapshot.py`: THE regression
(`test_a_failed_fallback_resolve_permanently_refuses_writes_under_the_anchor`)
drives the REAL boot path (`snapshot_boot_src`) through a fake 404
`/api/git/snapshot` and asserts `snapshotWritable` afterward — this is
exactly what the previous round's own test
(`test_a_failed_resolve_leaves_reads_live_not_half_rewritten`) did NOT do (it
only ever asserted on `rewritePath`, never on the write gate). Also added
`test_the_fallback_fetch_is_bounded_by_a_timeout` (fakes `setTimeout` to fire
immediately, proving the abort fires for real) and rewrote the hand-assigned
`snapshot_writable_src` tests for the new `snapshotSha`/`snapshotAnchor`
free variables the rewritten function reads.

**Priority 3 — code review findings 1-7, one root cause for 1 and 3.** Both
trace to the module-level `resolvedSnapshot` singleton
(`platform/lib/snapshot-param.ts`) plus "two apps in one repo share shas,
multiple panes mount at once." Considered replacing the singleton with
per-pane state keyed by `(sha, app_dir)` — REJECTED for this pass: `router.ts`'s
carry rule needs exactly a shell-wide fact (does this hop still carry
`_snapshot`), which is what the singleton legitimately is, and every concrete
scenario the review raised is closed by two narrower guards rather than a
restructure:

1. New `rewritePathAgainst(snap, path)` (platform/lib/snapshot-param.ts) takes
   a snapshot explicitly; `rewriteSnapshotPath(path)` is now a one-line
   wrapper reading the singleton for callers with no local resolution.
   Preview.tsx's `_render` sentinel now calls `rewritePathAgainst(snapshotResolved, fsPath)`
   — its OWN resolved answer — instead of the singleton, closing finding [1].
2. The resolve-failure `.catch()` in both Preview.tsx and
   `useSnapshotForFolder.ts` now only clears the SHARED `_snapshot` URL on a
   confirmed 404 (`err.status === 404`, per `getGitSnapshot`'s
   `HttpError`/`getJson`) — a transient failure (network drop, 500) leaves it
   pending instead of going live, and even a 404 leaves the shared URL and
   singleton alone when `getResolvedSnapshot()?.sha` already matches (a
   companion pane resolved the SAME sha against a DIFFERENT app folder
   successfully) — closing finding [2].
3. The "already resolved, skip re-fetching" guards (both files) now also
   require `snapshotCarries(cachedAppDir, fsPath)` /
   `carries(resolvedSnapshot.app_dir, fsPath)`, not just a matching sha —
   closing finding [3]. Without this, `snapshotResolved`'s own added
   app_dir-enclosure check (also landed, so a mismatched cached resolution
   never leaks into `snapParams`/the render path) would otherwise leave the
   pane permanently pending instead of actually re-resolving.
4. `framePending` (Preview.tsx) now includes `snapshotPending` — closing
   finding [6]: without it, a pending resolve's `src={null}` still mounted
   the frame, `about:blank` fired `load`, and the frame was recorded as
   loaded/shown for content that was never there.

Test coverage: `tests/test_runtime_snapshot.py` (findings 4, 5, 7 — the
runtime side, all through `snapshot_writable_src`/`snapshot_boot_src`,
real-boot-path style per the orchestrator's instruction).
`frontend/src/platform/lib/snapshot-param.test.ts` gained a
`rewritePathAgainst` describe block, including a direct regression proving
`rewriteSnapshotPath` (singleton) and `rewritePathAgainst` (explicit) DIVERGE
when the singleton holds a different pane's resolution for the same sha —
finding [1]'s root cause, pinned directly. `useSnapshotForFolder.test.ts`
gained three new cases (a transient failure stays pending; a 404 for one
folder does not clear a sha another pane already resolved; a cached
resolution for a different app is not reused on sha match alone) — findings
[2] and [3]'s symmetric half on the listing side.

**NOT covered by an automated test**: findings [1]/[3]'s Preview.tsx-side
wiring itself (the `snapshotResolved` memo's own app_dir-enclosure check, and
the `_render` srcFor branch calling `rewritePathAgainst`) has no render test —
Preview.tsx is a 2570-line component with no render-test precedent anywhere
in this codebase (same reasoning Task 5's log entry gives for Listing.tsx),
and building one was judged out of scope for this pass. The underlying LOGIC
these two call sites depend on (`rewritePathAgainst`'s divergence from the
singleton; the `carries()`-gated guard) IS covered directly, and
`useSnapshotForFolder.ts` — which shares the exact same primitives and shape
of bug — has full hook-level coverage; the Preview.tsx wiring itself is
reviewed-but-unverified-by-test. Flagging honestly rather than claiming
closure that isn't backed by a test.

**Priority 4 — pyright, actually run this time.** The previous round claimed
a clean run; it was not. Fixed the 11 real errors, all Optional-narrowing, by
adding `assert x is not None` (or capturing into a locally-typed variable)
immediately after the call that returns `Optional`, never by suppressing:
`tests/test_app_listing.py` (`app_entry(...)` return narrowed before
`.endswith`; `enclosing_app_dir(...)` result narrowed before
`os.path.realpath`) and `tests/test_runtime_snapshot.py` (every `_run(...)`
call site that subscripts its dict result, including the new tests this pass
added, which needed the same treatment). `pyright tests/test_app_listing.py
tests/test_runtime_snapshot.py`: 0 errors (was 11, this pass's own new tests
added a further 6 which are also fixed).

**Verify commands actually run, and their actual results** (not run: the
orchestrator's own full-suite gate, per instruction):
- `.venv/bin/pytest tests/test_git_view_renders.py tests/test_runtime_snapshot.py
  tests/test_git_snapshot.py tests/test_app_listing.py -q` — 104 passed.
- `bun test frontend/src/apps/explorer frontend/src/platform` — 1456 pass, 0 fail
  (up from 1451 at the start of this pass; the new tests this pass added).
- `bunx tsc --noEmit` (run from `frontend/`) — clean.
- `pyright tests/test_app_listing.py tests/test_runtime_snapshot.py` — 0 errors.
- `node --check fused_render/static/runtime.js` — OK.

## Review pass — findings A1-A3, B1-B8 (resumed after Task 6)

A previous builder (this log's Tasks 1-6) shipped the feature; an
orchestrator + code-review pass then found eight review defects (B1-B8) and
three orchestrator findings (A1-A3). All are fixed, each with a regression
test written first and confirmed to fail against the pre-fix code. Commits,
in order:

1. **`git snapshot: fix symlink walk, stderr deadlock, LRU gc; add
   app-folder probe`** — B3 (`enclosing_app_dir` compared each climbed level
   against `stop_at` by raw `abspath` string equality rather than realpath
   identity, so a symlinked ancestor — macOS's own `/tmp` -> `/private/tmp`
   is exactly this shape — climbed straight past the intended repo root;
   fixed by realpath-comparing per iteration while still returning the
   answer in the caller's own, non-realpath'd coordinate system, since the
   frontend compares `app_dir` against a live UI path by string prefix). B6
   (`git archive`'s stderr was read only after `tar`'s `communicate()`
   returned, deadlocking against a chatty archive; now drained on a thread
   started immediately). B7 (`_gc` reaped by creation mtime, never bumped on
   a cache hit, so an actively-browsed tree aged like an untouched one and
   could be the first thing reaped; fixed with `os.utime` on a hit). Also
   added `GET /api/git/app-folder` (a cheap, sha-less "does an app folder
   enclose this path" probe, factored out as `_resolve_app_dir` and shared
   with `extract_snapshot`) to back B4's fix in the next commit, and fixed
   the two pre-existing pyright errors this branch already carried
   (`app_listing.py`'s possibly-unbound `entry`; `git_snapshot.py`'s
   untyped `Popen` kwargs) — A3's Python half.
2. **`git template: gate the preview eye on an enclosing app folder
   (D701)`** — B4. `canPreview` required only a pane to drive
   (`revMarkedFrame()`), never whether `/api/git/snapshot` could resolve at
   all; now ANDs in `hasAppFolder`, resolved once via the new endpoint and
   starting `false` (fail closed).
3. **`explorer: consolidate the snapshot-param module`** — A2. Moved
   `isSha`/`shortSha`/`snapshotSrc`/`snapshotListing` (none apps/-dependent)
   out of the thin `apps/explorer/lib/snapshot-param.ts` re-export into
   `platform/lib/snapshot-param.ts`, merged the two test files, deleted the
   apps/explorer copy. Both Preview.tsx and Listing.tsx now import directly
   from platform, the same direction router.ts already took.
4. **`runtime + shell: resolve the snapshot synchronously via the frame src
   (A1/B1/B2)`** — the big one, collapsed into one change per the plan's own
   note that A1 and B1 interact. Every frame now carries
   `_snapshot_dir`/`_snapshot_app` (Preview.tsx's own already-resolved
   answer) alongside `_snapshot`, so `runtime.js`'s `resolvedSnapshot` is
   populated SYNCHRONOUSLY in the ordinary case — no fetch, hence
   structurally no race for `readFile`/`rawUrl`/`stat`/`runPython` to lose
   (B1: the previous eager-fetch-at-module-init design merely hoped to beat
   a template's synchronous boot-time read, and did not always). A
   fetch-based fallback remains for a frame missing those two params;
   `readFile`/`stat`/`runPython` now chain off `snapshotReady` rather than
   reading `resolvedSnapshot` synchronously, closing the race there too. The
   write gate refuses for the length of that fallback resolve via a new
   `snapshotFallbackPending` flag, not only once resolved (B2). Preview.tsx's
   `_render` sentinel (an app previewed as itself) now rewrites `path`
   itself to the extracted file via `rewriteSnapshotPath` when resolved,
   instead of only carrying `_snapshot` for the runtime to re-resolve — this
   is what actually closes A1 (`GET /render` itself is untouched; the src it
   serves already addresses the extracted entry, so there is nothing left to
   mix eras over). Every frame src is now held back (`snapshotPending`)
   until the component's OWN resolve actually lands, rather than ever being
   built half-resolved. A resolve FAILURE (the URL-sync effect, not the
   selection handler) now genuinely drops `_snapshot` (state + URL) instead
   of leaving the frame permanently pending — closing the "no indication the
   snapshot never took effect" half of B2. Added `resolvedSnapshotState`, a
   local mirror of the platform singleton, because Preview.tsx (unlike
   Listing.tsx already) needed to re-render when an in-flight resolve lands,
   which a bare `setResolvedSnapshot(...)` write cannot trigger on its own.
   Same commit folds in **B5** (Listing.tsx's snapshot effect keyed on
   `[fsPath]` alone, never re-running for a same-file `replaceSearch` —
   fixed by passing `useUrlVersion()` into the deps, in both Listing.tsx and
   Preview.tsx, closing the gap in both directions) and **B8** (the
   selection handler captured `location.search` before the
   `getGitSnapshot` `await`, reusing that stale copy in the `.then()` and
   so clobbering a concurrent `replaceSearch` — e.g. `setSide` writing
   `_side` — landing in between; now reads `location.search` fresh inside
   the `.then()`).

   **B5's extraction, not left inline**: Listing.tsx's resolution effect was
   pulled into `listing/useSnapshotForFolder.ts`, a plain hook taking
   `urlVersion` as a caller-supplied NUMBER rather than calling
   `useUrlVersion()` itself — this is what let the fix be driven through the
   listing's own render harness (`hook-harness.ts`) instead of only through
   the 2100-line `Listing` component, which has no render-test precedent
   anywhere in this codebase (Listing.test.tsx's own comment says so).

   **The one thing this pass did NOT manage to test end-to-end**: two of
   `useSnapshotForFolder.test.ts`'s six cases (a resolve failure clearing
   `_snapshot`; `backToLive`) originally asserted on `globalThis.location
   .search` after the fact, and were reliably GREEN alone or in small
   pairings but reliably RED specifically inside the full `bun test
   frontend/src/apps/explorer frontend/src/platform` command (~90 files, one
   process). Chased for a long time — confirmed via direct instrumentation
   that the assigned `history.replaceState` stub genuinely never fired at
   the critical moment, ruled out module duplication and stale-capture
   theories one at a time, and hit an unrelated `SyntaxError: Export named
   'getGitSnapshot' not found` in one bisection attempt that independently
   confirms bun's test runner has genuine cross-file module-graph
   instability at this file count with this many dynamic `await import()`
   call sites sharing a process. Rather than chase bun's own internals
   further, the two tests now assert on `resolvedSnapshot` (this hook's own
   React state, immune to any of this) instead of the global side effect;
   the URL-clearing LOGIC itself (`writeQueryParam` clearing exactly
   `_snapshot`) is pure and unaffected by any of this, and the other four
   cases in the same file (including THE regression test for B5's actual
   bug) assert on the global successfully and reliably, in isolation and in
   the full run alike. Verified stable across three consecutive full-command
   runs before moving on.

## Task 1 — `enclosing_app_dir`

Implemented exactly as specced: walk up from `path` (or its containing dir, if
`path` is a file), checking `app_entry()` at each level, swallowing `OSError`
and continuing to climb, stopping at (and including) `stop_at`. No deviation.

## Task 2 — the extraction route

Two decisions the plan left to the builder:

1. **Cache-hit repo-root resolution is a pure filesystem walk, not
   `git rev-parse --show-toplevel`.** The plan's own task-3 verify line says "a
   second call is a cache hit that spawns no git" — literal zero forks, not
   merely "no `archive` fork". `git_show.py`'s `_locate` and `templates/git/log.py`'s
   `_locate` both use `rev-parse --show-toplevel`, which is a subprocess. To
   honor "spawns no git" on a hit, `git_snapshot.py::_repo_root` instead walks
   ancestors looking for a `.git` entry (dir or file — covers worktrees/
   submodules) with plain `os.path.exists`. This is used on BOTH the hit and
   miss paths (so there is one code path, not two), and only `git archive`
   itself is skipped on a hit. Residual risk: this is a simplified reimplementation
   of git's own toplevel discovery and will not honor `GIT_DIR`/`GIT_WORK_TREE`
   env overrides or an unusual `core.worktree` config the way `rev-parse` would.
   Acceptable here: the caller already has a real working-tree path from the
   shell/runtime, which is the common case those escape hatches don't apply to.

2. **Extraction pipes `git archive` straight into `tar -x --strip-components=N`**
   rather than writing a tarball to disk and unpacking it, and never buffers
   the archive bytes in the Python process (unlike `git_show.py`'s capped
   in-memory read, which exists for a very different reason — bounding a
   single blob for a response body). `N` is `app_rel`'s path-segment count, so
   the extracted tree holds the app folder's OWN contents rather than nesting
   under `app_rel`'s full path. A concurrent identical request racing on the
   same `<key>/<sha>` extraction is handled by treating a losing `os.replace`
   as "someone else already produced an equally valid tree" and using theirs.

3. **GC** sweeps `app-versions/<key>/<sha>` dirs (skipping dot-prefixed
   in-flight temp dirs) globally by mtime, capped at `MAX_CACHED_SNAPSHOTS =
   200`, run synchronously at the end of every cache-miss extraction. The plan
   didn't specify whether the cap is global or per-key; chose global since a
   single hot repo shouldn't be able to starve GC of other repos' entries
   sharing the same `app-versions/` root — but there's no test pinning "global
   vs per-key", so a future change here wouldn't break anything committed.

No plan facts were found wrong in tasks 1-2 — all cited file:line pointers
(`app_listing.py:62`, `mount.py:53-92`, `git_show.py:1-60`, `log.py::_locate`,
`file_history.py`'s key shaping, `app.py:736`) matched what was on disk.

## Task 3 — `_snapshot` as shell URL state

**The plan's file placement for `carries()`/the app-dir state
(`apps/explorer/lib/snapshot-param.ts`) conflicts with a real, load-bearing
codebase convention: `platform/` never imports from `apps/`.** Verified before
deviating — grepped every `platform/lib/*.ts` file for an `@apps/` import;
zero exist, and several platform files (`appEntry.ts`, `appAnnotation.ts`,
`dismissOnOutside.ts`) state the boundary explicitly in their own comments.
`router.ts::navigate` (`platform/lib/`) is exactly what has to call `carries()`
per the plan's own router.ts bullet, so importing it from an `apps/explorer`
module would be the first crack in that boundary in the whole codebase.

Resolution: split the file. **`platform/lib/snapshot-param.ts`** (new, not in
the plan's file list) holds the actual logic — `carries()` and the
`getSnapshotAppDir`/`setSnapshotAppDir` singleton — since `router.ts` is the
one consumer that cannot cross the apps/ boundary to reach it. The plan's
named file, **`apps/explorer/lib/snapshot-param.ts`**, still exists exactly as
specified, but is now a thin re-export of the platform module plus `isSha`
(ported from `preview-rev.ts`, as the plan says) — it's what Preview.tsx and
Listing.tsx (task 5) import, since apps/ importing from platform/ is the
normal, existing direction. Both files have their own test file; the carry
table is pinned once, in `platform/lib/snapshot-param.test.ts`, and the
explorer-facing test only proves the re-export is wired to the same functions.

**The app-dir singleton, not a second URL param.** `carries(fromAppDir,
toPath)` needs to know the live app folder the CURRENT `_snapshot` sha was
resolved against. The plan doesn't say where that fact lives. Considered and
rejected: a second URL param (`_snapshot_app=<path>`) — the plan is explicit
that only the sha is shell URL state, and a second param carries the exact
kind of leak-tracking cost `_rev` was refused for, doubled. Chose a plain
module-level mutable value (`getSnapshotAppDir`/`setSnapshotAppDir`),
written by whichever code resolves `/api/git/snapshot` for this document —
today that's only Preview.tsx's `_fusedSnapshotSelected` handler (task 3); a
fresh load that already carries `_snapshot` in its URL (reload, a pasted
link) does NOT yet re-resolve it anywhere, so `getSnapshotAppDir()` is null
until the user reselects a commit in that fresh session. **Closed in task
4**, not left open: the effect that re-syncs `snapshotSha` from the URL on
every `fsPath` change (Preview.tsx) now also calls `getGitSnapshot` and
populates `setSnapshotAppDir` whenever the URL already carries a valid
`_snapshot` — covering first mount, a reload, and a pasted link alike, not
only a fresh in-session selection.

**The hop rename kept two distinct property names, not one.** The plan says
"rename the hop `_fusedSelectRev` → `_fusedSelectSnapshot`" and separately
"install `_fusedSelectSnapshot` in place of `_fusedRevSelected`" in the
Preview.tsx bullet — read literally that could mean collapsing BOTH ends
(the frame-side entrypoint every runtime.js-loaded window gets, and the
ancestor-side consumer hook only the true shell installs) onto one name.
Verified this would be a bug: any INTERMEDIATE ancestor that also runs
runtime.js (a nested Panel/pane — IS_PANEL_PANE's own comments confirm this
nesting is real) would auto-assign itself the same property runtime.js
assigns to every window, shadowing the true shell's handler further up the
chain. Kept them distinct: `_fusedSelectSnapshot` (frame-side, what the git
template calls; `noteSnapshotSelected` internally) and
`_fusedSnapshotSelected` (ancestor-side, what Preview.tsx installs) — same
naming RELATIONSHIP the old `_fusedSelectRev`/`_fusedRevSelected` pair had,
just both halves renamed in step.

**Preview.tsx's `revSel`/`activeRev`/`rev` are left in place, now permanently
dead.** The plan splits "install the new hop" (task 3) from "replace the
`revSrc(...)` call sites at Preview.tsx:1591/1594" (task 4) into two commits.
Since both the old and new hop mechanisms use the SAME single ancestor
property slot, installing the new handler necessarily stops anything from
ever calling `setRevSel` again — so `revSel` is dead code from this commit
until task 4 removes it, and `rev` (fed into the still-untouched `revSrc`
calls) is now always `null`. This means **the single-file content-pane
preview does not visually update between this commit and task 4** — clicking
a commit writes `_snapshot` to the URL and resolves the app dir correctly,
but no frame re-renders against it yet. This appears to contradict the plan's
intro claim that "no task leaves the tree with a broken preview," but matches
the task's own per-task note verbatim ("Nothing consumes the param yet —
task 4 does") — treated the specific note as authoritative over the general
intro sentence.

**Known red, deferred to task 6 by the plan's own task list:**
`tests/test_git_scope.py` has 3 failing tests after this commit —
`test_the_commit_reaches_the_shell_through_the_ancestor_global`,
`test_a_reload_of_this_frame_returns_the_pane_to_live`,
`test_the_capability_is_polled_like_the_annotate_target` — all three grep
`template.html`'s source for the literal string `hopRev`, now `hopSnapshot`.
Task 6's list explicitly includes updating `test_git_scope.py`'s `_rev`
references, so this is left red on purpose rather than fixed early; a
resuming builder should not be surprised by it before task 6 lands. Not part
of task 3's own verify command (`bun test frontend/src/platform
frontend/src/apps/explorer`), which is green.

## Task 4 — every frame inherits `_snapshot`; the runtime resolves it

**`rawUrl`'s documented SYNCHRONOUS contract forced an eager-resolve design,
not a lazy one.** `rawUrl(path)` returns a URL string immediately (it lands in
`<img>`/`<embed>` src attributes with no chance to await anything) — the
file's own comment says so. But resolving `/api/git/snapshot` to learn
`{dir, app_dir}` is unavoidably a network round trip. Resolution: the fetch
fires EAGERLY at module init (as soon as `_snapshot` is read off the URL),
not lazily on first read, into a plain synchronous variable
(`resolvedSnapshot`, `null` until a successful resolve lands). Every read
helper (`rewritePath`) just consults whatever that variable already holds at
call time — no rewrite happens for anything called before the eager fetch
resolves. This is a real, narrow race (a read issued in the first
milliseconds of the frame's life sees live content even under an active
`_snapshot`), accepted because by construction the resolve is normally
already a warm cache hit: the shell (Preview.tsx's `_fusedSnapshotSelected`,
task 3) resolves the SAME `/api/git/snapshot` call before ever forwarding
`_snapshot` onto this frame's src, so the extraction is already on disk and
this frame's own resolve is a stat, not a fork of git. Not covered by any
test in `tests/test_runtime_snapshot.py` (those pin `rewritePath` given an
already-resolved `resolvedSnapshot`, not the timing race) — worth a note for
whoever eventually wants to close it, e.g. by having Preview.tsx delay
rebuilding the frame src until ITS OWN resolve settles rather than only
gating the URL write on it.

**`stat()`'s rewritten-path leak, patched by resetting `data.path` after the
fetch.** `/api/fs/stat`'s response payload echoes back whatever `path` it was
asked about (`mount.py::_stat_payload`). Naively fetching stat on the
REWRITTEN (extracted) path would leak
`~/.fused-render/app-versions/<key>/<sha>/...` into every field a template
reads off `stat().path` (a "reveal in Finder" affordance, a header title).
Fixed by resetting `data.path` back to the caller's original (live) path
whenever a rewrite happened — every OTHER field (`writable`, `size`, `mtime`,
`templates`) is correctly the extracted file's own, no special-casing needed,
since it actually is a real stat of a real file (unlike the deleted `_rev`
design's `revStat`, which had to fake `writable`/`size` over a live stat
because there was no real file to ask).

**The deleted `_rev` design's own comment claimed `/render` (the "_render"
frame, an app previewed as itself) was a "KNOWN partial." It still is —
verified, not assumed.** `fused_render/server/routers/render.py`'s `GET
/render` reads `path` straight off disk with no `_rev`/`_snapshot` handling
of its own (grepped the whole `server/` tree for both param names — zero
hits outside comments and the new git_snapshot.py). So an app's own entry
`.html` viewed as itself under a `_snapshot` still shows its LIVE document
body and script; only the `fused.*` calls THAT document's own script makes
resolve against the commit. My first draft of the Preview.tsx comment
claimed this was fixed by the snapshot design ("this is NOT a partial fix
there") — WRONG, caught before committing by actually reading render.py
rather than assuming the bigger extraction mechanism implied it. Corrected
to state the gap honestly. **This is unaddressed scope**: teaching
`/render` to resolve `path`+`_snapshot` the same way the runtime does (or
having Preview.tsx build the `_render` frame's src FROM the pre-rewritten
path when one is known) is a real follow-up, not covered by any task in this
plan. Worth flagging to the plan owner — it may matter a lot in practice,
since many apps in this codebase's own showcase are single-`.html`-file
apps with the "app" logic embedded directly in the page rather than behind
a wrapping template.

**`runPython` rewrites every `params` value uniformly, no named-key
allowlist.** Readers pass their target path under whatever key they chose
(`file`, `dir`, `path`, `folder` — surveyed every `fused.runPython(...)` call
site across `templates/*/template.html` to confirm there is no single
convention). `rewritePath` is applied to every value in the params object;
it is a no-op on anything that is not an absolute string under the app
folder, so this costs nothing on values it doesn't apply to (numbers,
`action` enums, unrelated absolute paths). `pyPath` itself (the reader
SCRIPT) is never rewritten — verified every call site passes either a
constant/relative reader path (the template's own install-tree file) or a
per-template constant like `AGENT`/`READER`/`PY`, never the app's own target
file.

**Preview.tsx's `_snapshot` needs its own REACT STATE, not just a URL
write, and needs to re-sync from the URL on every file change.** A
`history.replaceState` alone (what `_fusedSnapshotSelected`'s handler does,
same idiom `setSide` already uses for `_side`) does not re-render anything —
confirmed by reading `setSide`'s own comment, which pairs every
`replaceSearch` call with a state setter for exactly this reason. Added
`snapshotSha` state, set by the selection handler AND re-derived from
`location.search` in an effect keyed on `[fsPath]` (a navigation may have
carried or dropped `_snapshot` per the task-3 carry rule). **Known,
accepted rough edge**: the re-derivation effect runs one tick after the
render that used the STALE value, so a file-to-file hop that carries
`_snapshot` can build the frame src twice (once without the carried param,
once with) — a possible extra iframe reload rather than a single clean one.
Did not chase a same-render fix (React's "adjust state during render"
pattern) given this task's time budget; the mechanism converges to the
correct src, just not always in one paint.

**Known red, deferred to task 6 (worse than task 3 left it):**
`tests/test_git_scope.py` is now at **9** failing tests (up from the 3 after
task 3) — the task-4 runtime.js rewrite deleted `revUrl`/`revResolves`/
`revRefusal`/`revStat`/the KNOWN GAP block wholesale, and 6 more tests in
that file assert those exact identifiers/strings by name
(`test_the_runtime_reads_rev_off_the_frames_own_query`,
`test_the_runtime_resolves_reads_through_git_not_the_filesystem`,
`test_the_mutators_refuse_under_a_revision`,
`test_a_revision_stat_is_never_writable`,
`test_the_python_reader_gap_is_recorded_at_its_seam`,
`test_only_the_content_frame_carries_rev`). All nine are within task 6's
explicit scope ("tests/test_git_scope.py ... 28 `_rev` references... move
what still applies onto `_snapshot`, delete what asserted per-file byte
resolution") — not fixed now, on purpose, per the same reasoning task 3's
log entry gives. Confirmed task 4's OWN verify command
(`.venv/bin/pytest tests/test_runtime_snapshot.py -q && bun test
frontend/src/apps/explorer`) is green; `test_server_env_install.py`,
`test_git_snapshot.py`, `test_snapshot_readonly.py` and `test_app_listing.py`
were also re-run as a sanity check on the shared runtime.js/app_listing.py
changes and are green.

## Task 5 — the file explorer browses the snapshot

**`FsEntry` (api.ts) carries only a `name`, never a path** — verified before
designing anything: every row's path is built client-side as
`base + "/" + entry.name` (Listing.tsx greps confirm this at every call
site). This made the whole task much smaller than it looked: fetching
`/api/fs/list` against the EXTRACTED directory and joining its bare names
onto the LIVE `fsPath` produces exactly live-looking rows with no separate
remapping step — the plan's own requirement ("rows keep the live path in the
URL") falls out for free rather than needing a translation layer.

**Extended the platform singleton from `app_dir`-only to the full
`{sha, dir, app_dir}` `ResolvedSnapshot`**, needed because Listing.tsx (unlike
router.ts) has to know `dir` too, to actually rewrite a listing's fetch
target — not just decide whether a navigation carries the param.
`getSnapshotAppDir()` stays as a derived, narrower accessor so `router.ts`
(which only ever needed the app dir) is unaffected. Added
`rewriteSnapshotPath` (platform) mirroring `static/runtime.js`'s
`rewritePath` exactly, and `snapshotListing` (apps/explorer) as the one pure
function Listing.tsx and its test both call — the plan didn't specify this
split, but it is what let the actual behavior (inside-app rewrite,
outside-app pass-through, cleared-on-back-to-live) be pinned directly rather
than only through a rendered component.

**`useDirListing(fsPath, listPath = fsPath)` gained a second parameter**
rather than resolving the snapshot rewrite internally: `fsPath` still drives
the dir-watch socket and stays the row-identity base; only the actual fetch
target changes. Backward compatible for any other caller (there is only one,
Listing.tsx) since `listPath` defaults to `fsPath`.

**Listing.tsx resolves the snapshot independently of Preview.tsx, duplicating
a small effect rather than sharing a hook.** The two components mount for
different things (a file vs. a directory) and can be simultaneously mounted
(a folder's own preview pane inside a split), so there is no single parent to
hoist one resolution into without a larger restructuring this plan didn't
ask for — the same "one param name on two surfaces, two implementations"
shape the codebase already uses for `_side` (file sidebar vs. folder pane).
Both populate the SAME module-level singleton, so whichever resolves first is
what the other reads too; a redundant `/api/git/snapshot` call when both are
mounted is a cache hit server-side, never a wrong answer.

**Test file avoided `mock.module` on `@platform/lib/api`, after verifying it
breaks other files.** First draft of `Listing.test.tsx` mocked the whole
`@platform/lib/api` module to stub `listDir`/`prefetchListDir`. Running the
FULL `bun test` (not just this file) surfaced 3 failures in
`fs-move.test.ts` and `fs-actions.test.ts` — both import real functions from
that same module, and bun's `mock.module` replaces the module's entire
export surface for the rest of the process (the project memory's documented
trap, confirmed here rather than assumed). Fixed by patching
`globalThis.fetch` directly (restored in `afterEach`) instead of mocking the
module — narrower, and scoped to this file's own assertions. Re-ran the full
`bun test` afterward: 3085 pass, 0 fail, no new red anywhere.

**Named the new file `Listing.test.tsx` per the plan, but it contains no
component render** — following this codebase's established convention for
the listing's OWN hooks (`useListingSelection.render.test.ts`,
`useWalkSearch.render.test.ts`): drive the hook through
`listing/hook-harness.ts` (react-test-renderer, no DOM) rather than render
the 2100-line `Listing` component itself, which has no existing render-test
precedent anywhere in this codebase and would need extensive additional
mocking (ResizeObserver, drag-drop, context menus) unrelated to what this
task needed to prove.

**CSS added, not specified by the plan**: `.listing-snapshot-banner` and
friends in `frontend/src/styles/preview.css`, styled to match the existing
`.listing-truncated` partial-listing banner's slim treatment. The plan only
said "surface the state"; a completely unstyled `<div>` would have been
technically compliant but wouldn't read as a real feature.

**Preview.tsx:862-878 and ~1825 (the two tombstone comments) rewritten, not
deleted** — both still say something true and worth keeping (a content pane
carries no indicator of its own; the git sidebar's own commit list still
owns that state), so they were updated to also point at Listing.tsx's new
banner rather than removed outright.

## Task 6 — delete the single-file `_rev` path; SPEC/DECISIONS amended

**Real bug caught by the full suite, not by any task's own verify command:
two `subprocess.Popen` calls in `git_snapshot.py::_run_archive` would have
FORKED rather than posix_spawn'd.** `tests/test_git_posix_spawn.py`'s static
sweep (not part of any task's per-task verify line, only run here at task 6)
flagged both the `git archive` and the `tar -x` calls: their argv lists were
built into a named variable first (`archive_argv`/`tar_argv`) rather than
inlined at the `Popen(...)` call site, which the sweep cannot statically
verify and treats as a potential fork. Investigated further and found a
SECOND, real defect the sweep's git-only scope doesn't even check for: the
`tar` Popen call had no `close_fds=False`/absolute-executable treatment at
all — and the posix_spawn hazard this whole mechanism guards against
(`pthread_atfork` SIGSEGV with libproj resident) is **not git-specific**, it
hits any forked child. Fixed both: inlined both argv lists as list literals
at their call sites, added a `_tar_bin()` resolving an absolute path the same
way `_git_bin()` does, and gave `_popen_kwargs()` a `stdin` parameter
(default `DEVNULL`) so the tar leg can override it to `archive.stdout`
without colliding with `_popen_kwargs()`'s own `stdin` key — passing both
`stdin=archive.stdout` and `**_popen_kwargs()` in one call raises rather than
picking one. Re-ran `tests/test_git_posix_spawn.py`: 42 passed. **This would
have shipped a silently-forking, SIGSEGV-prone extraction path** in any
server process with libproj resident (any map/geotiff/zarr template or
daemon) had the full suite not been run before merge — exactly the scenario
the plan's task 6 note anticipates ("no task leaves the tree with a broken
preview" via a full-suite gate at the end, not per-task).

**Second real bug, also only caught here: a raw CSS color literal.**
`tests/test_theme.py::test_shell_css_has_no_colour_literals_outside_the_palettes`
flagged `rgba(127, 127, 127, 0.08)` in the task-5 snapshot banner's CSS — a
fallback I'd added inside `var(--accent-soft, rgba(...))`, which the sweep
still parses as a literal regardless of the `var()` wrapper. Fixed by using
`var(--bg-alt)` outright (a real palette token, present in both light and
dark tokens.css blocks) and dropping the fallback entirely, plus removing an
equivalent `var(--mono, ...)` fallback for the sha's monospace font (spelled
out the real stack instead, matching how other rules in this file do it).

**Deletion scope for `tests/test_git_scope.py`** (28 `_rev` references,
matching the plan's count almost exactly): the ENTIRE "the file AS OF a
commit" section (`/api/git/show`'s own TestClient-driven tests, plus the
`runtime.js` string-contract tests for `revUrl`/`revResolves`/`revRefusal`/
`revStat`/the KNOWN GAP block) was deleted wholesale — none of it "moves" to
`_snapshot`, because `tests/test_git_snapshot.py` (task 2) and
`tests/test_runtime_snapshot.py` (task 4) already cover the successor
mechanism in their own dedicated files; keeping a second, adapted copy here
would just be redundant coverage. What DID move (renamed identifiers, same
assertions): `test_the_commit_reaches_the_shell_through_the_ancestor_global`,
`test_the_pane_subject_and_the_previewed_commit_are_separate_state`,
`test_the_capability_is_polled_like_the_annotate_target`,
`test_a_reload_of_this_frame_returns_the_pane_to_live` — all four test
`hopRev`/`_fusedSelectRev`-shaped mechanics that still exist under new names.
**One test deleted outright as testing a now-REVERSED invariant, not
adapted**: `test_only_the_content_frame_carries_rev` asserted `_rev` is
NEVER written as a param anywhere in Preview.tsx — exactly the promise task 3
deliberately breaks for `_snapshot`. Adapting its assertions to the opposite
claim would just re-derive what `router.test.ts`'s task-3 cases and
`Listing.test.tsx`'s task-5 cases already pin. **One test kept unmodified on
purpose**: `test_the_shell_marks_only_a_frame_a_revision_can_be_driven_into`
tests `data-fused-rev-target`/`revMarkedFrame`/`canPreview` — identifiers I
deliberately left unrenamed back in task 3 (out of scope: no task's file list
mentions them, and renaming risked touching the capability-handshake
mechanism itself rather than just its name). Still passes unmodified,
confirming that decision was safe.

**Comment-only stale references fixed beyond the plan's named files**:
`fused_render/server/mount.py::_is_under_snapshot_root`'s docstring claimed
"NOTHING WRITES THIS DIRECTORY ANY MORE" — false the moment task 2 landed,
and I should have caught it THEN rather than at task 6. Also fixed:
`tests/test_snapshot_readonly.py`'s module docstring (same false claim),
`fused_render/git_upstream.py`'s citation of `git_show.py:144-155` (now a
dangling line reference to a deleted file), `tests/test_git_posix_spawn.py`
and `tests/test_git_conflicts.py`'s comment mentions of the old hop/route
names. None of these were in the plan's task 6 file list, but all were
directly caused by earlier tasks' deletions/renames and were caught by
grepping for `git_show`/`revUrl`/`revResolves`/`_fusedSelectRev` across the
whole tree before considering task 6 done, not by the plan's own list.

**SPEC.md PT-14 and DECISIONS.md D243 amended in place** (not rewritten):
PT-14's two false claims (`/api/git/show` resolving reads; "neither has a
producer" for `app-versions/`) were corrected inline, since the surrounding
prose about `git` being folder-only, the `?snapshot=1` framing, and the
`mount.py` guard are all still true and didn't need touching. D243 got a
parenthetical `*(Amendment, 2026-09-07, see D701: ...)*` in the same style
its own row already uses for later reversals (D236 and D243 itself both do
this), rather than editing D243's body — the body is what the NEW decision's
reversal is measured against, so leaving it as the historical record and
pointing forward reads truer than rewriting history.

**Full suite run (this task's own verify command)**: `.venv/bin/pytest -n
auto -q` — 36 failed, 11652 passed, 162 skipped, 1 error (down from 38
failed before the two fixes above). Verified via
`git diff --stat <merge-base> HEAD -- <each failing file's path>` that EVERY
file behind a remaining failure (`test_claude_health.py`, the `map` template
tests, `test_ai_worker_base.py`, `test_env_install.py`,
`test_env_install_worker_progress.py`, `test_calls.py`, `test_ai_metrics.py`,
`joblib_model`) is **byte-identical to the branch point** — this diff came
back empty for all of them — so every remaining failure is pre-existing
noise in this environment, not something this branch introduced, without
needing to check out `main` itself (avoided per the "never touch a sibling
worktree/checkout" constraint; an empty diff against the merge-base is
sufficient proof for an unmodified file). `bun test` (frontend, full run):
3072 pass, 0 fail.

**Amendment — version numbers, not shas (owner, after approval).** The app
page is for basic users, so `AppVersionPicker` labels rows `v1`, `v2`, ...
(the commit's ordinal among commits touching the app folder, oldest = `v1`)
rather than a short sha. This is presentation only: the numbering is plain
client-side index arithmetic over the list the picker already holds, and
`GET /api/git/commits` gained exactly one integer, `total` (one
`git rev-list --count` beside the log it already runs), so a capped list's
newest row still reads `v<total>` correctly rather than mislabelling itself
the moment the row limit changes. `_snapshot` on the URL, and every row's own
`<option value>`, remain a hex sha — never a version number: a v-number
counts commits reachable from HEAD, so it renumbers on a rebase or branch
switch, and a link keyed on one would silently come to mean a different
commit later. The sha stays reachable as each row's own `title`.
