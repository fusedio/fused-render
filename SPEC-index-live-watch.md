# SPEC — a live filesystem watcher keeps the file index fresh

Branch: `index-live-watch` (off `origin/main` at `5280de0e2`).
Worktree: `/Users/iamsdas/Work/fused-render/.claude/worktrees/index-live-watch`.

## 1. Problem

Home search is index-backed, and nothing refreshes the index when a file
lands from outside the app (a download, `touch ~/a.txt`, a sync client).
Every existing trigger is a *pull* that guesses staleness from a clock:

| trigger | where | what it can see |
|---|---|---|
| startup scan | `server/routers/index.py:run_startup_scan` | once per boot, `SCAN_DEBOUNCE_S = 300` |
| in-app mutations | `server/index_touch.py:note_index_mutation` | only edits THIS app makes |
| folder-open freshness | `index/freshness.py:note_folder_opened` | only the folder being listed; `MIN_INTERVAL_S = 60` |

PR #1221 (branch `worktree-focus-change-detection`, now parked as draft) tried
a fourth pull trigger on window focus. Three designs in a row shipped green
unit tests and failed the user's real test, because **no time constant can
answer "did anything change?"** — a 300 s floor refused a file created 12 s
after a scan, and a floor small enough to catch it rescans forever. The
lesson is recorded there (`DECISIONS.md` on that branch, 2026-09-19 to
2026-09-21) and is the premise here: **stop guessing, observe.**

Everyone who does this well (Spotlight, Everything, Watchman, git's
fsmonitor daemon, Syncthing) runs a persistent watcher that consumes the
change stream *live*, keeps a small dirty set, settles, and flushes. On
overflow they mark the root dirty and recrawl. A periodic full crawl is a
safety net, never the trigger.

## 2. Measured facts you must not re-derive wrongly

All measured on the developer's real `~` (78,717 non-ignored dirs):

- A whole-root **incremental** walk is **4.37 s**; the compaction that ends
  every scan rewrites the whole store (`index_touch.py:73-88` explains why
  the cost of a rescan is a function of the index, not the folder).
- FSEvents replay cost tracks **event count, not elapsed time**, and event
  density under `~` swings **60x within minutes** (363 events in a 1.5-min
  window vs 67k–78k in a 5.5-min window taken moments later). A cursor left
  idle for hours returns `None` (200k cap) exactly when new files are most
  likely missing. `index/fsevents.py:hint()` is therefore a scan
  **narrower** used inside the worker with the scan's own fresh cursor,
  never a gate. Do not build anything that replays from a stale cursor.
- A directory's mtime moves only when its DIRECT entries change, so a
  `stat` of `~` sees `~/a.txt` but never `~/Downloads/foo.dmg`.
- `watchfiles 1.2.0` is already installed in the dev venvs as uvicorn's
  transitive dependency but is **not declared** in `pyproject.toml`
  (`uvicorn>=0.29`, plain, line 24). It wraps the Rust `notify` crate:
  FSEvents on macOS, inotify on Linux (one watch per directory, added
  recursively by the crate), ReadDirectoryChangesW on Windows. Signature:
  `watch(*paths, watch_filter=..., debounce=1600, step=50, stop_event=None,
  rust_timeout=5000, yield_on_timeout=False, force_polling=None,
  recursive=True, ignore_permission_denied=None)`, yielding
  `set[(Change, path)]`. `Change` is added/modified/deleted only; overflow
  and internal errors surface as `WatchfilesRustInternalError` or the
  generator ending — verify this against the installed package before
  relying on it and write down what you find.

## 3. Design

### 3.1 One new module: `fused_render/server/index_watch.py`

A daemon thread started from `create_app`'s startup handlers (pattern:
`app.py:437-442` `_startup_resurrect_background_apps`, and the index hooks
at `app.py:~938-955`), stopped from a shutdown handler via a
`threading.Event`. Tests build apps without lifespan, so no watcher ever
starts in tests — same convention every other startup hook relies on.

The loop, per configured root (`index_routes.scan_roots(load_config())`):

1. **Gate.** While `index_gate.indexing_blocked()` is truthy (pref off, or no
   Full Disk Access on the packaged mac app), do not open a watch; poll the
   gate every 30 s. Re-check `indexing_allowed()` on every flush too — the
   pref can flip at runtime.
2. **Watch** with `watchfiles.watch(root, watch_filter=<ours>, stop_event=stop,
   rust_timeout=5000, yield_on_timeout=True, ignore_permission_denied=True)`.
   `yield_on_timeout=True` makes the loop tick every 5 s with an empty set,
   which is what drives the flush floor and the periodic rescan below
   without a second timer thread.
