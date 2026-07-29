from collections import deque
import os
import stat as stat_mod
import sys
from types import SimpleNamespace

from fused_render._server_common import _error
from fused_render._server_gitignore import _IgnoreOracle, _repo_toplevel



# Recursive-walk cap (/api/fs/walk): stop collecting after this many entries.
# With the streamed BFS walk this is a memory/latency safety valve, not a
# coverage budget — shallow entries (the ones a search almost always wants)
# are all emitted long before the cap can bite. Module-level so tests can
# shrink it.
WALK_MAX_ENTRIES = 200_000
# Flat cap on a single /api/fs/list response, across all three routes (direct,
# rc, local). An unbounded listing of a directory with a million entries builds
# and serializes a million-entry JSON response — slow to produce, slow to render.
# The response's `truncated` flag (and, on the resumable direct route, its
# `cursor`) tells the client the listing is partial. Module-level so tests can
# shrink it.
LIST_MAX_ENTRIES = 10_000
# Per-request cap for the RESUMABLE direct (S3/GCS) listing route. Deliberately
# one store page: each page runs seconds on a slow bucket (mur-sst ~2s), so a
# bigger first paint just multiplies the wait, and unlike the local/rc routes
# the client can always fetch the next 1000 via the cursor (Load more). Module-
# level so tests can shrink it.
S3_LIST_MAX_ENTRIES = 1_000
# Much smaller cap when the walked path sits under a mount mountpoint
# (shell/mounts.py): there every directory listing is a remote LIST call
# (S3 etc.), so an unbounded walk over a bucket is a slow, potentially paid
# API storm. The walk truncates early and the existing `truncated` flag tells
# the client search was bounded.
WALK_MAX_ENTRIES_REMOTE = 2_000
# Depth cap for a mount-backed walk, enforced INSIDE _walk_bfs so the generator
# stops DESCENDING (stops enqueuing deeper dirs), not just the consumer. The
# entry-count cap alone doesn't bound a deep, LOW-fan-out tree (e.g. NAIP
# state/year/quad/tile): each level is one more remote LIST round-trip, and a
# handful of children per level never trips the entry cap while the walk marches
# arbitrarily deep. Kept generous enough for a real search (a few levels below a
# bucket prefix) but finite so a search-as-you-type over a mount root can't kick
# off an unbounded remote enumeration. Root is depth 0. Module-level so tests
# can shrink it.
WALK_MAX_DEPTH_REMOTE = 6
# Depth cap for a LOCAL walk. Local listings are cheap kernel calls, so this is a
# generous runaway guard (a symlink-free but pathologically deep tree) rather
# than a budget — a normal project never approaches it. Module-level so tests can
# shrink it.
WALK_MAX_DEPTH_LOCAL = 40
# Per-directory hard timeout for the rc listing of a mount-backed dir during a
# walk (see _walk_bfs). Shorter than the interactive fs/list timeout: a walk
# fans out across many directories, so a single slow/huge one is skipped (the
# walk moves on) rather than stalling the whole subtree — same "dead mount ->
# skipped dir" safety, without failing the request.
WALK_RC_LIST_TIMEOUT_S = 10.0
# Overall wall-clock budget for accumulating direct (S3/GCS) pages into ONE
# /api/fs/list response. The per-page timeout (mounts.S3_LIST_TIMEOUT_S /
# GCS_LIST_TIMEOUT_S, 15s) bounds a single
# page, but page COUNT is unbounded — a prefix that returns few keys per page
# could run many pages and stall a request for minutes. On budget exhaustion the
# accumulator stops and returns what it has with the last continuation token, a
# valid resumable page (truncated=True, cursor set), NOT an error. Kept well
# under the rc timeouts because this is FIRST-PAINT latency: on a slow bucket
# (mur-sst pages run ~2s each) the user waits this long for the partial listing,
# and Load more resumes from the cursor. Module-level so tests can shrink it.
S3_LIST_OVERALL_TIMEOUT_S = 8.0
# Max entries per NDJSON batch line in the streamed walk — a framing CAP, not
# the streaming lever (WALK_FLUSH_INTERVAL_S below is). Kept large so a big
# local walk emits few lines; the timer guarantees timely flushing regardless.
WALK_BATCH_SIZE = 500
# Flush whatever has accumulated this long after the last flush, even if the
# batch isn't full. This is what makes the walk actually STREAM: without it, a
# tree smaller than one batch (a bucket prefix is often dozens–hundreds of
# objects) buffers entirely and arrives as one end-of-walk lump, so the
# client's incremental scoring/paint never runs and results appear only once
# the whole walk finishes. With it, entries paint per directory as the walk
# descends, on mounts and locally alike. Checked between yielded entries
# (best-effort — a single blocking listdir can't be interrupted mid-call).
WALK_FLUSH_INTERVAL_S = 0.15
# Directory names never descended into by the walk, checked against the bare
# name so it also applies under hidden=1 (".git" is machine noise, not
# "hidden data"). This is only the UNIVERSAL floor — inside a git repository
# the walk additionally prunes whatever the repo's own .gitignore ignores
# (see _IgnoreOracle), which is what actually catches dist/, build/, .next/,
# target/ and friends without hardcoding every ecosystem's junk dir. The
# floor still matters outside repos (a stray node_modules in ~/Downloads)
# and for .git itself, which git never reports as ignored.
WALK_IGNORE_DIRS = {"node_modules", "__pycache__", "venv", ".venv", ".git", "site-packages"}
# Cap on concurrently open check-ignore co-processes during one walk (a home
# walk crosses dozens of repos; each oracle holds a git subprocess).
WALK_MAX_ORACLES = 8
# macOS package directories: emitted as a single (dir) entry but never
# descended — their internals are implementation details (Finder hides them
# too), and one Electron .app alone can be thousands of files.
WALK_LEAF_DIR_SUFFIXES = (".app", ".framework", ".bundle", ".photoslibrary")


