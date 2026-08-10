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