3. **Filter at arrival** (`watch_filter`). Drop a path when
   `ignore.ignored_for_index(cfg.rules, path, tree=True)` says so, or when
   `MountGuard(mounts_dir=runner._mounts_dir()).blocks(path)`. Read the
   `tree=` semantics in `ignore.py:119` and use the form that means "this
   path or any ancestor is ignored". The app's own state folder
   (`~/.fused-render`, D548) and the index store under it (`cfg.dir`) are
   already in `default_ignore()`; **this is load-bearing** — a scan writes
   parquet into `cfg.dir`, and without the filter the watcher would trigger
   the scan that triggers the watcher. Test it explicitly (§5).
4. **Reduce each batch to folders** with `index_touch._folder_of(path)` (the
   parent, always — a scan recurses, so a new directory is covered by
   scanning its parent). Accumulate into a pending set.
5. **Flush floor.** Forward the pending set no more often than
   `WATCH_FLUSH_FLOOR_S` (start at **30.0**). Rationale in the constant's
   comment: `RescanQueue`'s 20 s floor is per *folder*, but the expensive
   part of any scan is the whole-store compaction, so churn across many
   different folders needs one global floor. Measure compaction time from a
   real run's `events.jsonl` (`run_end` summary) and record it in
   `DECISIONS.md`; if 30 s gives a worst-case duty cycle above ~10 %, raise
   it and say why.
6. **Forward to the existing policy.** Add
   `RescanQueue.note_folders(*folders)` to `index_touch.py` (folders taken
   as-is, no `_folder_of`), and a module-level `note_index_folders(...)`
   mirroring `note_index_mutation`'s gate. `RescanQueue` already does
   outermost-only collapse, "wait out a live run covering it", the per-folder
   floor, the deferral deadline, the mount / ignored / foreign-device refusal
   (`_real_blocked`), and wakes the job bridge so the search box shows
   "indexing…". Reuse it; do not write a second queue.
7. **Collapse to the root** when a single flush holds more than
   `index_touch.MAX_FOLDERS` distinct outermost folders: forward `{root}`
   instead. One whole-root incremental walk (4.4 s) beats sixteen folder
   scans each ending in its own compaction, and it is also the honest answer
   to a burst we cannot attribute.
8. **Overflow and errors.** If the generator raises or ends while `stop` is
   not set: log once at WARNING (include the exception text), forward
   `{root}` (an incremental root scan is the recrawl every watcher falls
   back to), back off (5 s, then 30 s, then 120 s, capped) and reopen the
   watch. On Linux an `ENOSPC`-class failure at open means the inotify watch
   limit; name `fs.inotify.max_user_watches` in that one log line and fall
   back to the shallow watch (§3.2).
9. **Periodic safety net.** On each tick, if `runner.last_scan(cfg, root)` is
   older than `WATCH_RESCAN_S` (start at **3600.0**) and no run is live for
   it, forward `{root}`. This is the Syncthing-style backstop for changes
   missed while the server was off (already covered by the startup scan) or
   dropped by the kernel. It is the only time-based trigger left, and it is
   a floor, not the mechanism.

### 3.2 Linux shallow fallback

`watchfiles` has no depth limit — `recursive` is all-or-nothing. When the
recursive open fails with the watch-limit error, open **non-recursive**
watches on the root and on each of its immediate non-ignored subdirectories
(a few hundred watches, well under any default). That still catches
`~/Downloads/foo.dmg` and `~/a.txt`; deeper changes fall to the periodic
rescan. Implement this as a list of paths passed to one `watch(...)` call
with `recursive=False`. Log the degraded mode once.

### 3.3 Ignore rules

Add `~/Library/Caches` to `ignore.default_ignore()` as a path pattern
(`ignore.py:339` documents that a `~`-prefixed pattern matches that path
only). Do **not** widen to the whole `~/Library` — that was tried on the
parked branch and reverted because it hid legitimately-indexed content.
Write down the known caveat in `DECISIONS.md`: `default_ignore()` is a
`default_factory` consulted only when the saved config has no `ignore` key,
so a user who ever pressed Save in the Indexing panel carries a frozen list
and does not receive this pattern. Changing how saved configs merge defaults
is a separate change; do not make it here.

Adding a pattern changes `IgnoreRules.sig()`, so the first scan after
upgrade is a full rescan for users on the default list. State it in the PR.

### 3.4 Dependency and bundle

- `pyproject.toml`: add `"watchfiles>=1.0"` to core `dependencies` with a
  comment saying why it is core (the index is core; the watcher is what
  keeps it honest) and that uvicorn only ships it under an extra we do not
  use.
- `scripts/setup_py2app.py` derives forced packages from
  `importlib.metadata` over the build venv (`bundled_force_lists`, lines
  ~207-260). `watchfiles` has a native extension `_rust_notify`. Read the
  derivation and confirm — by running the derivation function in the venv,
  not by reasoning — that `watchfiles` lands in the forced list. If it does
  not, add it the way the file's own comments say to, and record what you
  found. The `bundle-contents` CI job is the remote check.

### 3.5 Prose that becomes false

- `server/index_touch.py:1-3` says "There is no filesystem watcher
  (index/specs/scan.md)". Correct both places.