class _RcDirEntry:
    """os.DirEntry-shaped view of one rclone operations/list entry, so
    _walk_bfs can consume mount-backed listings (fetched via the rcd rc API,
    off the kernel NFS mount) through the exact same loop as local os.scandir
    entries. Remote listings carry no symlinks (is_symlink is always False) and
    the size/mtime an os.stat would return are already in the entry, so stat()
    never touches the kernel."""

    __slots__ = ("name", "_is_dir", "_stat")

    def __init__(self, entry, mtime):
        self.name = entry.get("Name")
        self._is_dir = bool(entry.get("IsDir"))
        self._stat = SimpleNamespace(st_size=entry.get("Size"), st_mtime=mtime)

    def is_dir(self):
        return self._is_dir

    def is_symlink(self):
        return False

    def stat(self):
        return self._stat


def _mount_list_item(de):
    """Map one rc/direct listing entry (Name/Size/IsDir/ModTime, the shared shape
    of rc_list_dir and the direct S3/GCS pagers) to an /api/fs/list item.
    `ignored` is always
    False under a mount: there's no git repo there, and `git check-ignore`
    against a mount path is the very kernel I/O these routes avoid."""
    from fused_render.shell import mounts as shell_mounts

    is_dir = bool(de.get("IsDir"))
    return {
        "name": de.get("Name"),
        "is_dir": is_dir,
        "size": None if is_dir else de.get("Size"),
        "mtime": shell_mounts.rc_modtime_epoch(de.get("ModTime")),
        "ignored": False,
    }


def _win_protected(entry: "os.DirEntry") -> bool:
    """True for a Windows hidden+system entry — the "protected operating system
    files" Explorer hides by default. Checked with follow_symlinks=False so a
    reparse junction is judged by its own attributes (the deny-ACL
    Documents\\My Videos / My Music / My Pictures compat junctions are exactly
    this), not the target it points at. Always False off Windows."""
    if sys.platform != "win32":
        return False
    try:
        attrs = entry.stat(follow_symlinks=False).st_file_attributes
    except OSError:
        return False
    return bool(attrs & stat_mod.FILE_ATTRIBUTE_HIDDEN
                and attrs & stat_mod.FILE_ATTRIBUTE_SYSTEM)


