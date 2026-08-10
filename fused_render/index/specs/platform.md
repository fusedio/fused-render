# Platform support

> **Status — partial.** This file owns **path canonical form** and **per-platform
> behaviour**. Implementing modules: `ignore.py` (`WINDOWS`, `norm`, `SKIP_DIRS`),
> `runner.py` (`_detach_kwargs`), `scan.py` (`scan_dir_once`), `query.py` (`_DRIVE`,
> `pattern_for`). macOS is the only platform verified end to end (§4).

## 1. Canonical path form

**Every path stored, matched, or compared uses forward slashes and no trailing slash.**
Windows accepts `/` in every filesystem call, so the canonical form stays directly
usable — nothing downstream has to think about separators.

Normalization happens at the **edges**, never in the middle:

| Edge | Where | Note |
|---|---|---|
| the scan root | `runner.start` | `expanduser` → `abspath` → `norm`, before `spec.json` is written |
| every discovered path | `scan_dir_once` | each `e.path` leaves through `norm`; this is where all index paths are born |
| ignore patterns | `IgnoreRules.__init__` | normalized **before** the `"/" in p` test, so `C:\Users\me\x` is recognized as a path, not a folder name |
| a user query | `query.pattern_for` | accepts either form; `~` expansion is re-normalized |

Because the form is canonical, the rest of the system may — and does — assume `/`:
`path.split("/")` in ignore matching (`scan-ignore.md §2`), `root + "/"` prefix tests
in the dir cache (`scan-incremental.md §2`), `dir LIKE 'root/%'` in compaction
(`index-store.md §4`), and partition range tests (`query.md §4`).

The one place that deliberately uses the OS separator is `MountGuard`
(`scan-ignore.md §7`): it compares `os.path.abspath` forms on both sides, so the two
agree by construction on either platform.

**Anchoring** must therefore recognize absolute paths in both shapes: POSIX `/…`, `~…`,
and Windows `C:/…` (`_DRIVE` in `query.py`). Missing the drive case doesn't error — it
silently disables partition pruning, which is why it is spelled out here.

## 2. Per-platform behaviour

| Concern | macOS | Linux | Windows |
|---|---|---|---|
| Walk (`scandir` + `stat`) | yes | yes | yes |
| Directory-mtime reuse (`scan-incremental.md §2`) | yes | yes (ext4/xfs/btrfs) | yes (NTFS) |
| FSEvents journal fast path | **yes** | no equivalent | no |
| Detached worker | `start_new_session` | `start_new_session` | `DETACHED_PROCESS \| CREATE_NEW_PROCESS_GROUP` |
| Skip list entries in `SKIP_DIRS` | `/System/Volumes`, `/Volumes`, `/cores`, `/Network`, `/private/var/vm`, `/dev` | `/proc`, `/sys`, `/run`, `/mnt`, `/media`, `/dev` | none needed |

`SKIP_DIRS` is a single cross-platform union: an entry that doesn't exist on this OS
never matches, so no branching is required.

**Detachment is not cosmetic.** `start_new_session` is POSIX-only; Windows accepts the
argument and silently ignores it, so without the creation flags the worker would be an
ordinary child of the server process. `_detach_kwargs` is a separate function precisely
so this is testable without spawning.

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
- **Windows** — separator handling, pattern compilation, query anchoring, and the
  detach flags are unit-verified; **no scan has ever run on Windows.** `(TARGET)`

## 5. Known platform gaps

- **Long paths (Windows)** — paths over 260 characters need the `\\?\` prefix or
  opt-in long-path support; untested.
- **UNC paths (Windows)** — `\\server\share\…` normalizes to `//server/share/…`, which
  is plausible but unverified, and `_DRIVE` does not treat it as anchored.
- **Case sensitivity** — ignore-name matching is exact, so `Node_Modules` is not
  pruned on case-insensitive filesystems. Search is unaffected: `lookup` uses `ILIKE`.
- **Network filesystems** — directory-mtime reuse assumes a parent's mtime changes when
  a child is added or removed; NFS/SMB caching can break that on any platform. Remote
  mounts are not indexed at all (`scan-ignore.md §7`).

## Non-goals

- **Which folders to prune by preference** — `scan-ignore.md`.
- **The reuse strategies themselves** — `scan-incremental.md`.

## See also

- `scan.md` — the walk, the skip list, and worker spawning.
- `scan-ignore.md` — the matcher that relies on §1, and the mount guard.
- `query.md` — anchoring and pruning rely on §1.
