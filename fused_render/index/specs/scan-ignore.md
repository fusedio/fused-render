# Ignore list and mount guard

> **Status — shipped.** This file owns the **prune rules**: the pattern grammar, where
> the list is stored, the three places it is applied, the fingerprint that forces a
> rebuild when it changes, and the structural mount guard that does not depend on it.
> Implementing module: `ignore.py` (`DEFAULT_IGNORE_NAMES`, `default_ignore`,
> `clean_patterns`, `_path_regex`, `IgnoreRules`, `ignore_sig`, `MountGuard`), plus
> `store.py` (`applied_ignore_sig`, `save_applied_ignore`) and `scan.py`
> (`keep_subdirs`). Hardcoded device/mount skips are `scan.md §6`.

## 1. Purpose

Dependency and build directories (`node_modules`, `.venv`, `__pycache__`, build output)
are enormous, machine-generated, and useless to search. Pruning them is **not a display
filter** — the walk never descends into them, so they cost no `stat` calls, no parquet
rows, and no query time.

## 2. Pattern grammar

One pattern per entry. `clean_patterns` trims, drops blanks and `#` comments,
de-duplicates, preserves order, and strips a trailing `/`. `IgnoreRules` then sorts each
pattern into one of three matchers:

| Pattern shape | Matcher | Semantics |
|---|---|---|
| bare name — `node_modules` | exact set | that directory **name** at any depth; O(1), the hot path |
| name glob — `*.egg-info` | `fnmatch` on the name | one segment only |
| contains `/` — `~/Library/Caches` | compiled regex | that **path**, and everything under it |

Path patterns are `expanduser`-ed and translated by `_path_regex`, not handed to
`fnmatch` — `fnmatch`'s `*` matches across `/`, which is wrong for paths:

- **`**/`** → any number of directory levels, **including zero**. So
  `<home>/**/mounts` matches both `<home>/mounts` and `<home>/branches/<name>/mounts`.
- **`**`** → anything, slashes included.
- **`*`**, **`?`** → within one segment (`/a/*/c` matches `/a/b/c`, not `/a/b/x/c`).

Everything else is `re.escape`-d. All path patterns compile into a single alternation
with two `fullmatch` closures: `is_ignored` (the directory itself) and
`is_ignored_tree` (the directory or anything beneath it).

Patterns are normalized to the canonical path form before being sorted, so a
Windows-style pattern (`C:\Users\me\x`) is recognized as a path rather than mistaken for
a folder name (`platform.md §1`).

Matching is **directory-oriented**: patterns select folders to skip. Ignoring individual
files by name is not supported. Name matching is **case-sensitive** on every platform;
see `platform.md §5`.

## 3. Where it is applied

A path can reach the index by three routes, and all three are filtered — otherwise
stale rows survive a scan:

| Route | Filter | Function |
|---|---|---|
| the walk's subdirectory list | `keep_subdirs` (also applies `SKIP_DIRS` and the mount guard) | `scan_dir_once`, both the unchanged and rescanned branches |
| cached directories loaded from `dirs.parquet` | `is_ignored_tree` | `load_dir_cache` |
| paths replayed from the FSEvents journal | `is_ignored_tree` + the mount guard | `_run_fsevents` |

`is_ignored` tests one directory (the walk already pruned its parents);
`is_ignored_tree` also matches paths *inside* an ignored folder, for the two routes
where a path arrives without its ancestors having been checked.

Filtering the cache is what makes newly-ignored folders **self-purging**: an ignored
directory is absent from the cache, so it is never added to the keep list, so
compaction drops its file rows (`index-store.md §4`). No full rescan is needed.

### Package leaves

`LEAF_DIR_SUFFIXES` (`.app`, `.framework`, `.bundle`, `.photoslibrary`) is a
*fourth*, differently-shaped rule, and not an ignore pattern: a package is
**recorded and not descended**, where an ignored directory is neither. It is one
`dirs.parquet` row with no file rows, produced by `scan_dir_once` returning before
it lists the directory — so it costs the stat it had already taken and no scandir,
whatever the package holds.

Both halves matter, and each fixes parity in the opposite direction from the other.
`/api/fs/walk` emits `Foo.app` as a single leaf entry (`WALK_LEAF_DIR_SUFFIXES`,
derived from this constant), so:

- **descending** it filled the index with `Foo.app/Contents/...` paths the walk
  never emits — thousands of rows per Electron app, spending a 200 000-row corpus
  budget on entries nobody searches for and, because ranking scores the whole
  relative path, out-ranking real hits;
- **skipping** it would drop the package from the corpus entirely, and the walk
  does list it.

One consequence, handled in `query.search_under`: a package's dirs row means "this
is a leaf", not "we know what is inside", so a search whose *root* is a package
reports `covered: false` and falls back to the live walk — which does list a leaf
it was pointed at. Migration for an index that already descended packages is a
full rescan.

## 4. Rules-changed fingerprint

