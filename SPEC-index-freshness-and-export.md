# Index freshness, app export, and path bar

Five independent workstreams. Commit each as its own logical unit. They touch
mostly disjoint files; build them in the order below.

## Ground rules

- Read `skills/setting-up-dev-env` **first** and follow it before running any test.
- Read `fused_render/index/specs/*.md` (`overview.md`, `scan.md`,
  `scan-incremental.md`, `index-store.md`, `server-api.md`) before touching
  `fused_render/index/`. These specs are normative — if a change makes a spec
  stale, update the spec in the same commit.
- TDD: write the test, watch it fail, implement, watch it pass.
- **Scoped tests only** in your inner loop (`pytest tests/test_index_runtime.py -k ...`,
  a single vitest file). Never run the full suite — the orchestrator does that once.
- **No history in comments, code, specs, or commit bodies.** Describe behaviour as
  it is. Never write "previously", "used to", "this used to be 55", or reference
  PR numbers. A reader should not be able to tell a change happened.
- Append anything you learn that this spec got wrong to `DECISIONS-index-freshness.md`
  in the worktree root.

---

## A. Compaction is invisible to the liveness watchdog

**Bug:** a scan over ~600k files reports `"the scan worker died without finishing
(no activity for 300s)"` in the Notifications panel while the worker is alive and
compacting normally.

**Cause:** `_looks_abandoned` (`fused_render/index/runner.py:407-413`) decides
liveness from the newest mtime of entries directly inside the per-run directory.
`_compact_locked` (`fused_render/index/store.py:421-593`) writes into
`cfg.files_dir` and `dirs.parquet` — outside `run_dir` — and emits only two phase
events for the whole operation: `"writing index"` (`store.py:510`) and
`"writing signatures"` (`store.py:554`). Between them sits one monolithic DuckDB
merge/sort/dedup plus a per-partition parquet write. Nothing touches `run_dir` for
the duration.

**Fix:** give compaction a heartbeat visible to the watchdog. Emit progress from
inside `_compact_locked`'s partition loop (`store.py:536-551`) — it already
iterates per partition, so emit after each one. Route it through the same
`_emit`/progress channel the scan walk uses so it lands in `events.jsonl`.

`compact()` is called from `scan.py:565` and must stay usable when no run context
exists — thread the emit callback through optionally and no-op when absent.

**Do not** raise `ABANDONED_RUN_S` as the fix for this. It is adjusted separately
in workstream D for its own reasons.

**Tests:** extend `tests/test_index_runtime.py`. Assert that a compaction over a
corpus large enough to span multiple partitions writes progress events to
`events.jsonl`, and that `_looks_abandoned` stays false across it.

---

## B. Export a `.fused` app to disk, not through the browser

**Bug:** downloading an app produces a file that is not searchable for ~110s, and
no notification. The app runs in a real OS browser (`fused_render/app.py` calls
`webbrowser.open`); there is no Electron shell, so the browser owns the save and
the real path is unobtainable.

**Change the export to a server-side write.** Decided by the user: the backend
writes the file itself and reports the real path.

### Backend

`fused_render/server/routers/appfile.py:84-129`
(`api_appfile_export_with_preview`) currently builds into `tempfile.mkdtemp(...)`,
returns a `FileResponse` attachment, and `rmtree`s the temp dir.

Add a sibling route that instead:

1. Resolves a destination directory — the platform Downloads dir
   (`~/Downloads`, `%USERPROFILE%\Downloads`). Follow whatever convention the
   codebase already uses for user directories; if none exists, put the helper
   next to the route and keep it small.
2. Writes via the existing `export_app_file()` (`fused_render/appfile.py:289`),
   resolving filename collisions rather than clobbering (`App.fused`,
   `App (2).fused`, …). **Never overwrite an existing file.**
3. Calls `note_index_mutation(dest_dir)` (`fused_render/server/index_touch.py:309`)
   synchronously after the write. This is the whole point — it reuses the
   coalesced, floor-protected queue that every `fs_mutate.py` route already uses,
   and bypasses the freshness gates entirely.
