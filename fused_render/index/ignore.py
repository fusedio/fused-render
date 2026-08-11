"""Prune rules for the scan: the user-editable ignore list, the hardcoded
device/synthetic-filesystem skips, and the structural mount guard.

Three layers, deliberately distinct:

  * `SKIP_DIRS` — never descended, on any platform. Devices and synthetic
    trees that hang, churn or duplicate the tree. Not a preference.
  * `IgnoreRules` — the user-editable list (defaults in `default_ignore()`).
    Dependency and build caches that are huge, machine-generated and useless
    to search. Pruning them is not a display filter: the walk never descends,
    so they cost no stat, no parquet row and no query time.
  * the LEAF rules (`LEAF_DIR_SUFFIXES`, `LEAF_DIR_NAMES`) — recorded as one
    opaque row and never listed. The middle ground between indexing a tree and
    pretending it isn't there: a macOS package or a `.git` directory is worth
    knowing about but never worth looking inside. Cheaper than an ignore rule,
    too, since ignore rules prune subdirectories but not the files sitting
    directly in the pruned directory.
  * `MountGuard` — remote buckets mounted by fused-render. `default_ignore()`
    already names the mounts dir, but that list is user-editable and a kernel
    `scandir`/`stat` on an rclone NFS mount path can wedge the mount
    permanently (a single READDIR on a flat million-key S3 prefix has killed
    mounts in production). So the crawler ALSO refuses these paths
    structurally, and deleting the ignore entry cannot re-expose the hazard.

See specs/scan-ignore.md.
"""
import fnmatch
import hashlib
import os
import re

WINDOWS = os.sep == "\\"


def norm(p: str) -> str:
    """Canonical path form for everything stored, matched, or compared:
    forward slashes, no trailing slash (except a bare root). Windows accepts
    `/` in every filesystem call, so the canonical form stays usable as-is —
    which is why normalizing at the edges is enough and no code downstream
    needs to think about separators. See specs/platform.md."""
    if WINDOWS:
        p = p.replace("\\", "/")
    return p


# Never descend into these — devices, synthetic filesystems and mount points
# that hang, churn, or duplicate the tree. The set is the union across
# platforms; an entry that doesn't exist on this OS simply never matches.
SKIP_DIRS = {
    # macOS: /System/Volumes duplicates the data volume already reachable via
    # firmlinks (/Users, /private, ...) and holds OS update snapshots.
    "/System/Volumes", "/Volumes", "/cores", "/Network", "/private/var/vm",
    # Linux: /proc and /sys are synthetic (recursive symlinks, blocking
    # pseudo-files); /run is volatile; /mnt and /media are user mounts, the
    # counterpart of macOS /Volumes.
    "/proc", "/sys", "/run", "/mnt", "/media",
    # both
    "/dev",
}

