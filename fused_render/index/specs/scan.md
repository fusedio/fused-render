# Scan lifecycle

> **Status — shipped.** This file owns the **run lifecycle**: how a scan is started,
> observed, cancelled, reclaimed, and how the worker fans work out across processes.
> Implementing modules: `runner.py` (`start`, `status`, `cancel`, `list_runs`,
> `prune_runs`, `derive_state`), `worker.py` (the entrypoint), `scan.py` (`run_scan`,
> `scan_dir_once`, `_scan_subtree`, `_scan_dirs_threaded`). Which directories get
> *skipped* is `scan-ignore.md`; which get *reused* is `scan-incremental.md`; what the
> worker writes is `index-store.md`.

## 1. Why a detached worker

A full home-directory scan takes minutes; no HTTP request may wait for one. So the
control plane and the work are split:

| Call | Returns | Blocking |
|---|---|---|
| `runner.start(cfg, root, full)` | `{run_id, root}` immediately | no — spawns and returns |
| `runner.status(cfg, run_id, since)` | `{state, events[since:], cursor}` | no — reads the event log |
| `runner.cancel(cfg, run_id)` | `{cancelled}` | no — touches a flag file |
| `runner.list_runs(cfg)` | `{runs}` — 20 most recent | no |

`start` spawns **`python -m fused_render.index.worker <run_dir>`** with the detach
kwargs from `_detach_kwargs` — `start_new_session` on POSIX, creation flags on Windows
(`platform.md §2`) — so the scan outlives the request and any page reload. The module
form (OpenIndex used `Popen([python, __file__, "--worker", …])`) is what makes it work
inside a py2app bundle, where there is no source file to point at.

`start` also **refuses a mount-backed root** (`scan-ignore.md §7`) and records the
start time for the scheduler's debounce (`server-api.md §2`).

## 2. The run directory

One directory per run under `<index>/runs/<run_id>`, where `run_id` is
`%Y%m%d-%H%M%S` plus 3 random bytes hex. Contents:

- **`spec.json`** — `{root, full, started, config, mounts_dir}`. The worker's ONLY
  input: it re-derives nothing from the environment, so a home that moved between the
  request and the spawn cannot make the worker compact into a directory nobody reads.
- **`events.jsonl`** — append-only progress log (§3). The only channel worker → client.
- **`cancel`** — presence is the cancel signal (§5); content is irrelevant.
- **`worker.log`** — the worker's stdout+stderr, for post-mortem debugging.
- **`shards/`** — parquet shards awaiting compaction (`index-store.md §3`).
- **`progress-<pid>.json`** — one per pool child, aggregated by the parent (§4).

**Reclamation.** OpenIndex left run dirs in the system temp dir forever. Here they are
inside the user's index store, so `prune_runs(cfg, keep)` deletes all but the newest
`keep`. A run whose log has no terminal event is kept while it still looks live —
anything untouched for `STALE_RUN_S` (24 h) died without closing its log and its shards
are dead weight.

## 3. The event log

One JSON object per line, each with a `ts`. `read_events` tolerates a half-written
trailing line (skipped, not fatal) and assigns each record its line number as `_i`;
`since`/`cursor` are line-number offsets, so a poller fetches only what it hasn't seen.

| `type` | Fields | Meaning |
|---|---|---|
| `run_start` | `msg` = root | worker began |
| `phase` | `msg` | human-readable stage, e.g. `scanning (incremental)`, `writing index` |
| `progress` | `dirs`, `files`, `reused`, `current` | counters + the directory in flight |
| `run_end` | `msg` = `complete` \| `cancelled` \| `failed`, `summary`, `error` | terminal |

`derive_state` folds the whole log into the flat state a client renders: `running`,
`phase`, `dirs`, `files`, `reused`, `current`, `summary`, `cancelled`, `error`. Folding
is idempotent, which is what makes resume-after-reload work.

A `failed` run carries a formatted traceback in `error`; `run_scan` catches every
exception at the top level so a crash still terminates the log rather than leaving a
poller watching a run that never ends.

## 4. Work distribution

The GIL caps one process at roughly 15k dirs/s, so the worker fans out:

1. **Breadth-first in-process** from the root until the frontier holds `cfg.nproc * 24`
   subtree roots (`nproc = max(2, min(10, cpu_count))`).
