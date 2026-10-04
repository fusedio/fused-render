"""The live watcher's own process: `python -m fused_render.index.watcher SPEC`.

`watchfiles` sets up its watches inside `RustNotify`'s constructor and first
`watch()` call while holding the GIL, and a recursive inotify watch of `~`
walks every directory under it. Run inside the server, that walk freezes
every Python thread for as long as it takes — uvicorn's startup included —
so the watching lives here instead, in a child the server reads JSON lines
from (`server/index_watch.py`'s `_process_source`).

What gets watched is the tree the index covers, not the whole root:
`plan_watch` walks the root with the scan's own descent rule
(`scan.keep_subdirs`, the scan root's filesystem only, leaf dirs never
entered, the index's own dir never) and returns the smallest cover of it: a
directory whose whole subtree is indexed is watched recursively, and a
directory with anything pruned beneath it is watched non-recursively while
the walk continues into its kept children. A home directory holds far more
than the index does (caches, `node_modules`, every branch's mounts), and
watching only the indexed part keeps both the setup walk and the inotify
watch count proportional to what the index can ever act on.

A plan goes stale when a directory appears where it would be classified
differently — a new folder under a non-recursively watched one (nothing
watches inside it yet), or an ignored folder or leaf dir created inside a
recursive watch (`npm install` in a clean tree would otherwise put all of
`node_modules` under the watch). Either one re-plans, after a short settle so
a clone or install lands first.

macOS does none of that planning. FSEvents is a per-volume kernel journal:
one stream rooted at `~` costs the same as one rooted at `~/Documents`, and
constructing it takes ~0.1 s. What is NOT cheap there is a watch with many
paths: notify's FSEvents backend rebuilds its single stream every time a
path is appended (`FSEventStreamCreate` plus a `realpath` of every path so
far), so N paths cost O(N²) — a home directory's plan of ~16k non-recursive
plus ~24k recursive directories was measured at tens of minutes of a full
core, all of it inside `RustNotify.__init__` with the GIL held. That hold is
also why those watchers outlived their server: the stdin-EOF thread below
needs the GIL to read the EOF, and nothing in this process can notice a
dead parent without it, so the only real fix is a construction that ends in
well under a second. On darwin `serve` therefore watches the root with one
recursive stream and leaves the pruning to `make_dropped` on each event
(the same gate the FSEvents journal replay in `scan.py` applies to raw
journal paths), and never re-plans: there is no plan to go stale.

Protocol, one JSON object per stdout line:

  {"changes": [[<watchfiles.Change int>, "<path>"], ...]}
  {"log": "<message>"}
  {"error": "<message>"}          -- the last line before a non-zero exit

The process ends on stdin EOF, which is how it dies with the server: the
server holds the write end of that pipe, and the kernel closes it when the
server exits for any reason.
"""
import errno
import json
import os
import sys
import threading
import time

from fused_render.index.ignore import (
    IgnoreRules,
    MountGuard,
    ignored_for_index,
    is_inside_leaf_dir,
    is_leaf_dir,
    norm,
)
from fused_render.index.scan import keep_subdirs

# How long a re-plan waits after the change that triggered it, so a burst of
# directory creation (a clone, an install, an unzip) is walked once, whole.
REPLAN_SETTLE_S = 2.0

# How often each watcher thread returns to Python to check its stop event.
RUST_TIMEOUT_MS = 1000

# Consecutive re-plans a vanished watch path may cause before the process
# gives up and reports an error (the server then backs off and reopens).
MAX_VANISHED_REPLANS = 3


