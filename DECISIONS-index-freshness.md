# Decisions and spec corrections — index freshness, export, path bar

## Workstream A — compaction heartbeat

- The spec says "extend `tests/test_index_runtime.py`". That file is unrelated:
  it tests the `fused.fileIndex.*` JS runtime bridge (`static/runtime.js`), not
  the scan/compaction control plane. The tests instead extend
  `tests/test_index_store.py`, which already has `compact()` coverage
  including phase-event assertions, plus one test that imports `runner` to
  check `_looks_abandoned` directly against a compaction's own event stream.
- The fix emits a `phase` event (not a `progress` event) after each partition
  write. `derive_state` overwrites `dirs`/`files`/`reused` with whatever a
  `progress` event carries (defaulting to 0 for fields it omits), and
  `_compact_locked` has no access to the run's dirs/files/reused totals — only
  `run_scan` does. A `progress` event from inside compaction would reset the
  displayed counters to 0 while the merge is still running, which is a visible
  regression the bug report does not ask for. `phase` only touches `state["phase"]`
  and already carries the loop's two existing calls ("writing index",
  "writing signatures"), so a per-partition phase message
  ("writing index (partition i/n)") keeps existing behavior and satisfies the
  watchdog, which only cares about the run directory's mtime, not event type.
- Regression test note: a fast in-test compaction cannot reproduce the bug on
  real wall-clock time (a few small partitions finish in milliseconds either
  way), so `test_compaction_progress_keeps_the_watchdog_from_reporting_abandoned`
  drives `_emit`'s clock through a fake, monotonically-advancing value passed
  to `os.utime` after each emit, sized so that two emits alone (the old
  behavior) would span past `ABANDONED_RUN_S` while the new per-partition
  cadence never lets any single gap approach it.

## Workstream B — export a `.fused` app to disk

- The spec's docstring language calling a `.fused` file "a manifest+payload
  zip" is loose — `zipfile.is_zipfile()` returns `False` on a real export.
  `.fused` is written by `appfile_container.write` in a custom container
  format; validate exports with `appfile_container.is_container()`, not the
  stdlib zip check.
- New route: `POST /api/appfile/export/save`, taking `path` (the app folder,
  `Form`) and an optional `preview` (`File`) — not a JSON body. The existing
  browser-download export (`api_appfile_export_with_preview`) already
  supports baking a caller-captured screenshot into the export as
  `preview.png` when the folder has none authored; the new server-side-write
  route keeps that same feature (multipart, not JSON) so exporting to disk is
  not a regression for apps that rely on it. An over-cap or non-PNG capture
  is dropped rather than raised, mirroring the existing route.
- Destination directory: `~/Downloads`, resolved via `os.path.expanduser("~")`
  (works on Windows too, since `USERPROFILE`-based expansion applies there).
  Collision-avoidance (`App.fused`, `App (2).fused`, ...) happens before
  calling `appfile.export_app_file`, which itself refuses to overwrite an
  existing `out_path`.
- The route calls `note_index_mutation(dest_dir)` synchronously after the
  write succeeds, and raises a `jobs.upsert(...)` row (`page=<real_path>`,
  `origin="Export"`, `server=True`) mirroring the AI image/video render
  pattern (`fused_render/ai/supervisor.py`) of pointing a job's `page` at its
  real output file, rather than inventing a separate reporting mechanism. Job
  id uses `SERVER_ID_PREFIX + "export:" + secrets.token_hex(4)`, matching the
  dominant id convention already used by `app_id.py`/`lan.py`/`templates_api.py`.
- Test gotcha: `appfile.py` imports `note_index_mutation` by name
  (`from fused_render.server.index_touch import note_index_mutation`), which
  binds a local reference at import time. Monkeypatching
  `index_touch.note_index_mutation` (the origin module) has no effect —
  patch it on the importing module instead
  (`monkeypatch.setattr(appfile_router, "note_index_mutation", ...)`). This
  applies to any function imported by name into a router module.
