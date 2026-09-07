# Build log — app-folder snapshot preview

Resume point: ALL SIX TASKS COMPLETE AND COMMITTED. Full suite green modulo
pre-existing, branch-unrelated red (see the Task 6 entry below for the exact
verification). Nothing left to resume — a future reader picking this up
should read the Task 6 entry first for the two real bugs the full-suite gate
caught (a forking subprocess, a raw CSS color literal) and the deferred
`/render`-under-snapshot gap noted in Task 4/5's entries.

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