2. **Split the giants** — with a cache available, any frontier entry whose cached
   subtree exceeds `cfg.split_dirs` (4 000) dirs is expanded further, so one huge folder
   (`~/Library`) can't serialize the tail of the run.
3. **Process pool** — a `multiprocessing` **spawn** pool over the frontier,
   `chunksize=1`, initialized by `_child_init` (its own dir cache, `Sink`, thread pool,
   and a `MountGuard` rebuilt from `spec.json`). Spawn, never fork: a forked child
   re-runs PROJ's SQLite atfork handler and dies with SIGSEGV.
4. **Thread-pool escape hatch** — `_scan_subtree` walks single-threaded (fastest when
   metadata is warm) but if a 100-dir window takes over 1 s, the rest of that subtree
   is handed to a 16-thread pool, where blocking syscalls overlap because they release
   the GIL. This is what keeps sandbox containers and dataless iCloud directories from
   stalling a run.

**Progress aggregation:** each child rewrites `progress-<pid>.json` atomically
(tmp + `os.replace`) every 500 dirs; the parent globs and sums them every 0.5 s and
emits one `progress` event. Counters can lag slightly but never double-count.

**Scheduling priority:** all that fan-out is CPU the user did not ask to spend, so the
worker calls `os.nice(SCAN_NICE_INCREMENT)` (+10) on itself at startup — in the child,
after exec, never as a `preexec_fn` (which would force the spawn back onto `fork()`).
Pool children and stat threads inherit it, and so does the compaction, which
additionally caps DuckDB to `store.compaction_threads()` (`index-store.md §5`). The
invariant: an interactive `/api/index/rank` keystroke wins the scheduler against a
scan. Without it a full scan of a big home starved the lock-free rank read path for
seconds at a time.

**Device ids** are collected during the walk — a root spanning more than one volume
disqualifies the FSEvents fast path (`scan-incremental.md §3`).

## 5. Cancellation

The client writes the `cancel` flag; every walker polls for it — the in-process walk
every 200 dirs, pool children every 500 dirs, the FSEvents path every iteration. On
cancel the worker stops walking, emits `run_end` with `msg: "cancelled"`, and
**skips compaction entirely**, so a cancelled run leaves the existing index untouched.

## 6. Hardcoded skips and the filesystem boundary

**The walk never leaves the scan root's filesystem.** `run_scan` stats the root once,
writes its `st_dev` into `spec.json`, and every process compares each directory's device
against it (`scan_dir_once`'s `root_dev`); a mismatch is skipped, not indexed, not
descended. A mount — rclone, iCloud, SMB, an external disk — is always its own device,
so this single comparison refuses every mount, including the ones no name list can
predict. It costs nothing: the `stat` it reads is the one that function already takes,
and the check happens at the mount's own directory rather than at its parent, so there
is no extra stat per child either. `devs` therefore stays single-valued, which also
keeps the FSEvents fast path eligible (`scan-incremental.md §3`).

The named skips remain, because they are cheaper still and cover synthetic trees that
are not separate devices:

Independent of the user ignore list (`scan-ignore.md`), `SKIP_DIRS` is never descended
into. These are correctness/safety skips — devices, synthetic filesystems, and mount
points that hang, churn, or duplicate the tree — not preferences, so they are not
user-editable. The set is a cross-platform union (macOS `/System/Volumes`, `/Volumes`,
…; Linux `/proc`, `/sys`, …); an entry absent on this OS simply never matches. Full
table: `platform.md §2`.

Symlinks are never followed (`e.is_symlink()` short-circuits in `scan_dir_once`), which
is what keeps the walk acyclic — and what lets the mount guard settle every in-walk
decision by string comparison (`scan-ignore.md §7`).

## Non-goals

- **What gets pruned by preference** — `scan-ignore.md`.
- **What gets reused instead of re-stat'd** — `scan-incremental.md`.
- **Shard → partition compaction and file formats** — `index-store.md`.
- **Reading the finished index** — `query.md`.
- **Watching the filesystem continuously** — there is no daemon; a scan happens when
  someone (or the startup scheduler, `server-api.md §2`) asks for one.

## Open questions

- A cancelled run's shards are left in the run dir until `prune_runs` reclaims it.

## See also

- `scan-incremental.md` — the reuse strategies this lifecycle chooses between.
- `scan-ignore.md` — the prune rules applied inside the walk.
- `index-store.md` — what the worker produces.
- `server-api.md` — the routes that drive §1 and the startup scheduler.