- Frontend: `saveAppFileToDisk()` (`api.ts`) posts the multipart request with
  raw `fetch`/`FormData` rather than the JSON `postJson` helper, since the
  backend expects multipart. `exportAppFile()` (`appShot.ts`) now returns
  `Promise<string>` (the real path) instead of `Promise<void>`; the 3 other
  call sites (`EntryActionsMenu.tsx`, `appCardMenu.ts`, `AppPage.tsx`) all
  already discard or `.catch()` the promise without consuming a return value,
  so this widening needed no changes there and added no notification at
  those sites — the spec's two-action success notification is wired only at
  `AppPreviewCard.tsx`'s export button, the one call site backed by a visible
  card the export originated from.
- `NotificationCard` already has two independent, distinctly-styled action
  slots reachable off `StoredNotification`: `action` (-> `navAction`) and
  `extraAction` (a second, separate `.q-all` button below the status line).
  Threaded `extraAction` through `NotificationInput`/`StoredNotification`
  (`notifications.ts`, including `forwardToShell()` for the iframe-forwarding
  path) and into `RepoUpdatesDock.tsx`'s `MessageRowView`, which now passes
  both `navAction={notification.action}` and
  `extraAction={notification.extraAction}`. Deliberately NOT wired into
  `MessagePopupCard.tsx` (the transient popup) — precedent already set by
  `NotificationInput.page`'s existing "unused by the popup card" comment: a
  fleeting toast is not where a two-destination choice belongs.
- The export success notification: `action` = "Reveal folder" (calls the
  existing `revealPath(realPath)`, which already handles being given a file
  path by revealing/selecting it inside its parent folder — no dirname
  needed); `extraAction` = "Open file" (calls `navigate(realPath, { isDir:
  false })` from `router.ts` directly, NOT `navigateToJobPage` — that
  helper's `KNOWN_FILE_EXTENSIONS` allowlist only recognizes
  `.html`/`.htm`/`.png`/`.mp4`, so it would misclassify a `.fused` path as a
  directory; `EmbedStrip.tsx` already treats `isDir === false && path ends in
  .fused` as its one signal for "a `.fused` file is open", confirming
  `navigate(path, { isDir: false })` is the correct call for this file type).

## Workstream C — path bar drops the filename

- Verified the spec's warned-about hazard is real: seeding the box from
  `crumbsPath` (the file's own full path) without anything else breaks TWO
  separate `isPristineQuery` call sites that assumed the seed was always
  `fsPath` (the search scope / parent folder):
  - `FileSearchField.tsx`'s own navigation-effect guard
    (`isPristineQuery(q, parentPath, home)`) exists specifically to stop a
    plain focus from immediately handing off to the parent folder with the
    pre-filled seed as a "committed" search (its own header comment says so).
    Seeding with the fuller file path made that seed no longer equal
    `parentPath`, so the guard read it as "already edited" and fired
    `navigate(parentPath, { isDir: true, q: query })` on focus alone —
    confirmed by temporarily reverting the fix and watching
    `FileSearchField.render.test.tsx`'s new focus test fail with the seeded
    value truncated back to the parent.
  - `SearchField.tsx`'s own `pristine` (search-affordance/completion
    exclusion) and its "already holding the pre-filled path" re-focus branch
    (`:696`) both compared against `fsPath` alone too.
  - Fix (per spec's fallback): `isPristineQuery` (query-pristine.ts) takes an
    optional 4th argument, `crumbsFsPath` — a query is pristine if it matches
    either `fsPath` (the scope) or `crumbsFsPath` (the display path), in
    either `contractHome` notation. `SearchField.tsx` passes `crumbsPath` at
    both its own call sites (`:432`'s `pristine`, `:696`'s re-focus check);
    `FileSearchField.tsx`'s guard passes its own `fsPath` (the file) as that
    4th argument. This preserves the scope/display split exactly: a
    committed query still searches/navigates against `fsPath` (the parent) —
    nothing about `commitSearch`/`navigate`'s own target changed — while the
    box's own pristine detection recognizes either seed shape as untouched.
  - Folder views pass no `crumbsFsPath` prop, so `crumbsPath` there already
    equals `fsPath` (`SearchField.tsx:237`) — the new 4th argument is then a
    no-op duplicate of the existing check, confirmed unchanged by running the
    full `src/apps/explorer` test directory (1217 tests) after the fix.
  - Ctrl-L / Breadcrumb click-to-edit (`Breadcrumb.tsx:694`,
    `requestSearchFocus`) was not touched — it seeds verbatim through a
    different branch (`focusFromRequestRef`), confirmed unchanged by
    `search-focus.test.ts` and the "Ctrl/Cmd+L and click-to-edit" describe
    block in `search-button-empty-open.render.test.tsx`.
- An existing test, `search-button-empty-open.render.test.tsx`'s "a plain
  focus not routed through requestSearchFocus is unchanged" test, was
  asserting the OLD buggy behavior verbatim (seeded value truncated to the
  parent folder) — it mounts `FileSearchField` directly with a file path.
  Updated its expectation and comment to the fixed value (the file's own
  path) rather than leaving it silently locking in the bug.
- New tests: `query-pristine.test.ts` (the `crumbsFsPath` argument, both that
  it's recognized as pristine and that it doesn't loosen a genuinely-edited
  query), and `FileSearchField.render.test.tsx`'s new "focusing the box over
  a file" describe block (seeds the full file path; a second focus after the
  first still reads as pristine, i.e. selects rather than re-seeding
  differently) — the latter confirmed to fail against the pre-fix code
  (reverted `crumbsPath` back to `fsPath` in the seed line, watched it fail
  with the truncated parent path, then restored the fix).

