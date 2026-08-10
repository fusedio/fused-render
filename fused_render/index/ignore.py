"""Prune rules for the scan: the user-editable ignore list, the hardcoded
device/synthetic-filesystem skips, and the structural mount guard.

Three layers, deliberately distinct:

  * `SKIP_DIRS` — never descended, on any platform. Devices and synthetic
    trees that hang, churn or duplicate the tree. Not a preference.
  * `IgnoreRules` — the user-editable list (defaults in `default_ignore()`).
    Dependency and build caches that are huge, machine-generated and useless
    to search. Pruning them is not a display filter: the walk never descends,
    so they cost no stat, no parquet row and no query time.
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
DEFAULT_IGNORE_NAMES = [
    "node_modules", ".venv", "venv", "__pycache__", ".git", ".svn",
    ".hg", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".gradle",
    ".terraform", ".next", ".nuxt", ".parcel-cache", ".turbo", ".cache",
    "site-packages", "Pods", ".Trash", "*.egg-info",
]


def default_ignore() -> list[str]:
    """The starting ignore list, INCLUDING the mounts dir for this machine.

    The mounts entry is resolved at call time rather than hardcoded as
    `~/.fused-render/**/mounts`: FUSED_RENDER_HOME moves the whole shell home
    (every test redirects it), and a pattern naming a directory nobody uses
    would silently leave the real mounts dir walkable. `**/` spans zero or
    more levels, so one pattern covers both the unnested home and every
    branch-nested checkout's own mounts folder."""
    base = (os.environ.get("FUSED_RENDER_HOME")
            or os.path.expanduser("~/.fused-render"))
    return DEFAULT_IGNORE_NAMES + [norm(os.path.join(base, "**", "mounts"))]


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
        return ignore_sig(self.patterns)


class MountGuard:
    """Structural refusal of every path under the mounts dir — the layer that
    survives a user emptying the ignore list.

    Hot-path cheap on purpose: the mount roots are resolved ONCE (one
    `realpath` of the mounts dir at construction, off any mount) and every
    per-directory decision is then a pure string comparison, no syscall. That
    matters at millions of directories, and it is sound here because the walk
    never follows symlinks — so a mount path is only ever reached by real
    descent, in the canonical form `blocks()` compares against.

    `blocks_root()` is the authoritative check for a path arriving from
    outside the walk (a scan root a user typed, which CAN be a symlink into
    the mounts dir); it defers to `mounts.is_mount_backed`, which pays a
    realpath to resolve exactly that case."""

    def __init__(self, mounts_dir: str | None = None):
        if mounts_dir is None:
            from fused_render.shell.mounts import mounts_dir as _mounts_dir
            mounts_dir = _mounts_dir()
        roots = {os.path.abspath(mounts_dir)}
        try:
            roots.add(os.path.realpath(mounts_dir))
        except OSError:  # unreadable home: the abspath form still guards
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