def _sort_entries(entries):
    """Sort /api/fs/list items in place and return them: dirs first, then
    case-insensitive by name with the exact name as a deterministic tiebreak so
    case-only variants get a stable order. The single sort key for all three
    list routes (direct, rc, local)."""
    entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower(), e["name"]))
    return entries


def _list_response(path, entries, truncated, cursor):
    """The single /api/fs/list response shape, shared by all three routes."""
    return {"path": path, "entries": entries,
            "truncated": truncated, "cursor": cursor}


def _list_direct(path, cursor):
    """Accumulate direct-listing pages (S3 ListObjectsV2 / GCS objects.list) for
    a mount-backed dir on an anonymous S3 or GCS remote into sorted /api/fs/list
    items, up to S3_LIST_MAX_ENTRIES within an overall time budget — a
    deliberately small per-request cap, since this route is resumable (Load more
    pages in the rest). Returns (entries, next_token); a non-None token means the
    listing is partial and resumable. Raises shell_mounts.DirectListError on any
    page failure so the caller can fall back to the rc route.

    The direct→rc ladder itself lives in pathops.list_mount_dir (single-sourced
    with the fs/walk); here we drive its DIRECT route only (allow_rc_fallback
    False) so api_fs_list keeps its own rc handling — the warning and the
    cursor-specific 503 are HTTP response shaping. This route is reached only
    after the caller has confirmed direct_list_capable(path)."""
    from fused_render.shell import pathops

    listing = pathops.list_mount_dir(
        path, cursor=cursor, max_entries=S3_LIST_MAX_ENTRIES,
        overall_timeout=S3_LIST_OVERALL_TIMEOUT_S, allow_rc_fallback=False)
    # Sorted over what was fetched, not the whole directory — a truncated
    # listing is honestly partial (see the endpoint's sort caveat). Skip any
    # entry missing a Name (a malformed page must not 500 the request).
    entries = _sort_entries(
        [_mount_list_item(de) for de in listing.entries if de.get("Name")])
    return entries, listing.token


# Yielded by _walk_bfs when a directory's listing was cut short (direct S3/GCS
# pages stopped early, rc listing over the per-dir cap, or a per-dir rc/direct
# failure skipped it). The walk's `truncated` flag counts YIELDED entries, but
# dotfile /
# gitignore filtering means a dir cut at the per-dir cap can yield fewer than the
# cap while thousands of keys went unlisted — so incompleteness is signalled
# out-of-band with this sentinel rather than inferred from the entry count. The
# endpoint sets truncated=True on it and emits nothing.
_WALK_TRUNCATED = object()