- `index/specs/scan-incremental.md §3` (FSEvents fast path) and `§5`
  (open-folder freshness): add a short §6 "Live watcher" describing the
  trigger and its relationship to the fast path (the watcher keeps the
  scan's own cursor fresh by triggering scans often, which is what keeps
  `hint()` cheap — replay cost tracks event count).

### 3.6 Non-goals

- No change to `spec.json`, `runner.start`, or the worker. A narrowed
  scan is not expressible through them today, and on macOS the worker's
  own `fsevents.hint` narrows the walk whenever the cursor is fresh — which
  a live watcher makes the common case. Folder-sized scans via
  `RescanQueue` cover the rest.
- No client change. The "indexing…" state already flows through the index
  job bridge.
- No Spotlight/`mdfind` adjunct, no hot-directory stat at query time. Both
  are recorded as follow-ups in `DECISIONS.md`, not built.
- Nothing from PR #1221's focus trigger is ported. It is redundant once a
  watcher exists.

## 4. Environment setup (first thing, before any test)

Follow the repo skill `.claude/skills/setting-up-dev-env/SKILL.md`,
substituting `bun` for every `npm`/`npx` (project-wide rule):

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e ".[dev,bundled,fused]"
cd frontend && bun install && bun run build && cd ..
ls fused_render/static/shell-dist/index.html
```

Never start the dev server or any server process; the user runs `dev.sh`
themselves. Never `cd` into another worktree or the main checkout. Never
use bare `git stash`. Run only the tests you touched (`-k`, file paths); the
full suite is the orchestrator's job. Use `.venv/bin/python -m pytest`.

## 5. Tests (TDD: write each, watch it fail, then implement)

`tests/test_index_watch.py`, driving the policy with injected dependencies
the way `tests/test_index_touch.py` drives `RescanQueue` (fake `now`, fake
`forward`, fake `last_scan`, fake `live`, an in-memory event source instead
of `watchfiles.watch`). Cover:

1. A batch of file paths is reduced to parent folders and forwarded once.
2. Paths under an ignored tree (`node_modules/...`, a `~/Library/Caches`
   child) and under `cfg.dir` are dropped by the filter — the **index store
   writing itself must never trigger a flush**.
3. Two batches inside `WATCH_FLUSH_FLOOR_S` forward once, with the union.
4. More than `MAX_FOLDERS` outermost folders in one flush forwards `{root}`.
5. A source that raises forwards `{root}`, backs off with the documented
   schedule, and reopens; a source that ends with `stop` set does neither.
6. Ticks with no events and `last_scan` older than `WATCH_RESCAN_S` forward
   `{root}` once; a live run for the root suppresses it.
7. Indexing disabled → nothing is opened, nothing forwarded; flipping the
   gate on mid-run opens the watch on the next poll.
8. `RescanQueue.note_folders` takes folders as-is (a root is not turned into
   its parent) and still collapses outermost-only.
9. One real-filesystem test: `watchfiles.watch` on `tmp_path`, create a
   file, assert the folder arrives through the real filter within a bounded
   wait. Mark it with a generous timeout; the Windows CI lane is starved and
   flaky on main already.

Also: `tests/test_index_ignore.py` gets a case for `~/Library/Caches` in
`default_ignore()` and for `~/Library/Documents`-style siblings NOT being
ignored. Update any existing test that asserts the exact default list.

## 6. Live measurement (no server involved)

Write a throwaway script in your scratch directory (not the repo) that runs
`index_watch`'s real filter and batching over the developer's real `~` for
**5 minutes**, printing per flush: raw event count, post-filter folder
count, the folders. Report the numbers in `DECISIONS.md`. This answers the
one open question the design cannot settle on paper: how much of `~`'s
churn survives the ignore rules. If post-filter flushes are firing
continuously from a handful of `~/Library/...` folders, say which ones and
propose (do not apply) additional default ignores.

## 7. Commits

One commit per unit, in roughly this order, each with its scoped tests
green:

1. `Index: declare watchfiles and add ~/Library/Caches to the default ignore list`
2. `Index: RescanQueue.note_folders — accept folders as-is for non-mutation callers`
3. `Index: live filesystem watcher feeds the rescan queue (index_watch.py)`
4. `Index: start/stop the watcher from create_app; Linux shallow fallback`
5. `Index: hourly safety-net rescan; docs and spec prose updated`
6. `DECISIONS.md: index-live-watch — measurements, bundle derivation, follow-ups`

End every commit message with:

```
Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
```

Do not open a PR; the orchestrator does. Do not push; report done at the
last commit boundary. If you pass ~150 turns, commit the unit in progress,
bring `DECISIONS.md` up to date, and report partial-done with the resume
point.

## 8. Report format

What was built (per commit), every deviation from this spec and why, the
exact test commands you ran and their pass counts, the live-measurement
numbers, and — separately and plainly — what you could **not** verify. The
end-to-end check (download a file, search for it) can only be done by the
user on their machine with `dev.sh` running in this worktree; list it as
theirs.