# Defaults are deliberately limited to names that mean ONLY "generated" — a
# folder nobody names by accident. Generic words are left out even when they
# are usually build output (`dist`, `build`, `target`, `vendor`, `env`),
# because a false positive here is invisible: the file silently isn't in the
# index and the search just comes back empty. Add them per machine if the
# trade is worth it there (`target` in particular is large for Rust).
# The floor BOTH corpus sources share (server/walk.py imports this as
# WALK_IGNORE_DIRS). Search is answered by the live walk or by the index
# depending on whether a scan has reached the folder, so a name pruned by one
# and kept by the other makes results flip between two sources that are meant
# to be interchangeable — the same inconsistency server/index_gitignore.py
# exists to prevent for gitignored entries.
#
# `.git` is deliberately NOT here — it is a LEAF dir (LEAF_DIR_NAMES below), so
# it gets one row and no contents rather than vanishing. Ignoring it would be
# strictly worse than the leaf rule even on cost grounds: ignore rules prune
# SUBdirectories but not files, so a pruned-tree `.git` would still contribute
# the ~15 loose files sitting directly in it (HEAD, config, index, …) and still
# pay to list the directory. A leaf dir is never listed at all.
SHARED_IGNORE_DIRS = (
    "node_modules", ".venv", "venv", "__pycache__", "site-packages",
)
# macOS package directories: ONE entry, never descended. Their internals are
# implementation details (Finder hides them too) and one Electron .app alone is
# thousands of files, which on a 200k-capped corpus is budget spent on rows
# nobody searches for — and, because search scores the whole relative path, on
# rows a matching package name would then rank ahead of real hits.
#
# Shared with server/walk.py (WALK_LEAF_DIR_SUFFIXES) for the same reason
# SHARED_IGNORE_DIRS is: search is answered by the live walk or by the index
# depending on whether a scan has reached the folder, so a rule applied by one
# and not the other flips results between two interchangeable sources. The
# dependency direction is server -> index; do not invert it.
LEAF_DIR_SUFFIXES = (".app", ".framework", ".bundle", ".photoslibrary")
# Leaf directories matched by EXACT NAME rather than by suffix.
#
# `.git` is one: the explorer's homepage lists this machine's git repositories,
# and making that a queryable index fact ("which dirs rows are named .git") beats
# the alternative of stat-ing every one of ~71k indexed directories for a `.git`
# child on every request. The leaf rule is what makes it nearly free — one row,
# no descent, so a repo's object database (routinely 10k+ files) stays out.
#
# Name equality, NOT an extra LEAF_DIR_SUFFIXES entry: that tuple is matched with
# `endswith`, and a bare repository is conventionally named `foo.git` — a suffix
# rule would record those as opaque leaves and hide their entire contents, which
# for a bare repo is the whole repository. `.git` as a NAME is unambiguous.
#
# Shared with server/walk.py (WALK_LEAF_DIR_NAMES) for the same reason
# LEAF_DIR_SUFFIXES is, and it matters more here: `.git` moved OUT of
# SHARED_IGNORE_DIRS to get this treatment, so a walk that kept pruning it would
# disagree with the index about whether `.git` exists at all.
LEAF_DIR_NAMES = (".git",)


def is_leaf_dir(path: str) -> bool:
    """Whether `path` is a leaf directory — recorded, but never descended.

    A macOS package (LEAF_DIR_SUFFIXES) or a directory whose name is one of
    LEAF_DIR_NAMES (`.git`). Both are opaque for the same reason: the contents
    are machine-managed implementation detail nobody searches for, while the
    directory's own existence is worth a row."""
    tail = path.rpartition("/")[2].lower()
    return tail in LEAF_DIR_NAMES or tail.endswith(LEAF_DIR_SUFFIXES)


def is_inside_leaf_dir(path: str) -> bool:
    """Whether any ANCESTOR of `path` is a leaf directory — i.e. whether the
    leaf rule means this path should not exist as a row at all.

    `is_leaf_dir` is enough wherever descent is what's being decided: a walk
    that refuses to list `Foo.app` never reaches anything below it. Callers that
    are handed a path instead of descending to it — the FSEvents fast path
    (scan._run_fsevents), the coverage test in query.search_under — see package
    internals directly and need this test, because the final component of
    `Foo.app/Contents/Resources` says nothing about the package above it. `.git`
    makes this path hotter, not different: an active repo writes under
    `.git/objects` constantly, so the journal names those directories on almost
    every incremental run.

    Ancestors ONLY: a final component that is itself a leaf is is_leaf_dir's
    business, and that one gets RECORDED where these get dropped. Pure string
    work on purpose — it runs once per journal-reported directory, so it must
    not stat anything. Paths are `norm`ed, so "/" is the only separator."""
    head, _, _ = path.rpartition("/")
    return any(part.lower() in LEAF_DIR_NAMES
               or part.lower().endswith(LEAF_DIR_SUFFIXES)
               for part in head.split("/"))


# The index prunes MORE than the floor, and may: a scan is a background crawl
# of the whole home, where these caches are pure cost. The walk cannot use the
# same list, because inside a repo it defers to the repo's own .gitignore
# (which catches these and more) and outside one it must stay conservative.
DEFAULT_IGNORE_NAMES = [
    *SHARED_IGNORE_DIRS,
    ".svn", ".hg", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
    ".gradle", ".terraform", ".next", ".nuxt", ".parcel-cache", ".turbo",
    ".cache", "Pods", ".Trash", "*.egg-info",
]


