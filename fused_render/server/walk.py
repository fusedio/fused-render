from collections import deque
import os
import re
import stat as stat_mod
import sys

from fused_render.index.ignore import (
    LEAF_DIR_NAMES,
    LEAF_DIR_SUFFIXES,
    SHARED_IGNORE_DIRS,
)



# Recursive-walk cap (/api/fs/walk): stop collecting after this many entries.
# With the streamed BFS walk this is a memory/latency safety valve, not a
# coverage budget — shallow entries (the ones a search almost always wants)
# are all emitted long before the cap can bite. Module-level so tests can
# shrink it.
WALK_MAX_ENTRIES = 200_000
# Flat cap on a single /api/fs/list response. An unbounded listing of a
# directory with a million entries builds and serializes a million-entry JSON
# response — slow to produce, slow to render. The response's `truncated` flag
# tells the client the listing is partial. Module-level so tests can shrink it.
LIST_MAX_ENTRIES = 10_000
# Depth cap for the walk. Local listings are cheap kernel calls, so this is a
# generous runaway guard (a symlink-free but pathologically deep tree) rather
# than a budget — a normal project never approaches it. Module-level so tests can
# shrink it.
WALK_MAX_DEPTH_LOCAL = 40
# Max entries per NDJSON batch line in the streamed walk — a framing CAP, not
# the streaming lever (WALK_FLUSH_INTERVAL_S below is). Kept large so a big
# local walk emits few lines; the timer guarantees timely flushing regardless.
WALK_BATCH_SIZE = 500
# Flush whatever has accumulated this long after the last flush, even if the
# batch isn't full. This is what makes the walk actually STREAM: without it, a
# tree smaller than one batch buffers entirely and arrives as one end-of-walk lump, so the
# client's incremental scoring/paint never runs and results appear only once
# the whole walk finishes. With it, entries paint per directory as the walk
# descends. Checked between yielded entries
# (best-effort — a single blocking listdir can't be interrupted mid-call).
WALK_FLUSH_INTERVAL_S = 0.15
# Directory names never emitted or descended into by the walk, checked against
# the bare name so it also applies under hidden=1 (a node_modules is machine
# noise, not "hidden data"). This is the ONLY pruning the walk does beyond
# dot-segments — there is no gitignore lookup here, so a junk dir an
# ecosystem's tooling produces (dist/, build/, target/, …) is walked and
# searched like anything else UNLESS its name happens to be one of these few.
#
# Deliberately just SHARED_IGNORE_DIRS, not the index's larger
# DEFAULT_IGNORE_NAMES (index/ignore.py) — the seeded list carries generic
# build-output names (dist, build, target, …) this walk does not prune, so the
# same folder can answer differently depending on whether a scan has reached
# it: the live walk shows it, the index (once it has scanned) does not. That
# is a real, accepted divergence between the two corpus sources, not a bug —
# see DEFAULT_IGNORE_NAMES's own comment for why the larger list is index-only.
# SHARED_IGNORE_DIRS itself stays index-defined for the names that DO have to
# agree (node_modules, .venv, …): a name pruned by one but kept by the other
# there would flip results for folders any scan reaches almost immediately.
#
# `.git` is NOT in here — it is a LEAF name (WALK_LEAF_DIR_NAMES below), emitted
# as one entry and never descended. It has to be, in lockstep with the index:
# the index records a `.git` dirs row now (that row is what /api/git-repos reads
# instead of stat-ing 71k directories), and a walk that kept pruning the name
# would be the exact disagreement this shared definition exists to prevent.
WALK_IGNORE_DIRS = set(SHARED_IGNORE_DIRS)
# macOS package directories: emitted as a single (dir) entry but never
# descended — their internals are implementation details (Finder hides them
# too), and one Electron .app alone can be thousands of files.
# Defined once in index/ignore.py for the same reason WALK_IGNORE_DIRS is: the
# index applies the identical leaf rule, and a package descended by one corpus
# source but not the other flips results between two sources meant to be
# interchangeable.
WALK_LEAF_DIR_SUFFIXES = LEAF_DIR_SUFFIXES
# The same treatment for directories matched by exact NAME rather than suffix —
# `.git`. Emitted (so the corpus agrees with the index's `.git` dirs row) and
# never descended (so a repo's object database stays out of a search that has a
# 200k-entry budget). Under the walk's default hidden=0 a dot-name is dropped
# before this rule is even consulted, so in practice only an explicitly
# hidden-inclusive walk ever emits it. Suffix matching is wrong here: a bare
# repository is conventionally `foo.git`, and treating those as opaque would hide
# the entire repository — see LEAF_DIR_NAMES.
WALK_LEAF_DIR_NAMES = LEAF_DIR_NAMES