## Workstream D — relitigate the unmeasured indexing constants

**FRESHNESS_CHECK_S (55 -> 62).** The spec's diagnosis was exactly right and
verified against the code before touching it: `_freshness_due` stamps the
in-memory per-root check clock the instant a check comes due, whether or not
that check goes on to actually scan. With the constant below
`freshness.MIN_INTERVAL_S` (60), every other check landed before the scan
floor had cleared, got refused, and stamped anyway — doubling the intended
~60s cadence to ~110s. Raising it above the floor (62, matching the spec's
suggested "plus the spawn offset") means every due check now finds the floor
already clear.

Wrote a dedicated simulation test
(`test_the_effective_folder_open_scan_cadence_tracks_freshness_check_s`)
rather than trusting the arithmetic — per the spec's own instruction that a
test which "just reads the constant back is worthless." It runs on a
synthetic clock (`now` values fed straight into `_freshness_due` and
`note_folder_opened`), stepping one simulated second at a time, and asserts
every gap between consecutive real scans falls in
`[MIN_INTERVAL_S, FRESHNESS_CHECK_S + 2]`. `runner._record_scan` stamps the
real wall clock internally and cannot take an injected `now`, so the
simulated "a scan just happened" moment is written directly into
`cfg.scans_json` via `runner.storage.write_json`, bypassing that dependency
rather than monkeypatching `time.time` globally (which would also perturb
anything else in the loop reading the real clock). Confirmed this test fails
(observed ~110s gaps) against the unfixed constant before making the change.

The old test pinning the bug
(`test_the_scan_floor_matches_the_routers_check_debounce`) was renamed to
`test_the_scan_floor_sits_below_the_routers_check_debounce` and now asserts
the corrected relationship (`FRESHNESS_CHECK_S > MIN_INTERVAL_S + 1`) with a
present-tense docstring — no "used to be"/history language, per the no-history
rule. Its sibling `test_the_deferral_is_absorbed_inside_the_check_interval`
needed no numeric change (its assertion is a ratio, not an absolute), only a
docstring edit removing the now-stale claim that a test forbidding
`FRESHNESS_CHECK_S > MIN_INTERVAL_S` would "lock the bug in" — that relationship
is now the fix, not a forbidden one.

Rewrote the large "KNOWN, PRE-EXISTING, AND DELIBERATELY NOT FIXED HERE"
comment block over `FRESHNESS_CHECK_S` in `routers/index.py`, the parallel
narrative in `index/freshness.py` over `MIN_INTERVAL_S`, and the "Known bug,
pre-existing and deliberately not fixed" paragraph in
`specs/scan-incremental.md` §5 gate 3 — all three described the OLD
intentional-non-fix decision at length; all three now describe the corrected
behaviour in the present tense, per the no-history-in-comments rule. Also
corrected a comment in `tests/test_index_rank_concurrency.py` that cited the
old "~110s" figure.

