# Incremental reuse

> **Status — shipped.** This file owns **how a rescan avoids work**: the per-directory
> signature cache, the mtime reuse rule, and the macOS FSEvents fast path. Implementing
> modules: `store.py` (`load_dir_cache`), `scan.py` (`scan_dir_once`, `_dir_sig`,
> `_run_fsevents`), `fsevents.py` (`hint`, `_replay`, `current_id`, `device_uuid`,
> `load_states`, `save_state`). Run orchestration is `scan.md`; the parquet files named
> here are `index-store.md`.

## 1. The three strategies

A run picks exactly one, in this order (`run_scan`):

| Strategy | Chosen when | Directories visited |
|---|---|---|
| `scanning (full)` | no usable cache, or `full=true`, or the ignore list changed (`scan-ignore.md §4`) | every directory under root |
| `scanning (incremental)` | a dir cache for this root exists | every directory, but unchanged ones are stat-only |
| `scanning (fsevents journal)` | incremental **and** `fsevents.hint` returns a usable change set | only directories the OS journal reports |

The chosen strategy is announced as a `phase` event.

## 2. The mtime reuse rule

`load_dir_cache` reads `dirs.parquet` into `{dir: (mtime_ns, n_files, n_subdirs)}`,
restricted to the scan root's subtree and filtered through the ignore list. A directory
is **unchanged** when its `st_mtime_ns` equals the cached value:

- **Unchanged leaf** (`n_subdirs == 0`) → one `stat`, nothing else. Kind `"u"`.
- **Unchanged non-leaf** → `scandir` for subdirectory names only (no per-file `stat`,
  no rewrite of its file rows), then recurse. Kind `"u"`.
- **Changed, or absent from cache** → full `scandir` with a `stat` per entry, producing
  file rows, a directory signature, total size, `mtime_ns`, and `n_subdirs`. Kind `"s"`.

This is sound because on every filesystem this targets, adding, removing, or renaming a
child bumps the parent's mtime. It does **not** catch an in-place edit of a file's
contents that leaves its size and mtime intact.

**`n_subdirs == -1`** marks a pre-upgrade cache row whose subdir count is unknown; such
a directory is rescanned once so the count backfills. This is also why the ignore list
is fingerprinted: a cached `n_subdirs` computed under different prune rules would keep
a re-included folder invisible forever (`scan-ignore.md §4`).

**`_dir_sig`** is a sha1 over the directory's sorted `(name, size, mtime_ns)` entries,
with subdirectories contributing `name/` and zeros. It is not used to decide reuse — the
mtime is — it exists so compaction can report changed/added/removed directory counts
(`index-store.md §4`).

## 3. FSEvents fast path (macOS)

macOS keeps a persistent per-volume change journal. `fsevents.hint` replays it so a
rescan can visit only what actually changed, skipping even the stat-per-directory pass.
The libraries are loaded lazily through `ctypes` (`CoreFoundation` + `CoreServices`);
on any non-darwin platform every entry point returns `None`.

**Best-effort by construction.** Every ctypes call sits inside a bare `except
Exception` that returns `None`: a renamed symbol or an OS change must cost the scan its
speed, never its correctness.

**State** lives in `fsevents.json` under the index dir, keyed by root:
`{event_id, uuid, devs, updated}`. The event id is captured **before** scanning, so
events that land mid-scan are replayed — harmlessly re-checked — next time rather than
missed.

**Replayed alongside the cache read, not after it.** `load_dir_cache` (parquet IO) and
`hint` (a CFRunLoop draining the journal) are independent and both release the GIL, so
`run_scan` runs them on two threads and pays `max()` rather than the sum — measured
~0.75 s and 0.1–2.9 s respectively on a 588k-file index. A run that has already decided
to rescan everything (`full`, or the ignore rules changed) skips the replay entirely:
both facts are known before either read starts, there is no cache read to hide the
replay behind, and a full scan must stay usable as the remedy for a corrupt
`fsevents.json` — `hint` parses that file with no guard of its own. An empty cache is
the one case still replayed for nothing, since only the read can report it.