4. Raises a notification job row via `jobs.upsert(..., page=<real file path>,
   origin=..., server=True)` (`fused_render/jobs.py:525`), matching how AI
   image/video renders point at their output file.
5. Returns the real absolute path in its JSON response.

Keep the existing download route working — it is still referenced and removing it
is out of scope.

### Frontend

`frontend/src/platform/lib/api.ts:2207` (`downloadAppFile`) does fetch+blob+anchor.
Add the server-side call alongside it and switch the export button
(`frontend/src/platform/ui/AppPreviewCard.tsx:441-459`, via
`exportAppFile` in `frontend/src/platform/lib/appShot.ts:99`) to use it.

Raise a notification with **two** actions: **Open file** and **Reveal folder**.

`notify()`'s `NotificationInput` (`frontend/src/platform/lib/notifications.ts:47`)
exposes only one `action` slot, always rendered as `navAction`. Extend
`NotificationInput` with a second action and thread it into `NotificationCard`'s
existing `extraAction` slot — the component already supports it
(`frontend/src/platform/ui/NotificationCard.tsx:54`), and
`frontend/src/shell/RepoUpdatesDock.tsx:439-455` is a working two-action example
to copy. Do not invent a new card.

Wire the two actions to whatever in-app routes already open a file and reveal a
folder in the explorer. Find them; do not add new ones.

**Tests:** a backend test that the route writes the file, does not clobber an
existing one, and calls `note_index_mutation` with the destination. A frontend
test that the two-action notification renders both buttons.

---

## C. Path bar drops the filename

**Bug:** clicking the path bar while viewing a file seeds the editable input with
the parent folder (`~/Fused/sandbox/Surya/fused-share`) instead of the full file
path (`.../fused-share/index.html`) that the crumbs just displayed.

**Cause:** `SearchField` takes `fsPath` (search scope) and `crumbsFsPath`
(display) separately (`frontend/src/apps/explorer/SearchField.tsx:116-124`).
`FileSearchField.tsx:67-68` sets `fsPath={dirname(file)}` and
`crumbsFsPath={file}`. Resting crumbs correctly render
`crumbsPath = crumbsFsPath ?? fsPath` (`:237`, used `:643`), but the plain-focus
branch of `onFocus` seeds from the raw scope prop:

- `SearchField.tsx:677` — `setQuery(contractHome(fsPath, home))`
- `SearchField.tsx:688` — `isPristineQuery(query, fsPath, home)`

**Fix:** seed both from `crumbsPath`.

**Verify before committing:** this value doubles as the search query, and a
committed query searches against `fsPath` (the parent). Check every consumer of
`isPristineQuery` and the commit path to confirm seeding the full file path does
not change what a committed query does. If it does, the fix must preserve
scope-vs-display separation — seed the display value while keeping the search
scope on the parent. Record what you found in `DECISIONS-index-freshness.md`.

Folder views pass no `crumbsFsPath` (`Listing.tsx:1903-1922`) so the two are
identical there — do not regress that. Ctrl-L / Breadcrumb click-to-edit is
already correct (`Breadcrumb.tsx:694`) — leave it alone.

**Tests:** a frontend test that clicking the bar over a file seeds the full file
path, and that folder views and Ctrl-L are unchanged.

---

## D. Relitigate the unmeasured indexing constants

Five constants trace to filed incidents or profiled measurements. **Do not touch
these, and do not weaken them indirectly:**

| Constant | Why it stays |
|---|---|
| `_renice_self`, `_set_background_io_policy` (`index/worker.py:62-99`) | Fixed measured 4s+ `/api/index/rank` stalls from CPU/IO starvation |
| `MAX_COMPACTION_THREADS = 4` (`index/store.py:33-37`) | Other half of that same fix, with a regression test |
| `MIN_INTERVAL_S = 60` (`index/freshness.py:81`) | Set from a profiled 588k-file corpus |
| `FRESHNESS_DELAY_S = 3` (`routers/index.py:1287`) | Fixed a reproduced 2-3s stall + CPU spike |
| `MUTATION_SCAN_FLOOR_S = 20` (`index_touch.py:80`) | Stops a 2s editor autosave rewriting a 571k-row store |