def make_dropped(rules, mounts_dir: str, index_dir: str | None = None):
    """A `path -> bool` filter: True when the watcher must never act on
    `path`. Four structural refusals — the real analogue is not
    `index_touch._real_blocked` (that one filters a scan ROOT, chosen by
    something that already decided to scan) but the FSEvents journal gate at
    `scan.py`'s `_run_fsevents`: `ignored_for_index(...) or guard.blocks(d)
    or is_inside_leaf_dir(d)`, which filters raw watched paths the same way
    this does:

      * `ignored_for_index(rules, path, tree=True)` — the ignore list,
        checked tree-wise because a watched path arrives with no vetted
        ancestors (same reason the FSEvents journal gate uses `tree=True`).
        Every branch's mounts folder is already in `default_ignore()`.
      * `MountGuard(mounts_dir=...).blocks(path)` — the structural refusal
        that survives a user emptying the ignore list; it blocks the WHOLE
        fused-render home tree, not only the mounts subdirectory — which is
        what covers the index store's own directory (`cfg.dir`) WHEN it
        sits at its default location, but `cfg.dir` is a settable config
        key, not a fixed one.
      * `index_dir` (pass `cfg.dir`) — the explicit check for the case the
        guard misses: an index dir configured OUTSIDE the fused-render home
        but under a watched root. Without this, that configuration reopens
        the self-trigger loop this filter exists to prevent (a scan writes
        parquet into `cfg.dir`, the watcher observes its own write,
        triggers the scan that triggered it). Optional only so the many
        tests that don't care about this case don't have to pass it; real
        wiring always does.
      * `is_inside_leaf_dir(path)` — whether an ANCESTOR of `path` is a leaf
        directory (`.git`, an `.app` bundle, ...). `.git` is deliberately
        NOT in the ignore names (it is a LEAF_DIR_NAME instead), so without
        this check a write to `~/repo/.git/objects/ab/cdef` would survive
        the filter and forward a folder the index deliberately never
        indexes — and an active git repo writes under `.git/objects`
        constantly, making this the hottest of the four in practice.

    Returning True for any means: this path or a change under it must never
    cause a flush. That is load-bearing, not an optimization."""
    guard = MountGuard(mounts_dir=mounts_dir)
    idx = norm(str(index_dir or ""))

    def dropped(path: str) -> bool:
        p = norm(str(path or ""))
        if not p:
            return True
        if guard.blocks(p):
            return True
        if idx and (p == idx or p.startswith(idx + "/")):
            return True
        if is_inside_leaf_dir(p):
            return True
        return ignored_for_index(rules, p, tree=True)

    return dropped


def is_watch_limit_error(exc: BaseException) -> bool:
    """Whether `exc` is the platform's watch-limit failure. inotify reports
    it as ENOSPC; `watchfiles` surfaces notify's `MaxFilesWatch` as a bare
    OSError with no errno, so the message is the only thing to match."""
    if not isinstance(exc, OSError):
        return False
    return (getattr(exc, "errno", None) == errno.ENOSPC
            or "watch limit" in str(exc).lower())


class Pruner:
    """The scan's descent rule, applied to one candidate directory at a time.

    `kept(path)` answers whether the index walk would descend into `path`
    from its parent: `scan.keep_subdirs` (the hardcoded skips, the mount
    guard, the ignore list), `scan.scan_dir_once`'s filesystem check against
    the root's device, never a leaf dir, and never anything `make_dropped`
    refuses (which adds the index's own directory)."""

    def __init__(self, root: str, rules, mounts_dir: str,
                 index_dir: str | None = None):
        self.rules = rules
        self.guard = MountGuard(mounts_dir=mounts_dir)
        self.dropped = make_dropped(rules, mounts_dir, index_dir=index_dir)
        self.root_dev = os.stat(root).st_dev

    def rule_kept(self, path: str) -> bool:
        """The part of `kept` that needs no syscall."""
        return (bool(keep_subdirs([path], self.rules, self.guard))
                and not is_leaf_dir(path) and not self.dropped(path))

    def kept(self, path: str) -> bool:
        if not self.rule_kept(path):
            return False
        try:
            return os.lstat(path).st_dev == self.root_dev
        except OSError:
            return False


def _child_dirs(d: str):
    """`(real_dirs, has_dir_symlink)` for directory `d`. A symlink to a
    directory counts against its parent: the recursive watch follows
    symlinks, the index walk does not."""
    dirs, linked = [], False
    try:
        with os.scandir(d) as it:
            for e in it:
                try:
                    if e.is_symlink():
                        if e.is_dir(follow_symlinks=True):
                            linked = True
                    elif e.is_dir(follow_symlinks=False):
                        dirs.append(norm(e.path))
                except OSError:
                    continue
    except OSError:
        pass
    return dirs, linked