def _walk_bfs(path, include_hidden, max_entries=None, max_depth=None):
    """Level-order walk of `path` yielding /api/fs/walk entry dicts.

    Breadth-first via a FIFO of pending directories: every entry at depth N is
    yielded before any entry at depth N+1, so a caller that stops early (cap,
    client disconnect) always has complete shallow coverage. Within one parent,
    dirs come first, then files, each sorted by name (the old walk's per-level
    order). Symlinks are yielded but never descended; classification and stat
    follow the link (matching os.walk/os.stat), so a broken symlink is skipped
    like any other unstatable entry. Unreadable directories are skipped
    silently (matches /api/fs/list).

    Inside a git repository, entries the repo's own gitignore rules ignore are
    pruned entirely — not emitted, not descended (the generic answer to
    build/cache junk; WALK_IGNORE_DIRS is just the non-repo floor). Each
    directory inherits its parent's repo root through the queue; a child
    directory containing a `.git` entry (dir or worktree/submodule gitfile)
    starts a nested repo with its own rules. Verdicts come from one streaming
    check-ignore co-process per repo (_IgnoreOracle), capped at
    WALK_MAX_ORACLES concurrently, all closed when the walk ends.

    `max_entries`/`max_depth` bound the walk from INSIDE the generator, not just
    the consumer: once `max_entries` entry dicts have been yielded the walk stops
    (a low-fan-out subtree can't keep the consumer's cap from ever biting), and a
    directory at `max_depth` is listed but its subdirs are never enqueued (a deep
    chain can't march on forever below a mount root). Either bound, when it fires,
    also emits a `_WALK_TRUNCATED` sentinel so the endpoint flags partial
    coverage. `None` means unbounded on that axis.
    """
    from fused_render.shell import mounts as shell_mounts
    from fused_render.shell import pathops

    oracles = {}  # repo root -> _IgnoreOracle, insertion order = LRU

    def oracle_for(repo):
        oracle = oracles.pop(repo, None)
        if oracle is None:
            oracle = _IgnoreOracle(repo)
            while len(oracles) >= WALK_MAX_ORACLES:
                oracles.pop(next(iter(oracles))).close()
        oracles[repo] = oracle  # re-insert = mark most-recently-used
        return oracle

    try:
        # (abs dir, rel from walk root, repo root or None, rel from repo root)
        # A mount-backed root gets no repo: mounts hold no git repositories, and
        # `git -C <mount> rev-parse` (like every gitignore check below) is kernel
        # I/O on the mount we're deliberately routing around.
        top = None if shell_mounts.is_mount_backed(path) else _repo_toplevel(path)
        top_rel = "" if top is None else os.path.relpath(path, top).replace(os.sep, "/")
        # 5th tuple element is depth (root = 0), used only for the max_depth cap.
        queue = deque([(path, "", top, "" if top_rel == "." else top_rel, 0)])
        emitted = 0  # entry dicts yielded so far (for the max_entries cap)
        while queue:
            current, rel_base, repo, repo_rel_base, depth = queue.popleft()
            # Mount-backed dir: list it via the rcd rc API, off the kernel mount
            # (see rc_list_dir / the mur-sst incident). A dir that times out or
            # can't be listed is skipped and the walk moves on, rather than
            # failing the whole request or wedging the mount.
            mount_backed = shell_mounts.is_mount_backed(current)
            if mount_backed:
                is_root = current == path
                dir_cut = False  # this dir's listing stopped short (1.3)
                # Mount-backed dir: the direct→rc ladder (anonymous S3/GCS page
                # the store's own listing API up to the remote walk cap; else, or
                # on a direct page failure, the rc listing rclone can't paginate),
                # single-sourced in pathops.list_mount_dir.
                try:
                    listing = pathops.list_mount_dir(
                        current, max_entries=WALK_MAX_ENTRIES_REMOTE,
                        page_timeout=WALK_RC_LIST_TIMEOUT_S,
                        overall_timeout=WALK_RC_LIST_TIMEOUT_S,
                        rc_timeout=WALK_RC_LIST_TIMEOUT_S)
                except shell_mounts.RcListError:
                    # rc route rejected the listing (a file / missing / broken —
                    # RcListTimeout and RcListUnavailable subclass RcListError).
                    # The ROOT listing failing is fatal — surface it with the same
                    # status codes fs/list uses (see api_fs_walk, which pulls the
                    # first item eagerly to catch this). A non-root dir keeps
                    # skip-and-continue, but marks the walk truncated so the
                    # client knows coverage is partial.
                    if is_root:
                        raise
                    yield _WALK_TRUNCATED
                    continue
                listed = listing.entries
                if listing.direct:
                    if listing.token is not None:
                        dir_cut = True  # more keys remained unlisted
                else:
                    # rclone can't paginate, so a huge dir comes back whole: cap
                    # it at the per-dir remote budget and flag the cut.
                    if len(listed) > WALK_MAX_ENTRIES_REMOTE:
                        dir_cut = True
                        listed = listed[:WALK_MAX_ENTRIES_REMOTE]
                if dir_cut:
                    yield _WALK_TRUNCATED
                children = [
                    _RcDirEntry(e, shell_mounts.rc_modtime_epoch(e.get("ModTime")))
                    for e in listed if e.get("Name")
                ]
            else:
                try:
                    with os.scandir(current) as it:
                        children = list(it)
                except OSError:
                    continue  # unreadable dir skipped silently
            # A .git entry (dir, or gitfile for worktrees/submodules) marks a
            # nested repository: its own gitignore rules take over below here.
            # A .gitignore WITHOUT any repo in scope marks a standalone
            # ignore root (un-inited project, vault, …): same pruning, backed
            # by the empty-GIT_DIR graft (see _IgnoreOracle). Not applied
            # inside a real repo — there git already cascades nested
            # .gitignore files itself. Skipped entirely for mount-backed dirs:
            # they hold no repos, and check-ignore is kernel I/O on the mount.
            if not mount_backed:
                names = {c.name for c in children}
                if ".git" in names and current != repo:
                    repo, repo_rel_base = current, ""
                elif repo is None and ".gitignore" in names:
                    repo, repo_rel_base = current, ""
            dirs = []
            files = []
            for child in children:
                name = child.name
                if not include_hidden and name.startswith("."):
                    continue
                if not mount_backed and _win_protected(child):
                    continue  # hide protected OS junctions, as /api/fs/list does
                try:
                    is_dir = child.is_dir()
                except OSError:
                    continue
                if is_dir:
                    if name in WALK_IGNORE_DIRS:
                        continue
                    dirs.append(child)
                else:
                    files.append(child)
            if repo is not None and not mount_backed and (dirs or files):
                prefix = repo_rel_base + "/" if repo_rel_base else ""
                ignored = oracle_for(repo).ignored(
                    [prefix + c.name for c in dirs + files]
                )
                if ignored:
                    dirs = [c for c in dirs if prefix + c.name not in ignored]
                    files = [c for c in files if prefix + c.name not in ignored]
            dirs.sort(key=lambda e: e.name)
            files.sort(key=lambda e: e.name)
            # Don't enqueue this dir's subdirs once we've hit the depth cap: a
            # dir AT max_depth is still listed (its entries are yielded below),
            # but the walk stops descending past it. Flagged so we emit one
            # truncation sentinel per capped parent (not one per child).
            can_descend = max_depth is None or depth < max_depth
            depth_capped = False
            for child, is_dir in [(d, True) for d in dirs] + [(f, False) for f in files]:
                try:
                    st = child.stat()
                except OSError:
                    continue  # unreadable entries skipped silently
                rel = rel_base + "/" + child.name if rel_base else child.name
                yield {
                    "rel": rel,
                    "is_dir": is_dir,
                    "size": None if is_dir else st.st_size,
                    "mtime": st.st_mtime,
                }
                # Entry-count cap enforced HERE, inside the generator, so the walk
                # actually terminates early instead of the consumer draining a
                # huge (or unbounded) tree. Flag partial coverage and stop.
                emitted += 1
                if max_entries is not None and emitted >= max_entries:
                    yield _WALK_TRUNCATED
                    return
                if is_dir:
                    try:
                        is_link = child.is_symlink()
                    except OSError:
                        is_link = True  # can't tell — safer not to descend
                    if not is_link and not child.name.lower().endswith(WALK_LEAF_DIR_SUFFIXES):
                        if not can_descend:
                            depth_capped = True
                            continue  # at the depth cap — don't enqueue deeper
                        repo_rel = (
                            (repo_rel_base + "/" + child.name if repo_rel_base else child.name)
                            if repo is not None
                            else ""
                        )
                        queue.append(
                            (os.path.join(current, child.name), rel, repo, repo_rel, depth + 1)
                        )
            if depth_capped:
                yield _WALK_TRUNCATED  # subtree(s) left unwalked at the depth cap
    finally:
        for oracle in oracles.values():
            oracle.close()


def _mount_list_error_response(path, exc):
    """Map an RcList* failure to the same HTTP response /api/fs/list returns, so
    fs/walk surfaces a failed ROOT listing identically (timeout/down rcd/broken
    mount -> 503, a file or otherwise-not-a-directory -> 400) instead of a
    200-empty body. Subclasses are checked before the RcListError base."""
    from fused_render.shell import mounts as shell_mounts

    if isinstance(exc, shell_mounts.RcListTimeout):
        return _error(
            f"directory listing timed out — too many entries to list ({path})",
            status=503)
    if isinstance(exc, shell_mounts.RcListUnavailable):
        broken = shell_mounts.broken_mount_error(path)
        return _error(broken or f"cannot list directory {path}", status=503)
    broken = shell_mounts.broken_mount_error(path)
    if broken:
        return _error(broken, status=503)
    return _error(f"not a directory: {path}", status=400)
