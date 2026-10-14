# Platform support

> **Status — partial.** This file owns **path canonical form** and **per-platform
> behaviour** (macOS and Linux). Implementing modules: `ignore.py` (`norm`,
> `SKIP_DIRS`), `runner.py` (`_detach_kwargs`), `scan.py` (`scan_dir_once`),
> `query.py` (`_root_or_bare`, `stats`). macOS is the only platform verified end
> to end (§4).

## 1. Canonical path form

**Every path stored, matched, or compared uses forward slashes and no trailing slash.**
A POSIX path is already in that form, so `norm` is the identity — it stays the one
named seam every path goes through, and nothing downstream has to think about
separators.

Normalization happens at the **edges**, never in the middle:

| Edge | Where | Note |
|---|---|---|
| the scan root | `runner.start` | `expanduser` → `abspath` → `norm`, before `spec.json` is written |
| every discovered path | `scan_dir_once` | each `e.path` leaves through `norm`; this is where all index paths are born |
| ignore patterns | `IgnoreRules.__init__` | a pattern containing `/` is a path, anything else a name or glob |
| a `stats` root | `query.stats` | accepts either form; `~` expansion is re-normalized |

Because the form is canonical, the rest of the system may — and does — assume `/`:
`path.split("/")` in ignore matching (`scan-ignore.md §2`), `root + "/"` prefix tests
in the dir cache (`scan-incremental.md §2`), `dir LIKE 'root/%'` in compaction
(`index-store.md §4`), and partition range tests (`query.md §4`).

The one place that deliberately uses the OS separator is `MountGuard`
(`scan-ignore.md §7`): it compares `os.path.abspath` forms on both sides, so the two
agree by construction.

**Anchoring** recognizes absolute paths as `/…` and `~…`.

## 2. Per-platform behaviour

| Concern | macOS | Linux |
|---|---|---|
| Walk (`scandir` + `stat`) | yes | yes |
| Directory-mtime reuse (`scan-incremental.md §2`) | yes | yes (ext4/xfs/btrfs) |
| FSEvents journal fast path | **yes** | no equivalent |
| Detached worker | worker's own `os.setsid()` | worker's own `os.setsid()` |
| Skip list entries in `SKIP_DIRS` | `/System/Volumes`, `/Volumes`, `/cores`, `/Network`, `/private/var/vm`, `/dev` | `/proc`, `/sys`, `/run`, `/mnt`, `/media`, `/dev` |

`SKIP_DIRS` is a single cross-platform union: an entry that doesn't exist on this OS
never matches, so no branching is required.

**Detachment is not cosmetic, and it happens in the child.** `start_new_session=True`
at the spawn would force CPython off `posix_spawn` onto `fork()`, which a server that
has loaded PROJ does not survive (`runner._detach_kwargs`), so the worker calls
`os.setsid()` itself after exec. `_detach_kwargs` is a separate function precisely so
this is testable without spawning.

## 3. macOS-only optimization

**FSEvents** (`scan-incremental.md §3`) is the one genuinely platform-specific
optimization, and it is a large one: an incremental rescan visits only the directories
the OS journal names, instead of stat-ing every directory in the tree. Every entry point
is gated on `sys.platform == "darwin"` and returns `None` otherwise, so other platforms
fall back to the mtime walk — correct, but O(directories) per rescan.

Linux has no persistent equivalent: `inotify` is not durable across runs and `fanotify`
requires privileges. Treating that as a permanent gap is a deliberate choice.

The **thread-pool escape hatch** (`scan.md §4`) is often mistaken for macOS-specific
because iCloud dataless files and sandbox containers motivated it. It is triggered by
measured latency, not platform, so it helps equally on NFS/SMB anywhere.

## 4. Verification status

- **macOS** — verified end to end: real scans, incremental reuse, the FSEvents path,
  ignore pruning, the mount guard.
- **Linux** — the code paths are platform-neutral and the POSIX branches are exercised
  by CI, but no large scan has been run there. `(TARGET)`

## 5. Known platform gaps

- **Case sensitivity** — ignore-name matching is exact, so `Node_Modules` is not
  pruned on case-insensitive filesystems. Search is unaffected: `lookup` uses `ILIKE`.
- **Network filesystems** — directory-mtime reuse assumes a parent's mtime changes when
  a child is added or removed; NFS/SMB caching can break that on any platform. Network
  volumes are not indexed at all (`scan.md §6`).

## Non-goals

- **Which folders to prune by preference** — `scan-ignore.md`.
- **The reuse strategies themselves** — `scan-incremental.md`.

## See also

- `scan.md` — the walk, the skip list, and worker spawning.
- `scan-ignore.md` — the matcher that relies on §1, and the mount guard.
- `query.md` — anchoring and pruning rely on §1.