def plan_watch(root: str, pruner: Pruner, *, shallow: bool = False):
    """`(recursive, flat)`: the directories to watch recursively and
    non-recursively so that exactly the indexed tree under `root` is covered.

    `shallow` is the watch-limit fallback: the root and its kept children,
    all non-recursive — it still sees `~/a.txt` and `~/Downloads/x.dmg`, and
    anything deeper falls to the periodic rescan."""
    root = norm(root)
    if shallow:
        kids, _ = _child_dirs(root)
        return [], [root, *sorted(k for k in kids if pruner.kept(k))]

    # Pass 1, pre-order: every kept directory, its kept children, and
    # whether anything beneath it directly was pruned.
    order, kids, impure = [], {}, set()
    stack = [root]
    while stack:
        d = stack.pop()
        order.append(d)
        subs, linked = _child_dirs(d)
        keep = [s for s in subs if pruner.kept(s)]
        kids[d] = keep
        if linked or len(keep) != len(subs):
            impure.add(d)
        stack.extend(keep)

    # Pass 2, children before parents: a directory is clean when nothing in
    # its subtree was pruned.
    clean = {}
    for d in reversed(order):
        clean[d] = d not in impure and all(clean[k] for k in kids[d])

    # Pass 3, top-down: the topmost clean directories go recursive; every
    # directory above them is watched on its own.
    recursive, flat = [], []
    stack = [root]
    while stack:
        d = stack.pop()
        if clean[d]:
            recursive.append(d)
        else:
            flat.append(d)
            stack.extend(kids[d])
    return sorted(recursive), sorted(flat)


class _Out:
    """Line-at-a-time JSON writer shared by the watcher threads. A closed
    pipe means the server is gone, so it ends the process."""

    def __init__(self, stream):
        self.stream = stream
        self.lock = threading.Lock()

    def send(self, **msg) -> None:
        line = json.dumps(msg) + "\n"
        with self.lock:
            try:
                self.stream.write(line)
                self.stream.flush()
            except (BrokenPipeError, OSError, ValueError):
                os._exit(0)


class _Session:
    """One plan's watchers: a thread per watch mode, each forwarding its
    batches, and a `done` event set when the plan is stale or a watcher
    failed.

    `replan=False` turns staleness off entirely: the session only ends on a
    watcher failure. That is the darwin whole-tree watch (see the module
    docstring); the plan there is the root itself, so nothing can go stale.

    `grows_under` is the set of directories a new kept folder directly
    inside of changes the plan. Normally that is every flat watch, since the
    next plan watches the new folder. The shallow plan only ever adds the
    root's children, so it passes just the root: a folder appearing deeper
    would re-plan into the same watches, and each re-plan is a gap."""

    def __init__(self, recursive, flat, pruner: Pruner, out: _Out, *,
                 grows_under=None, replan: bool = True):
        self.recursive, self.flat = recursive, flat
        self.flat_set = set(flat)
        self.grows_under = self.flat_set if grows_under is None else set(grows_under)
        # `replan=False` is the whole-tree watch: `[root]` recursive is also
        # a legitimate planned shape (nothing pruned under the root), so the
        # choice cannot be read off the plan — the caller states it.
        self.replan = replan
        self.pruner = pruner
        self.out = out
        self.stop = threading.Event()
        self.done = threading.Event()
        self.error: BaseException | None = None
        self.threads = []

    def stale_by(self, change, path: str) -> bool:
        """Whether an added `path` means the plan no longer covers the
        indexed tree correctly. String checks first; the `isdir` only for a
        path that could matter."""
        from watchfiles import Change

        if change != Change.added:
            return False
        p = norm(path)
        parent = p.rpartition("/")[0] or "/"
        if parent in self.flat_set:
            return (parent in self.grows_under
                    and self.pruner.rule_kept(p) and os.path.isdir(p)
                    and not os.path.islink(p))
        # Inside a recursive watch, which follows symlinks.
        if os.path.islink(p):
            return os.path.isdir(p)
        return not self.pruner.rule_kept(p) and os.path.isdir(p)

    def _filter(self, change, path) -> bool:
        if (self.replan and not self.done.is_set()
                and self.stale_by(change, path)):
            self.done.set()
        return not self.pruner.dropped(path)

    def _run(self, paths, recursive: bool) -> None:
        import watchfiles

        try:
            for batch in watchfiles.watch(
                    *paths, watch_filter=self._filter, stop_event=self.stop,
                    rust_timeout=RUST_TIMEOUT_MS, yield_on_timeout=True,
                    recursive=recursive, ignore_permission_denied=True):
                if batch:
                    self.out.send(changes=[[int(c), p] for c, p in batch])
        except BaseException as e:  # noqa: BLE001 - reported to the main loop
            if not self.stop.is_set():
                self.error = e
                self.done.set()

    def start(self) -> None:
        for paths, recursive in ((self.recursive, True), (self.flat, False)):
            if not paths:
                continue
            t = threading.Thread(target=self._run, args=(paths, recursive),
                                 daemon=True)
            t.start()
            self.threads.append(t)

    def close(self) -> None:
        self.stop.set()
        for t in self.threads:
            t.join(timeout=5.0)