def default_home_dirs() -> list[str]:
    """Every fused-render home this machine might have: the default one and,
    when it differs, whatever FUSED_RENDER_HOME points at.

    BOTH matter to the guard, not just the active one. A dev server, a test
    run, or a branch checkout redirects FUSED_RENDER_HOME — and then a scan of
    the user's home directory walks straight into the DEFAULT home's mounts,
    which the active config knows nothing about. That is not hypothetical: it
    is what a live home scan did, blocking ten scan processes for minutes on
    S3 prefix listings before anything was indexed."""
    homes = [os.path.expanduser("~/.fused-render")]
    env = os.environ.get("FUSED_RENDER_HOME")
    if env:
        homes.append(env)
    return homes


def default_ignore() -> list[str]:
    """The starting ignore list, INCLUDING the mounts dir for this machine.

    The mounts entries are resolved at call time rather than hardcoded as
    `~/.fused-render/**/mounts`: FUSED_RENDER_HOME moves the whole shell home
    (every test and dev server redirects it), and a pattern naming a directory
    nobody uses would silently leave the real mounts dir walkable. Both homes
    are listed when they differ, for the same reason `MountGuard` covers both:
    a redirected home does not stop the DEFAULT home's mounts from sitting in
    the middle of the tree being scanned. `**/` spans zero or more levels, so
    one pattern per home also covers every branch-nested checkout's own mounts
    folder."""
    seen, out = set(), []
    for base in default_home_dirs():
        pattern = norm(os.path.join(base, "**", "mounts"))
        if pattern not in seen:
            seen.add(pattern)
            out.append(pattern)
    return DEFAULT_IGNORE_NAMES + out


def clean_patterns(pats) -> list[str]:
    """Trim, drop blanks/comments, dedupe — order preserved."""
    out, seen = [], set()
    for p in pats:
        p = str(p).strip().rstrip("/")
        if not p or p.startswith("#") or p in seen:
            continue
        seen.add(p)
        out.append(p)
    return out


def _path_regex(pat: str) -> str:
    """Glob source for a path pattern. `**/` spans any number of directory
    levels *including none* (so `~/.fr/**/mounts` matches `~/.fr/mounts` too),
    a lone `**` spans anything, and `*`/`?` stay inside one segment."""
    out, i, n = [], 0, len(pat)
    while i < n:
        c = pat[i]
        if c == "*":
            if pat[i:i + 3] == "**/":
                out.append("(?:[^/]+/)*")
                i += 3
                continue
            if pat[i:i + 2] == "**":
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)


def ignore_sig(pats) -> str:
    """Fingerprint of an ignore list, so a scan can tell whether the index it
    is updating was built under different rules (specs/scan-ignore.md §4)."""
    h = hashlib.sha1("\n".join(pats).encode("utf-8", "replace"))
    return h.hexdigest()