**Any doubt falls back to a walk.** `hint` returns `None` when: no saved event id for
this root; the root spans more than one device (per-device ids don't apply); the volume
UUID differs from the saved one; or the replay itself bails. `_replay` bails on
dropped-event flags (user or kernel), a wrapped id, a root-changed flag, a 20 s timeout,
or more than 200 000 events — at which point a plain walk is cheaper anyway.

**Per-path handling** of the replayed set:

- `MustScanSubDirs` (flag `0x01`) → treat as a **subtree** to walk (the journal admits
  it lost detail here).
- otherwise → a **forced** directory: rescanned ignoring its cache entry.

## 4. Reconciling what wasn't visited

The fast path visits a fraction of the tree, so it must explicitly account for
everything else, or unvisited directories would silently vanish from the index:

- **Deleted** — a visited path that can't be stat'd, plus every cached descendant of
  it (found by bisect over the sorted cache keys), is dropped.
- **Missing children** — for each visited directory, cached children no longer present
  on disk are dropped with their subtrees.
- **Kept** — every remaining cached directory is added to the run's *keep* list, which
  is what tells compaction to carry its existing file rows forward unchanged
  (`index-store.md §4`).

Because ignored directories are filtered out of the cache at load time (§2), they never
reach the keep list — which is precisely how newly-ignored folders get purged from an
existing index without a full rescan (`scan-ignore.md §4`). Mount-backed paths are
refused here too, by the same `MountGuard` the walk uses: a journal replay is exactly
the route by which a path can arrive without its ancestors having been checked.

## 5. Open-folder freshness

> Implementing module: `freshness.py` (`enclosing_root`, `indexed_mtime_ns`,
> `note_folder_opened`); the server glue is `routers/index.py`
> (`note_folder_opened`, `_run_freshness_check`), called from `GET /api/fs/list`.

Between scans the index is a snapshot, so a folder changed out of band answers search
from stale rows. Opening a folder is the signal that its slice has to be fresh, so the
listing request checks it and, when it is behind, starts the **ordinary incremental
scan** of its enclosing configured root. Deliberately not a new scanning path: §2 and §3
already narrow a rescan to what moved, and a subtree-scoped run would need its own copy
of every root-keyed store (`scans.json`, the applied-ignore map, the FSEvents position).

Detection is the §2 rule read backwards: `dirs.parquet` already stores `mtime_ns` per
directory, so it is one indexed row lookup against one `os.stat` — no walk. Three
values read as "nothing to compare against" and trigger nothing: no index, no row for
this directory (an uncovered folder is scanned on demand the moment somebody searches
it, `server-api.md §7.2`, so this cheaper check has nothing to add), and a stored `0` (the placeholder a partition
predating the column compacts to — reading it as a real mtime would make the folder
stale forever and fire on every open).

Five gates, cheapest first, because `/api/fs/list` fires for every folder opened **and
again on every watch tick of a folder on screen**:

0. `indexing_enabled` (`shell/prefs.py`) is on — the router's `note_folder_opened` checks
   this before even trying to acquire the one-at-a-time slot below, so a listing burst
   while the pref is off never contends for it, let alone reaches a scan. This is the one
   gate that covers every caller of `note_folder_opened` (`fs_read.py`, `git_repos.py`,
   `exported_apps.py`) in one place, rather than each of them re-checking it.
1. The path is inside a configured root (segment-wise), else nothing. The scan started
   is always that **configured root** — a per-folder root would pollute `scans.json`
   and defeat `runner.start`'s exact-match join.
2. Not mount-backed. This is checked before any syscall on the path, by the same pure
   string `MountGuard` §4 uses: `os.stat` under a wedged rclone mount blocks its thread
   indefinitely, and this one is serving a listing.
3. `MIN_INTERVAL_S` (60 s) since any scan of that root, read off `scans.json` — so no
   state file of its own, and a scan that just ran for any reason suppresses a trigger.
   `routers/index.FRESHNESS_CHECK_S` (62 s) paces the checks in memory, and sits *above*
   `MIN_INTERVAL_S` plus the spawn offset on purpose: the check clock is stamped whenever
   a check comes due, whether or not that check then scans, so a value at or below
   `MIN_INTERVAL_S` would waste every other check on a scan floor not yet clear and double
   the real cadence. Kept above it, every check that comes due finds the floor already
   clear, so the folder-open rescan cadence tracks `FRESHNESS_CHECK_S` itself — about a
   minute, matching `MIN_INTERVAL_S`'s own floor. Scanning sooner is also *cheaper*, since
   both dominant scan costs (the journal replay and the visit set it names) scale with the
   window since the last one.
4. `QUIET_S` (3 s) since the directory's own mtime moved. A build tree's mtime never
   stops moving, so it is never quiet and never triggers; the open after the churn stops
   still does. Only needs to outlast a single write burst — `MIN_INTERVAL_S` above
   already stops a churning directory from queueing scan after scan, independently.

A live run of the root is refused rather than joined, the check runs on a background
thread throttled to one at a time, and nothing about the listing waits on it or fails
with it. That thread also waits `FRESHNESS_DELAY_S` (3 s) before it commits, so the scan
it may start lands after a page's opening burst rather than inside it — `/home` draws one
folder card per Claude session and so lists a fistful of folders on a plain refresh. The
wait sits **after** the gates that can refuse for free (root membership, and a
non-stamping peek at the check interval) and **before** the stamp, so a check that was
never going to act does not hold the one-at-a-time slot for three seconds. Waiting costs
nothing in accuracy: the comparison is folder mtime against the *index*, not against when
the folder was opened. It does widen the window in which a `/api/git-repos` rotation turn
is lost to a failed acquire — see the note on that function.

**Who fires it:** `/api/fs/list`, naming the folder just opened, and `/api/git-repos`,
naming **one configured root per request, round-robin** — that tab is served entirely
from the index and has no folder to name, so without it the homepage's Repos list could
never notice a stale index. One root, not all of them, because the slot admits a single
check at a time and is released by that check's own thread: offering the whole list in a
loop checks the first root and loses every other one on a failed acquire, on every
request. The root form catches little anyway (a root's mtime moves only when its own
direct entries change); it is cheap, and the tab has no better signal.

**Bound, by design:** a directory's mtime moves only when entries are added, removed or
renamed *in that directory*. An edit five levels down does not flip the mtime of the
folder being viewed. This makes the index fresher; it does not make it correct, and the
`fresh`/`covered` flags of `query.md §6` still describe the honest state.

## 6. Live watcher

> Implementing module: `server/index_watch.py` (`WatchLoop`, `make_dropped`, `start`,
> `stop`); feeds `server/index_touch.py`'s `note_index_folders` / `RescanQueue`.

§5 answers "is this folder stale?" only when something asks — a folder someone has open.
Nothing above notices a change to a folder nobody happens to visit, and SPEC-index-live-
watch.md §1 documents three earlier attempts to answer that with a better time constant,
each shipping green unit tests and failing the same real test: no clock-based guess can
tell a genuinely quiet folder from one that changed five seconds after the last check.

The watcher instead observes the real change stream — `watchfiles` (the `notify` Rust
crate: FSEvents on macOS, inotify on Linux, ReadDirectoryChangesW on Windows) — one
`WatchLoop` per configured root (`server/routers/index.py`'s `scan_roots`), running on
its own background thread from app startup to shutdown. A batch of raw events is
filtered at arrival by the same two structural refusals the rest of the index uses
(`ignored_for_index(..., tree=True)`, `MountGuard`) — critically including the index
store's own directory, without which a scan's own write would trigger the scan that
triggered it — then reduced to parent folders and forwarded to `note_index_folders` no
more often than a 30s flush floor. A burst that touches more folders than
`MAX_FOLDERS` collapses to the whole root, exactly as `RescanQueue`'s own overflow
handling does when a mutation endpoint touches too many folders at once (§ above); both
share one "does folder A already cover folder B" definition
(`index_touch.outermost_folders`).

An hourly per-root safety net (`WATCH_RESCAN_S`) forces a rescan on an idle tick if nothing
has forwarded one that long and no run already covers the root — the one time-based
trigger left, and deliberately a floor under the watcher rather than the mechanism: it
catches a change made while the process was off, or a kernel-dropped event, and nothing
else. A watch that raises (a mount disappearing, a permissions change) forwards the whole
root and backs off on an escalating schedule (5s/30s/120s, reset the next time a tick is
delivered) rather than spinning; the indexing-enabled pref is polled every 30s so toggling
it in the sidebar takes effect without a restart.

**Non-recursive fallback (Linux only, code-reviewed but not measured on this branch's
dev machine, which is macOS):** `watchfiles` has no depth limit — `recursive` is
all-or-nothing — so a home tree that exhausts `fs.inotify.max_user_watches` cannot fall
back to "watch shallower, still recursively." On that specific error the loop instead
opens a non-recursive watch of the root plus its immediate non-ignored subdirectories,
which still catches `~/newfile.txt` and `~/Downloads/x.dmg`; anything deeper is left to
the hourly safety net.

## Non-goals

- **Choosing strategies / emitting phases** — `scan.md §1`, `§4`.
- **Prune rules** — `scan-ignore.md`.
- **How keep/drop decisions become parquet** — `index-store.md §4`.
- **Content-level change detection** (hashing file bodies) — not implemented; reuse is
  metadata-only, see §2.

## Open questions

- A file edited in place with identical size and mtime is not detected.
- FSEvents state is keyed by root string, so scanning `~` and `~/Documents` keeps
  independent journal positions.

## See also

- `scan.md` — the lifecycle that selects a strategy.
- `index-store.md` — `dirs.parquet`, the cache's storage.
- `scan-ignore.md` — why a rules change forces a full rescan.
- `platform.md` — why §3 is macOS-only, and what other platforms fall back to.
