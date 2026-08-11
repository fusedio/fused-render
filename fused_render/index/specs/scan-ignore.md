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
| the walk's subdirectory list | `ignored_for_index(…, tree=False)` (also applies `SKIP_DIRS` and the mount guard) | `scan_dir_once`, both the unchanged and rescanned branches |
| cached directories loaded from `dirs.parquet` | `ignored_for_index(…, tree=True)` | `load_dir_cache` |
| paths replayed from the FSEvents journal | `ignored_for_index(…, tree=True)` + the mount guard | `_run_fsevents` |

All three go through **one predicate**, `ignore.ignored_for_index` — "does the
ignore list forbid a row for this path?". `tree` says what the CALLER knows, not
what the rule is: `tree=False` tests the directory's own name (the walk already
pruned its parents), `tree=True` also matches paths *inside* an ignored folder, for
the two routes where a path arrives without its ancestors having been checked.
`tree=False` must NOT be widened to the tree test — a scan root that itself sits
inside a directory matching an ignore *name* (`~/venv/myproject`) would then have
its whole subtree forbidden.

One predicate rather than three call sites spelling out the same rule, because the
leaf exemption below is a rule about *what may exist as a row* and it was once
applied at only the first of these three. The result was not a partial fix but
DATA LOSS: a full rescan wrote the `.git` rows, and the next incremental pass —
where the cache filter decides what is carried forward and the journal gate
decides what is re-added — deleted every one of them, so a working Repos tab
broke on the next scan. Same failure shape as the walk/index parity rule and
`runner.canonical_root`: one rule, several implementations, silent disagreement.

Filtering the cache is what makes newly-ignored folders **self-purging**: an ignored
directory is absent from the cache, so it is never added to the keep list, so
compaction drops its file rows (`index-store.md §4`). No full rescan is needed.

### Leaf directories

`LEAF_DIR_SUFFIXES` (`.app`, `.framework`, `.bundle`, `.photoslibrary`) and
`LEAF_DIR_NAMES` (`.git`) are a
*fourth*, differently-shaped rule, and not an ignore pattern: a leaf dir is
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

`.git` is a leaf by NAME, and it is the reason the name half of the rule exists.
It used to sit in `SHARED_IGNORE_DIRS`; it was moved here so the index carries one
row per repository, which makes "is this directory a git repo" a queryable index
fact — `/api/git-repos` (the Explorer homepage's Repos tab) selects the `.git`
rows and takes each one's parent, instead of stat-ing every one of ~71 000 indexed
directories on every request. Three consequences worth stating:

- matching is **name equality, never a suffix**: `LEAF_DIR_SUFFIXES` is matched
  with `endswith`, and a bare repository is conventionally named `foo.git` —
  a suffix rule would make those opaque and hide the whole repository;
- a leaf is strictly cheaper than an ignore pattern here, because ignore rules
  prune *subdirectories* but not files: an ignored `.git` would still contribute
  the ~15 loose files sitting directly in it (`HEAD`, `config`, `index`, …) and
  still pay to list the directory. A leaf is never listed at all;
- a leaf dir survives the **user's** ignore list (`SKIP_DIRS` and the mount guard
  keep their veto). An ignore entry buys a scan no-descent and no-row; for a leaf
  the first is already true, so all it could still do is delete the row that repo
  detection depends on — and `.git` shipped in the default list once, so old saved
  configs really do name it. The exemption lives in `ignored_for_index` (§3) and so
  applies at **all three** gates; ancestors keep their veto, so `<repo>/.git`
  survives an entry naming `.git` while `node_modules/pkg/.git` does not.

`is_leaf_dir` tests the path's own final component, which is all a descent needs:
a walk that refuses to list `Foo.app` never reaches anything below it. Two callers
are *handed* a path rather than descending to it, and use `is_inside_leaf_dir`
(any ancestor is a package) instead:

- the **FSEvents fast path** (`scan-incremental.md §1`) visits whatever directories
  the OS journal names, and what the journal names inside a package is always a
  descendant — an app update writes `Foo.app/Contents/Resources`, Photos writes
  `Foo.photoslibrary/database`, never the package itself. A final-component test
  passes those straight through, so package internals entered the index by this
  path even though the walk-driven path excluded them, and the keep list then
  carried the rows forward on every later run.
- **`query.search_under`'s coverage test**: a package's dirs row means "this is a
  leaf", not "we know what is inside", so a search rooted at a package reports
  `covered: false` and falls back to the live walk — which does list a leaf it was
  pointed at. The same has to hold one level down, because an index written before
  this rule holds real dirs rows *inside* packages; answering
  `Foo.app/Contents` from that partial set while `Foo.app` goes to the walk is the
  two-interchangeable-sources-disagree bug again.

Neither test purges rows an older index already has: they stop packages entering.
Migration for an index that already descended packages is a full rescan.

## 4. Rules-changed fingerprint

Removing a pattern cannot be handled incrementally: cached `n_subdirs` values were
computed under the old rules (`scan-incremental.md §2`), so a re-included folder would
stay invisible while its parent's mtime is unchanged.

- `ignore_sig` — sha1 of the newline-joined active pattern list.
- `IgnoreRules.sig()` — what every caller actually compares: `ignore_sig` over the
  patterns **plus the leaf rules** (§3). The leaf rules decide index content just as
  the patterns do, so leaving them out would mean an index built before `.git`
  became a leaf keeps matching, never rescans, and holds no `.git` rows — making
  `/api/git-repos` report zero repositories forever. Including them makes the first
  scan after that change a full rescan, and lets a reader detect a pre-rule index
  (`applied_ignore_sig(cfg) != cfg.rules.sig()`) and report "not ready" instead of
  trusting it.
- `ignore_applied.json` in the index dir — the fingerprint the current index was
  **built** with, written by `save_applied_ignore` only after a *successful*
  compaction.
- `run_scan` compares the two: a mismatch discards the cache, emits the phase
  `ignore rules changed - full rescan`, and rebuilds. An **absent** applied file
  counts as a mismatch too (phase `no applied rules fingerprint - full rescan`).

  That last point reverses the earlier rule ("absent is safe incrementally"), and
  the reason is the leaf rules. Absent *was* safe while every rule only ever
  *removed* rows: dropping a pattern is self-purging through the filtered cache
  (§3), so an unfingerprinted index reconciled itself. A rule that **adds** rows
  cannot work that way — a `.git` row appears only by visiting the repo directory,
  and an incremental scan skips exactly that directory because its mtime has not
  changed. The scan would then *stamp* the new fingerprint over an index that
  never grew the rows, and every reader trusting the stamp (`/api/git-repos`)
  would be permanently, confidently wrong. One full rescan is the cheap side of
  that trade.

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
