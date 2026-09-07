# Build log — app-folder snapshot preview

Resume point: tasks 1-3 committed. Next up: task 4 (every frame inherits
`_snapshot`; the runtime resolves it).

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
until the user reselects a commit in that fresh session. This is a real,
narrow gap: `carries()` will drop `_snapshot` on the FIRST hop after such a
load, even though the destination is genuinely still inside the app. **Left
for whoever does task 4 to close** — task 4 already needs to resolve
`/api/git/snapshot` from the runtime's own boot (to build `fused.snapshot`),
and Preview.tsx (which owns the shell-level state) should call
`setSnapshotAppDir` there too, on mount, whenever the URL already carries
`_snapshot`. Noted here rather than silently left broken.

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