class IgnoreRules:
    """A compiled ignore list. Patterns are split by shape so the hot path is
    a set lookup for the common `node_modules`-style pattern."""

    def __init__(self, patterns):
        self.patterns = list(patterns)
        names, globs, paths = set(), [], []
        for p in self.patterns:
            # normalize first: a Windows-style pattern (C:\Users\me\x) must be
            # recognized as a path, not mistaken for a folder name
            p = norm(p)
            if "/" in p:
                paths.append(_path_regex(norm(os.path.expanduser(p))))
            elif any(c in p for c in "*?["):
                globs.append(p)
            else:
                names.add(p)
        self._names = names
        self._globs = globs
        if paths:
            alt = "|".join(paths)
            self._path = re.compile(f"(?:{alt})$").fullmatch
            self._path_tree = re.compile(f"(?:{alt})(?:/.*)?$").fullmatch
        else:
            self._path = None
            self._path_tree = None

    def _name_ignored(self, name: str) -> bool:
        if name in self._names:
            return True
        for g in self._globs:
            if fnmatch.fnmatch(name, g):
                return True
        return False

    def is_ignored(self, path: str) -> bool:
        """True when this directory path matches the ignore list. A bare name
        (`node_modules`) matches at any depth; a pattern with a slash
        (`~/Library/Caches`) matches that path only, glob syntax allowed."""
        if (self._names or self._globs) and self._name_ignored(path.rsplit("/", 1)[-1]):
            return True
        return bool(self._path and self._path(path))

    def is_ignored_tree(self, path: str) -> bool:
        """True when `path` is ignored *or* sits inside an ignored folder.
        Used where a path arrives out of nowhere (cached rows, the FSEvents
        journal) rather than from a walk that already pruned its parents."""
        if self._names or self._globs:
            for name in path.split("/"):
                if name and self._name_ignored(name):
                    return True
        return bool(self._path_tree and self._path_tree(path))

    def keep_subdirs(self, subdirs):
        return [s for s in subdirs
                if s not in SKIP_DIRS and not self.is_ignored(s)]

    def sig(self) -> str:
        """The fingerprint of everything that decides WHAT LANDS IN THE INDEX,
        which is what every caller actually means by this value: `scan.run_scan`
        drops its incremental cache when it changes, and the router's
        needs_rescan bit is the same comparison.

        So it covers the leaf-dir rules as well as the user's patterns. That is
        not decoration — `.git` moved from the ignore list to LEAF_DIR_NAMES,
        which turns "no `.git` rows" from a fact into a rule change, and an index
        built before it holds no `.git` rows at all. Without the leaf rules in
        here, that index's sig would still match and nothing would ever rescan
        it, so /api/git-repos would confidently report zero repositories forever.
        Including them makes the first scan after the upgrade a full rescan for
        everyone, and lets a reader detect a pre-rule index (routers/git_repos.py)
        instead of trusting it."""
        return ignore_sig([*self.patterns,
                           "\x00leaf-names=" + ",".join(LEAF_DIR_NAMES),
                           "\x00leaf-suffixes=" + ",".join(LEAF_DIR_SUFFIXES)])


class MountGuard:
    """Structural refusal of every path inside a fused-render home — the layer
    that survives a user emptying the ignore list.

    It blocks the WHOLE home tree, not only its `mounts` subdirectory. A home
    holds mounts (one per branch checkout, `branches/<ref>/mounts`), caches,
    sidecars and the index itself: none of it is user content anyone searches
    for, and naming the tree rather than the mount points means a mounts dir
    the guard has not been told about — another home's, a future layout's —
    is covered anyway.

    Hot-path cheap on purpose: the roots are resolved ONCE at construction and
    every per-directory decision is then a pure string comparison, no syscall.
    That matters at millions of directories, and it is sound because the walk
    never follows symlinks — so a guarded path is only ever reached by real
    descent, in the canonical form `blocks()` compares against.

    `blocks_root()` is the authoritative check for a path arriving from
    outside the walk (a scan root a user typed, which CAN be a symlink into
    the mounts dir); it defers to `mounts.is_mount_backed`, which pays a
    realpath to resolve exactly that case.

    This is one of two defences. The other — refusing to cross onto another
    filesystem at all (`scan.scan_dir_once`'s `root_dev`) — is what covers
    every mount nobody named: iCloud, SMB, an external disk."""

    def __init__(self, mounts_dir: str | None = None, home_dirs=None):
        if mounts_dir is None:
            from fused_render.shell.mounts import mounts_dir as _mounts_dir
            mounts_dir = _mounts_dir()
        candidates = [mounts_dir]
        candidates += (list(home_dirs) if home_dirs is not None
                       else default_home_dirs())
        roots = set()
        for c in candidates:
            if not c:
                continue
            roots.add(os.path.abspath(c))
            try:
                roots.add(os.path.realpath(c))
            except OSError:  # unreadable: the abspath form still guards
                pass
        self.roots = tuple(sorted(roots))

    def blocks(self, path: str) -> bool:
        ap = os.path.abspath(path)
        return any(ap == r or ap.startswith(r + os.sep) for r in self.roots)

    def blocks_root(self, path: str) -> bool:
        """Whether a scan may start at `path` at all. Authoritative (follows
        symlinks) because a root is user-supplied, and paid once per run."""
        if self.blocks(path):
            return True
        from fused_render.shell.mounts import is_mount_backed
        return is_mount_backed(path)