**QUIET_S (30 -> 3).** Verified this was genuinely unmeasured (no cited
incident, unlike `MIN_INTERVAL_S`, `FRESHNESS_DELAY_S`,
`MUTATION_SCAN_FLOOR_S`) before touching it. `MIN_INTERVAL_S` already
independently stops a churning directory from queueing scan after scan, so
`QUIET_S` only has to outlast one write burst, not thirty seconds of one.
Added `test_a_folder_quiet_for_only_a_few_seconds_is_already_actionable`
(fails at the old value, passes at 3s). Existing tests that compute offsets
as `QUIET_S ± n` needed no changes — they assert the mechanism, not a
literal duration, so they are robust to the constant moving. One test in
`test_index_api.py` (`test_a_folder_that_goes_quiet_after_the_check_refused_it_still_gets_scanned`)
hardcoded a literal `+ 3.0` offset that happened to equal the OLD margin
inside a 30s window; at the new 3s window that offset landed exactly on the
boundary (`now == quiet_at`, not `<`), flipping the test from "refused" to
"started". Fixed by shrinking the offset to `+ 1.0`, comfortably inside the
new window, and generalizing the comment to say "freshness.QUIET_S" rather
than quoting a value that would go stale again.

**SCAN_DEBOUNCE_S (15 min -> 5 min).** Same reload-loop job, a quarter the
cost to a machine left on and reopened. Added
`test_the_startup_debounce_is_a_few_minutes_not_fifteen` (bounds check, 4-6
min) since the existing debounce tests only assert the mechanism
symbolically. Updated the three `server-api.md` cross-references citing "15
min" / "15-minute".

**COALESCE_S (1.5s) and DEFER_DEADLINE_S (120s) — left unchanged**, per the
spec's explicit instruction. `COALESCE_S` is already short and shortening it
only trades away burst batching (a multi-file drag/delete arriving as one
scan) for no user-visible latency gain. `DEFER_DEADLINE_S` is a safety valve
against a wedged worker, not a latency knob a user experiences directly — the
20s `MUTATION_SCAN_FLOOR_S` floor it sits behind is the number a save/rename
actually waits on.

**ABANDONED_RUN_S (5 min -> 90s).** Sequenced after workstream A (compaction
heartbeat), already committed. The long silent gap `ABANDONED_RUN_S` used to
tolerate (a whole-store compaction rewrite with no progress emit) no longer
exists — compaction now heartbeats per partition — so the threshold could
come down without risking a false "the scan died" report during a real,
still-running compaction. Verified this by re-reading
`test_compaction_progress_keeps_the_watchdog_from_reporting_abandoned`
(test_index_store.py), which already runs on a fake clock parameterized by
`runner.ABANDONED_RUN_S` and passed unchanged at the new value. Added
`test_a_dead_run_is_reported_promptly_not_after_several_minutes` (bounds
check, 60-120s).

Also updated the stale comment directly above the constant in `runner.py`
that still said "a big DuckDB compaction can go quiet for a while" — that
was true before workstream A and is not true after it; left uncorrected it
would have actively misled the next person reading the threshold.

`WARM_WAIT_DEADLINE_S` (6 min -> 2 min): the spec flagged
`server-api.md`'s "just past `ABANDONED_RUN_S`" relationship as needing a
recheck. Moved it to stay "just past" the new 90s value (2 min, a 30s
margin, same proportion as the old 60s margin over 5 min) rather than leaving
it at 6 min, which would have meant a wedged-but-technically-alive worker
could poll for 4.5x longer than a merely-dead one needs. Added
`test_the_warm_wait_ceiling_still_sits_just_past_the_abandoned_threshold`
asserting both that it stays above `ABANDONED_RUN_S` and that the gap between
them stays bounded (<=60s), so the two cannot silently drift apart again.
Updated the one `server-api.md` cross-reference citing "6 min".

**Pre-existing, unrelated test failures found and left alone.** While running
the full freshness/scan-on-demand test files to check for regressions,
`tests/test_index_scan_on_demand.py` showed 5 failures. Reproduced on a clean
`git stash` of every workstream-D change (i.e. against this branch's own
prior commit, `3aea728f6`) — identical failures, so this is pre-existing
breakage on this branch unrelated to any constant changed here, not something
introduced this session. Out of scope for workstream D; not touched.

**Scoped tests run for workstream D (all passing unless noted above):**
`tests/test_index_freshness.py` (26), `tests/test_index_api.py` (118),
`tests/test_index_runner.py`, `tests/test_index_store.py`,
`tests/test_index_rank_concurrency.py`.