Removing a pattern cannot be handled incrementally: cached `n_subdirs` values were
computed under the old rules (`scan-incremental.md §2`), so a re-included folder would
stay invisible while its parent's mtime is unchanged.

- `ignore_sig` — sha1 of the newline-joined active pattern list.
- `ignore_applied.json` in the index dir — the fingerprint the current index was
  **built** with, written by `save_applied_ignore` only after a *successful*
  compaction.
- `run_scan` compares the two: a mismatch discards the cache, emits the phase
  `ignore rules changed - full rescan`, and rebuilds. An **absent** applied file is
  *not* treated as a change — that case is safe incrementally, per §3.

## 5. Storage and API

The list lives in `<index>/config.json` alongside the scan roots
(`server-api.md §3`), read through `load_config()` on every call rather than frozen at
import. When the file is missing or unparseable, `default_ignore()` applies.

## 6. Defaults

**The selection rule: a default must name *only* generated content** — a folder nobody
creates by accident.

| Group | Patterns |
|---|---|
| VCS metadata | `.git`, `.svn`, `.hg` |
| Python | `.venv`, `venv`, `__pycache__`, `site-packages`, `*.egg-info`, `.mypy_cache`, `.pytest_cache`, `.ruff_cache`, `.tox` |
| JS | `node_modules`, `.next`, `.nuxt`, `.parcel-cache`, `.turbo` |
| Other tooling | `Pods`, `.gradle`, `.terraform`, `.cache` |
| macOS | `.Trash` |
| fused-render | `<home_dir()>/**/mounts` — see §7 |

**Deliberately excluded: `dist`, `build`, `target`, `vendor`, `env`.** Each is usually
build output — but each is also an ordinary word a person may have named a real folder.
The asymmetry decides it: over-indexing costs disk and some query time, both visible and
recoverable, whereas a false positive is **invisible** — the file is silently absent
from the index and the search simply returns nothing.

The mounts entry is **computed**, not a literal `~/.fused-render/**/mounts`:
FUSED_RENDER_HOME moves the whole shell home (every test redirects it), and a pattern
naming a directory nobody uses would silently leave the real mounts dir walkable.

## 7. The mount guard

`MountGuard` refuses mount paths independently of §6. It exists because the ignore list
is user-editable and the failure mode is not "some junk gets indexed": a kernel
`scandir`/`stat` on an rclone NFS mount path can **wedge the mount permanently** — a
single READDIR on a flat million-key S3 prefix has killed mounts in production — and a
background crawler nobody is watching is more dangerous than an interactive walk.

**It blocks every fused-render home, whole.** Not just the active home's `mounts`
subdirectory:

- **Every home** (`default_home_dirs`): the default `~/.fused-render` *and* whatever
  FUSED_RENDER_HOME points at. A dev server, a test run or a branch checkout redirects
  the home — and then a scan of the user's home directory walks into the *other* home's
  mounts, which the active config knows nothing about. That is not hypothetical: it is
  what a live home scan did, hanging ten scan processes on S3 listings with nothing
  indexed.
- **The whole tree**, not the mounts subdir: a home holds one mounts dir per branch
  checkout, plus caches, sidecars and the index itself. None of it is content anyone
  searches for, and naming the tree covers a mounts dir the guard was never told about.

Two entry points:

- **In the walk** (`blocks`) the decision is a pure string comparison against roots
  resolved once per process. No syscall per directory, which matters at millions of
  them; sound because the walk never follows symlinks, so a guarded path is only ever
  reached by real descent in canonical form.
- **At the root** (`blocks_root`) the check defers to `mounts.is_mount_backed`, which
  pays a `realpath` — a scan root arrives from a user and CAN be a symlink into the
  mounts dir. `runner.start` refuses such a root outright.

**The general case is `scan.md §6`'s same-filesystem rule**, which needs no names at
all: a mount is its own device, so a walk confined to the scan root's filesystem
refuses every mount — iCloud, SMB, an external disk — including any this guard has
never heard of. The guard remains because it is specific, cheap, and names the hazard.

Indexing remote mounts later is possible — routed through the rclone rc listing API,
opt-in per mount — but it is its own project, not a relaxation of this rule.

## Non-goals

- **Non-negotiable device/synthetic-fs skips** — `scan.md §6`, `platform.md §2`.
- **The canonical path form the matcher assumes** — `platform.md §1`.
- **Ignoring individual files** — not supported, see §2.
- **Excluding paths at query time** — the index simply doesn't contain them.

## Open questions

- A pattern list containing only path patterns still walks every directory name; there
  is no early-exit for "no name patterns".
- Nothing tells the user a folder *was* skipped; a per-scan count of pruned roots would
  make it auditable.

## See also

- `scan.md` — the walk these rules prune.
- `scan-incremental.md` — the cached subdir counts §4 protects against.
- `index-store.md` — how dropped directories leave the index.
- `platform.md` — canonical paths, and case-sensitivity of §2's matching.