def watches_whole_tree() -> bool:
    """Whether this platform watches the root with one recursive stream
    instead of a planned cover. True on darwin: FSEvents makes the one
    stream nearly free and the many-path cover ruinously expensive (module
    docstring). inotify is the opposite — a recursive watch of `~` walks
    and registers every directory, caches included — so Linux keeps the
    plan, and Windows (`ReadDirectoryChangesW` per path) with it."""
    return sys.platform == "darwin"


def serve(root: str, pruner: Pruner, out: _Out) -> int:
    """Watch `root` until the process is killed; returns an exit code only
    when watching cannot continue."""
    shallow = False
    vanished = 0
    whole = watches_whole_tree()
    while True:
        t0 = time.monotonic()
        if whole:
            recursive, flat = [norm(root)], []
            out.send(log=(f"watching {root}: whole tree, one recursive "
                          f"stream ({sys.platform}); pruning per event"))
        else:
            recursive, flat = plan_watch(root, pruner, shallow=shallow)
            out.send(log=(f"watching {root}: {len(recursive)} recursive, "
                          f"{len(flat)} non-recursive "
                          f"(planned in {time.monotonic() - t0:.1f}s"
                          f"{', shallow' if shallow else ''})"))
        session = _Session(recursive, flat, pruner, out,
                           grows_under=[norm(root)] if shallow else None,
                           replan=not whole)
        session.start()
        session.done.wait()
        err = session.error
        if err is None:
            time.sleep(REPLAN_SETTLE_S)
        session.close()
        if err is None:
            vanished = 0
            continue
        if is_watch_limit_error(err) and not shallow:
            out.send(log=(f"hit the platform watch limit watching {root} "
                          "(raise fs.inotify.max_user_watches); falling back "
                          "to a shallow, non-recursive watch"))
            shallow = True
            continue
        if isinstance(err, FileNotFoundError) and vanished < MAX_VANISHED_REPLANS:
            vanished += 1
            time.sleep(REPLAN_SETTLE_S)
            continue
        out.send(error=f"{type(err).__name__}: {err}")
        return 1


def _exit_on_stdin_eof() -> None:
    try:
        while sys.stdin.buffer.read(4096):
            pass
    except (OSError, ValueError):
        pass
    os._exit(0)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    spec = json.loads(argv[0])
    threading.Thread(target=_exit_on_stdin_eof, daemon=True).start()
    # The planning walk is background disk work, same as a scan.
    from fused_render.index.worker import _set_background_io_policy

    _set_background_io_policy()
    out = _Out(sys.stdout)
    root = spec["root"]
    try:
        pruner = Pruner(root, IgnoreRules(spec.get("ignore") or []),
                        spec.get("mounts_dir") or "",
                        index_dir=spec.get("index_dir"))
    except OSError as e:
        out.send(error=f"{type(e).__name__}: {e}")
        return 1
    return serve(root, pruner, out)


if __name__ == "__main__":
    sys.exit(main())