def junk_path(path: str) -> bool:
    """Whether `path` is machine-managed junk or hidden data that no explorer
    surface may show.

    The screening standard for results that did NOT come out of this walk — the
    index-backed search (routers/search.py) and the homepage's repo list
    (routers/git_repos.py) both answer from parquet rows, and the index's own
    ignore rules are a user-editable NAME list that says nothing about hidden
    files. So a row is held to what this walk enforces during traversal:
    WALK_IGNORE_DIRS segments and dot-segments never surface.

    It lives here, next to WALK_IGNORE_DIRS, for the reason that constant does:
    one definition, so two interchangeable sources cannot disagree about which
    paths exist.
    """
    # Both separators: the index stores posix paths (index/ignore.norm) whatever
    # the platform, and one screening standard has to cover both spellings.
    for seg in re.split(r"[/\\]", path):
        if seg in WALK_IGNORE_DIRS:
            return True
        if seg.startswith(".") and seg not in (".", ".."):
            return True
    return False


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
    case-only variants get a stable order."""
    entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower(), e["name"]))
    return entries


def _list_response(path, entries, truncated, cursor):
    """The single /api/fs/list response shape."""
    return {"path": path, "entries": entries,
            "truncated": truncated, "cursor": cursor}


# Yielded by _walk_bfs when the walk stopped short (entry cap or depth cap).
# The walk's `truncated` flag counts YIELDED entries, so incompleteness is
# signalled out-of-band with this sentinel rather than inferred from the entry
# count. The endpoint sets truncated=True on it and emits nothing.
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

    No gitignore lookup runs during the walk — WALK_IGNORE_DIRS (a fixed
    floor) and, separately, the index's user-editable DEFAULT_IGNORE_NAMES
    are the only pruning of build/cache junk. See WALK_IGNORE_DIRS above.

    `max_entries`/`max_depth` bound the walk from INSIDE the generator, not just
    the consumer: once `max_entries` entry dicts have been yielded the walk stops
    (a low-fan-out subtree can't keep the consumer's cap from ever biting), and a
    directory at `max_depth` is listed but its subdirs are never enqueued (a deep
    chain can't march on forever). Either bound, when it fires,
    also emits a `_WALK_TRUNCATED` sentinel so the endpoint flags partial
    coverage. `None` means unbounded on that axis.
    """
    # (abs dir, rel from walk root, depth). depth (root = 0) is used only for
    # the max_depth cap.
    queue = deque([(path, "", 0)])
    emitted = 0  # entry dicts yielded so far (for the max_entries cap)
    while queue:
        current, rel_base, depth = queue.popleft()
        try:
            with os.scandir(current) as it:
                children = list(it)
        except OSError:
            continue  # unreadable dir skipped silently
        dirs = []
        files = []
        for child in children:
            name = child.name
            if not include_hidden and name.startswith("."):
                continue
            if _win_protected(child):
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
                lowered = child.name.lower()
                is_leaf = (lowered in WALK_LEAF_DIR_NAMES
                           or lowered.endswith(WALK_LEAF_DIR_SUFFIXES))
                if not is_link and not is_leaf:
                    if not can_descend:
                        depth_capped = True
                        continue  # at the depth cap — don't enqueue deeper
                    queue.append(
                        (os.path.join(current, child.name), rel, depth + 1)
                    )
        if depth_capped:
            yield _WALK_TRUNCATED  # subtree(s) left unwalked at the depth cap