The rest were never measured. Change these:

**`FRESHNESS_CHECK_S = 55` (`routers/index.py:1240`)** — `_freshness_due` stamps
the check clock when a check comes due whether or not it scans, so a 55s check
under a 60s scan floor yields a ~110s effective cadence. Raise it above the floor
(62s) so the intended ~60s cadence holds. The code comment at `index.py:1222-1231`
documents the mechanism; update that comment to describe the new behaviour
without narrating the change.

**`QUIET_S = 30` (`index/freshness.py:43`)** — defers any directory whose mtime is
under 30s old, which is exactly the directory a user just changed and is most
likely to search. This is the primary reason a fresh file is not findable.
Reduce it substantially (target ~2-3s). It exists so a continuously churning
directory does not trigger a scan on every check; `MIN_INTERVAL_S = 60` already
provides that protection independently, so the quiet window only needs to outlast
a single write burst, not 30 seconds of it.

**`SCAN_DEBOUNCE_S = 15 * 60` (`routers/index.py:297`)** — startup-only debounce.
Reduce to ~5 min. Its stated job is stopping a dev-server reload loop from queuing
scan after scan; 5 min does that just as well.

**`COALESCE_S = 1.5` (`index_touch.py:57`)** — leave as is. It is already short,
and shortening it trades away burst batching for no user-visible gain.
Note this decision in `DECISIONS-index-freshness.md`.

**`DEFER_DEADLINE_S = 120` (`index_touch.py:71`)** — leave as is. It is a safety
valve against a wedged worker, not a latency knob. Note the decision.

**`ABANDONED_RUN_S = 300` (`index/runner.py:384`)** — with workstream A giving
compaction a heartbeat, the long silent gap this had to tolerate is gone. Reduce
to ~90s so a genuinely dead worker is reported promptly. **Sequence this after A
is committed and its tests pass** — lowering it first would make the false-death
toast fire sooner. `server-api.md` documents `WARM_WAIT_DEADLINE_S` as "just past
`ABANDONED_RUN_S`" — re-check and update that relationship.

**Tests:** for each changed constant, a test asserting the behaviour it now
produces. For `FRESHNESS_CHECK_S`, assert the effective cadence, not the literal
value — a test that just reads the constant back is worthless.

---

## E. Indexing performance harness

No indexing benchmark or perf harness exists (`scripts/` has none; the only
indexing perf assertion is
`tests/test_index_rank_concurrency.py::test_rank_route_stays_fast_while_a_real_scan_is_running`,
which starts a real scan over a synthetic tree and asserts
`max(latencies) < 2.0`).

Workstream D loosens four unmeasured constants with nothing guarding the
interactive-search latency they were balanced against. Build the guard.

Generalise the pattern in that existing test into a reusable harness:

- builds a synthetic corpus of configurable size,
- runs a real scan while sampling `/api/index/rank` latency,
- reports p50/p95/max and asserts a ceiling.

Put it where the project's conventions say it belongs — a `tests/` helper module
importable by tests, plus a `scripts/` entry point if `scripts/` has a convention
for runnable tools. Keep the existing test passing; refactor it onto the harness.

Add at least one test using the harness that covers the freshness path made more
eager by D, so a future loosening that starves interactive search fails here.

Size the default corpus so the test runs in seconds in CI, and make the large
size opt-in via a marker or env var. Do not add a multi-minute test to the
default suite.

---

## Out of scope

- Adding a real filesystem watcher (inotify/FSEvents). `index/fsevents.py` is a
  history replay consulted inside a running scan, not a live watcher. Building one
  is a separate project.
- Removing the existing browser-download export route.
- Touching the five evidenced constants in the table above.
